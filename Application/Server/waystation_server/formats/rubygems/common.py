"""RubyGems responses shared by the proxy, hosted and group handlers."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable

from starlette.responses import Response

from waystation_server.formats.base import FormatRequest
from waystation_server.formats.rubygems.protocol import TEXT_TYPE

SAFE_HEADERS = {'X-Content-Type-Options': 'nosniff'}


def gem_error(status: int, message: str, *, headers: dict[str, str] | None = None) -> Response:
    """``gem`` and Bundler print the body of an error: plain text."""
    return Response(
        message.encode() + b'\n', status_code=status, headers={**(headers or {}), **SAFE_HEADERS}, media_type=TEXT_TYPE
    )


def digest_header(sha256_hex: str) -> str:
    """``Digest`` / ``Repr-Digest`` value of a whole representation (Bundler checks the file it appended to)."""
    return 'sha-256=:' + base64.b64encode(bytes.fromhex(sha256_hex)).decode() + ':'


def add_digest(response: Response) -> Response:
    """Add ``Repr-Digest`` and ``Digest`` to a response served from a stored blob (``X-Ws-Digest``)."""
    found = response.headers.get('x-ws-digest', '')
    if found.startswith('sha256:') and len(found) == 71:
        value = digest_header(found[len('sha256:') :])
        response.headers['Repr-Digest'] = value
        response.headers['Digest'] = value
    response.headers.update(SAFE_HEADERS)
    return response


def generated(
    req: FormatRequest, body: bytes, *, account: Callable[[int, int], None], content_type: str = TEXT_TYPE
) -> Response:
    """A document rendered per request (hosted compact index files): ``ETag``, conditional ``304``, ``HEAD``,
    ``Repr-Digest``. ``Range`` is ignored (a full ``200``: Bundler replaces its copy). ``account(status, bytes
    sent)`` records the request."""
    sha256 = hashlib.sha256(body).hexdigest()
    etag = '"' + sha256[:32] + '"'
    value = digest_header(sha256)
    headers = {'ETag': etag, 'Cache-Control': 'no-cache', 'Repr-Digest': value, 'Digest': value, **SAFE_HEADERS}
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
