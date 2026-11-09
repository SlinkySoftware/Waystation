"""Helm classic chart repositories (T-P5-HELM; FORMATS.md "Helm"; SPECIFICATION §4.1-§4.3).

**Proxy**:

- ``GET /repository/<repo>/index.yaml``: the upstream ``index.yaml`` cached as metadata (revalidated after the
  repository's metadata max age), validated and rewritten in a thread (``formats.helm.index.rewrite_index``: chart
  URLs relative to this repository, served as JSON). When a new index is stored its chart versions are recorded in
  ``helm_charts`` in the background (``gen`` = the index blob's digest).
- ``GET <path>.tgz``: a chart listed by the cached index, cached as content and verified while it streams against the
  index's ``digest`` (sha256) when it has one. The listing is looked up only on a cache miss: a path the index does
  not list is refreshed once (``index.yaml`` fetched again), then 404, so ``_ext/<scheme>/<host>/...`` paths never
  reach a host the upstream index did not name. ``<path>.tgz.prov`` (provenance) passes through for listed charts.

**Hosted**: ``formats.helm.hosted``. **Group**: ``index.yaml`` is the members' indexes merged (the first member that
lists a chart version wins it); the merge is kept in the blob store and served by X-Accel until a member's index
changes. Charts and provenance files come from the first member that has them; uploads and deletes go to the
writable member (a hosted repository), which the principal must be allowed to write too.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from starlette.responses import Response

from waystation_common.errors import Forbidden, NotFound, UpstreamError, ValidationFailed, WaystationError
from waystation_common.events import CacheState
from waystation_server.db import helm as helm_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.common.background import Background
from waystation_server.formats.helm import index
from waystation_server.formats.helm.browse import HelmBrowse
from waystation_server.formats.helm.descriptor import DESCRIPTOR
from waystation_server.formats.helm.hosted import CHART_TYPE, PROV_TYPE, HelmHosted
from waystation_server.problems import native_error
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
from waystation_server.security.actions import Action

log = logging.getLogger('waystation.formats.helm')

INDEX_MAX_BYTES = 64 * 1024 * 1024
MERGES_KEPT = 256


@dataclass(frozen=True)
class _Merged:
    key: str
    sha256: str
    size: int
    at: datetime


def is_chart_file(path: str) -> bool:
    return path.endswith(('.tgz', '.tgz.prov'))


class HelmPlugin:
    descriptor = DESCRIPTOR

    def __init__(self) -> None:
        self.browser = HelmBrowse()
        self.hosted = HelmHosted()
        self._indexing = Background('helm-index')
        self._locks: dict[str, asyncio.Lock] = {}
        self._merged: OrderedDict[str, _Merged] = OrderedDict()

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        await self._indexing.drain()

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def handle(self, req: FormatRequest) -> Response:
        if req.repo.type is RepoType.GROUP:
            return await self.group(req)
        return await self.member(req)

    async def member(self, req: FormatRequest) -> Response:
        """A proxy or hosted repository's own answer (also what a group asks its members)."""
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.handle(req)
        if req.method not in ('GET', 'HEAD'):
            return native_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD'})
        if req.path == index.INDEX_PATH:
            req.log(op='index')
            return await req.proxy.serve(req, self.index_spec(req))
        if is_chart_file(req.path):
            req.log(op='chart')
            return await req.proxy.serve(req, self.chart_spec(req))
        raise NotFound('Not Found')

    # ------------------------------------------------------------------ proxy: index
    def index_spec(self, req: FormatRequest, *, max_age_s: int | None = None) -> AssetSpec:
        upstream = req.repo.proxy.upstream_url if req.repo.proxy else ''

        def rewrite(data: bytes, info: UpstreamInfo) -> bytes:
            try:
                return index.rewrite_index(data, upstream_url=upstream)
            except ValidationFailed as exc:
                raise UpstreamError(f'upstream index.yaml refused: {exc.detail}') from None

        return AssetSpec(
            path=index.INDEX_PATH,
            kind=AssetKind.METADATA,
            upstream_url=index.index_url(upstream),
            content_type=index.INDEX_TYPE,
            transform=rewrite,
            max_size=INDEX_MAX_BYTES,
            max_age_s=max_age_s,
            on_stored=self._on_index_stored(req.proxy, req.repo.id),
            artifact=index.INDEX_PATH,
        )

    def _on_index_stored(self, engine: ProxyEngine, repo_id: str) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            async def work() -> None:
                await self.ensure_listed(engine, repo_id, asset)

            self._indexing.spawn((repo_id, asset.blob_sha256), work)

        return stored

    async def ensure_listed(self, engine: ProxyEngine, repo_id: str, asset: StoredAsset) -> None:
        """Record the chart versions of the cached index ``asset`` in ``helm_charts`` unless already done."""
        if await helm_db.has_gen(engine.db, repo_id, asset.blob_sha256):
            return
        async with self._lock(f'list:{repo_id}'):
            if await helm_db.has_gen(engine.db, repo_id, asset.blob_sha256):
                return
            data = await engine.read(asset, max_bytes=INDEX_MAX_BYTES)
            listed = await asyncio.to_thread(index.listing, data)
            await helm_db.replace_listing(engine.db, repo_id, asset.blob_sha256, [item.record() for item in listed])

    # ------------------------------------------------------------------ proxy: charts
    def chart_spec(self, req: FormatRequest) -> AssetSpec:
        prov = req.path.endswith('.prov')
        chart_path = req.path.removesuffix('.prov')
        upstream = req.repo.proxy.upstream_url if req.repo.proxy else ''

        async def expect() -> Expected | None:
            record = await self.listed(req, chart_path)
            digest = record.get('digest')
            return Expected(sha256=digest) if digest and not prov else None

        return AssetSpec(
            path=req.path,
            kind=AssetKind.CONTENT,
            upstream_url=index.upstream_of(chart_path, upstream) + ('.prov' if prov else ''),
            expect=expect,
            content_type=PROV_TYPE if prov else CHART_TYPE,
            head_passthrough=False,  # a HEAD on a miss is checked against the index like a GET
            artifact=req.path.rsplit('/', 1)[-1],
        )

    async def listed(self, req: FormatRequest, chart_path: str) -> dict[str, Any]:
        """The cached index's entry for ``chart_path``; the index is refreshed once when it does not list it."""
        for max_age_s in (None, 0):
            asset = await req.proxy.ensure(req, self.index_spec(req, max_age_s=max_age_s))
            await self.ensure_listed(req.proxy, req.repo.id, asset)
            record = await helm_db.by_path(req.proxy.db, req.repo.id, chart_path)
            if record is not None:
                return record
        raise NotFound('Not Found')

    # ------------------------------------------------------------------ groups
    async def members(self, req: FormatRequest) -> list[RepoConfig]:
        """The group's online Helm members, in order (missing ones are skipped)."""
        group = req.repo.group
        out: list[RepoConfig] = []
        for name in group.members if group is not None else ():
            member = await req.repository(name)
            if member is None or member.format != 'helm' or member.type is RepoType.GROUP or not member.online:
                continue
            out.append(member)
        return out

    async def group(self, req: FormatRequest) -> Response:
        if req.method not in ('GET', 'HEAD'):
            return await self._group_write(req)
        members = await self.members(req)
        req.log(group=req.repo.name)
        if req.path == index.INDEX_PATH:
            return await self._group_index(req, members)
        if not is_chart_file(req.path):
            raise NotFound('Not Found')
        failure: WaystationError | Response | None = None
        for member in members:
            try:
                response = await self.member(dataclasses.replace(req, repo=member))
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
            response.headers['X-Ws-Group-Member'] = member.name
            return response
        if isinstance(failure, Response):
            return failure
        if failure is not None:
            raise failure
        raise NotFound('Not Found')

    async def _member_index(self, req: FormatRequest, member: RepoConfig) -> StoredAsset | None:
        sub = dataclasses.replace(req, repo=member)
        try:
            if member.type is RepoType.PROXY:
                return await req.proxy.ensure(sub, self.index_spec(sub))
            return await req.hosted.get(member, index.INDEX_PATH)
        except NotFound:
            return None
        except WaystationError as exc:  # an unreachable upstream without a cached index: left out of the merge
            log.warning('group member index unavailable', extra={'repo': member.name, 'error': exc.detail})
            return None

    async def _group_index(self, req: FormatRequest, members: list[RepoConfig]) -> Response:
        sources: list[tuple[RepoConfig, StoredAsset]] = []
        for member in members:
            asset = await self._member_index(req, member)
            if asset is not None:
                sources.append((member, asset))
        key = hashlib.sha256(
            '|'.join(f'{m.id}:{a.blob_sha256}' for m, a in sources).encode(), usedforsecurity=False
        ).hexdigest()
        async with self._lock(f'merge:{req.repo.id}'):
            merged = self._merged.get(req.repo.id)
            if merged is None or merged.key != key or not await req.hosted.blobs.exists(merged.sha256):
                merged = await self._merge(req, sources, key)
            self._merged[req.repo.id] = merged
            self._merged.move_to_end(req.repo.id)
            while len(self._merged) > MERGES_KEPT:
                self._merged.popitem(last=False)
        return req.hosted.serve_generated(
            req, merged.sha256, merged.size, content_type=index.INDEX_TYPE, last_modified=merged.at
        )

    async def _merge(self, req: FormatRequest, sources: list[tuple[RepoConfig, StoredAsset]], key: str) -> _Merged:
        started = time.perf_counter()
        bodies = [await req.hosted.read(asset, max_bytes=INDEX_MAX_BYTES) for _, asset in sources]
        now = datetime.now(UTC)
        body = await asyncio.to_thread(index.merge, bodies, now=now)
        async with await req.hosted.blobs.open_writer(expected_size=len(body)) as writer:
            await writer.write(body)
            ref = await req.hosted.blobs.commit(writer)
        req.log(merged_ms=round((time.perf_counter() - started) * 1000, 1), cache=CacheState.FETCHING.value)
        return _Merged(key=key, sha256=ref.sha256, size=ref.size, at=now)

    async def _group_write(self, req: FormatRequest) -> Response:
        group = req.repo.group
        name = group.writable_member if group is not None else None
        member = await req.repository(name) if name else None
        if member is None or member.type is not RepoType.HOSTED or member.format != 'helm':
            return native_error(
                405, 'This group has no writable member: upload to a hosted repository', headers={'Allow': 'GET, HEAD'}
            )
        action = Action.DELETE if req.method == 'DELETE' else Action.WRITE
        if not req.allows_member(action, member):
            raise Forbidden(f'{action.value} permission required on {member.name}')
        if not member.online:
            return native_error(503, 'The writable member is offline')
        req.log(group=req.repo.name)
        return await self.hosted.handle(dataclasses.replace(req, repo=member))
