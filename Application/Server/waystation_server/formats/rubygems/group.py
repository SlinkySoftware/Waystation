"""RubyGems group repositories (T-P6-GEMS; SPECIFICATION §4.3; FORMATS.md "RubyGems").

Members (RubyGems proxy and hosted repositories, in order; offline or missing ones are skipped) answer through the
group:

- ``versions`` and ``names``: merged from the members' (a proxy's cached upstream file, a hosted repository's
  rendered one) whenever one of them changes, kept in the blob store and served by X-Accel (``Range`` and
  ``Repr-Digest`` as for a proxy). A gem only one member has keeps that member's versions and info MD5; a gem
  several members have gets the union of their versions and the MD5 of the merged info file (below).
- ``info/<gem>``: the one member that has the gem answers as it does; when several have it, their lines are merged
  (the first member that has a version and platform wins it) and rendered per request.
- ``.gem`` files and gemspecs: the first member that stores or caches the file (X-Accel), else the proxy members in
  order (each checks the file against its own info file).
- The legacy ``specs.4.8.gz`` indexes list the hosted members' gems (a proxy's are opaque Ruby Marshal and are not
  merged); pushes go to a hosted repository directly.

A member that fails (upstream down, nothing cached) is skipped unless no member answers; then its error is returned.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from starlette.responses import Response

from waystation_common.errors import NotFound, WaystationError
from waystation_common.events import CacheState
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.rubygems import marshal, protocol
from waystation_server.formats.rubygems.common import SAFE_HEADERS, add_digest, generated
from waystation_server.formats.rubygems.hosted import tuples, versions_of
from waystation_server.formats.rubygems.protocol import GemVersions, InfoLine, Op, Route
from waystation_server.proxy.engine import StoredAsset
from waystation_server.repos.model import RepoConfig, RepoType

if TYPE_CHECKING:
    from waystation_server.formats.rubygems.plugin import RubygemsPlugin

MERGES_KEPT = 16
RECLAIM_S = 3600.0  # well inside blob GC's grace period (24 h)
MAX_SHARED = 500  # gems several members have whose info MD5 is computed (beyond: an MD5 that makes Bundler re-ask)


@dataclass(frozen=True)
class _Blob:
    sha256: str
    size: int


@dataclass(frozen=True)
class _Merged:
    key: str
    versions: _Blob
    names: _Blob
    at: datetime
    claimed: float  # time.monotonic() of the last GC claim


@dataclass
class _Source:
    member: RepoConfig
    token: str
    asset: StoredAsset | None = None  # proxy: the cached versions file
    docs: list[dict[str, Any]] | None = None  # hosted: the gem assets


def _merge_versions(maps: list[dict[str, GemVersions]]) -> tuple[dict[str, GemVersions], list[str]]:
    """Members' versions merged (blocking): the union per gem in member order, and the gems several members have."""
    out: dict[str, GemVersions] = {}
    shared: list[str] = []
    for found in maps:
        for name, entry in found.items():
            if not entry.versions:
                continue
            current = out.get(name)
            if current is None:
                out[name] = GemVersions(list(entry.versions), entry.md5)
                continue
            if current.md5 != '?':
                shared.append(name)
                current.md5 = '?'
            current.versions += [v for v in entry.versions if v not in current.versions]
    return out, shared


def merged_info(members: list[list[InfoLine]]) -> bytes:
    return protocol.render_info(line.raw for line in protocol.merge_info(members))


