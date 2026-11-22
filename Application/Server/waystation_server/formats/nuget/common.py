"""NuGet responses and records shared by the proxy, hosted and group handlers."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from starlette.responses import Response

from waystation_common import nuget
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.nuget.protocol import JSON_TYPE, HostedVersion, sha512_b64
from waystation_server.problems import native_error

SAFE_HEADERS = {'X-Content-Type-Options': 'nosniff'}


def nuget_error(status: int, message: str, *, headers: dict[str, str] | None = None) -> Response:
    """NuGet clients show the status line and a plain-text body."""
    return native_error(status, message, headers=headers)


def generated(
    req: FormatRequest, body: bytes, *, account: Callable[[int, int], None], content_type: str = JSON_TYPE
) -> Response:
    """A document rendered per request (service index, hosted and group documents): ``ETag``, conditional ``304``,
    ``HEAD``. ``account(status, bytes sent)`` records the request."""
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


def hosted_version(doc: Mapping[str, Any]) -> HostedVersion:
    """A hosted package version from its ``.nupkg`` asset (``nuget_db.package_files``)."""
    details: Mapping[str, Any] = doc.get('nuget') or {}
    sha512 = details.get('sha512')
    return HostedVersion(
        details=details,
        size=int(doc.get('size') or 0),
        sha512_b64=sha512_b64(sha512 if isinstance(sha512, str) else None),
    )


def latest_of(versions: list[str]) -> str | None:
    """The newest stable version, else the newest."""
    stable = [v for v in versions if not nuget.is_prerelease(v)]
    pool = stable or versions
    return max(pool, key=nuget.sort_string) if pool else None
