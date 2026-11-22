"""Composer group repositories (T-P6-COMPOSER; SPECIFICATION §4.3; FORMATS.md "Composer").

Members (Composer proxy and hosted repositories, in order; offline or missing ones are skipped) answer through the
group:

- ``packages.json``: rendered for the group (``available-packages`` only when every member is hosted: their union).
- ``p2`` documents: every member's entries of the package merged, the first member that has a version (normalised)
  winning it, archive URLs pointing at the group; rendered per request.
- Archives (``dists/...``): the first member that stores or caches the file (X-Accel), else the proxy members in
  order (each looks the archive up in its own cached metadata).
- Uploads go to a hosted repository directly (a group has no writable member).

A member that fails (upstream down, nothing cached) is skipped unless no member answers; then its error is returned.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from starlette.responses import Response

from waystation_common.errors import NotFound, WaystationError
from waystation_common.events import CacheState
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.composer import protocol
from waystation_server.formats.composer.common import SAFE_HEADERS, generated
from waystation_server.formats.composer.hosted import split
from waystation_server.formats.composer.protocol import Op, Route
from waystation_server.repos.model import RepoConfig, RepoType

if TYPE_CHECKING:
    from waystation_server.formats.composer.plugin import ComposerPlugin


class ComposerGroup:
    def __init__(self, plugin: ComposerPlugin) -> None:
        self._plugin = plugin

    async def members(self, req: FormatRequest) -> list[RepoConfig]:
        """The group's online Composer members, in order."""
        group = req.repo.group
        out: list[RepoConfig] = []
        for name in group.members if group is not None else ():
            member = await req.repository(name)
            if member is None or member.format != 'composer' or member.type is RepoType.GROUP or not member.online:
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

        def missing() -> NotFound:
            account(404, 0)
            return NotFound('Not Found')

        match route.op:
            case Op.ROOT:
                available: list[str] | None = None
                if members and all(m.type is RepoType.HOSTED for m in members):
                    lists = [await self._plugin.hosted.names(req, m) for m in members]
                    if all(found is not None for found in lists):
                        available = sorted({n for found in lists for n in found or ()})
                body = protocol.render_root(req.repo_url, available=available)
                return generated(req, body, account=account)
            case Op.P2:
                found = await self._collect(members, lambda m: self.member_entries(req, m, route.name, route.branches))
                merged = protocol.merge(found)
                if not merged:
                    raise missing()
                body = await asyncio.to_thread(protocol.render_p2, route.name, merged)
                return generated(req, body, account=account)
            case Op.DIST:
                return await self._file(req, members, route)
        raise missing()

    async def _collect[T](self, members: list[RepoConfig], ask: Callable[[RepoConfig], Awaitable[T | None]]) -> list[T]:
        """Each member's answer in order (None: the member does not know it); a failing member is skipped unless
        none answers."""
        out: list[T] = []
        failure: WaystationError | None = None
        for member in members:
            try:
                found = await ask(member)
            except NotFound:
                continue
            except WaystationError as exc:
                failure = failure or exc
                continue
            if found is not None:
                out.append(found)
        if not out and failure is not None:
            raise failure
        return out

    async def member_entries(
        self, req: FormatRequest, member: RepoConfig, name: str, dev: bool
    ) -> list[dict[str, Any]] | None:
        """A member's entries of ``name`` with the group's archive URLs (None: the member does not have it)."""
        sub = self._sub(req, member)
        if member.type is RepoType.HOSTED:
            found = split(await self._plugin.hosted.entries(sub, member, name, req.repo_url), dev=dev)
            return found or None
        entries = await self._plugin.stored_entries(sub, name, dev=dev)
        return protocol.reprefix(entries, sub.repo_url, req.repo_url) or None

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
