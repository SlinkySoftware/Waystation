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
_REV = re.compile(r'[0-9A-Za-z-]{1,64}')
_TAG = re.compile(r'[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,127}')
_VERSION = re.compile(
    r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)'
    r'(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?'
)
COUCH_USER = 'org.couchdb.user:'
MAX_USERNAME = 256
MAX_TOKEN = 512
MAX_VERSION_LENGTH = 256


class Op(StrEnum):
    PACKUMENT = 'packument'  # GET; PUT = publish or packument update (deprecate); DELETE = unpublish everything
    PACKAGE_REV = 'package-rev'  # ``<pkg>/-rev/<rev>``: PUT = packument update (unpublish a version), DELETE = all
    TARBALL = 'tarball'  # GET; DELETE (also ``/-rev/<rev>`` and the ``repository/<repo>/`` form npm unpublish sends)
    DIST_TAGS = 'dist-tags'  # ``-/package/<pkg>/dist-tags``: GET
    DIST_TAG = 'dist-tag'  # ``-/package/<pkg>/dist-tags/<tag>``: PUT (body: the version as a JSON string), DELETE
    LOGIN = 'login'  # ``-/user/org.couchdb.user:<name>``: PUT (``npm login --auth-type=legacy``)
    LOGOUT = 'logout'  # ``-/user/token/<token>``: DELETE (``npm logout``)
    PING = 'ping'
    WHOAMI = 'whoami'
    SEARCH = 'search'
    AUDIT = 'audit'


@dataclass(frozen=True)
class Route:
    op: Op
    package: str = ''  # '@scope/name' or 'name'
    filename: str = ''  # tarball file name
    arg: str = ''  # dist-tag name, login user name, logout token


def valid_name(package: str) -> bool:
    if len(package) > MAX_NAME_LENGTH:
        return False
    scope, sep, name = package.partition('/')
    if sep:
        return bool(_SCOPE.fullmatch(scope)) and bool(_NAME.fullmatch(name))
    return bool(_NAME.fullmatch(package))


def _split_package(segments: list[str]) -> tuple[str, list[str]] | None:
    if not segments or not segments[0]:
        return None
    if segments[0].startswith('@'):
        if len(segments) < 2:
            return None
        package, rest = f'{segments[0]}/{segments[1]}', segments[2:]
    else:
        package, rest = segments[0], segments[1:]
    return (package, rest) if valid_name(package) else None


def _package_route(segments: list[str]) -> Route | None:
    split = _split_package(segments)
    if split is None:
        return None
    package, rest = split
    if not rest:
        return Route(Op.PACKUMENT, package)
    if len(rest) == 2 and rest[0] == '-rev' and _REV.fullmatch(rest[1]):
        return Route(Op.PACKAGE_REV, package, arg=rest[1])
    if len(rest) in (2, 4) and rest[0] == '-' and _TARBALL.fullmatch(rest[1]):
        if len(rest) == 4 and (rest[2] != '-rev' or not _REV.fullmatch(rest[3])):
            return None
        return Route(Op.TARBALL, package, rest[1])
    return None


def _special_route(segments: list[str]) -> Route | None:
    """``-/package/<pkg>/dist-tags[/<tag>]``, ``-/user/org.couchdb.user:<name>``, ``-/user/token/<token>``."""
    if len(segments) == 3 and segments[1] == 'user' and segments[2].startswith(COUCH_USER):
        name = segments[2][len(COUCH_USER) :]
        return Route(Op.LOGIN, arg=name) if 0 < len(name) <= MAX_USERNAME else None
    if len(segments) == 4 and segments[1:3] == ['user', 'token'] and 0 < len(segments[3]) <= MAX_TOKEN:
        return Route(Op.LOGOUT, arg=segments[3])
    if len(segments) >= 4 and segments[1] == 'package':
        split = _split_package(segments[2:])
        if split is None:
            return None
        package, rest = split
        if rest == ['dist-tags']:
            return Route(Op.DIST_TAGS, package)
        if len(rest) == 2 and rest[0] == 'dist-tags' and valid_tag(rest[1]):
            return Route(Op.DIST_TAG, package, arg=rest[1])
    return None


def parse_route(path: str) -> Route | None:
    """Map a normalised repository path to an npm operation. ``@scope%2fname`` arrives decoded (``@scope/name``).

    ``npm unpublish <pkg>@<version>`` deletes the tarball at the *path* of its URL appended to the registry URL
    (``<registry>/repository/<repo>/<pkg>/-/<file>/-rev/<rev>``), so a tarball route is also accepted behind a
    ``repository/<name>/`` prefix (no package path has a second segment other than ``-``/``-rev``)."""
    if path in AUDIT_PATHS:
        return Route(Op.AUDIT)
    if path == '-/ping':
        return Route(Op.PING)
    if path == '-/whoami':
        return Route(Op.WHOAMI)
    if path == '-/v1/search':
        return Route(Op.SEARCH)
    segments = path.split('/') if path else []
    if not segments:
        return None
    if segments[0] == '-':
        return _special_route(segments)
    route = _package_route(segments)
    if route is None and segments[0] == 'repository' and len(segments) > 2:
        inner = _package_route(segments[2:])
        return inner if inner is not None and inner.op is Op.TARBALL else None
    return route


def valid_tag(tag: str) -> bool:
    """A dist-tag name: URL-safe, not a version (npm would read it as one), no ``$``/``.`` start (MongoDB keys)."""
    return bool(_TAG.fullmatch(tag)) and not _VERSION.fullmatch(tag.removeprefix('v'))


def valid_version(version: str) -> bool:
    """A strict SemVer 2.0 version, as npm publishes them."""
    return len(version) <= MAX_VERSION_LENGTH and bool(_VERSION.fullmatch(version))


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
