"""Cargo routes, crate names, the sparse index layout and index entries, shared by the proxy, hosted, group and browse
code (FORMATS.md "Cargo").

- Crate names follow Cargo's registry rules: ASCII letters, digits, ``-`` and ``_``, a letter first, at most 64
  characters. Names compare case-insensitively (index paths and asset keys use the lower-case name); on a hosted
  repository ``-`` and ``_`` are the same crate too (``canonical``), as on crates.io.
- Index files live at ``index/<prefix>/<name>`` with Cargo's prefix layout: ``1/<n>``, ``2/<n>``, ``3/<c>/<n>`` and
  ``<ab>/<cd>/<n>`` (lower case). ``index/config.json`` is generated per request.
- ``.crate`` files: ``crates/<name>/<name>-<version>.crate`` (the ``dl`` template Waystation announces; the key is the
  lower-case name and the version as published).
- Index entries are JSON objects, one per line (``name``, ``vers``, ``deps``, ``cksum``, ``features``, ``yanked``,
  ``links``, ``v``, ``features2``, ``rust_version``). Upstream files are checked line by line before they are cached:
  every line a JSON object for this crate with a SemVer version and a sha256 ``cksum``, bounded in size and count.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from starlette.responses import JSONResponse, Response

from waystation_common.cleanup.versions import is_prerelease, sort_key
from waystation_common.errors import UpstreamError
from waystation_server.formats.base import FormatRequest
from waystation_server.problems import native_error

INDEX_TYPE = 'text/plain; charset=utf-8'
CONFIG_TYPE = 'application/json'
CRATE_TYPE = 'application/gzip'
SAFE_HEADERS = {'X-Content-Type-Options': 'nosniff'}
MAX_INDEX = 32 * 1024 * 1024  # bytes of one index file (crates.io's largest are a few MiB)
MAX_LINES = 100_000  # versions in one index file
MAX_LINE = 1024 * 1024  # bytes of one index entry
MAX_CONFIG = 64 * 1024
UPSTREAM_CONFIG = '_upstream/config.json'  # a proxy's cached copy of its upstream's config.json (for ``dl``)
CRATES_IO_INDEX = 'https://github.com/rust-lang/crates.io-index'

_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
_SEMVER = re.compile(
    r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)'
    r'(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$'
)
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
MAX_VERSION = 200


class Op(StrEnum):
    CONFIG = 'config'
    INDEX = 'index'
    DOWNLOAD = 'download'
    PUBLISH = 'publish'
    YANK = 'yank'
    UNYANK = 'unyank'
    OWNERS = 'owners'
    SEARCH = 'search'


@dataclass(frozen=True)
class Route:
    op: Op
    crate: str = ''  # lower-case crate name (index, download, yank, owners)
    version: str = ''  # download, yank


# ---------------------------------------------------------------------------- names and versions
def valid_name(name: str) -> bool:
    return bool(_NAME.match(name))


def canonical(name: str) -> str:
    """crates.io's uniqueness key: case and ``-``/``_`` do not make another crate."""
    return name.lower().replace('-', '_')


def valid_version(version: str) -> bool:
    return len(version) <= MAX_VERSION and bool(_SEMVER.match(version))


def without_build(version: str) -> str:
    """The version without SemVer build metadata (``1.0.0+abc`` is the same version as ``1.0.0`` for Cargo)."""
    return version.split('+', 1)[0]


def prefix_of(name: str) -> str:
    """Cargo's index directory for ``name`` (case kept: ``{prefix}``; lower-case it for ``{lowerprefix}``)."""
    match len(name):
        case 1:
            return '1'
        case 2:
            return '2'
        case 3:
            return f'3/{name[0]}'
        case _:
            return f'{name[0:2]}/{name[2:4]}'


def index_path(name: str) -> str:
    """The repository path of a crate's index file (``index/se/rd/serde``)."""
    lower = name.lower()
    return f'index/{prefix_of(lower)}/{lower}'


def crate_path(name: str, version: str) -> str:
    lower = name.lower()
    return f'crates/{lower}/{lower}-{version}.crate'


def crate_prefix(name: str) -> str:
    return f'crates/{name.lower()}/'


