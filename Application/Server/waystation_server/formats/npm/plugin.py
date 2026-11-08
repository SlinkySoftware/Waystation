"""npm proxy (FORMATS.md "npm"; T-P2-NPM M0 slice).

- ``GET /<pkg>``, ``/@scope%2f<pkg>`` and ``/@scope/<pkg>``: the packument, full (``application/json``) or abbreviated
  (``Accept: application/vnd.npm.install-v1+json``), cached separately as metadata and revalidated after the
  repository's metadata max age. Every ``versions.*.dist.tarball`` is rewritten to this repository (in a thread).
- ``GET /<pkg>/-/<file>.tgz`` and the scoped form: the tarball, cached as content and verified while it streams
  against ``dist.integrity`` (SRI, sha512) and/or ``dist.shasum`` (sha1) from the cached abbreviated packument
  (fetched first when needed; looked up only on a cache miss). A tarball the packument does not list is 404.
- ``/-/ping`` (answered locally), ``/-/whoami``, ``/-/v1/search`` (proxied, cached 5 minutes per query), audit POSTs
  (``/-/npm/v1/security/...``, relayed uncached when ``audit_passthrough`` is on).

Errors answer npm's JSON shape ``{"error": "..."}``. Hosted (publish, dist-tags) and group are later tasks.
"""

from __future__ import annotations

import asyncio
import hashlib

from starlette.responses import JSONResponse, Response

from waystation_common.errors import NotFound, PayloadTooLarge, WaystationError
from waystation_server.formats.base import FormatDescriptor, FormatRequest
from waystation_server.formats.npm import metadata
from waystation_server.formats.npm.descriptor import DESCRIPTOR, NpmOptions
from waystation_server.formats.npm.metadata import Op, Route
from waystation_server.proxy.engine import AssetKind, AssetSpec, Expected, UpstreamInfo

SEARCH_MAX_AGE_S = 300
SEARCH_MAX_BYTES = 4 * 1024 * 1024
MAX_QUERY_LENGTH = 2048
AUDIT_MAX_REQUEST_BYTES = 16 * 1024 * 1024
AUDIT_FORWARD_HEADERS = ('content-type', 'content-encoding', 'accept', 'npm-command')


def npm_error(status: int, message: str, *, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse({'error': message}, status_code=status, headers=headers)


def wants_abbreviated(accept: str) -> bool:
    return metadata.ABBREVIATED_TYPE in accept.lower()


class NpmPlugin:
    def __init__(self, descriptor: FormatDescriptor = DESCRIPTOR) -> None:
        self.descriptor = descriptor

    async def handle(self, req: FormatRequest) -> Response:
        try:
            return await self._handle(req)
        except WaystationError as exc:
            headers = getattr(exc, 'headers', None)
            return npm_error(exc.status, exc.detail, headers=dict(headers) if headers else None)

    async def _handle(self, req: FormatRequest) -> Response:
        route = metadata.parse_route(req.path)
        if route is None:
            raise NotFound('Not Found')
        req.log(op=route.op.value)
        if route.op is Op.AUDIT:
            if req.method != 'POST':
                return npm_error(405, 'method not allowed', headers={'Allow': 'POST'})
            return await self._audit(req)
        if req.method not in ('GET', 'HEAD'):
            return npm_error(405, 'method not allowed', headers={'Allow': 'GET, HEAD'})
        match route.op:
            case Op.PING:
                return JSONResponse({})
            case Op.WHOAMI:
                if req.principal.is_anonymous:
                    return npm_error(401, 'authentication required')
                return JSONResponse({'username': req.principal.name})
            case Op.SEARCH:
                return await self._search(req)
            case Op.PACKUMENT:
                abbreviated = wants_abbreviated(req.request.headers.get('accept', ''))
                return await req.proxy.serve(req, self.packument_spec(req, route.package, abbreviated=abbreviated))
            case Op.TARBALL:
                return await req.proxy.serve(req, self.tarball_spec(req, route))
        raise NotFound('Not Found')  # pragma: no cover - every Op is handled above

    # ------------------------------------------------------------------ packuments and tarballs
    @staticmethod
    def packument_spec(
        req: FormatRequest, package: str, *, abbreviated: bool, max_age_s: int | None = None
    ) -> AssetSpec:
        repo_url = req.repo_url

        def rewrite(data: bytes, info: UpstreamInfo) -> bytes:
            return metadata.rewrite_packument(data, package=package, repo_url=repo_url)

        return AssetSpec(
            path=metadata.packument_key(package, abbreviated=abbreviated),
            kind=AssetKind.METADATA,
            upstream_path=metadata.upstream_packument_path(package),
            upstream_headers={'Accept': metadata.ABBREVIATED_ACCEPT if abbreviated else metadata.JSON_TYPE},
            content_type=metadata.ABBREVIATED_TYPE if abbreviated else metadata.JSON_TYPE,
            transform=rewrite,
            max_age_s=max_age_s,
            artifact=package,
            component=package,
        )

    def tarball_spec(self, req: FormatRequest, route: Route) -> AssetSpec:
        path = metadata.tarball_path(route.package, route.filename)

        async def expect() -> Expected | None:
            return await self.expected_for(req, route.package, route.filename)

        return AssetSpec(
            path=path,
            kind=AssetKind.CONTENT,
            expect=expect,
            content_type='application/octet-stream',
            artifact=route.filename,
            component=route.package,
        )

    async def expected_for(self, req: FormatRequest, package: str, filename: str) -> Expected | None:
        """Digests for a tarball from the cached abbreviated packument; refreshed once when the version is not
        listed (published since the last fetch). Unlisted tarballs are refused."""
        for max_age_s in (None, 0):
            spec = self.packument_spec(req, package, abbreviated=True, max_age_s=max_age_s)
            asset = await req.proxy.ensure(req, spec)
            data = await req.proxy.read(asset)
            found = await asyncio.to_thread(metadata.find_dist, data, filename)
            if found.found:
                return found.expected
        raise NotFound('Not Found')

    # ------------------------------------------------------------------ pass-through endpoints
    async def _search(self, req: FormatRequest) -> Response:
        query = req.request.url.query
        if len(query) > MAX_QUERY_LENGTH:
            return npm_error(414, 'search query too long')
        key = hashlib.sha256(query.encode()).hexdigest()[:32]
        upstream = req.repo.proxy.upstream_url if req.repo.proxy else ''
        url = upstream.rstrip('/') + '/-/v1/search' + (f'?{query}' if query else '')
        spec = AssetSpec(
            path=f'-/v1/search/{key}.json',
            kind=AssetKind.METADATA,
            upstream_url=url,
            upstream_headers={'Accept': metadata.JSON_TYPE},
            content_type=metadata.JSON_TYPE,
            max_size=SEARCH_MAX_BYTES,
            max_age_s=SEARCH_MAX_AGE_S,
            artifact='search',
        )
        return await req.proxy.serve(req, spec)

    async def _audit(self, req: FormatRequest) -> Response:
        options = self.descriptor.parse_options(req.repo.format_options)
        if not isinstance(options, NpmOptions) or not options.audit_passthrough:
            raise NotFound('audit is not available from this repository')
        chunks: list[bytes] = []
        size = 0
        async for chunk in req.request.stream():
            size += len(chunk)
            if size > AUDIT_MAX_REQUEST_BYTES:
                raise PayloadTooLarge('audit request too large')
            chunks.append(chunk)
        headers = {k: v for k in AUDIT_FORWARD_HEADERS if (v := req.request.headers.get(k))}
        return await req.proxy.relay(req, upstream_path=req.path, body=b''.join(chunks), headers=headers)
