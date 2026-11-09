"""``maven-metadata.xml``: parse (defused, bounded), render and merge (FORMATS.md "Maven").

Three shapes share the document: groupId/artifactId level (``versioning/versions``, ``latest``, ``release``), SNAPSHOT
version level (``versioning/snapshot`` and ``snapshotVersions``) and groupId level (``plugins``: prefix → artifactId).

Parsing goes through ``defusedxml`` (no DTDs, entities or external references) after a size check, and every list is
bounded. Rendering is deterministic (same input, same bytes), so a checksum file generated from it matches the
document served a moment before. Group repositories merge their members' documents: versions are united and
``latest``/``release`` recomputed in Maven order; the newest snapshot build wins per classifier and extension;
plugin prefixes are united.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.etree.ElementTree import Element
from xml.sax.saxutils import escape

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from waystation_common.errors import ValidationFailed
from waystation_server.formats.maven.version import is_snapshot, sort_versions, version_order

MAX_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_TEXT = 512


@dataclass(frozen=True)
class SnapshotVersion:
    classifier: str | None
    extension: str
    value: str
    updated: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.classifier or '', self.extension)


@dataclass(frozen=True)
class Plugin:
    name: str
    prefix: str
    artifact_id: str


@dataclass
class Metadata:
    group_id: str | None = None
    artifact_id: str | None = None
    version: str | None = None
    versions: list[str] = field(default_factory=list)
    latest: str | None = None
    release: str | None = None
    last_updated: str | None = None
    snapshot_timestamp: str | None = None
    snapshot_build: int | None = None
    local_copy: bool = False
    snapshot_versions: list[SnapshotVersion] = field(default_factory=list)
    plugins: list[Plugin] = field(default_factory=list)

    @property
    def is_snapshot_level(self) -> bool:
        return self.snapshot_timestamp is not None or bool(self.snapshot_versions) or self.local_copy


# ---------------------------------------------------------------------------- parse
def _tag(element: Element) -> str:
    return element.tag.rsplit('}', 1)[-1]


def _child(element: Element | None, name: str) -> Element | None:
    if element is None:
        return None
    for child in element:
        if _tag(child) == name:
            return child
    return None


def _children(element: Element | None, name: str) -> list[Element]:
    if element is None:
        return []
    out = [child for child in element if _tag(child) == name]
    if len(out) > MAX_ENTRIES:
        raise ValidationFailed('maven-metadata.xml lists too many entries')
    return out


def _text(element: Element | None, name: str) -> str | None:
    child = _child(element, name)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    if len(value) > MAX_TEXT:
        raise ValidationFailed('maven-metadata.xml has an oversized value')
    return value or None


def parse(data: bytes) -> Metadata:
    """Parse a ``maven-metadata.xml`` (raises ``ValidationFailed`` when it is not one, malformed or too large)."""
    if len(data) > MAX_BYTES:
        raise ValidationFailed('maven-metadata.xml is too large')
    try:
        root = ElementTree.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ElementTree.ParseError, DefusedXmlException, ValueError) as exc:
        raise ValidationFailed(f'maven-metadata.xml is not well-formed XML: {exc}') from None
    if _tag(root) != 'metadata':
        raise ValidationFailed('not a maven-metadata.xml document')
    versioning = _child(root, 'versioning')
    snapshot = _child(versioning, 'snapshot')
    build = _text(snapshot, 'buildNumber')
    md = Metadata(
        group_id=_text(root, 'groupId'),
        artifact_id=_text(root, 'artifactId'),
        version=_text(root, 'version'),
        latest=_text(versioning, 'latest'),
        release=_text(versioning, 'release'),
        last_updated=_text(versioning, 'lastUpdated'),
        snapshot_timestamp=_text(snapshot, 'timestamp'),
        snapshot_build=int(build) if build is not None and build.isdigit() and len(build) < 10 else None,
        local_copy=(_text(snapshot, 'localCopy') or '').lower() == 'true',
    )
    for element in _children(_child(versioning, 'versions'), 'version'):
        value = (element.text or '').strip()
        if value and len(value) <= MAX_TEXT:
            md.versions.append(value)
    for element in _children(_child(versioning, 'snapshotVersions'), 'snapshotVersion'):
        extension, built = _text(element, 'extension'), _text(element, 'value')
        if extension and built:
            md.snapshot_versions.append(
                SnapshotVersion(_text(element, 'classifier'), extension, built, _text(element, 'updated') or '')
            )
    for element in _children(_child(root, 'plugins'), 'plugin'):
        prefix, artifact_id = _text(element, 'prefix'), _text(element, 'artifactId')
        if prefix and artifact_id:
            md.plugins.append(Plugin(_text(element, 'name') or '', prefix, artifact_id))
    return md


def validate(data: bytes) -> None:
    parse(data)


# ---------------------------------------------------------------------------- render
def _el(name: str, value: str | None, indent: str) -> str:
    return f'{indent}<{name}>{escape(value)}</{name}>\n' if value is not None else ''


def render(md: Metadata) -> bytes:
    """The document (UTF-8, deterministic)."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n']
    out.append('<metadata modelVersion="1.1.0">\n' if md.is_snapshot_level else '<metadata>\n')
    out.append(_el('groupId', md.group_id, '  '))
    out.append(_el('artifactId', md.artifact_id, '  '))
    out.append(_el('version', md.version, '  '))
    has_versioning = (
        md.versions or md.latest or md.release or md.last_updated or md.is_snapshot_level or md.snapshot_versions
    )
    if has_versioning:
        out.append('  <versioning>\n')
        out.append(_el('latest', md.latest, '    '))
        out.append(_el('release', md.release, '    '))
        if md.snapshot_timestamp is not None or md.local_copy:
            out.append('    <snapshot>\n')
            out.append(_el('timestamp', md.snapshot_timestamp, '      '))
            out.append(_el('buildNumber', str(md.snapshot_build) if md.snapshot_build is not None else None, '      '))
            if md.local_copy:
                out.append(_el('localCopy', 'true', '      '))
            out.append('    </snapshot>\n')
        if md.versions:
            out.append('    <versions>\n')
            out.extend(_el('version', v, '      ') for v in md.versions)
            out.append('    </versions>\n')
        out.append(_el('lastUpdated', md.last_updated, '    '))
        if md.snapshot_versions:
            out.append('    <snapshotVersions>\n')
            for sv in md.snapshot_versions:
                out.append('      <snapshotVersion>\n')
                out.append(_el('classifier', sv.classifier, '        '))
                out.append(_el('extension', sv.extension, '        '))
                out.append(_el('value', sv.value, '        '))
                out.append(_el('updated', sv.updated or None, '        '))
                out.append('      </snapshotVersion>\n')
            out.append('    </snapshotVersions>\n')
        out.append('  </versioning>\n')
    if md.plugins:
        out.append('  <plugins>\n')
        for plugin in md.plugins:
            out.append('    <plugin>\n')
            out.append(_el('name', plugin.name or None, '      '))
            out.append(_el('prefix', plugin.prefix, '      '))
            out.append(_el('artifactId', plugin.artifact_id, '      '))
            out.append('    </plugin>\n')
        out.append('  </plugins>\n')
    out.append('</metadata>\n')
    return ''.join(out).encode()


