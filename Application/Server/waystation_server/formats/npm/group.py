"""npm group repositories (T-P5-NPMH; SPECIFICATION §4.3; FORMATS.md "npm").

Members (npm proxy and hosted repositories, in order; offline, missing and nested group members are skipped):

- **Packuments**: the members' packuments in the requested representation (a proxy's cached one, fetched or
  revalidated as needed; a hosted repository's generated one) merged in a thread: versions union, the first member
  that has a version wins it; dist-tags from the first member that has the tag; every ``dist.tarball`` rewritten to
  the group. A member that does not know the package is skipped, one that fails (upstream down, nothing cached) too,
  unless no member answers. The merge is committed to the blob store and served by X-Accel (``ETag``/``304``) until
  a member's packument changes (an in-process pointer per group, package and representation, keyed by the
  members' packument digests). ``?write=true`` (what ``npm deprecate``/``unpublish`` read before writing) answers
  the writable member's own packument, so the changes they send back concern only it.
- **Tarballs**: asked of the members in order (hosted: stored; proxy: cached, else fetched when its packument lists
  it), so the member that won a version in the merge also serves its tarball.
- **dist-tags** (``GET``): from the merged packument. **Search**: the first proxy member's, else the hosted
  members'. **Audit**: the first proxy member that relays audits.
- **Writes** (publish, packument updates, unpublish, dist-tag changes) go to the ``writable_member`` (a hosted npm
  repository): the principal needs the action on the group (router) and on the member; without one the group is
  read-only (405).
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
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse, Response

from waystation_common.errors import Forbidden, NotFound, WaystationError
from waystation_common.events import CacheState
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.npm import metadata, publish
from waystation_server.formats.npm.hosted import npm_json, search_body, search_params
from waystation_server.formats.npm.metadata import Op, Route
from waystation_server.proxy.engine import StoredAsset
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.security.actions import Action

if TYPE_CHECKING:
    from waystation_server.formats.npm.plugin import NpmPlugin

log = logging.getLogger('waystation.formats.npm')

MERGES_KEPT = 512
RECLAIM_S = 3600.0  # well inside blob GC's grace period (24 h)
PACKUMENT_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class _Merged:
    key: str
    sha256: str
    size: int
    at: datetime
    claimed: float  # time.monotonic() of the last GC claim


def write_action(method: str, route: Route) -> Action:
    """The member action a group write needs (dist-tag removal only changes a tag: ``write``)."""
    return Action.DELETE if method == 'DELETE' and route.op is not Op.DIST_TAG else Action.WRITE


class NpmGroup:
    def __init__(self, plugin: NpmPlugin) -> None:
        self._plugin = plugin
        self._merged: OrderedDict[tuple[str, str, bool], _Merged] = OrderedDict()
        self._locks: dict[tuple[str, str, bool], asyncio.Lock] = {}

    async def members(self, req: FormatRequest) -> list[RepoConfig]:
        group = req.repo.group
        out: list[RepoConfig] = []
        for name in group.members if group is not None else ():
            member = await req.repository(name)
            if member is None or member.format != 'npm' or member.type is RepoType.GROUP or not member.online:
                continue
            out.append(member)
        return out

    @staticmethod
    def _sub(req: FormatRequest, member: RepoConfig) -> FormatRequest:
        return dataclasses.replace(req, repo=member)

    async def writable(self, req: FormatRequest) -> RepoConfig | None:
        group = req.repo.group
        name = group.writable_member if group is not None else None
        member = await req.repository(name) if name else None
        if member is None or member.type is not RepoType.HOSTED or member.format != 'npm':
            return None
        return member

    # ------------------------------------------------------------------ dispatch
    async def handle(self, req: FormatRequest, route: Route) -> Response:
        req.log(group=req.repo.name)
        if route.op is Op.AUDIT:
            return await self._audit(req)
        if req.method not in ('GET', 'HEAD'):
            return await self._write(req, route)
        members = await self.members(req)
        match route.op:
            case Op.PACKUMENT:
                if req.request.query_params.get('write') == 'true':
                    member = await self.writable(req)
                    if member is not None and member.online:
                        try:
                            return await self._plugin.hosted.serve_packument(
                                self._sub(req, member), member, route.package
                            )
                        except NotFound:
                            pass
                abbreviated = self._plugin.wants_abbreviated(req)
                return await self._packument(req, members, route.package, abbreviated=abbreviated)
            case Op.TARBALL:
                return await self._tarball(req, members, route)
            case Op.DIST_TAGS:
                return await self._dist_tags(req, members, route.package)
            case Op.SEARCH:
                return await self._search(req, members)
        return JSONResponse({'error': 'method not allowed'}, status_code=405, headers={'Allow': 'PUT, DELETE'})

    async def _write(self, req: FormatRequest, route: Route) -> Response:
        member = await self.writable(req)
        if member is None:
            return JSONResponse(
                {'error': 'this group has no writable member: publish to a hosted repository'},
                status_code=405,
                headers={'Allow': 'GET, HEAD'},
            )
        action = write_action(req.method, route)
        if not req.allows_member(action, member):
            raise Forbidden(f'{action.value} permission required on {member.name}')
        if not member.online:
            return JSONResponse({'error': 'the writable member is offline'}, status_code=503)
        response = await self._plugin.hosted.handle(self._sub(req, member), route)
        response.headers['X-Ws-Group-Member'] = member.name
        return response

    # ------------------------------------------------------------------ packuments
    async def _member_packument(
        self, req: FormatRequest, member: RepoConfig, package: str, *, abbreviated: bool
    ) -> StoredAsset | None:
        sub = self._sub(req, member)
        if member.type is RepoType.HOSTED:
            return await req.hosted.get(member, metadata.packument_key(package, abbreviated=abbreviated))
        try:
            return await req.proxy.ensure(sub, self._plugin.packument_spec(sub, package, abbreviated=abbreviated))
        except NotFound:
            return None

    async def merged(
        self, req: FormatRequest, members: list[RepoConfig], package: str, *, abbreviated: bool
    ) -> _Merged | None:
        """The group's merged packument in the blob store (None: no member has the package)."""
        sources: list[tuple[RepoConfig, StoredAsset]] = []
        failure: WaystationError | None = None
        for member in members:
            try:
                asset = await self._member_packument(req, member, package, abbreviated=abbreviated)
            except WaystationError as exc:  # upstream down and nothing cached: the others may still answer
                log.warning('group member packument unavailable', extra={'repo': member.name, 'error': exc.detail})
                failure = failure or exc
                continue
            if asset is not None:
                sources.append((member, asset))
        if not sources:
            if failure is not None:
                raise failure
            return None
        key = hashlib.sha256(
            '|'.join(f'{m.id}:{a.blob_sha256}' for m, a in sources).encode(), usedforsecurity=False
        ).hexdigest()
        slot = (req.repo.id, package, abbreviated)
        lock = self._locks.setdefault(slot, asyncio.Lock())
        async with lock:
            merged = self._merged.get(slot)
            if merged is None or merged.key != key or not await req.hosted.blobs.exists(merged.sha256):
                merged = await self._merge(req, sources, package, key, abbreviated=abbreviated)
            elif time.monotonic() - merged.claimed > RECLAIM_S and req.hosted.blobs.guard is not None:
                # no asset references the merge: keep claiming it so blob GC leaves it alone while it is served
                await req.hosted.blobs.guard.claim(merged.sha256, merged.size)
                merged = dataclasses.replace(merged, claimed=time.monotonic())
            self._merged[slot] = merged
            self._merged.move_to_end(slot)
            while len(self._merged) > MERGES_KEPT:
                old, _ = self._merged.popitem(last=False)
                self._locks.pop(old, None)
        return merged

    async def _merge(
        self,
        req: FormatRequest,
        sources: list[tuple[RepoConfig, StoredAsset]],
        package: str,
        key: str,
        *,
        abbreviated: bool,
    ) -> _Merged:
        started = time.perf_counter()
        bodies = [await req.hosted.read(asset, max_bytes=PACKUMENT_MAX_BYTES) for _, asset in sources]
        body = await asyncio.to_thread(publish.merge_packuments, bodies, package, req.repo_url, abbreviated=abbreviated)
        async with await req.hosted.blobs.open_writer(expected_size=len(body)) as writer:
            await writer.write(body)
            ref = await req.hosted.blobs.commit(writer)
        req.log(merged_ms=round((time.perf_counter() - started) * 1000, 1), cache=CacheState.FETCHING.value)
        return _Merged(key=key, sha256=ref.sha256, size=ref.size, at=datetime.now(UTC), claimed=time.monotonic())

    async def _packument(
        self, req: FormatRequest, members: list[RepoConfig], package: str, *, abbreviated: bool
    ) -> Response:
        started = time.perf_counter()
        merged = await self.merged(req, members, package, abbreviated=abbreviated)
        if merged is None:
            req.hosted.account(req, CacheState.HIT, 404, 0, started)
            raise NotFound('Not found')
        content_type = metadata.ABBREVIATED_TYPE if abbreviated else metadata.JSON_TYPE
        return req.hosted.serve_generated(
            req, merged.sha256, merged.size, content_type=content_type, last_modified=merged.at
        )

    async def _dist_tags(self, req: FormatRequest, members: list[RepoConfig], package: str) -> Response:
        merged = await self.merged(req, members, package, abbreviated=True)
        if merged is None:
            raise NotFound('Not found')
        data = await asyncio.to_thread(req.hosted.blobs.blob_path(merged.sha256).read_bytes)
        return npm_json(await asyncio.to_thread(publish.tags_of, data))

    # ------------------------------------------------------------------ tarballs, search, audit
    async def _tarball(self, req: FormatRequest, members: list[RepoConfig], route: Route) -> Response:
        path = metadata.tarball_path(route.package, route.filename)
        failure: WaystationError | None = None
        for member in members:
            sub = self._sub(req, member)
            try:
                if member.type is RepoType.HOSTED:
                    asset = await req.hosted.get(member, path)
                    if asset is None:
                        continue
                    response = req.hosted.serve(sub, asset)
                else:
                    response = await req.proxy.serve(sub, self._plugin.tarball_spec(sub, route))
            except NotFound:
                continue
            except WaystationError as exc:
                failure = failure or exc
                continue
            response.headers['X-Ws-Group-Member'] = member.name
            return response
        if failure is not None:
            raise failure
        raise NotFound('Not found')

    async def _search(self, req: FormatRequest, members: list[RepoConfig]) -> Response:
        for member in members:
            if member.type is RepoType.PROXY:
                return await self._plugin.search(self._sub(req, member))
        text, size, offset = search_params(req)
        found: dict[str, dict[str, Any]] = {}
        for member in members:
            for doc in await self._plugin.hosted.search_docs(req, member, text, limit=offset + size):
                found.setdefault(str(doc['name']), doc)
        docs = sorted(found.values(), key=lambda d: str(d['name']))
        return JSONResponse(search_body(docs[offset : offset + size]))

    async def _audit(self, req: FormatRequest) -> Response:
        if req.method != 'POST':
            return JSONResponse({'error': 'method not allowed'}, status_code=405, headers={'Allow': 'POST'})
        for member in await self.members(req):
            if member.type is RepoType.PROXY and self._plugin.audits(member):
                return await self._plugin.audit(self._sub(req, member))
        raise NotFound('audit is not available from this repository')
