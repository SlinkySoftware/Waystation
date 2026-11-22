"""Conda channel layout (FORMATS.md "Conda"): route parsing, content types, generated responses.

A channel is ``channeldata.json`` plus one directory per subdir (``noarch``, ``linux-64``, ``osx-arm64``...) holding
``repodata.json`` (and ``.zst`` / ``.bz2`` encodings, ``current_repodata.json``, ``repodata_from_packages.json``) and
the package files ``<name>-<version>-<build>.conda|.tar.bz2``. Incremental ``repodata.jlap`` and sharded repodata are
not served (404, so clients fall back to the full file).
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from starlette.responses import Response

from waystation_common.conda import PackageFile, parse_filename, valid_build, valid_name, valid_subdir, valid_version
from waystation_server.formats.base import FormatRequest

JSON_TYPE = 'application/json'
ZST_TYPE = 'application/zstd'
BZ2_TYPE = 'application/x-bzip2'
PACKAGE_TYPE = 'application/octet-stream'
SAFE_HEADERS = {'X-Content-Type-Options': 'nosniff'}
CHANNELDATA = 'channeldata.json'
REPODATA = 'repodata.json'
_REPODATA_BASES = ('repodata.json', 'current_repodata.json', 'repodata_from_packages.json')
REPODATA_FILES = frozenset(base + suffix for base in _REPODATA_BASES for suffix in ('', '.zst', '.bz2'))
UNSUPPORTED = frozenset({'repodata.jlap', 'repodata_shards.msgpack.zst', 'index.html', 'rss.xml'})
RESOLVE = '_ws/files/'
ANY = '-'


class Op(enum.StrEnum):
    CHANNELDATA = 'channeldata'
    REPODATA = 'repodata'
    PACKAGE = 'package'
    UNSUPPORTED = 'unsupported'
    RESOLVE = 'resolve'


@dataclass(frozen=True)
class Route:
    op: Op
    subdir: str = ''
    name: str = ''  # repodata: the file name; resolve: the package name
    package: PackageFile | None = None
    version: str | None = None  # resolve
    build: str | None = None  # resolve


def parse_route(path: str) -> Route | None:
    """The route of a repository path; None when it is not part of a conda channel."""
    if path == CHANNELDATA:
        return Route(Op.CHANNELDATA)
    if path.startswith(RESOLVE):
        parts = path[len(RESOLVE) :].split('/')
        if len(parts) != 4 or not valid_subdir(parts[0]) or not valid_name(parts[1]):
            return None
        version = None if parts[2] == ANY else parts[2]
        build = None if parts[3] == ANY else parts[3]
        if (version is not None and not valid_version(version)) or (build is not None and not valid_build(build)):
            return None
        return Route(Op.RESOLVE, parts[0], parts[1], version=version, build=build)
    subdir, sep, filename = path.partition('/')
    if not sep or not valid_subdir(subdir) or '/' in filename:
        if sep and valid_subdir(subdir) and filename.startswith('shards/'):
            return Route(Op.UNSUPPORTED, subdir, filename)
        return None
    if filename in REPODATA_FILES:
        return Route(Op.REPODATA, subdir, filename)
    if filename in UNSUPPORTED:
        return Route(Op.UNSUPPORTED, subdir, filename)
    package = parse_filename(filename)
    if package is not None:
        return Route(Op.PACKAGE, subdir, filename, package=package)
    return None


def repodata_type(filename: str) -> str:
    if filename.endswith('.zst'):
        return ZST_TYPE
    if filename.endswith('.bz2'):
        return BZ2_TYPE
    return JSON_TYPE


def generated(req: FormatRequest, body: bytes, content_type: str, *, account: Callable[[int, int], None]) -> Response:
    """A document rendered per request (empty repodata of a hosted subdir): ``ETag``, conditional ``304``, ``HEAD``."""
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
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