# ---------------------------------------------------------------------------- build and merge
def versions_metadata(group_id: str, artifact_id: str, versions: list[str], last_updated: str) -> Metadata:
    """groupId/artifactId level: versions in Maven order, ``latest`` the highest, ``release`` the highest release."""
    ordered = sort_versions(versions)
    releases = [v for v in ordered if not is_snapshot(v)]
    return Metadata(
        group_id=group_id,
        artifact_id=artifact_id,
        versions=ordered,
        latest=ordered[-1] if ordered else None,
        release=releases[-1] if releases else None,
        last_updated=last_updated,
    )


def _newest(a: SnapshotVersion, b: SnapshotVersion) -> SnapshotVersion:
    return a if (a.updated, version_order(a.value)) >= (b.updated, version_order(b.value)) else b


def merge(documents: list[Metadata]) -> Metadata:
    """One document from several (group repositories, in member order: the first member's identity wins)."""
    if not documents:
        raise ValueError('nothing to merge')
    out = Metadata(
        group_id=next((d.group_id for d in documents if d.group_id), None),
        artifact_id=next((d.artifact_id for d in documents if d.artifact_id), None),
        version=next((d.version for d in documents if d.version), None),
    )
    versions = [v for d in documents for v in d.versions]
    if versions:
        merged = versions_metadata(out.group_id or '', out.artifact_id or '', versions, '')
        out.versions, out.latest, out.release = merged.versions, merged.latest, merged.release
    else:
        out.latest = max((d.latest for d in documents if d.latest), key=version_order, default=None)
        out.release = max((d.release for d in documents if d.release), key=version_order, default=None)
    stamps = [d.last_updated for d in documents if d.last_updated]
    out.last_updated = max(stamps) if stamps else None
    builds = [
        (d.snapshot_timestamp, d.snapshot_build or 0, i)
        for i, d in enumerate(documents)
        if d.snapshot_timestamp is not None
    ]
    if builds:
        timestamp, build, _ = max(builds, key=lambda b: (b[0], b[1], -b[2]))
        out.snapshot_timestamp, out.snapshot_build = timestamp, build
    out.local_copy = out.snapshot_timestamp is None and any(d.local_copy for d in documents)
    by_key: dict[tuple[str, str], SnapshotVersion] = {}
    for d in documents:
        for sv in d.snapshot_versions:
            current = by_key.get(sv.key)
            by_key[sv.key] = sv if current is None else _newest(current, sv)
    out.snapshot_versions = [by_key[k] for k in sorted(by_key)]
    plugins: dict[str, Plugin] = {}
    for d in documents:
        for plugin in d.plugins:
            plugins.setdefault(plugin.prefix, plugin)
    out.plugins = [plugins[k] for k in sorted(plugins)]
    return out