def newest_first(versions: Iterable[str]) -> list[str]:
    return sorted(set(versions), key=lambda v: sort_key('cargo', v), reverse=True)


def latest_of(entries: Iterable[Mapping[str, Any]]) -> str | None:
    """The highest non-yanked release, else the highest non-yanked version, else the highest version."""
    pool = [e for e in entries if isinstance(e.get('vers'), str)]
    live = [e for e in pool if not e.get('yanked')]
    releases = [e for e in live if not is_prerelease('cargo', str(e['vers']))]
    candidates = releases or live or pool
    if not candidates:
        return None
    return str(max(candidates, key=lambda e: sort_key('cargo', str(e['vers'])))['vers'])


# ---------------------------------------------------------------------------- routes
def parse_route(path: str, method: str) -> Route | None:
    """Map a normalised repository path to a Cargo operation (None: not a Cargo path)."""
    segments = path.split('/')
    head, rest = segments[0], segments[1:]
    if head == 'index':
        if rest == ['config.json']:
            return Route(Op.CONFIG)
        if not rest or not valid_name(rest[-1]) or rest[-1] != rest[-1].lower():
            return None
        return Route(Op.INDEX, rest[-1]) if path == index_path(rest[-1]) else None
    if head == 'crates' and len(rest) == 2:
        crate, filename = rest
        if not valid_name(crate) or not filename.endswith('.crate'):
            return None
        stem = filename[: -len('.crate')]
        if not stem.lower().startswith(crate.lower() + '-'):
            return None
        version = stem[len(crate) + 1 :]
        return Route(Op.DOWNLOAD, crate.lower(), version) if valid_version(version) else None
    if head == 'api' and rest[:2] == ['v1', 'crates']:
        tail = rest[2:]
        if not tail:
            return Route(Op.SEARCH)
        if tail == ['new']:
            return Route(Op.PUBLISH)
        if len(tail) == 2 and tail[1] == 'owners' and valid_name(tail[0]):
            return Route(Op.OWNERS, tail[0].lower())
        if len(tail) == 3 and tail[2] in ('yank', 'unyank') and valid_name(tail[0]) and valid_version(tail[1]):
            return Route(Op.YANK if tail[2] == 'yank' else Op.UNYANK, tail[0].lower(), tail[1])
    return None


def is_api(path: str) -> bool:
    return path == 'api' or path.startswith('api/')


# ---------------------------------------------------------------------------- errors and generated answers
def cargo_error(status: int, message: str, *, api: bool, headers: dict[str, str] | None = None) -> Response:
    """The registry web API answers ``{"errors": [{"detail": ...}]}`` (cargo prints the details); index and download
    paths answer plain text."""
    if not api:
        return native_error(status, message, headers=headers)
    return JSONResponse({'errors': [{'detail': message}]}, status_code=status, headers=headers)


def body_etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def generated(req: FormatRequest, body: bytes, content_type: str, *, account: Callable[[int, int], None]) -> Response:
    """A response built per request (config, hosted and group index files): ``ETag``, conditional ``304``,
    ``HEAD``."""
    etag = body_etag(body)
    headers = {'ETag': etag, 'Cache-Control': 'no-cache', **SAFE_HEADERS}
    match = req.request.headers.get('if-none-match')
    if match and etag in {m.strip() for m in match.split(',')}:
        account(304, 0)
        return Response(status_code=304, headers=headers)
    if req.is_head:
        account(200, 0)
        headers['Content-Length'] = str(len(body))
        return Response(status_code=200, headers=headers, media_type=content_type)
    account(200, len(body))
    return Response(body, status_code=200, headers=headers, media_type=content_type)


def config_body(req: FormatRequest) -> bytes:
    """``index/config.json``: downloads and the web API on this repository; ``auth-required`` when the request
    needed credentials (Cargo sends its token on every request then: index, downloads, API)."""
    repo_url = req.repo_url
    doc = {
        'dl': f'{repo_url}crates/{{crate}}/{{crate}}-{{version}}.crate',
        'api': repo_url.rstrip('/'),
        'auth-required': not req.principal.is_anonymous,
    }
    return json.dumps(doc, separators=(',', ':')).encode()


