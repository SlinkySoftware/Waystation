"""Cargo plugin (T-P6-CARGO; FORMATS.md "Cargo"): sparse registries for proxy, hosted and group repositories.

Routes under ``/repository/<repo>/`` (Cargo: ``sparse+<base>/repository/<repo>/index/``):

- ``index/config.json``: generated per request: ``dl`` = ``<repo>/crates/{crate}/{crate}-{version}.crate``, ``api`` =
  the repository URL, ``auth-required`` when the request was authenticated (a repository anonymous cannot read
  answers the ``401`` challenge first; Cargo then retries with its token and sends it on every later request).
- ``index/<prefix>/<crate>``: index files (Cargo's ``1/``, ``2/``, ``3/<c>/``, ``<ab>/<cd>/`` layout, lower case).
- ``crates/<crate>/<crate>-<version>.crate``: downloads.
- ``api/v1/crates/new`` (``PUT``, publish), ``api/v1/crates/<crate>/<version>/yank`` (``DELETE``) and ``/unyank``
  (``PUT``), ``api/v1/crates/<crate>/owners``, ``api/v1/crates?q=`` (``cargo search``: the crates this repository
  knows, by name).

**Proxy**: an index file is cached as metadata (revalidated with the upstream's ``ETag``/``Last-Modified`` after the
repository's metadata max age) once every line checked (``common.parse_index``). A ``.crate`` is fetched from the URL
the upstream's ``config.json`` ``dl`` template gives (that file cached as ``_upstream/config.json``) and verified
against the index entry's ``cksum`` (sha256) before it is committed; a version the index does not list (after one
refresh) is 404. The upstream URL is the sparse index root (``https://index.crates.io/``).

**Hosted** (``formats.cargo.hosted``) and **group** (``formats.cargo.group``) index files are rendered per request.

Browse (``browser``, ``formats.cargo.browse``): crate summaries in ``cargo_crates`` (proxy: in the background after an
index file is cached; hosted: on publish and yank).
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections import OrderedDict
from typing import Any

from starlette.responses import JSONResponse, Response

from waystation_common.errors import NotFound, UpstreamError, WaystationError
from waystation_common.events import CacheState
from waystation_server.db import cargo as cargo_db
from waystation_server.formats.base import FormatDescriptor, FormatRequest, WarmContext
from waystation_server.formats.cargo import prefetch
from waystation_server.formats.cargo.browse import CargoBrowse
from waystation_server.formats.cargo.common import (
    CONFIG_TYPE,
    CRATE_TYPE,
    INDEX_TYPE,
    MAX_CONFIG,
    MAX_INDEX,
    SAFE_HEADERS,
    UPSTREAM_CONFIG,
    Op,
    Route,
    cargo_error,
    config_body,
    crate_path,
    download_url,
    find,
    generated,
    index_path,
    is_api,
    parse_index,
    parse_route,
    parse_upstream_config,
    summary_fields,
)
from waystation_server.formats.cargo.descriptor import DESCRIPTOR
from waystation_server.formats.cargo.group import CargoGroup
from waystation_server.formats.cargo.hosted import CargoHosted
from waystation_server.formats.common.background import Background
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

PARSED = 32
MAX_SEARCH = 100


class CargoPlugin:
    def __init__(self, descriptor: FormatDescriptor = DESCRIPTOR) -> None:
        self.descriptor = descriptor
        self.browser = CargoBrowse()
        self.hosted = CargoHosted()
        self.group = CargoGroup(self)
        self._indexing = Background('cargo-crates')
        self._parsed: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._dl: dict[str, str] = {}

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        await self._indexing.drain()

    async def warm(self, ctx: WarmContext, item: str) -> None:
        """Prefetch (T-O12): ``formats.cargo.prefetch``."""
        await prefetch.warm(ctx, item)

    async def handle(self, req: FormatRequest) -> Response:
        api = is_api(req.path)
        try:
            return await self._handle(req)
        except WaystationError as exc:
            headers = getattr(exc, 'headers', None)
            return cargo_error(exc.status, exc.detail, api=api, headers=dict(headers) if headers else None)

    async def _handle(self, req: FormatRequest) -> Response:
        route = parse_route(req.path, req.method)
        if route is None:
            raise NotFound('Not Found')
        req.log(op=route.op.value)
        match route.op:
            case Op.PUBLISH | Op.YANK | Op.UNYANK | Op.OWNERS:
                return await self.api(req, route)
            case Op.SEARCH:
                return await self._only(req, 'GET', self.search)
        if req.method not in ('GET', 'HEAD'):
            return cargo_error(405, 'method not allowed', api=False, headers={'Allow': 'GET, HEAD'})
        return await self.serve(req, route)

    @staticmethod
    async def _only(req: FormatRequest, method: str, call: Any) -> Response:
        if req.method != method:
            return cargo_error(405, 'method not allowed', api=True, headers={'Allow': method})
        response: Response = await call(req)
        return response

    async def api(self, req: FormatRequest, route: Route) -> Response:
        """The registry web API (hosted repositories, and groups through their writable member)."""
        allowed = {Op.PUBLISH: ('PUT',), Op.YANK: ('DELETE',), Op.UNYANK: ('PUT',), Op.OWNERS: ('GET', 'PUT', 'DELETE')}
        if req.method not in allowed[route.op]:
            return cargo_error(405, 'method not allowed', api=True, headers={'Allow': ', '.join(allowed[route.op])})
        if req.repo.type is RepoType.GROUP:
            return await self.group.api(req, route)
        if req.repo.type is not RepoType.HOSTED:
            return cargo_error(405, 'this repository is a read-only proxy: publish to a hosted repository', api=True)
        if route.op is Op.PUBLISH:
            return await self.hosted.publish(req)
        if route.op is Op.OWNERS:
            return await self.hosted.owners(req, route)
        return await self.hosted.yank(req, route)

    async def serve(self, req: FormatRequest, route: Route) -> Response:
        """Answer a read (config, index file, download) from ``req.repo``."""
        if route.op is Op.CONFIG:
            started = time.perf_counter()

            def account(status: int, sent: int) -> None:
                req.hosted.account(req, CacheState.HIT, status, sent, started)

            return generated(req, config_body(req), CONFIG_TYPE, account=account)
        if req.repo.type is RepoType.GROUP:
            return await self.group.serve(req, route)
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.serve(req, route)
        response = await self.proxy_serve(req, route)
        response.headers.update(SAFE_HEADERS)
        return response

    # ------------------------------------------------------------------ search
    async def search(self, req: FormatRequest) -> Response:
        """``cargo search``: crates this repository (a group: its members) knows, by name."""
        q = (req.request.query_params.get('q') or '').strip()[:64]
        per_page = req.request.query_params.get('per_page') or '10'
        limit = min(int(per_page), MAX_SEARCH) if per_page.isdigit() and int(per_page) > 0 else 10
        repos = await self.group.members(req) if req.repo.type is RepoType.GROUP else [req.repo]
        found: dict[str, dict[str, Any]] = {}
        for repo in repos:
            for doc in await cargo_db.page(req.hosted.db, repo.id, after=None, q=q or None, limit=limit):
                found.setdefault(str(doc['name']), doc)
        crates = [
            {
                'name': str(doc.get('display_name') or name),
                'max_version': doc.get('latest') or '0.0.0',
                'description': doc.get('description'),
            }
            for name, doc in sorted(found.items())[:limit]
        ]
        return JSONResponse({'crates': crates, 'meta': {'total': len(found)}})

    # ------------------------------------------------------------------ proxy
    async def proxy_serve(self, req: FormatRequest, route: Route) -> Response:
        if req.repo.proxy is None:
            raise NotFound('Not Found')
        if route.op is Op.INDEX:
            return await req.proxy.serve(req, self.index_spec(req, route.crate))
        if route.op is Op.DOWNLOAD:
            return await req.proxy.serve(req, await self.crate_spec(req, route.crate, route.version))
        raise NotFound('Not Found')

    def index_spec(self, req: FormatRequest, crate: str, *, max_age_s: int | None = None) -> AssetSpec:
        path = index_path(crate)
        found: dict[str, list[dict[str, Any]]] = {}

        def validate(data: bytes, info: UpstreamInfo) -> None:
            found['entries'] = parse_index(data, crate)

        return AssetSpec(
            path=path,
            kind=AssetKind.METADATA,
            upstream_path=path.removeprefix('index/'),
            content_type=INDEX_TYPE,
            max_size=MAX_INDEX,
            validate=validate,
            max_age_s=max_age_s,
            on_stored=self._on_index_stored(req.proxy, req.repo.id, crate, found),
            artifact=f'{crate} index',
            component=crate,
        )

    def _on_index_stored(
        self, engine: ProxyEngine, repo_id: str, crate: str, found: dict[str, list[dict[str, Any]]]
    ) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            entries = found.get('entries')
            if entries is None:
                return
            fields = summary_fields(entries, crate)

            async def index() -> None:
                await cargo_db.upsert_crate(engine.db, repo_id, crate, fields)

            self._indexing.spawn((repo_id, crate, asset.blob_sha256), index)

        return stored

    async def index_entries(
        self, req: FormatRequest, crate: str, *, max_age_s: int | None = None
    ) -> list[dict[str, Any]]:
        """A proxy's cached (refreshed as needed) index file of ``crate``, parsed (kept per blob digest)."""
        asset = await req.proxy.ensure(req, self.index_spec(req, crate, max_age_s=max_age_s))
        entries = self._parsed.get(asset.blob_sha256)
        if entries is None:
            data = await req.proxy.read(asset, max_bytes=MAX_INDEX)
            entries = await asyncio.to_thread(parse_index, data, crate)
            self._parsed[asset.blob_sha256] = entries
            while len(self._parsed) > PARSED:
                self._parsed.popitem(last=False)
        else:
            self._parsed.move_to_end(asset.blob_sha256)
        return entries

    async def lookup(self, req: FormatRequest, crate: str, version: str) -> dict[str, Any]:
        """The index entry of ``crate@version`` (refreshed once when the version is not listed); 404 otherwise."""
        for max_age_s in (None, 0):
            entry = find(await self.index_entries(req, crate, max_age_s=max_age_s), version)
            if entry is not None:
                return dict(entry)
        raise NotFound('Not Found')

    async def dl_template(self, req: FormatRequest) -> str:
        spec = AssetSpec(
            path=UPSTREAM_CONFIG,
            kind=AssetKind.METADATA,
            upstream_path='config.json',
            content_type=CONFIG_TYPE,
            max_size=MAX_CONFIG,
            validate=_validate_config,
            artifact='config.json',
        )
        asset = await req.proxy.ensure(req, spec)
        template = self._dl.get(asset.blob_sha256)
        if template is None:
            template = parse_upstream_config(await req.proxy.read(asset, max_bytes=MAX_CONFIG))
            self._dl = {asset.blob_sha256: template}
        return template

    async def crate_spec(self, req: FormatRequest, crate: str, version: str) -> AssetSpec:
        path = crate_path(crate, version)
        artifact = f'{crate}-{version}.crate'
        cached = await req.hosted.get(req.repo, path)
        if cached is not None and cached.upstream.url:
            # Cached: no index lookup unless the engine has to fetch it again (content max age).
            async def expect() -> Expected | None:
                return Expected(sha256=str((await self.lookup(req, crate, version))['cksum']))

            return AssetSpec(
                path=path,
                kind=AssetKind.CONTENT,
                upstream_url=cached.upstream.url,
                expect=expect,
                content_type=CRATE_TYPE,
                artifact=artifact,
                component=crate,
            )
        entry = await self.lookup(req, crate, version)
        try:
            url = download_url(await self.dl_template(req), entry)
        except NotFound:
            raise UpstreamError('the upstream has no config.json') from None
        return AssetSpec(
            path=path,
            kind=AssetKind.CONTENT,
            upstream_url=url,
            expected=Expected(sha256=str(entry['cksum'])),
            content_type=CRATE_TYPE,
            artifact=artifact,
            component=crate,
        )

    def sub(self, req: FormatRequest, member: RepoConfig) -> FormatRequest:
        return dataclasses.replace(req, repo=member)


def _validate_config(data: bytes, info: UpstreamInfo) -> None:
    parse_upstream_config(data)
