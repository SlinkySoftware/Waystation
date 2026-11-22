"""NuGet v3 plugin (T-P6-NUGET; FORMATS.md "NuGet v3"): proxy, hosted and group repositories.

Routes: ``formats.nuget.protocol``. The service index is rendered per request for every repository type.

**Proxy** (the upstream URL is a v3 service index, e.g. ``https://api.nuget.org/v3/index.json``):

- The upstream's service index is cached as metadata (``_upstream/index.json``, reduced to the resources used:
  ``PackageBaseAddress/3.0.0``, the best ``RegistrationsBaseUrl`` hive (SemVer 2 first), ``SearchQueryService``).
- Version lists (``v3-flatcontainer/<id>/index.json``) and registrations (index, pages, leaves under
  ``v3/registration/<id>/``) are cached as metadata (revalidated after the metadata max age), validated and rewritten:
  every URL under an upstream registration hive or the flat container points at this repository. Registration
  documents arrive gzip-encoded from nuget.org; the upstream client decodes them.
- ``.nupkg`` files are cached as content. Before one is committed it is checked (``CheckedExpected``): a readable
  package (zip-slip/zip-bomb guards) whose ``.nuspec`` names the requested id and version; with ``verify_catalog``
  (default) also the SHA-512 and size its catalog leaf publishes (``_catalog/<id>/<version>.json``, found through the
  registration; only on hosts the upstream's resources use). ``.nuspec`` files are cached as content after parsing.
- Search passes through to the upstream's ``SearchQueryService`` (not cached), URLs rewritten.
- A package summary per id (``nuget_packages``: versions, newest, description) is recorded in the background when a
  version list or registration index is cached (browse, global search).

**Hosted**: ``formats.nuget.hosted``. **Group**: ``formats.nuget.group``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit

from starlette.responses import Response

from waystation_common import nuget
from waystation_common.errors import DigestMismatch, NotFound, UpstreamError, ValidationFailed, WaystationError
from waystation_common.events import CacheState
from waystation_server.db import nuget as nuget_db
from waystation_server.formats.base import FormatDescriptor, FormatRequest, WarmContext
from waystation_server.formats.common.background import Background
from waystation_server.formats.nuget import package, prefetch, protocol
from waystation_server.formats.nuget.browse import NugetBrowse
from waystation_server.formats.nuget.common import SAFE_HEADERS, generated, latest_of, nuget_error
from waystation_server.formats.nuget.descriptor import DESCRIPTOR, NugetOptions
from waystation_server.formats.nuget.group import NugetGroup
from waystation_server.formats.nuget.hosted import NugetHosted
from waystation_server.formats.nuget.protocol import Op, Route, SearchQuery, Upstream
from waystation_server.proxy.engine import (
    AssetKind,
    AssetSpec,
    CheckedExpected,
    OnStored,
    ProxyEngine,
    StoredAsset,
    UpstreamInfo,
)
from waystation_server.repos.model import RepoType

log = logging.getLogger('waystation.formats.nuget')

UPSTREAMS_KEPT = 64
INDEX_MAX_BYTES = 1024 * 1024
CATALOG_MAX_BYTES = 16 * 1024 * 1024
CATALOG_AGE_S = 10 * 365 * 24 * 3600  # catalog leaves never change
MAX_PAGES = 64  # registration pages fetched to find a version or to merge a group
SEARCH_MAX_BYTES = 16 * 1024 * 1024
SUMMARY_VERSIONS = 5000


class NugetPlugin:
    def __init__(self, descriptor: FormatDescriptor = DESCRIPTOR) -> None:
        self.descriptor = descriptor
        self.browser = NugetBrowse()
        self.hosted = NugetHosted()
        self.group = NugetGroup(self)
        self._indexing = Background('nuget-packages')
        self._upstreams: OrderedDict[str, Upstream] = OrderedDict()

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        await self._indexing.drain()

    async def warm(self, ctx: WarmContext, item: str) -> None:
        """Prefetch (T-O12): ``formats.nuget.prefetch``."""
        await prefetch.warm(ctx, item)

    async def handle(self, req: FormatRequest) -> Response:
        try:
            return await self._handle(req)
        except WaystationError as exc:
            headers = getattr(exc, 'headers', None)
            return nuget_error(exc.status, exc.detail, headers=dict(headers) if headers else None)

    async def _handle(self, req: FormatRequest) -> Response:
        route = protocol.parse_route(req.path)
        if route is None:
            raise NotFound('Not Found')
        req.log(op=route.op.value)
        if route.op in (Op.PUBLISH, Op.PACKAGE):
            if req.repo.type is RepoType.GROUP:
                return await self.group.write(req, route)
            if req.repo.type is RepoType.HOSTED:
                return await self.hosted.handle_write(req, route)
            return nuget_error(405, 'this repository is a read-only proxy: push to a hosted repository')
        if req.method not in ('GET', 'HEAD'):
            return nuget_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD'})
        return await self.serve(req, route)

    async def serve(self, req: FormatRequest, route: Route) -> Response:
        if req.repo.type is RepoType.GROUP:
            return await self.group.serve(req, route)
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.serve(req, route)
        return await self.proxy_serve(req, route)

    # ------------------------------------------------------------------ proxy
    async def proxy_serve(self, req: FormatRequest, route: Route) -> Response:
        if req.repo.proxy is None:
            raise NotFound('Not Found')
        if route.op is Op.SERVICE_INDEX:
            started = time.perf_counter()

            def account(status: int, sent: int) -> None:
                req.hosted.account(req, CacheState.HIT, status, sent, started)

            return generated(req, protocol.service_index(req.repo_url, publish=False), account=account)
        upstream = await self.upstream(req)
        match route.op:
            case Op.FLAT_INDEX:
                spec = self.versions_spec(req, upstream, route.id)
            case Op.REGISTRATION | Op.REGISTRATION_PART:
                spec = self.registration_spec(req, upstream, route)
            case Op.NUPKG:
                spec = self.nupkg_spec(req, upstream, route)
            case Op.NUSPEC:
                spec = self.nuspec_spec(req, upstream, route)
            case Op.SEARCH:
                return await self.search(req, upstream)
            case _:
                raise NotFound('Not Found')
        response = await req.proxy.serve(req, spec)
        response.headers.update(SAFE_HEADERS)
        return response

    def upstream_spec(self, req: FormatRequest) -> AssetSpec:
        if req.repo.proxy is None:
            raise NotFound('Not Found')

        def transform(data: bytes, info: UpstreamInfo) -> bytes:
            return protocol.dump(protocol.parse_upstream_index(data, info.url).json())

        return AssetSpec(
            path=protocol.UPSTREAM_INDEX_KEY,
            kind=AssetKind.METADATA,
            upstream_url=protocol.service_index_url(req.repo.proxy.upstream_url),
            upstream_headers={'Accept': protocol.JSON_TYPE},
            content_type=protocol.JSON_TYPE,
            transform=transform,
            max_size=INDEX_MAX_BYTES * 4,
            artifact='service index',
        )

    async def upstream(self, req: FormatRequest) -> Upstream:
        """The proxy's upstream resources (cached service index; parsed copies kept per process by blob digest)."""
        asset = await req.proxy.ensure(req, self.upstream_spec(req))
        found = self._upstreams.get(asset.blob_sha256)
        if found is None:
            data = await req.proxy.read(asset, max_bytes=INDEX_MAX_BYTES)
            found = protocol.Upstream.from_json(protocol.load(data, 'the cached service index'))
            self._upstreams[asset.blob_sha256] = found
            while len(self._upstreams) > UPSTREAMS_KEPT:
                self._upstreams.popitem(last=False)
        else:
            self._upstreams.move_to_end(asset.blob_sha256)
        return found

    def versions_spec(
        self, req: FormatRequest, upstream: Upstream, package_id: str, *, max_age_s: int | None = None
    ) -> AssetSpec:
        found: dict[str, list[str]] = {}

        def transform(data: bytes, info: UpstreamInfo) -> bytes:
            versions = protocol.parse_versions(data)
            found['versions'] = versions
            return protocol.render_versions(versions)

        return AssetSpec(
            path=f'{protocol.FLAT}/{package_id}/index.json',
            kind=AssetKind.METADATA,
            upstream_url=f'{upstream.flat}{package_id}/index.json',
            upstream_headers={'Accept': protocol.JSON_TYPE},
            content_type=protocol.JSON_TYPE,
            transform=transform,
            max_age_s=max_age_s,
            on_stored=self._on_versions_stored(req.proxy, req.repo.id, package_id, found),
            artifact=f'{package_id} versions',
            component=package_id,
        )

    def _on_versions_stored(
        self, engine: ProxyEngine, repo_id: str, package_id: str, found: dict[str, list[str]]
    ) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            versions = found.get('versions')
            if versions is None:
                return
            fields = {
                'versions': sorted(versions, key=nuget.sort_string)[-SUMMARY_VERSIONS:],
                'version_count': len(versions),
                'latest': latest_of(versions),
            }

            async def index() -> None:
                await nuget_db.upsert_package(
                    engine.db, repo_id, package_id, fields, on_insert={'display_name': package_id}
                )

            self._indexing.spawn((repo_id, package_id, asset.blob_sha256), index)

        return stored

    def registration_spec(self, req: FormatRequest, upstream: Upstream, route: Route) -> AssetSpec:
        rest = route.rest or 'index.json'
        repo_url = req.repo_url
        found: dict[str, dict[str, Any]] = {}

        def transform(data: bytes, info: UpstreamInfo) -> bytes:
            body = protocol.rewrite_registration(data, upstream, repo_url)
            doc = protocol.load(body, 'the registration')
            if route.op is Op.REGISTRATION:
                found['summary'] = registration_summary(doc)
            elif isinstance(doc, dict) and isinstance(doc.get('items'), list):  # a page: the id and its newest entry
                page = registration_summary({'items': [doc]})
                found['summary'] = {k: v for k, v in page.items() if k not in ('versions', 'version_count', 'latest')}
            return body

        return AssetSpec(
            path=route.key,
            kind=AssetKind.METADATA,
            upstream_url=f'{upstream.registration}{route.id}/{rest}',
            upstream_headers={'Accept': protocol.JSON_TYPE},
            content_type=protocol.JSON_TYPE,
            transform=transform,
            max_size=protocol.MAX_DOCUMENT,
            on_stored=self._on_registration_stored(req.proxy, req.repo.id, route.id, found),
            artifact=f'{route.id} registration',
            component=route.id,
        )

    def _on_registration_stored(
        self, engine: ProxyEngine, repo_id: str, package_id: str, found: dict[str, dict[str, Any]]
    ) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            fields = found.get('summary')
            if not fields:
                return

            async def index() -> None:
                await nuget_db.upsert_package(engine.db, repo_id, package_id, fields)

            self._indexing.spawn((repo_id, package_id, asset.blob_sha256), index)

        return stored

    def nupkg_spec(self, req: FormatRequest, upstream: Upstream, route: Route) -> AssetSpec:
        options = NugetOptions.model_validate(req.repo.format_options or {})

        async def expect() -> CheckedExpected:
            def check(file: Any) -> None:
                try:
                    package.check_package(file, route.id, route.version)
                except ValidationFailed as exc:
                    raise DigestMismatch(f'upstream package refused: {exc.detail}') from None

            found = await self.catalog_hash(req, upstream, route) if options.verify_catalog else None
            if found is None:
                return CheckedExpected(check=check)
            return CheckedExpected(sha512=found.sha512_hex, size=found.size, check=check)

        return AssetSpec(
            path=route.key,
            kind=AssetKind.CONTENT,
            upstream_url=f'{upstream.flat}{route.id}/{route.version}/{route.id}.{route.version}.nupkg',
            expect=expect,
            content_type=protocol.NUPKG_TYPE,
            head_passthrough=False,
            artifact=route.key.rsplit('/', 1)[-1],
            component=route.id,
        )

    def nuspec_spec(self, req: FormatRequest, upstream: Upstream, route: Route) -> AssetSpec:
        def check(file: Any) -> None:
            data = file.read(package.MAX_NUSPEC + 1)
            try:
                spec = package.parse_nuspec(data)
            except ValidationFailed as exc:
                raise DigestMismatch(f'upstream .nuspec refused: {exc.detail}') from None
            if spec.id.lower() != route.id or spec.flat_version != route.version:
                raise DigestMismatch('the upstream .nuspec is for another package')

        return AssetSpec(
            path=route.key,
            kind=AssetKind.CONTENT,
            upstream_url=f'{upstream.flat}{route.id}/{route.version}/{route.id}.nuspec',
            expected=CheckedExpected(check=check),
            content_type=protocol.NUSPEC_TYPE,
            max_size=package.MAX_NUSPEC,
            head_passthrough=False,
            artifact=route.key.rsplit('/', 1)[-1],
        )

    # ------------------------------------------------------------------ registrations and the catalog
    async def registration_doc(self, req: FormatRequest, route: Route) -> Any:
        """A cached registration document of this proxy (``route``: the index or a part), parsed."""
        upstream = await self.upstream(req)
        asset = await req.proxy.ensure(req, self.registration_spec(req, upstream, route))
        data = await req.proxy.read(asset, max_bytes=protocol.MAX_DOCUMENT)
        return await asyncio.to_thread(protocol.load, data, 'the cached registration')

    async def inlined_pages(self, req: FormatRequest, package_id: str, *, version: str | None = None) -> Any:
        """The registration index with its pages' leaves inlined (only the page of ``version`` when given): pages the
        upstream did not inline are fetched through this repository's own routes (bounded by ``MAX_PAGES``)."""
        doc = await self.registration_doc(req, Route(Op.REGISTRATION, package_id))
        pages = protocol.registration_pages(doc)
        fetched = 0
        for page in pages:
            if version is not None and not protocol.in_page(page, version):
                continue
            if isinstance(page.get('items'), list):
                continue
            part = self._part_route(req, page.get('@id'), package_id)
            if part is None or fetched >= MAX_PAGES:
                continue
            fetched += 1
            page_doc = await self.registration_doc(req, part)
            items = page_doc.get('items') if isinstance(page_doc, dict) else None
            page['items'] = items if isinstance(items, list) else []
        return doc

    @staticmethod
    def _part_route(req: FormatRequest, url: Any, package_id: str) -> Route | None:
        if not isinstance(url, str) or not url.startswith(req.repo_url):
            return None
        route = protocol.parse_route(url[len(req.repo_url) :].split('#', 1)[0])
        if route is None or route.op is not Op.REGISTRATION_PART or route.id != package_id:
            return None
        return route

    async def registration_leaf(self, req: FormatRequest, route: Route) -> dict[str, Any] | None:
        """The registration leaf of ``route``'s version (None when the upstream does not register it)."""
        try:
            doc = await self.inlined_pages(req, route.id, version=route.version)
        except NotFound:
            return None
        for page in protocol.registration_pages(doc):
            for leaf in page.get('items') or ():
                if protocol.leaf_version(leaf) == route.version:
                    return dict(leaf)
        return None

    async def catalog_hash(self, req: FormatRequest, upstream: Upstream, route: Route) -> protocol.CatalogHash | None:
        """The hash and size the upstream catalog publishes for the package (None: no catalog entry to check)."""
        leaf = await self.registration_leaf(req, route)
        entry = leaf.get('catalogEntry') if leaf else None
        url = entry.get('@id') if isinstance(entry, dict) else None
        if not isinstance(url, str) or not url.startswith(('https://', 'http://')):
            return None
        if (urlsplit(url).hostname or '').lower() not in upstream.hosts():
            log.info('catalog entry on another host: not used', extra={'repo': req.repo.name})
            return None

        def transform(data: bytes, info: UpstreamInfo) -> bytes:
            return protocol.dump(protocol.parse_catalog_leaf(data))

        spec = AssetSpec(
            path=protocol.catalog_key(route.id, route.version),
            kind=AssetKind.METADATA,
            upstream_url=url.split('#', 1)[0],
            upstream_headers={'Accept': protocol.JSON_TYPE},
            content_type=protocol.JSON_TYPE,
            transform=transform,
            max_size=CATALOG_MAX_BYTES,
            max_age_s=CATALOG_AGE_S,
            artifact=f'{route.id} {route.version} catalog entry',
        )
        try:
            asset = await req.proxy.ensure(req, spec)
        except NotFound:
            return None
        doc = protocol.load(await req.proxy.read(asset, max_bytes=INDEX_MAX_BYTES), 'the catalog entry')
        if (
            str(doc.get('id', '')).lower() != route.id
            or nuget.flat_version(str(doc.get('version', ''))) != route.version
        ):
            raise UpstreamError('the upstream catalog entry is for another package')
        return protocol.catalog_hash(doc)

    # ------------------------------------------------------------------ search
    async def search_doc(self, req: FormatRequest, query: SearchQuery) -> Any:
        """The upstream's search answer for ``query`` with URLs pointing at this repository (``None``: no search)."""
        upstream = await self.upstream(req)
        if upstream.search is None:
            return None
        response = await req.proxy.relay(
            req,
            upstream_path=f'{upstream.search}?{query.upstream_query()}',
            headers={'Accept': protocol.JSON_TYPE},
            max_size=SEARCH_MAX_BYTES,
        )
        if response.status_code != 200:
            raise UpstreamError(f'upstream search answered {response.status_code}')
        body = await asyncio.to_thread(protocol.rewrite_search, bytes(response.body), upstream, req.repo_url)
        return json.loads(body)

    async def search(self, req: FormatRequest, upstream: Upstream) -> Response:
        query = SearchQuery.from_params(req.request.query_params)
        doc = await self.search_doc(req, query)
        body = protocol.dump(doc if doc is not None else {'totalHits': 0, 'data': []})
        headers = {'Cache-Control': 'no-cache', 'X-Ws-Cache': 'BYPASS', **SAFE_HEADERS}
        return Response(b'' if req.is_head else body, media_type=protocol.JSON_TYPE, headers=headers)