class RubygemsGroup:
    def __init__(self, plugin: RubygemsPlugin) -> None:
        self._plugin = plugin
        self._merged: OrderedDict[str, _Merged] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    async def members(self, req: FormatRequest) -> list[RepoConfig]:
        """The group's online RubyGems members, in order."""
        group = req.repo.group
        out: list[RepoConfig] = []
        for name in group.members if group is not None else ():
            member = await req.repository(name)
            if member is None or member.format != 'rubygems' or member.type is RepoType.GROUP or not member.online:
                continue
            out.append(member)
        return out

    @staticmethod
    def _sub(req: FormatRequest, member: RepoConfig) -> FormatRequest:
        return dataclasses.replace(req, repo=member)

    async def serve(self, req: FormatRequest, route: Route) -> Response:
        members = await self.members(req)
        req.log(group=req.repo.name)
        started = time.perf_counter()

        def account(status: int, sent: int) -> None:
            req.hosted.account(req, CacheState.HIT, status, sent, started)

        match route.op:
            case Op.VERSIONS | Op.NAMES:
                merged = await self.merged(req, members)
                blob = merged.versions if route.op is Op.VERSIONS else merged.names
                response = req.hosted.serve_generated(
                    req, blob.sha256, blob.size, content_type=protocol.TEXT_TYPE, last_modified=merged.at
                )
                return add_digest(response)
            case Op.INFO:
                return await self._info(req, members, route, account)
            case Op.SPECS:
                hosted = [m for m in members if m.type is RepoType.HOSTED]
                docs = [d for m in hosted for d in await self._plugin.hosted.files(self._sub(req, m), m)]
                body = await asyncio.to_thread(marshal.specs_gz, sorted(set(tuples(docs, route.name))))
                return generated(req, body, account=account, content_type=protocol.GZIP_TYPE)
            case Op.GEM | Op.GEMSPEC:
                return await self._file(req, members, route)
        account(404, 0)
        raise NotFound('Not Found')

    # ------------------------------------------------------------------ info
    async def member_info(self, req: FormatRequest, member: RepoConfig, name: str) -> list[InfoLine] | None:
        """A member's info lines of ``name`` (None: the member does not have it)."""
        sub = self._sub(req, member)
        if member.type is RepoType.HOSTED:
            raw = await self._plugin.hosted.info(sub, member, name)
            lines = [line for line in (protocol.parse_info_line(r) for r in raw) if line is not None]
        else:
            lines = await self._plugin.info_lines(sub, name)
        return lines or None

    async def _infos(
        self, req: FormatRequest, members: list[RepoConfig], name: str
    ) -> list[tuple[RepoConfig, list[InfoLine]]]:
        out: list[tuple[RepoConfig, list[InfoLine]]] = []
        failure: WaystationError | None = None
        for member in members:
            try:
                found = await self.member_info(req, member, name)
            except NotFound:
                continue
            except WaystationError as exc:
                failure = failure or exc
                continue
            if found is not None:
                out.append((member, found))
        if not out and failure is not None:
            raise failure
        return out

    async def _info(self, req: FormatRequest, members: list[RepoConfig], route: Route, account: Any) -> Response:
        found = await self._infos(req, members, route.name)
        if not found:
            account(404, 0)
            raise NotFound('Not Found')
        if len(found) == 1:
            member = found[0][0]
            sub = self._sub(req, member)
            if member.type is RepoType.HOSTED:
                response = await self._plugin.hosted.serve(sub, route)
            else:
                response = await self._plugin.proxy_serve(sub, route)
            response.headers['X-Ws-Group-Member'] = member.name
            return response
        body = await asyncio.to_thread(merged_info, [lines for _, lines in found])
        return generated(req, body, account=account)

    # ------------------------------------------------------------------ versions and names
    async def _sources(self, req: FormatRequest, members: list[RepoConfig]) -> list[_Source]:
        out: list[_Source] = []
        failure: WaystationError | None = None
        for member in members:
            sub = self._sub(req, member)
            try:
                if member.type is RepoType.HOSTED:
                    docs = await self._plugin.hosted.files(sub, member)
                    token = hashlib.sha256(
                        repr([(d['path'], str(d.get('created_at'))) for d in docs]).encode(), usedforsecurity=False
                    ).hexdigest()
                    out.append(_Source(member, f'h:{member.id}:{token}', docs=docs))
                else:
                    asset = await req.proxy.ensure(sub, self._plugin.versions_spec())
                    out.append(_Source(member, f'p:{member.id}:{asset.blob_sha256}', asset=asset))
            except NotFound:
                continue
            except WaystationError as exc:
                failure = failure or exc
        if not out and failure is not None:
            raise failure
        return out

    async def merged(self, req: FormatRequest, members: list[RepoConfig]) -> _Merged:
        slot = req.repo.id
        lock = self._locks.setdefault(slot, asyncio.Lock())
        async with lock:
            sources = await self._sources(req, members)
            key = hashlib.sha256('|'.join(s.token for s in sources).encode(), usedforsecurity=False).hexdigest()
            merged = self._merged.get(slot)
            blobs = req.hosted.blobs
            if (
                merged is None
                or merged.key != key
                or not await blobs.exists(merged.versions.sha256)
                or not await blobs.exists(merged.names.sha256)
            ):
                merged = await self._merge(req, sources, key)
            elif time.monotonic() - merged.claimed > RECLAIM_S and blobs.guard is not None:
                # no asset references the merge: keep claiming it so blob GC leaves it alone while it is served
                await blobs.guard.claim(merged.versions.sha256, merged.versions.size)
                await blobs.guard.claim(merged.names.sha256, merged.names.size)
                merged = dataclasses.replace(merged, claimed=time.monotonic())
            self._merged[slot] = merged
            self._merged.move_to_end(slot)
            while len(self._merged) > MERGES_KEPT:
                old, _ = self._merged.popitem(last=False)
                self._locks.pop(old, None)
        return merged

    async def _merge(self, req: FormatRequest, sources: list[_Source], key: str) -> _Merged:
        started = time.perf_counter()

        maps: list[dict[str, GemVersions]] = []
        for source in sources:
            if source.asset is None:
                maps.append(await asyncio.to_thread(versions_of, source.docs or []))
                continue
            data = await req.proxy.read(source.asset, max_bytes=protocol.MAX_VERSIONS_FILE)
            maps.append(await asyncio.to_thread(protocol.parse_versions, data))
            del data
        gems, shared = await asyncio.to_thread(_merge_versions, maps)
        members = [s.member for s in sources]
        for name in shared[:MAX_SHARED]:
            found = await self._infos(req, members, name)
            body = await asyncio.to_thread(merged_info, [lines for _, lines in found])
            gems[name].md5 = protocol.md5(body)
        for name in shared[MAX_SHARED:]:
            gems[name].md5 = '0' * 32
        now = datetime.now(UTC)
        versions = await asyncio.to_thread(protocol.render_versions, gems, now)
        names = await asyncio.to_thread(protocol.render_names, sorted(gems))
        stored = []
        for body in (versions, names):
            async with await req.hosted.blobs.open_writer(expected_size=len(body)) as writer:
                await writer.write(body)
                ref = await req.hosted.blobs.commit(writer)
            stored.append(_Blob(ref.sha256, ref.size))
        req.log(merged_ms=round((time.perf_counter() - started) * 1000, 1), cache=CacheState.FETCHING.value)
        return _Merged(key=key, versions=stored[0], names=stored[1], at=now, claimed=time.monotonic())

    # ------------------------------------------------------------------ files
    async def _file(self, req: FormatRequest, members: list[RepoConfig], route: Route) -> Response:
        for member in members:
            asset = await req.hosted.get(member, route.key)
            if asset is None:
                continue
            response = req.hosted.serve(self._sub(req, member), asset, headers=SAFE_HEADERS)
            response.headers['X-Ws-Group-Member'] = member.name
            return response
        failure: WaystationError | None = None
        for member in members:
            if member.type is not RepoType.PROXY:
                continue
            try:
                response = await self._plugin.proxy_serve(self._sub(req, member), route)
            except NotFound:
                continue
            except WaystationError as exc:
                failure = failure or exc
                continue
            if response.status_code == 404:
                continue
            response.headers['X-Ws-Group-Member'] = member.name
            return response
        if failure is not None:
            raise failure
        raise NotFound('Not Found')
