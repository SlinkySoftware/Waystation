"""npm protocol helpers (FORMATS.md "npm"): request routing, packument rewriting and tarball integrity lookup.

Pure functions, no I/O: the plugin runs the CPU-heavy ones (JSON parse/serialise of packuments that can be tens of
MiB) in a thread through the proxy engine's ``transform`` or ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from waystation_common.errors import UpstreamError, ValidationFailed
from waystation_server.formats.npm.descriptor import AUDIT_PATHS
from waystation_server.proxy.engine import Expected

MAX_NAME_LENGTH = 214
ABBREVIATED_TYPE = 'application/vnd.npm.install-v1+json'
ABBREVIATED_ACCEPT = f'{ABBREVIATED_TYPE}; q=1.0, application/json; q=0.8, */*'
JSON_TYPE = 'application/json'

# Legacy names may contain upper case; new ones may not, but the proxy serves whatever the upstream has.
_NAME = re.compile(r'[A-Za-z0-9_~-][A-Za-z0-9._~-]*')
_SCOPE = re.compile(r'@[A-Za-z0-9_~-][A-Za-z0-9._~-]*')
_TARBALL = re.compile(r'[A-Za-z0-9_~+-][A-Za-z0-9._~+-]*\.tgz')
_SHA1_HEX = re.compile(r'[0-9a-f]{40}')


class Op(StrEnum):
    PACKUMENT = 'packument'
    TARBALL = 'tarball'
    PING = 'ping'
    WHOAMI = 'whoami'
    SEARCH = 'search'
    AUDIT = 'audit'


@dataclass(frozen=True)
class Route:
    op: Op
    package: str = ''  # '@scope/name' or 'name'
    filename: str = ''  # tarball file name


def valid_name(package: str) -> bool:
    if len(package) > MAX_NAME_LENGTH:
        return False
    scope, sep, name = package.partition('/')
    if sep:
        return bool(_SCOPE.fullmatch(scope)) and bool(_NAME.fullmatch(name))
    return bool(_NAME.fullmatch(package))


def parse_route(path: str) -> Route | None:
    """Map a normalised repository path to an npm operation. ``@scope%2fname`` arrives decoded (``@scope/name``)."""
    if path in AUDIT_PATHS:
        return Route(Op.AUDIT)
    if path == '-/ping':
        return Route(Op.PING)
    if path == '-/whoami':
        return Route(Op.WHOAMI)
    if path == '-/v1/search':
        return Route(Op.SEARCH)
    segments = path.split('/') if path else []
    if not segments or segments[0] == '-':
        return None
    if segments[0].startswith('@'):
        if len(segments) < 2:
            return None
        package, rest = f'{segments[0]}/{segments[1]}', segments[2:]
    else:
        package, rest = segments[0], segments[1:]
    if not valid_name(package):
        return None
    if not rest:
        return Route(Op.PACKUMENT, package)
    if len(rest) == 2 and rest[0] == '-' and _TARBALL.fullmatch(rest[1]):
        return Route(Op.TARBALL, package, rest[1])
    return None


def upstream_packument_path(package: str) -> str:
    """The packument path on the upstream: scoped names with an encoded slash, as npm itself requests them."""
    return quote(package, safe='@')


def packument_key(package: str, *, abbreviated: bool) -> str:
    """Asset keys of the two cached representations (never a valid tarball path: tarballs end in ``.tgz``)."""
    return f'{package}/-/packument.{"install-v1." if abbreviated else ""}json'


def tarball_path(package: str, filename: str) -> str:
    return f'{package}/-/{filename}'


def tarball_filename(url: str) -> str:
    """The file name a ``dist.tarball`` URL points at (``is-number-7.0.0.tgz``)."""
    return unquote(urlsplit(url).path.rsplit('/', 1)[-1])


def rewrite_packument(data: bytes, *, package: str, repo_url: str) -> bytes:
    """Validate an upstream packument and point every ``versions.*.dist.tarball`` at this repository.

    ``repo_url`` is ``<base>/repository/<repo>/``. Raises ``UpstreamError`` (502, nothing cached) for anything that is
    not a packument of ``package``.
    """
    try:
        doc = json.loads(data)
    except ValueError:
        raise UpstreamError('upstream packument is not valid JSON') from None
    if not isinstance(doc, dict):
        raise UpstreamError('upstream packument is not a JSON object')
    name = doc.get('name')
    if name is not None and name != package:
        raise UpstreamError('upstream packument is for a different package')
    versions = doc.get('versions', {})
    if not isinstance(versions, dict):
        raise UpstreamError('upstream packument has invalid versions')
    for version in versions.values():
        if not isinstance(version, dict):
            raise UpstreamError('upstream packument has an invalid version entry')
        dist = version.get('dist')
        if isinstance(dist, dict) and isinstance(dist.get('tarball'), str):
            filename = tarball_filename(dist['tarball'])
            dist['tarball'] = f'{repo_url}{package}/-/{quote(filename, safe="")}'
    return json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode()


@dataclass(frozen=True)
class DistLookup:
    found: bool
    expected: Expected | None = None


def _expected(dist: dict[str, Any]) -> Expected | None:
    integrity = dist.get('integrity')
    expected = Expected()
    if isinstance(integrity, str) and integrity.strip():
        try:
            expected = Expected.from_sri(integrity)
        except ValidationFailed:
            raise UpstreamError('upstream packument has an invalid integrity value') from None
    shasum = dist.get('shasum')
    if isinstance(shasum, str) and expected.sha1 is None:
        shasum = shasum.strip().lower()
        if not _SHA1_HEX.fullmatch(shasum):
            raise UpstreamError('upstream packument has an invalid shasum')
        expected = replace(expected, sha1=shasum)
    return expected if expected.digests() else None


def find_dist(packument: bytes, filename: str) -> DistLookup:
    """Find the version whose tarball is ``filename`` in a (cached, rewritten) packument and return its digests:
    ``dist.integrity`` (SRI, strongest algorithm) and/or ``dist.shasum`` (sha1)."""
    try:
        doc = json.loads(packument)
    except ValueError:
        raise UpstreamError('cached packument is not valid JSON') from None
    versions = doc.get('versions') if isinstance(doc, dict) else None
    if not isinstance(versions, dict):
        return DistLookup(found=False)
    for version in versions.values():
        dist = version.get('dist') if isinstance(version, dict) else None
        tarball = dist.get('tarball') if isinstance(dist, dict) else None
        if isinstance(dist, dict) and isinstance(tarball, str) and tarball_filename(tarball) == filename:
            return DistLookup(found=True, expected=_expected(dist))
    return DistLookup(found=False)
