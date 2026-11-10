"""npm plugin (FORMATS.md "npm"): proxy (T-P2-NPM), hosted and group repositories (T-P5-NPMH).

**Proxy**:

- ``GET /<pkg>``, ``/@scope%2f<pkg>`` and ``/@scope/<pkg>``: the packument, full (``application/json``) or abbreviated
  (``Accept: application/vnd.npm.install-v1+json``), cached separately as metadata and revalidated after the
  repository's metadata max age. Every ``versions.*.dist.tarball`` is rewritten to this repository (in a thread).
- ``GET /<pkg>/<version>`` and ``/<pkg>/<dist-tag>`` (scoped: either encoding): one version object of the full
  packument (``versions[<v>]``, else the version the tag names; corepack fetches these), read from the cached full
  packument through the same ``ensure`` path (max age, coalescing, stale-on-error, rewritten ``dist.tarball``). An
  exact version the cache does not list refreshes it once (published since the last fetch); unknown answers 404
  ``{"error": "version not found: <v>"}``. Hosted: from the generated full packument; group: from the merged one.
- ``GET /<pkg>/-/<file>.tgz`` and the scoped form: the tarball, cached as content and verified while it streams
  against ``dist.integrity`` (SRI, sha512) and/or ``dist.shasum`` (sha1) from the cached abbreviated packument
  (fetched first when needed; looked up only on a cache miss). A tarball the packument does not list is 404.
- ``/-/v1/search`` (proxied, cached 5 minutes per query), audit POSTs (``/-/npm/v1/security/...``, relayed uncached
  when ``audit_passthrough`` is on), ``GET -/package/<pkg>/dist-tags`` (from the cached packument). Writes: 405.

**Hosted** (``formats.npm.hosted``): publish, packuments generated from the stored versions, deprecate, unpublish,
dist-tags, search. **Group** (``formats.npm.group``): merged packuments, tarballs from the members in order, writes
to the writable member.

**Every type**: ``/-/ping`` (answered locally), ``/-/whoami``, ``npm login --auth-type=legacy``
(``PUT /-/user/org.couchdb.user:<name>``: the password is exchanged for a new API token of the user,
``waystation_server.client_login``; the router does not authorise it, the sign-in applies the repository's
visibility) and ``npm logout`` (``DELETE /-/user/token/<token>``: revokes the token the request used).

Errors answer npm's JSON shape ``{"error": "..."}``.

Browse (``browser``, ``formats.npm.browse``): when a proxy stores a packument, its summary is recorded in
``npm_packages`` in the background (parsed in a thread), so the response never waits for it; hosted repositories
write theirs with every change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from starlette.responses import JSONResponse, Response

from waystation_common.errors import InsufficientStorage, NotFound, PayloadTooLarge, ValidationFailed, WaystationError
from waystation_server.db import npm as npm_db
from waystation_server.formats.base import FormatDescriptor, FormatRequest
from waystation_server.formats.common.background import Background
from waystation_server.formats.npm import metadata, publish
from waystation_server.formats.npm.browse import NpmBrowse, parse_packument
from waystation_server.formats.npm.descriptor import DESCRIPTOR, NpmOptions
from waystation_server.formats.npm.group import NpmGroup
from waystation_server.formats.npm.hosted import NpmHosted, read_body, version_not_found, version_response
from waystation_server.formats.npm.metadata import Op, Route
from waystation_server.proxy.engine import (
    AssetKind,
    AssetSpec,
    Expected,
    OnStored,
    ProxyEngine,
    StoredAsset,
    UpstreamInfo,
)
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.repos.quota import QuotaExceeded

SEARCH_MAX_AGE_S = 300
SEARCH_MAX_BYTES = 4 * 1024 * 1024
MAX_QUERY_LENGTH = 2048
AUDIT_MAX_REQUEST_BYTES = 16 * 1024 * 1024
AUDIT_FORWARD_HEADERS = ('content-type', 'content-encoding', 'accept', 'npm-command')
LOGIN_MAX_BYTES = 64 * 1024


def npm_error(status: int, message: str, *, headers: dict[str, str] | None = None) -> Response:
    return JSONResponse({'error': message}, status_code=status, headers=headers)


def wants_abbreviated(accept: str) -> bool:
    return metadata.ABBREVIATED_TYPE in accept.lower()


class NpmPlugin:
    def __init__(self, descriptor: FormatDescriptor = DESCRIPTOR) -> None:
        self.descriptor = descriptor
        self.browser = NpmBrowse()
        self.hosted = NpmHosted(self)
        self.group = NpmGroup(self)
        self._indexing = Background('npm-packages')

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        await self._indexing.drain()

    async def handle(self, req: FormatRequest) -> Response:
        try:
            return await self._handle(req)
        except QuotaExceeded as exc:  # a hard repository quota (SPECIFICATION §4.8)
            return npm_error(403, exc.detail)
        except InsufficientStorage:  # the low-disk guard
            return npm_error(507, 'not enough free space in the blob store')
        except WaystationError as exc:
            headers = getattr(exc, 'headers', None)
            return npm_error(exc.status, exc.detail, headers=dict(headers) if headers else None)

    @staticmethod
    def wants_abbreviated(req: FormatRequest) -> bool:
        return wants_abbreviated(req.request.headers.get('accept', ''))

    async def _handle(self, req: FormatRequest) -> Response:
        route = metadata.parse_route(req.path)
        if route is None:
            raise NotFound('Not Found')
        req.log(op=route.op.value)
        match route.op:
            case Op.LOGIN:
                return await self._login(req, route)
            case Op.LOGOUT:
                return await self._logout(req, route)
            case Op.PING | Op.WHOAMI:
                if req.method not in ('GET', 'HEAD'):
                    return npm_error(405, 'method not allowed', headers={'Allow': 'GET, HEAD'})
                if route.op is Op.PING:
                    return JSONResponse({})
                if req.principal.is_anonymous:
                    return npm_error(401, 'authentication required')
                return JSONResponse({'username': req.principal.name})
        if req.repo.type is RepoType.GROUP:
            return await self.group.handle(req, route)
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.handle(req, route)
        return await self._proxy(req, route)

    async def _proxy(self, req: FormatRequest, route: Route) -> Response:
        if route.op is Op.AUDIT:
            if req.method != 'POST':
                return npm_error(405, 'method not allowed', headers={'Allow': 'POST'})
            return await self.audit(req)
        if req.method not in ('GET', 'HEAD'):
            return npm_error(
                405,
                'this repository is a read-only proxy: publish to a hosted repository',
                headers={'Allow': 'GET, HEAD'},
            )
        match route.op:
            case Op.SEARCH:
                return await self.search(req)
            case Op.PACKUMENT:
                abbreviated = self.wants_abbreviated(req)
                return await req.proxy.serve(req, self.packument_spec(req, route.package, abbreviated=abbreviated))
            case Op.VERSION:
                return await self.version(req, route)
            case Op.TARBALL:
                return await req.proxy.serve(req, self.tarball_spec(req, route))
            case Op.DIST_TAGS:
                asset = await req.proxy.ensure(req, self.packument_spec(req, route.package, abbreviated=True))
                data = await req.proxy.read(asset)
                return JSONResponse(await asyncio.to_thread(publish.tags_of, data))
        raise NotFound('Not Found')

    # ------------------------------------------------------------------ npm login / logout
    async def _login(self, req: FormatRequest, route: Route) -> Response:
        if req.method != 'PUT':
            return npm_error(405, 'method not allowed', headers={'Allow': 'PUT'})
        raw = await read_body(req, LOGIN_MAX_BYTES)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ValidationFailed('the body must be a JSON object')
        name, password = body.get('name', route.arg), body.get('password')
        if not isinstance(name, str) or not isinstance(password, str) or name != route.arg:
            raise ValidationFailed('name and password are required')
        secret = await req.sign_in(name, password, client='npm')
        return JSONResponse(
            {'ok': True, 'id': f'{metadata.COUCH_USER}{name}', 'token': secret},
            status_code=201,
            headers={'Cache-Control': 'no-store'},
        )

    async def _logout(self, req: FormatRequest, route: Route) -> Response:
        if req.method != 'DELETE':
            return npm_error(405, 'method not allowed', headers={'Allow': 'DELETE'})
        if not await req.sign_out(route.arg):
            raise NotFound('Not Found')
        return JSONResponse({'ok': True})

    # ------------------------------------------------------------------ packuments and tarballs
    def packument_spec(
        self, req: FormatRequest, package: str, *, abbreviated: bool, max_age_s: int | None = None
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
            on_stored=self._on_packument_stored(req.proxy, req.repo.id, package),
            artifact=package,
            component=package,
        )

    def _on_packument_stored(self, engine: ProxyEngine, repo_id: str, package: str) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            async def index() -> None:
                raw = data if data is not None else await engine.read(asset)
                parsed = await asyncio.to_thread(parse_packument, raw, package)
                await npm_db.upsert(engine.db, repo_id, package, parsed.summary())

            self._indexing.spawn((repo_id, asset.path, asset.blob_sha256), index)

        return stored

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

    async def version(self, req: FormatRequest, route: Route) -> Response:
        """``<pkg>/<version or dist-tag>`` from the cached full packument (fetched or revalidated like a packument
        request). An exact version it does not list refreshes it once, as a tarball lookup does."""
        for max_age_s in (None, 0):
            spec = self.packument_spec(req, route.package, abbreviated=False, max_age_s=max_age_s)
            data = await req.proxy.read(await req.proxy.ensure(req, spec))
            response = await version_response(data, route.arg)
            if response is not None:
                return response
            if not metadata.valid_version(route.arg):
                break
        raise version_not_found(route.arg)

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
    async def search(self, req: FormatRequest) -> Response:
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

    def audits(self, repo: RepoConfig) -> bool:
        """Does this proxy relay audit requests (``audit_passthrough``)?"""
        options = self.descriptor.parse_options(repo.format_options)
        return isinstance(options, NpmOptions) and options.audit_passthrough

    async def audit(self, req: FormatRequest) -> Response:
        if not self.audits(req.repo):
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
