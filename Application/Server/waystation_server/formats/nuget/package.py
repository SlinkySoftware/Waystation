"""Reading NuGet packages (``.nupkg``) and their ``.nuspec`` safely (FORMATS.md "NuGet v3"; ARCHITECTURE §7).

A ``.nupkg`` is a zip archive (OPC). Nothing is extracted: the central directory is read (in a thread) and checked the
way an extracting client needs it to be (zip-slip guards): every entry name, with ``\\`` read as ``/`` (old Windows
packers wrote those), must be relative, without ``..`` segments, drive letters, NUL or control characters; at most
``MAX_ENTRIES`` entries whose declared sizes total at most ``MAX_UNPACKED`` (a zip bomb is refused before anything is
decompressed). Exactly one ``.nuspec`` at the root; only that entry is decompressed, bounded to ``MAX_NUSPEC`` bytes
whatever its header says. Encrypted entries are refused.

The ``.nuspec`` is parsed by ``defusedxml`` (no DTDs, entities or external references), namespace-agnostic (every
nuspec schema version), and every field is bounded. ``id`` and ``version`` are required and must be valid; the version
is normalised.
"""

from __future__ import annotations

import re
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from waystation_common import nuget
from waystation_common.errors import ValidationFailed

MAX_ENTRIES = 100_000
MAX_UNPACKED = 16 * 1024**3  # declared, before anything is decompressed
MAX_NUSPEC = 1024 * 1024
MAX_TEXT = 4096
MAX_LONG_TEXT = 64 * 1024
MAX_GROUPS = 200
MAX_DEPENDENCIES = 2000
MAX_TAGS = 100
_BAD = re.compile(r'[\x00-\x1f\x7f]')
_DRIVE = re.compile(r'^[A-Za-z]:')


@dataclass(frozen=True)
class Dependency:
    id: str
    range: str | None
    exclude: str | None = None

    def json(self) -> dict[str, Any]:
        out: dict[str, Any] = {'id': self.id, 'range': self.range or '(, )'}
        if self.exclude:
            out['exclude'] = self.exclude
        return out


@dataclass(frozen=True)
class DependencyGroup:
    target_framework: str | None
    dependencies: tuple[Dependency, ...]


@dataclass(frozen=True)
class Nuspec:
    id: str
    version: str  # normalised (with metadata)
    title: str | None = None
    authors: str | None = None
    owners: str | None = None
    description: str | None = None
    summary: str | None = None
    release_notes: str | None = None
    tags: tuple[str, ...] = ()
    project_url: str | None = None
    icon_url: str | None = None
    license_url: str | None = None
    license_expression: str | None = None
    require_license_acceptance: bool = False
    min_client_version: str | None = None
    language: str | None = None
    copyright: str | None = None
    package_types: tuple[str, ...] = ()
    dependency_groups: tuple[DependencyGroup, ...] = field(default_factory=tuple)

    @property
    def flat_version(self) -> str:
        found = nuget.parse_version(self.version)
        return found.flat if found is not None else self.version.lower()

    def details(self) -> dict[str, Any]:
        """The package fields kept with a hosted package (``assets.nuget``), JSON-ready."""
        return {
            'id': self.id,
            'version': self.version,
            'title': self.title,
            'authors': self.authors,
            'owners': self.owners,
            'description': self.description,
            'summary': self.summary,
            'release_notes': self.release_notes,
            'tags': list(self.tags),
            'project_url': self.project_url,
            'icon_url': self.icon_url,
            'license_url': self.license_url,
            'license_expression': self.license_expression,
            'require_license_acceptance': self.require_license_acceptance,
            'min_client_version': self.min_client_version,
            'language': self.language,
            'copyright': self.copyright,
            'package_types': list(self.package_types),
            'dependency_groups': [
                {
                    'target_framework': g.target_framework,
                    'dependencies': [d.json() for d in g.dependencies],
                }
                for g in self.dependency_groups
            ],
        }


# ---------------------------------------------------------------------------- zip
def safe_entry_name(name: str) -> str:
    """The entry name as a relative path (``\\`` read as ``/``); raises ``ValidationFailed`` when unsafe."""
    clean = name.replace('\\', '/')
    if not clean or clean.startswith('/') or _DRIVE.match(clean) or _BAD.search(clean):
        raise ValidationFailed('the package has an unsafe file name')
    parts = [p for p in clean.split('/') if p not in ('', '.')]
    if not parts or any(p == '..' for p in parts):
        raise ValidationFailed('the package has an unsafe file name')
    return '/'.join(parts) + ('/' if clean.endswith('/') else '')