def registration_summary(doc: Any) -> dict[str, Any]:
    """Browse fields from a registration index's newest inlined leaf ({} when no page is inlined); with every page
    inlined also the versions."""
    newest: dict[str, Any] | None = None
    newest_key = ''
    versions: list[str] = []
    complete = True
    for page in protocol.registration_pages(doc):
        if not isinstance(page.get('items'), list):
            complete = False
        for leaf in page.get('items') or ():
            version = protocol.leaf_version(leaf)
            entry = leaf.get('catalogEntry') if isinstance(leaf, dict) else None
            if version is None or not isinstance(entry, dict):
                continue
            versions.append(version)
            key = ('1' if not nuget.is_prerelease(version) else '0') + nuget.sort_string(version)
            if key > newest_key:
                newest, newest_key = entry, key
    if newest is None:
        return {}
    listing: dict[str, Any] = {}
    if complete:
        unique = sorted(set(versions), key=nuget.sort_string)
        listing = {'versions': unique[-SUMMARY_VERSIONS:], 'version_count': len(unique), 'latest': latest_of(unique)}
    authors = newest.get('authors')
    if isinstance(authors, list):
        authors = ', '.join(str(a) for a in authors)
    tags = newest.get('tags')
    out: dict[str, Any] = {
        'display_name': str(newest.get('id') or '')[: nuget.MAX_ID] or None,
        'description': str(newest.get('description') or '')[:1024] or None,
        'authors': str(authors)[:1024] if authors else None,
        'title': str(newest.get('title') or '')[:256] or None,
        'tags': [str(t)[:64] for t in tags[:50]] if isinstance(tags, list) else [],
    }
    return {**{k: v for k, v in out.items() if v is not None}, **listing}
