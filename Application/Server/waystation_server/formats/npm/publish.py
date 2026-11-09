"""Hosted npm documents (T-P5-NPMH; FORMATS.md "npm"): publish bodies, tarball inspection, packument updates and the
packuments generated from stored versions.

Pure functions, no I/O. The plugin runs them in a thread (``asyncio.to_thread``): publish bodies carry the tarball
base64-encoded and can be tens of MiB; packuments of packages with many versions are large too.

- ``parse_put``: a ``PUT /<pkg>`` body is a **publish** when it has ``_attachments`` (exactly one version and one
  tarball: decoded strictly, its ``length``, ``dist.integrity`` and ``dist.shasum`` checked when given, its
  ``package.json`` read from the archive and compared with the version's name and version), else a packument
  **update** (``npm deprecate``, ``npm unpublish <pkg>@<version>``): the versions and dist-tags the client wants.
- ``inspect_tarball``: gzip + tar read without extraction, bounded (members, unpacked bytes, ``package.json`` size);
  member names must be relative, without ``..`` or control characters.
- ``build_packuments``: the full packument (``_id``, ``_rev``, ``dist-tags``, ``versions``, ``time``, ``readme``
  and the latest version's descriptive fields) and the abbreviated one (``application/vnd.npm.install-v1+json``),
  every ``dist.tarball`` pointing at the repository.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import io
import json
import re
import tarfile
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from waystation_common.errors import PayloadTooLarge, ValidationFailed
from waystation_server.formats.npm.browse import version_key
from waystation_server.formats.npm.metadata import tarball_filename as url_filename
from waystation_server.formats.npm.metadata import valid_tag, valid_version

MAX_PUT_BYTES = 100 * 1024 * 1024  # a publish body: about 75 MiB of tarball once base64 is decoded
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_README_CHARS = 512 * 1024
MAX_PACKAGE_JSON_BYTES = 1024 * 1024
MAX_MEMBERS = 100_000
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_TAGS = 100
MAX_DEPRECATION = 1024

# Version fields npm's install metadata keeps (registry "corgi" documents).
ABBREVIATED_FIELDS = (
    'name',
    'version',
    'deprecated',
    'dependencies',
    'optionalDependencies',
    'devDependencies',
    'bundleDependencies',
    'bundledDependencies',
    'peerDependencies',
    'peerDependenciesMeta',
    'acceptDependencies',
    'bin',
    'directories',
    'dist',
    'engines',
    'os',
    'cpu',
    'libc',
    'funding',
    'license',
    '_hasShrinkwrap',
    'hasInstallScript',
)
# Descriptive fields the full packument takes from the latest version (as the public registry does).
PACKAGE_FIELDS = ('description', 'license', 'homepage', 'repository', 'bugs', 'keywords', 'author')
# Private version fields kept from the published manifest; every other ``_``-prefixed key is dropped.
KEPT_PRIVATE = ('_id', '_nodeVersion', '_npmVersion', '_hasShrinkwrap')
INSTALL_SCRIPTS = ('preinstall', 'install', 'postinstall')
_UNSAFE_MEMBER = re.compile(r'[\x00-\x1f\x7f\\]')


# ---------------------------------------------------------------------------- publish and update bodies
@dataclass(frozen=True)
class TarballInfo:
    package_json: dict[str, Any]
    file_count: int
    unpacked_size: int


@dataclass(frozen=True)
class Published:
    """A verified ``npm publish``: one version and its tarball."""

    package: str
    version: str
    filename: str  # stored file name: ``<name without scope>-<version>.tgz``
    tarball: bytes
    sha256: str
    manifest: dict[str, Any]  # the stored version document (``dist`` without ``tarball``)
    dist_tags: dict[str, str]  # tags the client asked for (pointing at this version)
    readme: str | None


@dataclass(frozen=True)
class Update:
    """A packument the client sent back with changes (deprecations, removed versions, dist-tags)."""

    versions: dict[str, dict[str, Any]]
    dist_tags: dict[str, str] | None


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationFailed(f'{what} must be a JSON object')
    return value


def tarball_filename(package: str, version: str) -> str:
    return f'{package.rsplit("/", 1)[-1]}-{version}.tgz'


def parse_put(data: bytes, package: str) -> Published | Update:
    """A ``PUT /<pkg>`` body (see the module docstring). Raises ``ValidationFailed`` (400)."""
    if len(data) > MAX_PUT_BYTES:
        raise PayloadTooLarge(f'the request body exceeds {MAX_PUT_BYTES} bytes')
    try:
        doc = json.loads(data)
    except ValueError:
        raise ValidationFailed('the body is not valid JSON') from None
    doc = _object(doc, 'the body')
    name = doc.get('name', doc.get('_id'))
    if name is not None and name != package:
        raise ValidationFailed(f'the body names package {str(name)[:214]!r}, the URL {package!r}')
    versions = _object(doc.get('versions', {}), 'versions')
    tags = doc.get('dist-tags')
    dist_tags = _tags(tags) if tags is not None else None
    attachments = doc.get('_attachments')
    if not attachments:
        for version, meta in versions.items():
            _object(meta, f'version {version}')
        return Update(versions=versions, dist_tags=dist_tags)
    return _publish(package, versions, _object(attachments, '_attachments'), dist_tags or {}, doc)


def _tags(value: Any) -> dict[str, str]:
    tags = _object(value, 'dist-tags')
    if len(tags) > MAX_TAGS:
        raise ValidationFailed(f'at most {MAX_TAGS} dist-tags')
    out: dict[str, str] = {}
    for tag, version in tags.items():
        if not valid_tag(tag):
            raise ValidationFailed(f'invalid dist-tag name {tag[:64]!r}')
        if not isinstance(version, str) or not valid_version(version):
            raise ValidationFailed(f'dist-tag {tag} does not name a version')
        out[tag] = version
    return out


def _publish(
    package: str,
    versions: dict[str, Any],
    attachments: dict[str, Any],
    dist_tags: dict[str, str],
    doc: dict[str, Any],
) -> Published:
    if len(versions) != 1 or len(attachments) != 1:
        raise ValidationFailed('publish one version with one tarball at a time')
    version, meta = next(iter(versions.items()))
    meta = _object(meta, f'version {version}')
    if not valid_version(version):
        raise ValidationFailed(f'invalid version {version[:64]!r}: use a SemVer version')
    if meta.get('name') != package or meta.get('version') != version:
        raise ValidationFailed('the version metadata names another package or version')
    filename = tarball_filename(package, version)
    key, attachment = next(iter(attachments.items()))
    if key not in (filename, f'{package}-{version}.tgz'):
        raise ValidationFailed(f'the attachment must be named {filename!r}')
    attachment = _object(attachment, 'the attachment')
    encoded = attachment.get('data')
    if not isinstance(encoded, str) or not encoded:
        raise ValidationFailed('the attachment has no data')
    try:
        tarball = base64.b64decode(encoded, validate=True)
    except binascii.Error, ValueError:
        raise ValidationFailed('the attachment is not valid base64') from None
    del encoded
    length = attachment.get('length')
    if length is not None and length != len(tarball):
        raise ValidationFailed(f'the attachment is {len(tarball)} bytes, its length says {length}')
    sha512 = hashlib.sha512(tarball).digest()
    sha1 = hashlib.sha1(tarball, usedforsecurity=False).hexdigest()
    sha256 = hashlib.sha256(tarball).hexdigest()
    integrity = 'sha512-' + base64.b64encode(sha512).decode()
    _check_dist(_object(meta.get('dist', {}), 'dist'), sha512=sha512, sha1=sha1)
    info = inspect_tarball(tarball)
    inner_name, inner_version = info.package_json.get('name'), info.package_json.get('version')
    if inner_name != package or inner_version != version:
        raise ValidationFailed(
            f'package.json in the tarball is {str(inner_name)[:214]}@{str(inner_version)[:64]}, '
            f'the publish is {package}@{version}'
        )
    manifest, readme = _manifest(meta, package, version, integrity=integrity, shasum=sha1, info=info)
    if readme is None and isinstance(doc.get('readme'), str):
        readme = doc['readme'][:MAX_README_CHARS]
    tags = {tag: v for tag, v in dist_tags.items() if v == version}
    return Published(
        package=package,
        version=version,
        filename=filename,
        tarball=tarball,
        sha256=sha256,
        manifest=manifest,
        dist_tags=tags,
        readme=readme,
    )


def _check_dist(dist: dict[str, Any], *, sha512: bytes, sha1: str) -> None:
    integrity = dist.get('integrity')
    if isinstance(integrity, str) and integrity.strip():
        for token in integrity.split():
            alg, _, b64 = token.partition('-')
            if alg != 'sha512':
                continue
            try:
                claimed = base64.b64decode(b64.split('?', 1)[0], validate=True)
            except binascii.Error, ValueError:
                raise ValidationFailed('invalid dist.integrity') from None
            if claimed != sha512:
                raise ValidationFailed('the tarball does not match dist.integrity')
    shasum = dist.get('shasum')
    if isinstance(shasum, str) and shasum.strip() and shasum.strip().lower() != sha1:
        raise ValidationFailed('the tarball does not match dist.shasum')


def _manifest(
    meta: dict[str, Any], package: str, version: str, *, integrity: str, shasum: str, info: TarballInfo
) -> tuple[dict[str, Any], str | None]:
    """The version document as stored: private fields dropped, ``readme`` moved out, ``dist`` computed here."""
    manifest = {k: v for k, v in meta.items() if not k.startswith('_') or k in KEPT_PRIVATE}
    readme = manifest.pop('readme', None)
    manifest.pop('readmeFilename', None)
    manifest['_id'] = f'{package}@{version}'
    scripts = manifest.get('scripts')
    if isinstance(scripts, dict) and any(s in scripts for s in INSTALL_SCRIPTS):
        manifest['hasInstallScript'] = True
    manifest['dist'] = {
        'integrity': integrity,
        'shasum': shasum,
        'fileCount': info.file_count,
        'unpackedSize': info.unpacked_size,
    }
    if len(json.dumps(manifest, ensure_ascii=False).encode()) > MAX_MANIFEST_BYTES:
        raise ValidationFailed(f'the version metadata exceeds {MAX_MANIFEST_BYTES} bytes')
    return manifest, readme[:MAX_README_CHARS] if isinstance(readme, str) else None


def inspect_tarball(data: bytes) -> TarballInfo:
    """Read ``package.json`` and count the files of an npm tarball without extracting it (bounded)."""
    if data[:2] != b'\x1f\x8b':
        raise ValidationFailed('the tarball is not gzip-compressed')
    package_json: dict[str, Any] | None = None
    files = 0
    unpacked = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            for count, member in enumerate(archive, start=1):
                if count > MAX_MEMBERS:
                    raise ValidationFailed(f'the tarball has more than {MAX_MEMBERS} entries')
                name = member.name
                parts = name.split('/')
                if name.startswith('/') or '..' in parts or _UNSAFE_MEMBER.search(name):
                    raise ValidationFailed('the tarball has an unsafe entry name')
                if not member.isfile():
                    continue
                files += 1
                unpacked += member.size
                if unpacked > MAX_UNPACKED_BYTES:
                    raise ValidationFailed('the tarball unpacks to more than 1 GiB')
                inner = [p for p in parts if p not in ('', '.')]
                if package_json is None and len(inner) == 2 and inner[1] == 'package.json':
                    if member.size > MAX_PACKAGE_JSON_BYTES:
                        raise ValidationFailed('package.json in the tarball is too large')
                    handle = archive.extractfile(member)
                    raw = handle.read() if handle is not None else b''
                    try:
                        parsed = json.loads(raw)
                    except ValueError:
                        raise ValidationFailed('package.json in the tarball is not valid JSON') from None
                    package_json = _object(parsed, 'package.json')
    except tarfile.TarError, EOFError, OSError, zlib.error, gzip.BadGzipFile:
        raise ValidationFailed('the tarball is not a valid gzip-compressed tar archive') from None
    if package_json is None:
        raise ValidationFailed('the tarball has no package/package.json')
    return TarballInfo(package_json=package_json, file_count=files, unpacked_size=unpacked)


def deprecation(meta: dict[str, Any]) -> str | None:
    """A version's deprecation message from a client's packument (``""`` or absent = not deprecated)."""
    value = meta.get('deprecated')
    if isinstance(value, str) and value:
        return value[:MAX_DEPRECATION]
    return None


# ---------------------------------------------------------------------------- generated packuments
@dataclass(frozen=True)
class VersionRecord:
    version: str
    manifest: dict[str, Any]
    filename: str
    published_at: datetime


@dataclass
class PackageState:
    """A hosted package: its versions and dist-tags as stored."""

    name: str
    rev: int
    dist_tags: dict[str, str]
    readme: str | None
    created: datetime | None
    modified: datetime | None
    versions: list[VersionRecord] = field(default_factory=list)  # newest first


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def is_prerelease(version: str) -> bool:
    return '-' in version.split('+', 1)[0]


def latest_of(versions: list[str]) -> str | None:
    """The newest release, else the newest version (SemVer precedence)."""
    ordered = sorted(versions, key=version_key, reverse=True)
    return next((v for v in ordered if not is_prerelease(v)), ordered[0] if ordered else None)


def effective_tags(tags: dict[str, str], held: list[str]) -> dict[str, str]:
    """Tags pointing at versions the package holds, with ``latest`` always set (when it holds any)."""
    present = set(held)
    out = {tag: version for tag, version in tags.items() if version in present}
    if 'latest' not in out:
        latest = latest_of(held)
        if latest is not None:
            out['latest'] = latest
    return dict(sorted(out.items()))


def records_of(docs: list[dict[str, Any]]) -> list[VersionRecord]:
    """``npm_versions`` documents → records, newest first (unreadable manifests are skipped)."""
    out: list[VersionRecord] = []
    for doc in docs:
        try:
            manifest = json.loads(doc['manifest'])
        except KeyError, TypeError, ValueError:
            continue
        if isinstance(manifest, dict):
            out.append(VersionRecord(str(doc['version']), manifest, str(doc['tarball']), doc['published_at']))
    out.sort(key=lambda r: version_key(r.version), reverse=True)
    return out


def summary(state: PackageState) -> dict[str, Any]:
    """The ``npm_packages`` summary fields of a hosted package (browse)."""
    tags = effective_tags(state.dist_tags, [r.version for r in state.versions])
    latest = tags.get('latest', '')
    meta = next((r.manifest for r in state.versions if r.version == latest), {})
    fields: dict[str, Any] = {
        'latest': latest,
        'dist_tags': tags,
        'version_count': len(state.versions),
        'modified': state.modified,
    }
    description = meta.get('description')
    fields['description'] = description[:1024] if isinstance(description, str) and description else None
    licence = meta.get('license')
    if isinstance(licence, dict):
        licence = licence.get('type')
    fields['license'] = licence[:256] if isinstance(licence, str) and licence else None
    return fields


def _rev(state: PackageState) -> str:
    digest = hashlib.sha256(f'{state.name}\0{state.rev}'.encode()).hexdigest()[:32]
    return f'{state.rev}-{digest}'


def build_packuments(state: PackageState, tarball_base: str) -> tuple[bytes, bytes]:
    """The full and the abbreviated packument of a hosted package. ``tarball_base`` is the repository URL with a
    trailing slash; tarballs are ``<base><package>/-/<file>``."""
    held = [r.version for r in state.versions]
    tags = effective_tags(state.dist_tags, held)
    versions: dict[str, dict[str, Any]] = {}
    abbreviated: dict[str, dict[str, Any]] = {}
    times: dict[str, str] = {}
    for record in sorted(state.versions, key=lambda r: version_key(r.version)):
        manifest = dict(record.manifest)
        dist = dict(manifest.get('dist') or {})
        dist['tarball'] = f'{tarball_base}{state.name}/-/{record.filename}'
        manifest['dist'] = dist
        versions[record.version] = manifest
        abbreviated[record.version] = {k: manifest[k] for k in ABBREVIATED_FIELDS if k in manifest}
        times[record.version] = _iso(record.published_at)
    published = [r.published_at for r in state.versions]
    created = state.created or (min(published) if published else datetime.now(UTC))
    modified = state.modified or (max(published) if published else created)
    full: dict[str, Any] = {
        '_id': state.name,
        '_rev': _rev(state),
        'name': state.name,
        'dist-tags': tags,
        'versions': versions,
        'time': {'created': _iso(created), 'modified': _iso(modified), **times},
    }
    latest = versions.get(tags.get('latest', ''), {})
    for key in PACKAGE_FIELDS:
        if key in latest:
            full[key] = latest[key]
    full['readme'] = state.readme or ''
    corgi = {'name': state.name, 'modified': _iso(modified), 'dist-tags': tags, 'versions': abbreviated}

    def dump(doc: dict[str, Any]) -> bytes:
        return json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode()

    return dump(full), dump(corgi)


# ---------------------------------------------------------------------------- group merges
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def merge_packuments(bodies: list[bytes], package: str, tarball_base: str, *, abbreviated: bool) -> bytes:
    """A group's packument: the members' packuments (in member order) merged. Versions: union, the first member
    that has a version wins it; ``dist-tags``: the first member that has the tag; ``time``: per version from the
    winning member; other top-level fields from the first member. Every ``dist.tarball`` points at the group."""
    merged: dict[str, Any] | None = None
    versions: dict[str, Any] = {}
    tags: dict[str, str] = {}
    times: dict[str, Any] = {}
    modified: list[str] = []
    created: str | None = None
    for body in bodies:
        try:
            doc = json.loads(body)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        if merged is None:
            merged = {
                k: v for k, v in doc.items() if k not in ('versions', 'dist-tags', 'time', '_rev', '_attachments')
            }
        member_times = _dict(doc.get('time'))
        member_versions = _dict(doc.get('versions'))
        for version, meta in member_versions.items():
            if version in versions or not isinstance(meta, dict):
                continue
            meta = dict(meta)
            dist = meta.get('dist')
            if isinstance(dist, dict) and isinstance(dist.get('tarball'), str):
                filename = quote(url_filename(dist['tarball']), safe='')
                meta['dist'] = {**dist, 'tarball': f'{tarball_base}{package}/-/{filename}'}
            versions[version] = meta
            if version in member_times:
                times[version] = member_times[version]
        member_tags = _dict(doc.get('dist-tags'))
        for tag, version in member_tags.items():
            if tag not in tags and isinstance(version, str):
                tags[tag] = version
        stamp = doc.get('modified') or member_times.get('modified')
        if isinstance(stamp, str):
            modified.append(stamp)
        if created is None and isinstance(member_times.get('created'), str):
            created = member_times['created']
    if merged is None:
        raise ValidationFailed('no member packument could be read')
    merged['name'] = package
    if not abbreviated:
        merged['_id'] = package
    merged['dist-tags'] = {t: v for t, v in tags.items() if v in versions}
    merged['versions'] = versions
    if modified:
        merged['modified'] = max(modified)
    if not abbreviated:
        merged.pop('modified', None)
        stamps = {'created': created} if created else {}
        if modified:
            stamps['modified'] = max(modified)
        merged['time'] = {**stamps, **times}
    return json.dumps(merged, ensure_ascii=False, separators=(',', ':')).encode()


def tags_of(packument: bytes) -> dict[str, str]:
    """The ``dist-tags`` of a packument."""
    try:
        doc = json.loads(packument)
    except ValueError:
        return {}
    tags = doc.get('dist-tags') if isinstance(doc, dict) else None
    return {str(k): v for k, v in tags.items() if isinstance(v, str)} if isinstance(tags, dict) else {}
