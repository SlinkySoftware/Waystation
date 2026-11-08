"""Serving cached blobs (ARCHITECTURE §4 step 5, §5; SPECIFICATION §5.1).

Cached bytes never pass through Python: a ``GET`` is answered with an empty response carrying
``X-Accel-Redirect: /_blobs/sha256/aa/bb/<hex>``; nginx's internal ``/_blobs/`` location then serves the file with
``sendfile`` and handles ``Range`` requests. Because nginx drops most upstream headers on an internal redirect and
computes its own ETag, the server:

- answers conditional requests itself: ``If-None-Match`` against the digest ETag (and ``If-Modified-Since`` when
  there is no ``If-None-Match``) → ``304`` before any redirect;
- answers ``HEAD`` itself (``Content-Length``, type and headers, no redirect);
- sends ``X-Ws-Etag``, ``X-Ws-Digest`` and ``X-Ws-Content-Type`` plus format headers (``Docker-Content-Digest``,
  ``Content-Disposition``...); the ``/_blobs/`` location re-adds ``ETag``, ``Docker-Content-Digest``,
  ``Docker-Distribution-API-Version`` and ``X-Ws-Cache`` from them. nginx keeps the server's ``Content-Type``.

Without nginx in front (unit tests, a misconfigured proxy) a ``GET`` returns an empty body: never the bytes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime

from starlette.requests import Request
from starlette.responses import Response

from waystation_common.storage import BlobStore

XACCEL_HEADER = 'X-Accel-Redirect'
DEFAULT_TYPE = 'application/octet-stream'


@dataclass(frozen=True)
class ServedBlob:
    sha256: str
    size: int
    content_type: str = DEFAULT_TYPE
    last_modified: datetime | None = None
    headers: Mapping[str, str] = field(default_factory=dict)  # format headers replayed on every response

    @property
    def etag(self) -> str:
        return f'"sha256:{self.sha256}"'


def _etag_matches(header: str, etag: str) -> bool:
    if header.strip() == '*':
        return True
    wanted = etag.strip('"')
    for candidate in header.split(','):
        value = candidate.strip()
        if value.startswith('W/'):
            value = value[2:]
        if value.strip('"') == wanted:
            return True
    return False


def not_modified(request: Request, blob: ServedBlob) -> bool:
    if_none_match = request.headers.get('if-none-match')
    if if_none_match is not None:
        return _etag_matches(if_none_match, blob.etag)
    since = request.headers.get('if-modified-since')
    if since and blob.last_modified is not None:
        try:
            when = parsedate_to_datetime(since)
        except TypeError, ValueError:
            return False
        if when.tzinfo is None:
            return False
        return blob.last_modified.replace(microsecond=0) <= when
    return False


def blob_headers(blob: ServedBlob, *, cache: str | None) -> dict[str, str]:
    headers = {
        'ETag': blob.etag,
        'X-Ws-Etag': blob.etag,
        'X-Ws-Digest': f'sha256:{blob.sha256}',
        'X-Ws-Content-Type': blob.content_type,
        'Accept-Ranges': 'bytes',
    }
    if cache:
        headers['X-Ws-Cache'] = cache
    if blob.last_modified is not None:
        headers['Last-Modified'] = format_datetime(blob.last_modified.astimezone(UTC), usegmt=True)
    headers.update(blob.headers)
    return headers


def serve_blob(request: Request, blob: ServedBlob, *, cache: str | None = None) -> Response:
    """The response for a cached blob: 304, a HEAD answer, or an X-Accel redirect (empty body)."""
    headers = blob_headers(blob, cache=cache)
    headers['Content-Type'] = blob.content_type  # as given: no charset appended
    if request.method in ('GET', 'HEAD') and not_modified(request, blob):
        keep = {k: v for k, v in headers.items() if k not in ('Accept-Ranges', 'X-Ws-Content-Type', 'Content-Type')}
        return Response(status_code=304, headers=keep)
    if request.method == 'HEAD':
        headers['Content-Length'] = str(blob.size)
        return Response(status_code=200, headers=headers)
    del headers['Accept-Ranges']  # nginx adds its own for the file it serves
    headers[XACCEL_HEADER] = BlobStore.path_for_xaccel(blob.sha256)
    return Response(status_code=200, headers=headers)