def _nuspec_entry(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ValidationFailed('the package has too many files')
    total = 0
    found: list[zipfile.ZipInfo] = []
    for info in infos:
        name = safe_entry_name(info.filename)
        if info.flag_bits & 0x1:
            raise ValidationFailed('the package has encrypted files')
        total += max(0, info.file_size)
        if total > MAX_UNPACKED:
            raise ValidationFailed('the package is too large when unpacked')
        if '/' not in name and name.lower().endswith('.nuspec'):
            found.append(info)
    if len(found) != 1:
        raise ValidationFailed('a NuGet package has exactly one .nuspec file at its root')
    if found[0].file_size > MAX_NUSPEC:
        raise ValidationFailed('the .nuspec file is too large')
    return found[0]


def read_nuspec_from(file: IO[bytes]) -> bytes:
    """The ``.nuspec`` bytes of the package in ``file`` (seekable), after checking the archive (blocking)."""
    try:
        with zipfile.ZipFile(file) as archive:
            info = _nuspec_entry(archive)
            with archive.open(info) as handle:
                data = handle.read(MAX_NUSPEC + 1)
    except zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, zlib.error, NotImplementedError:
        raise ValidationFailed('not a NuGet package (a zip archive)') from None
    except RuntimeError:  # encrypted members (zipfile asks for a password)
        raise ValidationFailed('the package has encrypted files') from None
    if len(data) > MAX_NUSPEC:
        raise ValidationFailed('the .nuspec file is too large')
    return data


def read_package(path: Path) -> Nuspec:
    """Check the package at ``path`` and parse its ``.nuspec`` (blocking: run in a thread)."""
    with path.open('rb') as file:
        return parse_nuspec(read_nuspec_from(file))


def check_package(file: IO[bytes], package_id: str, version: str) -> None:
    """Refuse a package (``ValidationFailed``) that is unsafe, unreadable or not ``package_id`` ``version``."""
    spec = parse_nuspec(read_nuspec_from(file))
    if spec.id.lower() != package_id.lower() or spec.flat_version != nuget.flat_version(version):
        raise ValidationFailed(f'the package is {spec.id} {spec.version}, not {package_id} {version}')


# ---------------------------------------------------------------------------- nuspec
def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _child(parent: Element, name: str) -> Element | None:
    for child in parent:
        if isinstance(child.tag, str) and _local(child.tag) == name:
            return child
    return None


def _children(parent: Element | None, name: str) -> list[Element]:
    if parent is None:
        return []
    return [c for c in parent if isinstance(c.tag, str) and _local(c.tag) == name]


def _text(parent: Element, name: str, limit: int = MAX_TEXT) -> str | None:
    node = _child(parent, name)
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value[:limit] or None


def _url(value: str | None) -> str | None:
    if value and value.startswith(('https://', 'http://')) and len(value) <= 2048 and not _BAD.search(value):
        return value
    return None


def _attr(node: Element, name: str, limit: int = 256) -> str | None:
    value = node.get(name)
    return value.strip()[:limit] or None if value else None


def _dependencies(parent: Element) -> tuple[Dependency, ...]:
    out: list[Dependency] = []
    for dep in _children(parent, 'dependency')[:MAX_DEPENDENCIES]:
        dep_id = _attr(dep, 'id', nuget.MAX_ID + 1)
        if dep_id and nuget.valid_id(dep_id):
            out.append(Dependency(dep_id, _attr(dep, 'version'), _attr(dep, 'exclude')))
    return tuple(out)


def _groups(metadata: Element) -> tuple[DependencyGroup, ...]:
    deps = _child(metadata, 'dependencies')
    if deps is None:
        return ()
    groups = _children(deps, 'group')
    if not groups:
        flat = _dependencies(deps)
        return (DependencyGroup(None, flat),) if flat else ()
    return tuple(DependencyGroup(_attr(g, 'targetFramework'), _dependencies(g)) for g in groups[:MAX_GROUPS])


def parse_nuspec(data: bytes) -> Nuspec:
    """A ``.nuspec`` document (bounded, defused); raises ``ValidationFailed``."""
    if len(data) > MAX_NUSPEC:
        raise ValidationFailed('the .nuspec file is too large')
    try:
        root = ElementTree.fromstring(data, forbid_dtd=True)
    except DefusedXmlException, ElementTree.ParseError, ValueError:
        raise ValidationFailed('the .nuspec file is not valid XML') from None
    if _local(str(root.tag)) != 'package':
        raise ValidationFailed('the .nuspec file has no <package> element')
    metadata = _child(root, 'metadata')
    if metadata is None:
        raise ValidationFailed('the .nuspec file has no <metadata>')
    package_id = _text(metadata, 'id', nuget.MAX_ID + 1)
    if not package_id or not nuget.valid_id(package_id):
        raise ValidationFailed('the .nuspec file needs a valid <id>')
    raw_version = _text(metadata, 'version', nuget.MAX_VERSION + 1) or ''
    version = nuget.normalise_version(raw_version)
    if version is None:
        raise ValidationFailed('the .nuspec file needs a valid <version>')
    license_node = _child(metadata, 'license')
    license_expression = None
    if license_node is not None and (license_node.get('type') or '').lower() == 'expression' and license_node.text:
        license_expression = license_node.text.strip()[:256] or None
    tags = tuple(t for t in re.split(r'[\s,;]+', _text(metadata, 'tags') or '') if t)[:MAX_TAGS]
    types = tuple(
        n for n in (_attr(t, 'name') for t in _children(_child(metadata, 'packageTypes'), 'packageType')) if n
    )
    return Nuspec(
        id=package_id,
        version=version,
        title=_text(metadata, 'title'),
        authors=_text(metadata, 'authors'),
        owners=_text(metadata, 'owners'),
        description=_text(metadata, 'description', MAX_LONG_TEXT),
        summary=_text(metadata, 'summary'),
        release_notes=_text(metadata, 'releaseNotes', MAX_LONG_TEXT),
        tags=tags,
        project_url=_url(_text(metadata, 'projectUrl')),
        icon_url=_url(_text(metadata, 'iconUrl')),
        license_url=_url(_text(metadata, 'licenseUrl')),
        license_expression=license_expression,
        require_license_acceptance=(_text(metadata, 'requireLicenseAcceptance') or '').lower() == 'true',
        min_client_version=_attr(metadata, 'minClientVersion', 64),
        language=_text(metadata, 'language', 64),
        copyright=_text(metadata, 'copyright'),
        package_types=types[:20],
        dependency_groups=_groups(metadata),
    )
