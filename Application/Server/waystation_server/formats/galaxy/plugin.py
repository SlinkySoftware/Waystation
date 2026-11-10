"""Ansible Galaxy repositories (T-P5-GALAXY; FORMATS.md "Ansible Galaxy"; SPECIFICATION §4.1-§4.3).

Routes under ``/repository/<repo>/`` (trailing slashes optional):

- ``''`` and ``api/``: discovery (``available_versions``; ``v1`` only where roles are proxied). The repository URL
  itself is the server URL clients configure.
- ``api/v3/collections/<ns>/<name>/``, ``.../versions/`` (``limit``/``offset`` pagination, ``links`` pointing back
  here), ``.../versions/<version>/`` (``download_url`` into this repository, ``artifact.sha256``). Galaxy NG's
  ``api/v3/plugin/ansible/content/<distribution>/collections/index/...`` paths are accepted as aliases, and
  ``.../collections/artifacts/<file>`` for the artifacts.
- ``collections/<ns>/<name>/<ns>-<name>-<version>.tar.gz``: the artifact.
- Hosted: ``POST api/v3/artifacts/collections/`` (publish), ``GET api/v3/imports/collections/<task>/`` (the import
  task), ``DELETE`` of a version or a whole collection (``formats.galaxy.hosted``).
- Roles (proxy, legacy v1, read-only): ``api/v1/roles/?owner__username=&name=``, ``api/v1/roles/<id>/versions/``,
  ``roles/<id>/<version>.tar.gz`` (the archive the upstream names, cached unverified: v1 publishes no checksums).

**Proxy**: the upstream's collection document, version list pages (``limit=100``, every page, so the whole list is
known) and version documents are METADATA (revalidated after the repository's metadata max age), validated, read
per request and rendered with this repository's URLs; artifacts are CONTENT fetched from the version document's
``download_url`` and verified against its sha256 and size while they stream. Upstream paths: the repository's
``collections_path`` (Galaxy NG layout by default) under the upstream root. Version lists and details are recorded in
``galaxy_collections`` in the background (browse).

**Group**: version lists merged (first member wins a version), collection documents computed from the merge,
version documents and artifacts from the first member that has the version, roles from the first proxy member that
answers. Publishing goes to the writable member.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

from starlette.responses import Response

from waystation_common import galaxy
from waystation_common.db import galaxy as galaxy_common_db
from waystation_common.errors import Forbidden, NotFound, UpstreamError, ValidationFailed, WaystationError
from waystation_common.events import CacheState
from waystation_server.db import galaxy as galaxy_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.common.background import Background
from waystation_server.formats.galaxy import api
from waystation_server.formats.galaxy.browse import GalaxyBrowse
from waystation_server.formats.galaxy.descriptor import DESCRIPTOR, GalaxyOptions, upstream_root
from waystation_server.formats.galaxy.hosted import GalaxyHosted, detail_of, info_of, version_of
from waystation_server.problems import native_error
from waystation_server.proxy.engine import AssetKind, AssetSpec, Expected, StoredAsset, UpstreamInfo
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.security.actions import Action

log = logging.getLogger('waystation.formats.galaxy')

PARSED_KEPT = 512
MAX_PAGES = api.MAX_VERSIONS // api.UPSTREAM_PAGE
MAX_ROLE_PAGES = 20
PAGE_SKEW = timedelta(seconds=60)
ARTIFACT_TYPE = 'application/gzip'
SAFE_HEADERS = {'X-Content-Type-Options': 'nosniff'}


@dataclass(frozen=True)
class Route:
    kind: str  # discovery | collection | versions | version | artifact | publish | task | role_search |
    # role_versions | role_archive
    namespace: str = ''
    name: str = ''
    version: str = ''
    ident: str = ''  # task id, role id
    prefix: str = ''  # discovery: the v3/v1 prefix relative to the request URL


def _collection_route(rest: list[str]) -> Route | None:
    if len(rest) < 2 or not galaxy.valid_name(rest[0], strict=False) or not galaxy.valid_name(rest[1], strict=False):
        return None
    ns, name = rest[0], rest[1]
    if len(rest) == 2:
        return Route('collection', ns, name)
    if rest[2] != 'versions':
        return None
    if len(rest) == 3:
        return Route('versions', ns, name)
    if len(rest) == 4 and galaxy.valid_version(rest[3]):
        return Route('version', ns, name, rest[3])
    return None


def _artifact_route(filename: str) -> Route | None:
    found = galaxy.parse_filename(filename, strict=False)
    return Route('artifact', found.namespace, found.name, found.version) if found is not None else None


def parse_route(path: str) -> Route | None:
    seg = [s for s in path.split('/') if s]
    if not seg:
        return Route('discovery', prefix='api/')
    if seg == ['api']:
        return Route('discovery')
    if seg[0] == 'collections' and len(seg) == 4:
        route = _artifact_route(seg[3])
        return route if route is not None and (route.namespace, route.name) == (seg[1], seg[2]) else None
    if seg[0] == 'roles' and len(seg) == 3 and api.ROLE_ID.match(seg[1]) and seg[2].endswith('.tar.gz'):
        version = seg[2].removesuffix('.tar.gz')
        return Route('role_archive', version=version, ident=seg[1]) if api.ROLE_VERSION.match(version) else None
    if seg[0] != 'api' or len(seg) < 3:
        return None
    if seg[1] == 'v1' and seg[2] == 'roles':
        if len(seg) == 3:
            return Route('role_search')
        if len(seg) == 5 and api.ROLE_ID.match(seg[3]) and seg[4] == 'versions':
            return Route('role_versions', ident=seg[3])
        return None
    if seg[1] != 'v3':
        return None
    rest = seg[2:]
    if rest[0] == 'collections':
        return _collection_route(rest[1:])
    if rest == ['artifacts', 'collections']:
        return Route('publish')
    if len(rest) == 3 and rest[:2] == ['imports', 'collections'] and len(rest[2]) <= 64:
        return Route('task', ident=rest[2])
    # Galaxy NG: api/v3/plugin/ansible/content/<distribution>/collections/{index,artifacts}/...
    if len(rest) >= 6 and rest[:3] == ['plugin', 'ansible', 'content'] and rest[4] == 'collections':
        if rest[5] == 'index':
            return _collection_route(rest[6:])
        if rest[5] == 'artifacts' and len(rest) == 7:
            return _artifact_route(rest[6])
        if rest[5] == 'artifacts' and len(rest) == 6:
            return Route('publish')
    return None


def _options(repo: RepoConfig) -> GalaxyOptions:
    options = DESCRIPTOR.parse_options(repo.format_options)
    return options if isinstance(options, GalaxyOptions) else GalaxyOptions()


def body_etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


class GalaxyPlugin:
    descriptor = DESCRIPTOR

    def __init__(self) -> None:
        self.browser = GalaxyBrowse()
        self.hosted = GalaxyHosted()
        self._indexing = Background('galaxy-listing')
        self._parsed: OrderedDict[tuple[str, str], Any] = OrderedDict()

    async def drain(self) -> None:
        """Wait for background listing work (tests, shutdown)."""
        await self._indexing.drain()

    # ------------------------------------------------------------------ dispatch
    async def handle(self, req: FormatRequest) -> Response:
        route = parse_route(req.path)
        if route is None:
            raise NotFound('Not Found')
        if req.method not in ('GET', 'HEAD'):
            return await self._write(req, route)
        req.log(op=route.kind)
        if req.repo.type is RepoType.GROUP:
            req.log(group=req.repo.name)
        urls = api.Urls(req.repo_url)
        started = time.perf_counter()
        if route.kind == 'discovery':
            doc = api.discovery(prefix=route.prefix, roles=await self._has_roles(req))
            return self._json(req, doc, started)
        if route.kind == 'collection':
            return self._json(req, await self.collection_doc(req, urls, route), started)
        if route.kind == 'versions':
            versions = await self.versions_of(req, route.namespace, route.name)
            if versions is None:
                raise NotFound('Not Found')
            limit, offset = api.page_params(req.request.query_params)
            doc = api.render_versions(urls, route.namespace, route.name, versions, limit=limit, offset=offset)
            return self._json(req, doc, started)
        if route.kind == 'version':
            detail = await self.detail_of(req, route.namespace, route.name, route.version)
            if detail is None:
                raise NotFound('Not Found')
            return self._json(req, api.render_detail(urls, detail), started)
        if route.kind == 'artifact':
            return await self.artifact(req, galaxy.Artifact(route.namespace, route.name, route.version))
        if route.kind == 'task':
            return await self._task(req, urls, route, started)
        if route.kind in ('role_search', 'role_versions', 'role_archive'):
            return await self._roles(req, urls, route, started)
        return native_error(405, 'Method Not Allowed', headers={'Allow': 'POST'})

    def _json(self, req: FormatRequest, doc: dict[str, Any], started: float, status: int = 200) -> Response:
        """A document built per request: ``ETag``, conditional ``304``, ``HEAD``."""
        body = api.dump(doc)
        etag = body_etag(body)
        headers = {'ETag': etag, 'Cache-Control': 'no-cache', **SAFE_HEADERS}
        match = req.request.headers.get('if-none-match')
        if status == 200 and match and etag in {m.strip() for m in match.split(',')}:
            req.hosted.account(req, CacheState.HIT, 304, 0, started)
            return Response(status_code=304, headers=headers)
        sent = 0 if req.is_head else len(body)
        req.hosted.account(req, CacheState.HIT, status, sent, started)
        if req.is_head:
            headers['Content-Length'] = str(len(body))
            return Response(status_code=status, headers=headers, media_type=api.JSON_TYPE)
        return Response(body, status_code=status, headers=headers, media_type=api.JSON_TYPE)

    async def members(self, req: FormatRequest) -> list[RepoConfig]:
        """The group's online Galaxy members, in order (missing ones are skipped)."""
        group = req.repo.group
        out: list[RepoConfig] = []
        for name in group.members if group is not None else ():
            member = await req.repository(name)
            if member is None or member.format != 'galaxy' or member.type is RepoType.GROUP or not member.online:
                continue
            out.append(member)
        return out

    async def _sources(self, req: FormatRequest) -> list[FormatRequest]:
        if req.repo.type is not RepoType.GROUP:
            return [req]
        return [dataclasses.replace(req, repo=m) for m in await self.members(req)]

    async def _has_roles(self, req: FormatRequest) -> bool:
        return any(sub.repo.type is RepoType.PROXY and _options(sub.repo).roles for sub in await self._sources(req))

    # ------------------------------------------------------------------ collections: one source
    def _parsed_get(self, kind: str, asset: StoredAsset) -> Any:
        key = (kind, asset.blob_sha256)
        found = self._parsed.get(key)
        if found is not None:
            self._parsed.move_to_end(key)
        return found

    def _parsed_put(self, kind: str, asset: StoredAsset, value: Any) -> None:
        self._parsed[(kind, asset.blob_sha256)] = value
        while len(self._parsed) > PARSED_KEPT:
            self._parsed.popitem(last=False)

    @staticmethod
    def _upstream(req: FormatRequest) -> str:
        if req.repo.proxy is None:
            raise NotFound('Not Found')
        return upstream_root(req.repo.proxy.upstream_url) + _options(req.repo).collections_path

    def _collection_spec(self, req: FormatRequest, ns: str, name: str) -> AssetSpec:
        def validate(data: bytes, info: UpstreamInfo) -> None:
            try:
                api.check_collection(data, ns, name)
            except ValidationFailed as exc:
                raise UpstreamError(f'upstream collection document refused: {exc.detail}') from None

        return AssetSpec(
            path=f'_meta/{ns}/{name}/collection.json',
            kind=AssetKind.METADATA,
            upstream_url=f'{self._upstream(req)}{ns}/{name}/',
            upstream_headers={'Accept': api.JSON_TYPE},
            content_type=api.JSON_TYPE,
            validate=validate,
            max_size=api.MAX_DOC,
            artifact=f'{ns}.{name}',
            component=f'{ns}.{name}',
        )

    def _page_spec(self, req: FormatRequest, ns: str, name: str, offset: int, max_age_s: int | None) -> AssetSpec:
        def validate(data: bytes, info: UpstreamInfo) -> None:
            try:
                api.parse_versions_page(data)
            except ValidationFailed as exc:
                raise UpstreamError(f'upstream version list refused: {exc.detail}') from None

        query = urlencode({'limit': api.UPSTREAM_PAGE, 'offset': offset})
        return AssetSpec(
            path=f'_meta/{ns}/{name}/versions-{offset}.json',
            kind=AssetKind.METADATA,
            upstream_url=f'{self._upstream(req)}{ns}/{name}/versions/?{query}',
            upstream_headers={'Accept': api.JSON_TYPE},
            content_type=api.JSON_TYPE,
            validate=validate,
            max_size=api.MAX_DOC,
            max_age_s=max_age_s,
            artifact=f'{ns}.{name} versions',
            component=f'{ns}.{name}',
        )

    def _detail_spec(self, req: FormatRequest, ns: str, name: str, version: str) -> AssetSpec:
        def transform(data: bytes, info: UpstreamInfo) -> bytes:
            try:
                api.parse_detail(data, ns, name, version)
                return api.slim_detail(data, info)
            except ValidationFailed as exc:
                raise UpstreamError(f'upstream version document refused: {exc.detail}') from None

        engine, repo_id = req.proxy, req.repo.id

        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            async def record() -> None:
                detail = api.parse_detail(await engine.read(asset, max_bytes=api.MAX_DOC), ns, name, version)
                fields = {
                    'sha256': detail.sha256,
                    'size': detail.size,
                    'description': detail.metadata.get('description'),
                }
                await galaxy_db.update_details(engine.db, repo_id, ns, name, version, fields)

            self._indexing.spawn(('detail', repo_id, asset.blob_sha256), record)

        return AssetSpec(
            path=f'_meta/{ns}/{name}/{version}.json',
            kind=AssetKind.METADATA,
            upstream_url=f'{self._upstream(req)}{ns}/{name}/versions/{quote(version, safe="+")}/',
            upstream_headers={'Accept': api.JSON_TYPE},
            content_type=api.JSON_TYPE,
            transform=transform,
            on_stored=stored,
            max_size=api.MAX_DOC,
            artifact=f'{ns}.{name} {version}',
            component=f'{ns}.{name}',
        )

    async def _read_json(self, req: FormatRequest, asset: StoredAsset) -> bytes:
        return await req.proxy.read(asset, max_bytes=api.MAX_DOC)

    async def source_info(self, req: FormatRequest, ns: str, name: str) -> tuple[api.Info, str | None] | None:
        """A member's collection fields and highest version; None when it does not have the collection."""
        if req.repo.type is RepoType.HOSTED:
            docs = await galaxy_common_db.collection_versions(req.hosted.db, [req.repo.id], ns, name)
            if not docs:
                return None
            versions = [version_of(d) for d in docs]
            top = api.highest(versions)
            return info_of(docs), top.version if top is not None else None
        try:
            asset = await req.proxy.ensure(req, self._collection_spec(req, ns, name))
        except NotFound:
            return None
        doc = api.load(await self._read_json(req, asset), 'the collection document')
        top = doc.get('highest_version')
        version = top.get('version') if isinstance(top, dict) else None
        return api.parse_info(doc), version if isinstance(version, str) else None

    async def source_versions(self, req: FormatRequest, ns: str, name: str) -> list[api.Version] | None:
        """A member's versions of a collection, newest first; None when it does not have the collection."""
        if req.repo.type is RepoType.HOSTED:
            docs = await galaxy_common_db.collection_versions(req.hosted.db, [req.repo.id], ns, name)
            return [version_of(d) for d in docs] or None
        try:
            first = await req.proxy.ensure(req, self._page_spec(req, ns, name, 0, None))
        except NotFound:
            return None
        assets = [first]
        page = await self._page(req, first)
        versions = list(page.versions)
        offset = api.UPSTREAM_PAGE
        while page.has_next and offset < page.count and len(assets) < MAX_PAGES:
            asset = await req.proxy.ensure(req, self._page_spec(req, ns, name, offset, None))
            if asset.upstream.fetched_at < first.upstream.fetched_at - PAGE_SKEW:
                # an older page of a list that changed since: fetch it again so the pages line up
                asset = await req.proxy.ensure(req, self._page_spec(req, ns, name, offset, 0))
            assets.append(asset)
            page = await self._page(req, asset)
            versions.extend(page.versions)
            offset += api.UPSTREAM_PAGE
        merged = api.merge([versions])
        self._record_listing(req, ns, name, assets, merged)
        return merged

    async def _page(self, req: FormatRequest, asset: StoredAsset) -> api.VersionsPage:
        page = self._parsed_get('page', asset)
        if page is None:
            page = await asyncio.to_thread(api.parse_versions_page, await self._read_json(req, asset))
            self._parsed_put('page', asset, page)
        return page  # type: ignore[no-any-return]

    def _record_listing(
        self, req: FormatRequest, ns: str, name: str, assets: list[StoredAsset], versions: list[api.Version]
    ) -> None:
        gen = hashlib.sha256('|'.join(a.blob_sha256 for a in assets).encode(), usedforsecurity=False).hexdigest()
        db, repo_id = req.proxy.db, req.repo.id

        async def work() -> None:
            if await galaxy_db.has_gen(db, repo_id, ns, name, gen):
                return
            records = [
                {
                    'version': v.version,
                    'version_sort': v.sort,
                    'path': galaxy.artifact_path(ns, name, v.version),
                    'created': api.parse_time(v.created_at),
                    'requires_ansible': v.requires_ansible,
                }
                for v in versions
            ]
            await galaxy_db.replace_listing(db, repo_id, ns, name, gen, records)

        self._indexing.spawn(('list', repo_id, ns, name, gen), work)

    async def source_detail(self, req: FormatRequest, ns: str, name: str, version: str) -> api.Detail | None:
        if req.repo.type is RepoType.HOSTED:
            doc = await galaxy_common_db.get_version(req.hosted.db, req.repo.id, ns, name, version)
            return detail_of(doc) if doc is not None else None
        try:
            asset = await req.proxy.ensure(req, self._detail_spec(req, ns, name, version))
        except NotFound:
            return None
        detail = self._parsed_get('detail', asset)
        if detail is None:
            data = await self._read_json(req, asset)
            try:
                detail = await asyncio.to_thread(api.parse_detail, data, ns, name, version)
            except ValidationFailed as exc:
                raise UpstreamError(f'cached version document unusable: {exc.detail}') from None
            self._parsed_put('detail', asset, detail)
        return detail  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ collections: the addressed repository
    async def collection_doc(self, req: FormatRequest, urls: api.Urls, route: Route) -> dict[str, Any]:
        ns, name = route.namespace, route.name
        if req.repo.type is not RepoType.GROUP:
            found = await self.source_info(req, ns, name)
            if found is None:
                raise NotFound('Not Found')
            info, top = found
            return api.render_collection(urls, ns, name, info, top)
        infos: list[api.Info] = []
        lists: list[list[api.Version]] = []
        for sub in await self._sources(req):
            found_info = await self._skip_failure(sub, self.source_info(sub, ns, name))
            if found_info is None:
                continue
            infos.append(found_info[0])
            lists.append(await self._skip_failure(sub, self.source_versions(sub, ns, name)) or [])
        if not infos:
            raise NotFound('Not Found')
        merged = api.merge(lists)
        best = api.highest(merged)
        created = [i.created_at for i in infos if i.created_at]
        updated = [i.updated_at for i in infos if i.updated_at]
        info = api.Info(
            deprecated=infos[0].deprecated,
            created_at=min(created) if created else None,
            updated_at=max(updated) if updated else None,
        )
        return api.render_collection(urls, ns, name, info, best.version if best is not None else None)

    async def _skip_failure[T](self, sub: FormatRequest, work: Awaitable[T | None]) -> T | None:
        """A group member's answer; an unreachable member without a cached copy is left out (logged)."""
        try:
            result = await work
        except WaystationError as exc:
            log.warning('group member unavailable', extra={'repo': sub.repo.name, 'error': exc.detail})
            return None
        return result

    async def versions_of(self, req: FormatRequest, ns: str, name: str) -> list[api.Version] | None:
        if req.repo.type is not RepoType.GROUP:
            return await self.source_versions(req, ns, name)
        lists: list[list[api.Version]] = []
        for sub in await self._sources(req):
            found = await self._skip_failure(sub, self.source_versions(sub, ns, name))
            if found is not None:
                lists.append(found)
        return api.merge(lists) if lists else None

    async def detail_of(self, req: FormatRequest, ns: str, name: str, version: str) -> api.Detail | None:
        if req.repo.type is not RepoType.GROUP:
            return await self.source_detail(req, ns, name, version)
        failure: WaystationError | None = None
        for sub in await self._sources(req):
            try:
                found = await self.source_detail(sub, ns, name, version)
            except WaystationError as exc:
                failure = failure or exc
                continue
            if found is not None:
                return found
        if failure is not None:
            raise failure
        return None

    # ------------------------------------------------------------------ artifacts
    async def artifact(self, req: FormatRequest, artifact: galaxy.Artifact) -> Response:
        if req.repo.type is RepoType.GROUP:
            return await self._first_member(req, lambda sub: self.artifact(sub, artifact))
        req.log(op='artifact')
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.serve_artifact(req, artifact)
        return await req.proxy.serve(req, await self._artifact_spec(req, artifact))

    async def _artifact_spec(self, req: FormatRequest, artifact: galaxy.Artifact) -> AssetSpec:
        path = artifact.path
        ns, name, version = artifact.namespace, artifact.name, artifact.version

        async def expect() -> Expected | None:
            detail = await self.source_detail(req, ns, name, version)
            if detail is None:
                raise NotFound('Not Found')
            return Expected(sha256=detail.sha256, size=detail.size)

        cached = await req.hosted.get(req.repo, path)
        if cached is not None and cached.upstream.url:
            url = cached.upstream.url  # cached: no version lookup unless the engine has to fetch again
            return AssetSpec(
                path=path,
                kind=AssetKind.CONTENT,
                upstream_url=url,
                expect=expect,
                content_type=ARTIFACT_TYPE,
                artifact=artifact.filename,
                component=artifact.fqcn,
            )
        detail = await self.source_detail(req, ns, name, version)
        if detail is None:
            raise NotFound('Not Found')
        return AssetSpec(
            path=path,
            kind=AssetKind.CONTENT,
            upstream_url=detail.download_url,
            expected=Expected(sha256=detail.sha256, size=detail.size),
            content_type=ARTIFACT_TYPE,
            head_passthrough=False,
            artifact=artifact.filename,
            component=artifact.fqcn,
        )

    async def _first_member(self, req: FormatRequest, serve: Callable[[FormatRequest], Any]) -> Response:
        failure: WaystationError | Response | None = None
        for sub in await self._sources(req):
            try:
                response: Response = await serve(sub)
            except NotFound:
                continue
            except WaystationError as exc:
                failure = failure or exc
                continue
            if response.status_code == 404:
                continue
            if response.status_code >= 500:
                failure = failure or response
                continue
            response.headers['X-Ws-Group-Member'] = sub.repo.name
            return response
        if isinstance(failure, Response):
            return failure
        if failure is not None:
            raise failure
        raise NotFound('Not Found')

    # ------------------------------------------------------------------ roles (legacy v1, proxy only)
    def _role_root(self, req: FormatRequest) -> str:
        if req.repo.type is not RepoType.PROXY or req.repo.proxy is None or not _options(req.repo).roles:
            raise NotFound('Not Found')
        return upstream_root(req.repo.proxy.upstream_url)

    def _role_search_spec(self, req: FormatRequest, owner: str, name: str) -> AssetSpec:
        def validate(data: bytes, info: UpstreamInfo) -> None:
            try:
                api.role_search_doc(data)
            except ValidationFailed as exc:
                raise UpstreamError(f'upstream role search refused: {exc.detail}') from None

        query = urlencode({'owner__username': owner, 'name': name})
        return AssetSpec(
            path=f'_roles/search/{owner}/{name}.json',
            kind=AssetKind.METADATA,
            upstream_url=f'{self._role_root(req)}api/v1/roles/?{query}',
            upstream_headers={'Accept': api.JSON_TYPE},
            content_type=api.JSON_TYPE,
            validate=validate,
            max_size=api.MAX_DOC,
            artifact=f'{owner}.{name}',
        )

    def _role_versions_spec(self, req: FormatRequest, role_id: str, page: int, url: str) -> AssetSpec:
        def validate(data: bytes, info: UpstreamInfo) -> None:
            try:
                api.parse_role_versions(data)
            except ValidationFailed as exc:
                raise UpstreamError(f'upstream role versions refused: {exc.detail}') from None

        return AssetSpec(
            path=f'_roles/{role_id}/versions-{page}.json',
            kind=AssetKind.METADATA,
            upstream_url=url,
            upstream_headers={'Accept': api.JSON_TYPE},
            content_type=api.JSON_TYPE,
            validate=validate,
            max_size=api.MAX_DOC,
            artifact=f'role {role_id} versions',
        )

    async def role_versions(self, req: FormatRequest, role_id: str) -> list[dict[str, Any]]:
        root = self._role_root(req)
        url = f'{root}api/v1/roles/{role_id}/versions/?page_size=50'
        out: list[dict[str, Any]] = []
        for page in range(MAX_ROLE_PAGES):
            asset = await req.proxy.ensure(req, self._role_versions_spec(req, role_id, page, url))
            parsed = self._parsed_get('role_versions', asset)
            if parsed is None:
                parsed = await asyncio.to_thread(api.parse_role_versions, await self._read_json(req, asset))
                self._parsed_put('role_versions', asset, parsed)
            out.extend(parsed.results)
            link = parsed.next_link
            # next pages only on the upstream host, under this role's versions
            if not link or not link.lstrip('/').startswith(f'api/v1/roles/{role_id}/versions/'):
                break
            url = root + link.lstrip('/')
        return out

    async def _role_source(self, req: FormatRequest, route: Route, urls: api.Urls) -> Response | dict[str, Any]:
        if route.kind == 'role_search':
            query = req.request.query_params
            owner, name = query.get('owner__username', ''), query.get('name', '')
            if not api.ROLE_PART.match(owner) or not api.ROLE_PART.match(name):
                raise NotFound('Not Found')
            asset = await req.proxy.ensure(req, self._role_search_spec(req, owner, name))
            doc = await asyncio.to_thread(api.render_role_search, await self._read_json(req, asset))
            if not doc['results']:
                raise NotFound('Not Found')  # a group asks the next member
            return doc
        if route.kind == 'role_versions':
            versions = await self.role_versions(req, route.ident)
            return api.render_role_versions(urls, route.ident, versions)
        url = api.role_download(await self.role_versions(req, route.ident), route.version)
        if url is None:
            raise NotFound('Not Found')
        return await req.proxy.serve(
            req,
            AssetSpec(
                path=f'roles/{route.ident}/{route.version}.tar.gz',
                kind=AssetKind.CONTENT,
                upstream_url=url,
                content_type=ARTIFACT_TYPE,
                head_passthrough=False,
                artifact=f'role {route.ident} {route.version}.tar.gz',
            ),
        )

    async def _roles(self, req: FormatRequest, urls: api.Urls, route: Route, started: float) -> Response:
        failure: WaystationError | None = None
        for sub in await self._sources(req):
            if sub.repo.type is not RepoType.PROXY or not _options(sub.repo).roles:
                continue
            try:
                found = await self._role_source(sub, route, urls)
            except NotFound:
                continue
            except WaystationError as exc:
                failure = failure or exc
                continue
            if isinstance(found, Response):
                if req.repo.type is RepoType.GROUP:
                    found.headers['X-Ws-Group-Member'] = sub.repo.name
                return found
            return self._json(req, found, started)
        if failure is not None:
            raise failure
        if route.kind == 'role_search':  # nobody has it: an empty search, as Galaxy answers
            return self._json(req, api.render_role_search(b'{"results":[]}'), started)
        raise NotFound('Not Found')

    # ------------------------------------------------------------------ hosted: tasks and writes
    async def _task(self, req: FormatRequest, urls: api.Urls, route: Route, started: float) -> Response:
        repos = [sub.repo for sub in await self._sources(req) if sub.repo.type is RepoType.HOSTED]
        task = await galaxy_common_db.get_import(req.hosted.db, route.ident)
        if task is None or task.get('repo_id') not in {r.id for r in repos}:
            raise NotFound('Not Found')
        return self._json(req, api.render_task(urls, task), started)

    async def _write(self, req: FormatRequest, route: Route) -> Response:
        if route.kind == 'publish' and req.method == 'POST':
            target = await self._writable(req, Action.WRITE)
            return await self.hosted.publish(target, api.Urls(req.repo_url))
        if route.kind in ('collection', 'version') and req.method == 'DELETE':
            target = await self._writable(req, Action.DELETE)
            return await self.hosted.delete(target, route.namespace, route.name, route.version or None)
        allow = (
            'POST'
            if route.kind == 'publish'
            else 'GET, HEAD, DELETE'
            if route.kind in ('collection', 'version')
            else 'GET, HEAD'
        )
        return native_error(405, 'Method Not Allowed', headers={'Allow': allow})

    async def _writable(self, req: FormatRequest, action: Action) -> FormatRequest:
        """The hosted repository a write goes to: this one, or a group's writable member (RBAC on the member)."""
        if req.repo.type is RepoType.HOSTED:
            return req
        if req.repo.type is RepoType.PROXY:
            raise Forbidden('a proxy repository is read-only: publish to a hosted repository')
        group = req.repo.group
        name = group.writable_member if group is not None else None
        member = await req.repository(name) if name else None
        if member is None or member.type is not RepoType.HOSTED or member.format != 'galaxy':
            raise Forbidden('this group has no writable member: publish to a hosted repository')
        if not req.allows_member(action, member):
            raise Forbidden(f'{action.value} permission required on {member.name}')
        if not member.online:
            raise UpstreamError('the writable member is offline')
        req.log(group=req.repo.name)
        return dataclasses.replace(req, repo=member)
