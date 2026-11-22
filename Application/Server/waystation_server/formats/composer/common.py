"""Composer responses shared by the proxy, hosted and group handlers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from starlette.responses import Response

from waystation_server.formats.base import FormatRequest
from waystation_server.formats.composer.protocol import JSON_TYPE

SAFE_HEADERS = {'X-Content-Type-Options': 'nosniff'}


def composer_error(status: int, message: str, *, headers: dict[str, str] | None = None) -> Response:
    """Composer shows the status line; a JSON ``{"message": ...}`` body is what Packagist-like servers send."""
    body = json.dumps({'status': 'error', 'message': message}).encode()
    return Response(body, status_code=status, headers={**(headers or {}), **SAFE_HEADERS}, media_type=JSON_TYPE)


def generated(req: FormatRequest, body: bytes, *, account: Callable[[int, int], None]) -> Response:
    """A document rendered per request (``packages.json``, hosted and group ``p2`` documents): ``ETag``, conditional
    ``304``, ``HEAD``. ``account(status, bytes sent)`` records the request."""
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {'ETag': etag, 'Cache-Control': 'no-cache', **SAFE_HEADERS}
    match = req.request.headers.get('if-none-match')
    if match and etag in {m.strip() for m in match.split(',')}:
        account(304, 0)
        return Response(status_code=304, headers=headers)
    if req.is_head:
        account(200, 0)
        headers['Content-Length'] = str(len(body))
        return Response(status_code=200, headers=headers, media_type=JSON_TYPE)
    account(200, len(body))
    return Response(body, status_code=200, headers=headers, media_type=JSON_TYPE)
