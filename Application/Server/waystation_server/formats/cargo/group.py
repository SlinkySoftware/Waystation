"""Cargo group repositories (T-P6-CARGO; SPECIFICATION §4.3; FORMATS.md "Cargo").

Members (Cargo proxy and hosted repositories, in order; offline or missing ones are skipped) answer through the group:

- ``index/config.json``: the group's own (downloads and the web API on the group).
- Index files: merged per request. Each member's entries (hosted: stored; proxy: the cached index file, refreshed as
  needed) are taken in member order and the first member listing a version wins it. A member that does not know the
  crate is skipped, one that fails (upstream down, nothing cached) too, unless no member answers: then its error is
  returned. ``ETag``, ``304``, ``HEAD``.
- ``.crate`` files: any member's stored or cached copy first (no upstream request), then the members in order
  (``X-Ws-Group-Member``).
- The web API (publish, yank, unyank, owners) goes to the writable member (a hosted Cargo repository), with ``write``
  checked on the group (router) and on the member (``req.allows_member``); without one: 405.

Reading through a group needs ``read`` on the group only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import TYPE_CHECKING, Any

from starlette.responses import Response

from waystation_common.errors import NotFound, WaystationError
from waystation_common.events import CacheState
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.cargo.common import (
    INDEX_TYPE,
    SAFE_HEADERS,
    Op,
    Route,
    cargo_error,
    crate_path,
    generated,
    render_index,
)
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.security.actions import Action

if TYPE_CHECKING:
    from waystation_server.formats.cargo.plugin import CargoPlugin

MEMBER_TYPES = (RepoType.PROXY, RepoType.HOSTED)


class CargoGroup:
    def __init__(self, plugin: CargoPlugin) -> None:
        self._plugin = plugin

    async def members(self, req: FormatRequest) -> list[RepoConfig]:
        """The group's online Cargo proxy and hosted members, in order."""
        group = req.repo.group
        out: list[RepoConfig] = []
        for name in group.members if group is not None else ():
            member = await req.repository(name)
            if member is None or member.format != 'cargo' or member.type not in MEMBER_TYPES or not member.online:
                continue
            out.append(member)
        return out

    @staticmethod
    def _sub(req: FormatRequest, member: RepoConfig) -> FormatRequest:
        return dataclasses.replace(req, repo=member)

    async def serve(self, req: FormatRequest, route: Route) -> Response:
        members = await self.members(req)
        req.log(group=req.repo.name)
        if route.op is Op.INDEX:
            return await self._index(req, members, route)
        if route.op is Op.DOWNLOAD:
            return await self._download(req, members, route)
        raise NotFound('Not Found')

    async def entries(self, req: FormatRequest, member: RepoConfig, crate: str) -> list[dict[str, Any]]:
        sub = self._sub(req, member)
        if member.type is RepoType.HOSTED:
            return await self._plugin.hosted.entries(sub, member, crate)
        return await self._plugin.index_entries(sub, crate)

    async def _index(self, req: FormatRequest, members: list[RepoConfig], route: Route) -> Response:
        started = time.perf_counter()
        merged: dict[str, dict[str, Any]] = {}
        answered = False
        failure: WaystationError | None = None
        for member in members:
            try:
                entries = await self.entries(req, member, route.crate)
            except NotFound:
                continue
            except WaystationError as exc:  # upstream down and nothing cached: the others may still answer
                failure = failure or exc
                continue
            answered = answered or bool(entries)
            for entry in entries:
                merged.setdefault(str(entry['vers']), entry)

        def account(status: int, sent: int) -> None:
            req.hosted.account(req, CacheState.HIT, status, sent, started)

        if not answered:
            if failure is not None:
                raise failure
            account(404, 0)
            raise NotFound('Not Found')
        body = await asyncio.to_thread(render_index, merged.values())
        return generated(req, body, INDEX_TYPE, account=account)

    async def _download(self, req: FormatRequest, members: list[RepoConfig], route: Route) -> Response:
        path = crate_path(route.crate, route.version)
        for member in members:
            asset = await req.hosted.get(member, path)
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
            response.headers.update(SAFE_HEADERS)
            response.headers['X-Ws-Group-Member'] = member.name
            return response
        if failure is not None:
            raise failure
        raise NotFound('Not Found')

    async def api(self, req: FormatRequest, route: Route) -> Response:
        group = req.repo.group
        target = group.writable_member if group is not None else None
        member = await req.repository(target) if target else None
        if member is None or member.format != 'cargo' or member.type is not RepoType.HOSTED:
            return cargo_error(405, 'this group has no writable member: publish to a hosted repository', api=True)
        action = Action.READ if route.op is Op.OWNERS and req.method == 'GET' else Action.WRITE
        if not req.allows_member(action, member):
            return cargo_error(403, f'not allowed to {action.value} the repository {member.name}', api=True)
        if not member.online:
            return cargo_error(503, f'the repository {member.name} is offline', api=True)
        sub = self._sub(req, member)
        if route.op is Op.PUBLISH:
            return await self._plugin.hosted.publish(sub)
        if route.op is Op.OWNERS:
            return await self._plugin.hosted.owners(sub, route)
        return await self._plugin.hosted.yank(sub, route)
