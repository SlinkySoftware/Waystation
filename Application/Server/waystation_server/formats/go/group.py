"""Go group repositories (T-P5-GO; SPECIFICATION §4.3; FORMATS.md "Go modules").

Members (Go proxy repositories, in order; offline or missing ones are skipped) answer through the group:

- ``@v/list``: merged. Each member's cached list (refreshed as needed) contributes its versions; a member that does not
  know the module is skipped, one that fails (upstream down, nothing cached) too, unless no member answers, then its
  error is returned. Rendered per request, newest first (``ETag``, ``304``, ``HEAD``).
- ``@latest`` and ``.info``/``.mod``/``.zip``: the first member that already holds the file serves it (X-Accel, no
  upstream request); otherwise the members are asked in order and the first that has it answers (``.mod``/``.zip``
  verified by that member's checksum database settings).
- ``sumdb/<name>/...``: the first member that proxies that checksum database answers (``supported`` = any does).

Reading through a group needs ``read`` on the group only (its members' content is served through it).
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import TYPE_CHECKING

from starlette.responses import Response

from waystation_common.errors import NotFound, WaystationError
from waystation_common.events import CacheState
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.go.common import SAFE_HEADERS, TEXT, generated, newest_first, parse_list, render_list
from waystation_server.formats.go.modpath import Op, Route, is_canonical
from waystation_server.repos.model import RepoConfig, RepoType

if TYPE_CHECKING:
    from waystation_server.formats.go.plugin import GoPlugin


class GoGroup:
    def __init__(self, plugin: GoPlugin) -> None:
        self._plugin = plugin

    async def members(self, req: FormatRequest) -> list[RepoConfig]:
        """The group's online Go proxy members, in order."""
        group = req.repo.group
        out: list[RepoConfig] = []
        for name in group.members if group is not None else ():
            member = await req.repository(name)
            if member is None or member.format != 'go' or member.type is not RepoType.PROXY or not member.online:
                continue
            out.append(member)
        return out

    @staticmethod
    def _sub(req: FormatRequest, member: RepoConfig) -> FormatRequest:
        return dataclasses.replace(req, repo=member)

    async def serve(self, req: FormatRequest, route: Route) -> Response:
        members = await self.members(req)
        req.log(group=req.repo.name)
        if route.op is Op.LIST:
            return await self._list(req, members, route)
        if route.op is Op.SUMDB:
            return await self._sumdb(req, members, route)
        return await self._file(req, members, route)

    async def _list(self, req: FormatRequest, members: list[RepoConfig], route: Route) -> Response:
        started = time.perf_counter()
        versions: list[str] = []
        answered = False
        failure: WaystationError | None = None
        for member in members:
            sub = self._sub(req, member)
            try:
                asset = await req.proxy.ensure(sub, self._plugin.module_spec(sub, route))
                data = await req.proxy.read(asset)
            except NotFound:
                continue
            except WaystationError as exc:  # upstream down and nothing cached: the others may still answer
                failure = failure or exc
                continue
            answered = True
            versions += await asyncio.to_thread(parse_list, data)

        def account(status: int, sent: int) -> None:
            req.hosted.account(req, CacheState.HIT, status, sent, started)

        if not answered:
            if failure is not None:
                raise failure
            account(404, 0)
            raise NotFound(f'not found: {route.module}')
        body = render_list(newest_first(versions))
        return generated(req, body, TEXT, account=account)

    async def _file(self, req: FormatRequest, members: list[RepoConfig], route: Route) -> Response:
        if route.op in (Op.MOD, Op.ZIP) or (route.op is Op.INFO and is_canonical(route.version)):
            # Immutable files: any member's copy (@latest and version queries move: ask the members in order)
            for member in members:
                asset = await req.hosted.get(member, route.key)
                if asset is None:
                    continue
                response = req.hosted.serve(self._sub(req, member), asset, headers=SAFE_HEADERS)
                response.headers['X-Ws-Group-Member'] = member.name
                return response
        return await self._first(req, members, route)

    async def _first(self, req: FormatRequest, members: list[RepoConfig], route: Route) -> Response:
        failure: WaystationError | None = None
        for member in members:
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
        raise NotFound(f'not found: {route.module or route.rest}')

    async def _sumdb(self, req: FormatRequest, members: list[RepoConfig], route: Route) -> Response:
        serving = [
            m
            for m in members
            if (options := self._plugin.options(self._sub(req, m))).sumdb_proxy and options.sumdb_name == route.sumdb
        ]
        if not serving:
            raise NotFound(f'not found: checksum database {route.sumdb} is not proxied here')
        return await self._first(req, serving[:1], route)
