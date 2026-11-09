"""The origin side: Remote Service Discovery and the Provider Registry Protocol documents, ``SHA256SUMS``, and the
network mirror documents Waystation renders from them. Pure functions on bytes (validated, size-bounded); callers
run the large ones in a thread.

- Discovery (``https://<hostname>/.well-known/terraform.json``): ``providers.v1`` is the provider registry's base
  URL, absolute or relative to the discovery document.
- Versions (``<base><namespace>/<type>/versions``): ``{"versions": [{"version", "protocols", "platforms": [{"os",
  "arch"}]}]}``. Entries with an invalid version or platform are skipped.
- Download (``<base><namespace>/<type>/<version>/download/<os>/<arch>``): ``filename``, ``download_url``,
  ``shasums_url``, ``shasums_signature_url``, ``shasum`` and ``signing_keys.gpg_public_keys[].ascii_armor``.
- ``SHA256SUMS``: ``<sha256 hex>  <file name>`` lines (``sha256sum`` output), signed by ``SHA256SUMS.sig``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from waystation_common.cleanup.versions import sort_key
from waystation_common.errors import UpstreamError
from waystation_server.formats.terraform.paths import FILENAME, PLATFORM, is_version

MAX_DISCOVERY = 64 * 1024
MAX_VERSIONS = 32 * 1024 * 1024
MAX_VERSION_COUNT = 20_000
MAX_PLATFORMS = 200
MAX_DOWNLOAD = 1024 * 1024
MAX_KEYS = 20
MAX_KEY_ARMOR = 256 * 1024
MAX_SUMS = 1024 * 1024
MAX_SUMS_LINES = 10_000
MAX_SIGNATURE = 64 * 1024
MAX_URL = 4096
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_SUMS_LINE = re.compile(r'^([0-9a-fA-F]{64}) [ *](\S(?:.*\S)?)$')


def _json(data: bytes, what: str, limit: int) -> Any:
    if len(data) > limit:
        raise UpstreamError(f'{what} is too large')
    try:
        return json.loads(data)
    except ValueError:
        raise UpstreamError(f'{what} is not JSON') from None


def _http_url(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL:
        raise UpstreamError(f'{what} has no valid URL')
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise UpstreamError(f'{what} has no valid URL')
    return value


# ---------------------------------------------------------------------------- discovery
def discovery_url(hostname: str) -> str:
    return f'https://{hostname}/.well-known/terraform.json'


def parse_discovery(data: bytes, hostname: str) -> str:
    """The provider registry base URL (ending with ``/``) a host's discovery document announces."""
    doc = _json(data, 'service discovery document', MAX_DISCOVERY)
    value = doc.get('providers.v1') if isinstance(doc, dict) else None
    if not isinstance(value, str) or not value:
        raise UpstreamError(f'{hostname} does not offer a provider registry (no providers.v1)')
    base = _http_url(urljoin(discovery_url(hostname), value), 'service discovery document')
    if '?' in base or '#' in base:
        raise UpstreamError('service discovery document has no valid URL')
    return base if base.endswith('/') else base + '/'


# ---------------------------------------------------------------------------- versions
@dataclass(frozen=True)
class VersionInfo:
    protocols: tuple[str, ...]
    platforms: tuple[tuple[str, str], ...]  # (os, arch)


def parse_versions(data: bytes) -> dict[str, VersionInfo]:
    """A registry version list: version -> protocols and platforms."""
    doc = _json(data, 'provider version list', MAX_VERSIONS)
    entries = doc.get('versions') if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        raise UpstreamError('provider version list has no versions')
    if len(entries) > MAX_VERSION_COUNT:
        raise UpstreamError('provider version list is too long')
    out: dict[str, VersionInfo] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        version = entry.get('version')
        if not isinstance(version, str) or not is_version(version):
            continue
        protocols = entry.get('protocols')
        platforms = entry.get('platforms')
        found: list[tuple[str, str]] = []
        for item in platforms if isinstance(platforms, list) else []:
            if not isinstance(item, dict):
                continue
            os_, arch = item.get('os'), item.get('arch')
            if isinstance(os_, str) and isinstance(arch, str) and PLATFORM.match(os_) and PLATFORM.match(arch):
                found.append((os_, arch))
        out[version] = VersionInfo(
            protocols=tuple(p for p in protocols if isinstance(p, str) and len(p) <= 16)[:16]
            if isinstance(protocols, list)
            else (),
            platforms=tuple(sorted(set(found)))[:MAX_PLATFORMS],
        )
    return out


def newest_first(versions: list[str]) -> list[str]:
    return sorted(versions, key=lambda v: sort_key('terraform', v), reverse=True)


def latest_of(versions: list[str]) -> str | None:
    """The newest version that is not a pre-release (else the newest)."""
    ordered = newest_first(versions)
    final = [v for v in ordered if '-' not in v.split('+', 1)[0]]
    return (final or ordered or [None])[0]