# ---------------------------------------------------------------------------- index entries
def _entry(doc: Any, crate: str) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise UpstreamError('index entry is not a JSON object')
    name, vers, cksum = doc.get('name'), doc.get('vers'), doc.get('cksum')
    if not isinstance(name, str) or name.lower() != crate:
        raise UpstreamError('index entry is for another crate')
    if not isinstance(vers, str) or not valid_version(vers):
        raise UpstreamError('index entry has an invalid version')
    if not isinstance(cksum, str) or not _SHA256.match(cksum):
        raise UpstreamError('index entry has an invalid checksum')
    if not isinstance(doc.get('deps', []), list) or not isinstance(doc.get('features', {}), dict):
        raise UpstreamError('index entry has invalid dependencies or features')
    if not isinstance(doc.get('yanked', False), bool):
        raise UpstreamError('index entry has an invalid yanked flag')
    return doc


def parse_index(data: bytes, crate: str) -> list[dict[str, Any]]:
    """The entries of an index file of ``crate`` (lower-case), in file order. Raises ``UpstreamError`` when the file
    is not a valid index file for that crate."""
    if len(data) > MAX_INDEX:
        raise UpstreamError('index file is too large')
    entries: list[dict[str, Any]] = []
    for raw in data.split(b'\n'):
        line = raw.strip()
        if not line:
            continue
        if len(line) > MAX_LINE:
            raise UpstreamError('index entry is too large')
        try:
            doc = json.loads(line)
        except ValueError:
            raise UpstreamError('index entry is not valid JSON') from None
        entries.append(_entry(doc, crate))
        if len(entries) > MAX_LINES:
            raise UpstreamError('index file has too many versions')
    return entries


def render_index(entries: Iterable[Mapping[str, Any]]) -> bytes:
    return b''.join(json.dumps(dict(e), separators=(',', ':')).encode() + b'\n' for e in entries)


def find(entries: Iterable[Mapping[str, Any]], version: str) -> Mapping[str, Any] | None:
    for entry in entries:
        if entry.get('vers') == version:
            return entry
    return None


def summary_fields(entries: list[dict[str, Any]], crate: str, description: str | None = None) -> dict[str, Any]:
    """``cargo_crates`` fields of a crate from its index entries."""
    names = [str(e['name']) for e in entries if isinstance(e.get('name'), str)]
    fields: dict[str, Any] = {
        'display_name': names[-1] if names else crate,
        'canonical': canonical(crate),
        'latest': latest_of(entries),
        'version_count': len(entries),
    }
    if description is not None:
        fields['description'] = description[:1000]
    return fields


def download_url(template: str, entry: Mapping[str, Any]) -> str:
    """Expand an upstream ``config.json`` ``dl`` template for an index entry (Cargo's markers; without any marker
    ``/{crate}/{version}/download`` is appended)."""
    name, version, cksum = str(entry['name']), str(entry['vers']), str(entry['cksum'])
    markers = ('{crate}', '{version}', '{prefix}', '{lowerprefix}', '{sha256-checksum}')
    if not any(m in template for m in markers):
        return f'{template.rstrip("/")}/{name}/{version}/download'
    prefix = prefix_of(name)
    return (
        template.replace('{crate}', name)
        .replace('{version}', version)
        .replace('{prefix}', prefix)
        .replace('{lowerprefix}', prefix.lower())
        .replace('{sha256-checksum}', cksum)
    )


def parse_upstream_config(data: bytes) -> str:
    """The ``dl`` template of an upstream ``config.json`` (an absolute http(s) URL)."""
    if len(data) > MAX_CONFIG:
        raise UpstreamError('upstream config.json is too large')
    try:
        doc = json.loads(data)
    except ValueError:
        raise UpstreamError('upstream config.json is not valid JSON') from None
    dl = doc.get('dl') if isinstance(doc, dict) else None
    if not isinstance(dl, str) or not dl.startswith(('https://', 'http://')) or len(dl) > 2000:
        raise UpstreamError('upstream config.json has no valid dl URL')
    return dl
