"""Go helpers shared by the proxy, group and browse code: errors, generated responses, version lists, GONOSUMDB-style
module path patterns."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable

from starlette.responses import Response

from waystation_common.cleanup.versions import is_prerelease, sort_key
from waystation_common.errors import UpstreamError
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.go import modpath
from waystation_server.problems import native_error

TEXT = 'text/plain; charset=utf-8'
JSON = 'application/json'
ZIP = 'application/zip'
SAFE_HEADERS = {'X-Content-Type-Options': 'nosniff'}
MAX_LIST = 4 * 1024 * 1024
MAX_LIST_VERSIONS = 100_000


def go_error(status: int, message: str, *, headers: dict[str, str] | None = None) -> Response:
    """The go command prints the plain-text body of an error (``server response: ...``)."""
    return native_error(status, message, headers=headers)


def parse_list(data: bytes) -> list[str]:
    """The versions of a ``@v/list`` answer (one per line; anything after the version on a line is ignored, as by
    the go command). Raises ``UpstreamError`` when it is not a version list."""
    if len(data) > MAX_LIST:
        raise UpstreamError('version list is too large')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise UpstreamError('version list is not UTF-8') from None
    versions: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        version = fields[0]
        if not modpath.is_canonical(version):
            raise UpstreamError('version list has an invalid version')
        if version not in seen:
            seen.add(version)
            versions.append(version)
        if len(versions) > MAX_LIST_VERSIONS:
            raise UpstreamError('version list is too long')
    return versions


def render_list(versions: Iterable[str]) -> bytes:
    return ''.join(f'{v}\n' for v in versions).encode()


def newest_first(versions: Iterable[str]) -> list[str]:
    return sorted(set(versions), key=lambda v: sort_key('go', v), reverse=True)


def latest_of(versions: Iterable[str]) -> str | None:
    """The newest release (semver), else the newest pre-release or pseudo-version."""
    pool = list(versions)
    releases = [v for v in pool if not is_prerelease('go', v)]
    candidates = releases or pool
    return max(candidates, key=lambda v: sort_key('go', v)) if candidates else None


def body_etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def generated(req: FormatRequest, body: bytes, content_type: str, *, account: Callable[[int, int], None]) -> Response:
    """A response built per request (group lists): ``ETag``, conditional ``304``, ``HEAD``. ``account(status, bytes
    sent)`` records the request."""
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


def _glob(pattern: str) -> re.Pattern[str]:
    """``path.Match`` semantics: ``*`` and ``?`` never match ``/``; ``[...]`` classes (``^`` negates)."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == '*':
            out.append('[^/]*')
        elif ch == '?':
            out.append('[^/]')
        elif ch == '[':
            end = pattern.find(']', i + 1)
            if end < 0:
                return re.compile(r'(?!)')  # malformed: matches nothing, as path.Match errors
            body = pattern[i + 1 : end]
            negate = body.startswith('^')
            body = body[1:] if negate else body
            out.append('[' + ('^' if negate else '') + re.escape(body).replace('\\-', '-') + ']')
            i = end
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile(''.join(out) + r'\Z')


def matches_prefix_patterns(patterns: Iterable[str], module: str) -> bool:
    """``module.MatchPrefixPatterns`` (GONOSUMDB, GOPRIVATE): does a pattern match a leading part of ``module`` with
    as many path elements as the pattern has?"""
    elems = module.split('/')
    for raw in patterns:
        pattern = raw.strip().rstrip('/')
        if not pattern:
            continue
        n = pattern.count('/') + 1
        if len(elems) < n:
            continue
        if _glob(pattern).match('/'.join(elems[:n])):
            return True
    return False