def render_index(versions: dict[str, VersionInfo]) -> bytes:
    """The mirror's ``index.json``."""
    body = {'versions': {version: {} for version in newest_first(list(versions))}}
    return json.dumps(body, separators=(',', ':')).encode()


# ---------------------------------------------------------------------------- download
@dataclass(frozen=True)
class Download:
    os: str
    arch: str
    filename: str
    download_url: str
    shasums_url: str
    shasums_signature_url: str
    shasum: str
    keys: tuple[str, ...]  # ASCII-armored public keys


def parse_download(data: bytes, os_: str, arch: str) -> Download:
    doc = _json(data, 'provider download document', MAX_DOWNLOAD)
    if not isinstance(doc, dict):
        raise UpstreamError('provider download document is not an object')
    if doc.get('os', os_) != os_ or doc.get('arch', arch) != arch:
        raise UpstreamError('provider download document is for another platform')
    filename = doc.get('filename')
    if not isinstance(filename, str) or not FILENAME.match(filename):
        raise UpstreamError('provider download document has no valid file name')
    shasum = doc.get('shasum')
    if not isinstance(shasum, str) or not _HEX64.match(shasum.lower()):
        raise UpstreamError('provider download document has no valid shasum')
    signing = doc.get('signing_keys')
    entries = signing.get('gpg_public_keys') if isinstance(signing, dict) else None
    keys: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        armor = entry.get('ascii_armor') if isinstance(entry, dict) else None
        if isinstance(armor, str) and armor and len(armor) <= MAX_KEY_ARMOR:
            keys.append(armor)
    return Download(
        os=os_,
        arch=arch,
        filename=filename,
        download_url=_http_url(doc.get('download_url'), 'provider download document'),
        shasums_url=_http_url(doc.get('shasums_url'), 'provider download document'),
        shasums_signature_url=_http_url(doc.get('shasums_signature_url'), 'provider download document'),
        shasum=shasum.lower(),
        keys=tuple(keys[:MAX_KEYS]),
    )


# ---------------------------------------------------------------------------- SHA256SUMS
def parse_sums(data: bytes) -> dict[str, str]:
    """File name -> sha256 (lower-case hex)."""
    if len(data) > MAX_SUMS:
        raise UpstreamError('SHA256SUMS is too large')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise UpstreamError('SHA256SUMS is not UTF-8') from None
    out: dict[str, str] = {}
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > MAX_SUMS_LINES:
        raise UpstreamError('SHA256SUMS is empty or too long')
    for line in lines:
        match = _SUMS_LINE.match(line.strip())
        if match is None:
            raise UpstreamError('SHA256SUMS has a malformed line')
        digest, name = match.groups()
        if out.setdefault(name, digest.lower()) != digest.lower():
            raise UpstreamError('SHA256SUMS lists a file twice')
    return out


@dataclass(frozen=True)
class Archive:
    os: str
    arch: str
    filename: str
    sha256: str


def archives(sums: dict[str, str], known: Download, platforms: tuple[tuple[str, str], ...]) -> list[Archive]:
    """The version's archives per platform: the ``SHA256SUMS`` entries named like ``known.filename`` with another
    platform (``<prefix>_<os>_<arch>.zip``), limited to the platforms the version list names (when it names any)."""
    if sums.get(known.filename) != known.shasum:
        raise UpstreamError('SHA256SUMS does not list the archive with the registry\'s shasum')
    suffix = f'_{known.os}_{known.arch}.zip'
    found: dict[tuple[str, str], Archive] = {}
    if known.filename.endswith(suffix):
        prefix = known.filename[: -len(suffix)]
        pattern = re.compile(rf'^{re.escape(prefix)}_([a-z0-9]{{1,32}})_([a-z0-9]{{1,32}})\.zip$')
        for name, digest in sums.items():
            match = pattern.match(name)
            if match is not None:
                found[(match.group(1), match.group(2))] = Archive(match.group(1), match.group(2), name, digest)
    found[(known.os, known.arch)] = Archive(known.os, known.arch, known.filename, known.shasum)
    wanted = set(platforms)
    return [found[p] for p in sorted(found) if not wanted or p in wanted]


def render_version(version: str, items: list[Archive], h1: dict[str, str]) -> bytes:
    """The mirror's ``<version>.json``: per platform the archive URL (relative) and its hashes, ``h1:`` (when the
    archive has been cached, so its contents were hashed) then ``zh:`` (the archive's sha256 from the signed
    ``SHA256SUMS``)."""
    out: dict[str, Any] = {}
    for item in items:
        platform = f'{item.os}_{item.arch}'
        hashes = [h1[platform]] if platform in h1 else []
        hashes.append(f'zh:{item.sha256}')
        out[platform] = {'url': f'{version}/{item.os}/{item.arch}/{item.filename}', 'hashes': hashes}
    return json.dumps({'archives': out}, separators=(',', ':')).encode()
