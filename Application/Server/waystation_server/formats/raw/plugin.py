"""Raw (generic files) plugin (FORMATS.md "Raw"; SPECIFICATION §4.1-§4.3).

Proxy repositories: ``GET``/``HEAD /repository/<repo>/<path>`` maps one-to-one onto ``<upstream_url>/<path>`` and is
cached by path:

- **Kind**: paths matching the repository's ``metadata_path_regex`` (searched in ``/<path>``, so ``(^|/)latest$``
  also matches a root-level ``latest``) are mutable *metadata*, revalidated after ``metadata_max_age_s``; everything
  else is immutable *content* (kept for ``content_max_age_s``, default forever).
- **Content-Type**: decided by the file extension from Python's built-in table (never the host's mime.types), so a
  wrong upstream type cannot change how a known file is served; otherwise the upstream's type when it is a well-formed
  media type; otherwise ``application/octet-stream``. With ``content_disposition: inline`` the types a browser would
  execute on this origin (HTML, XHTML, SVG, XML) are served as ``text/plain``.
- **Headers**: ``Content-Disposition`` (``attachment`` by default, the repository option) on file answers and
  ``X-Content-Type-Options: nosniff`` on every answer.
- Upstream 404/410 → native 404, remembered in the negative cache for ``negative_ttl_s``; other upstream failures →
  502/504 (the engine). Conditional requests on cached files (``If-None-Match``/``If-Modified-Since``) → 304.

Hosted repositories (uploads, deletes): ``formats.raw.hosted``; group repositories: ``formats.raw.group``. The
repository root and paths ending in ``/`` are directory listings of every type (``formats.raw.listing``).
"""

from __future__ import annotations

from urllib.parse import quote

from starlette.responses import Response

from waystation_server.formats.base import FormatRequest
from waystation_server.formats.raw import listing
from waystation_server.formats.raw.descriptor import DESCRIPTOR
from waystation_server.formats.raw.files import (
    DEFAULT_TYPE,
    URL_SAFE,
    asset_kind,
    content_disposition,
    finish,
    options_of,
    safe_type,
    type_by_extension,
    upstream_type,
)
from waystation_server.formats.raw.group import RawGroup
from waystation_server.formats.raw.hosted import RawHosted
from waystation_server.problems import native_error
from waystation_server.proxy.engine import AssetSpec
from waystation_server.repos.model import RepoType

__all__ = [
    'RawPlugin',
    'asset_kind',
    'content_disposition',
    'safe_type',
    'type_by_extension',
    'upstream_type',
]


def is_folder(req: FormatRequest) -> bool:
    return not req.path or req.request.url.path.endswith('/')


class RawPlugin:
    descriptor = DESCRIPTOR

    def __init__(self) -> None:
        self.hosted = RawHosted()
        self.group = RawGroup(self._member)

    async def handle(self, req: FormatRequest) -> Response:
        if req.repo.type is RepoType.GROUP:
            return await self.group.handle(req, is_folder=is_folder(req))
        return await self._member(req)

    async def _member(self, req: FormatRequest) -> Response:
        """A proxy or hosted repository's own answer (also what a group asks its members)."""
        if req.method in ('GET', 'HEAD') and is_folder(req):
            return await listing.serve(req)
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.handle(req)
        if req.method not in ('GET', 'HEAD'):
            return native_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD'})
        return await self.proxy(req)

    async def proxy(self, req: FormatRequest) -> Response:
        options = options_of(req.repo)
        disposition = options.content_disposition
        by_extension = type_by_extension(req.path)
        spec = AssetSpec(
            path=req.path,
            kind=asset_kind(req.path, options),
            upstream_path=quote(req.path, safe=URL_SAFE),
            content_type=safe_type(by_extension, disposition) if by_extension else None,
        )
        response = await req.proxy.serve(req, spec)
        if by_extension is None and 'content-type' in response.headers:
            # No known extension: the engine kept the upstream's type; serve only a sanitised form of it.
            served = safe_type(upstream_type(response.headers['content-type']) or DEFAULT_TYPE, disposition)
            response.headers['Content-Type'] = served
            if 'x-ws-content-type' in response.headers:
                response.headers['X-Ws-Content-Type'] = served
        return finish(response, req.path, options)
