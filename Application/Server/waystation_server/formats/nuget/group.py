"""NuGet group repositories (T-P6-NUGET; SPECIFICATION §4.3; FORMATS.md "NuGet v3").

Members (NuGet proxy and hosted repositories, in order; offline or missing ones are skipped) answer through the group:

- **Service index**: rendered for the group (``PackagePublish`` only with a writable member).
- **Version lists**: the union of the members' versions (a proxy's cached list, a hosted repository's packages).
- **Registrations**: one index with every member's leaves inlined (a proxy's pages fetched when the upstream did not
  inline them), the first member that has a version winning it, every URL pointing at the group. Registration leaves
  and pages: the first member that has them.
- **Packages** (``.nupkg``, ``.nuspec``): the first member that stores or caches the file (X-Accel), else the proxy
  members in order.
- **Search**: the members' answers merged (hosted: local search, proxy: the upstream's), first member wins per id.
- **Push and delete** go to the ``writable_member`` (a hosted NuGet repository): the principal needs the action on
  the group (router) and on the member.

A member that fails (upstream down, nothing cached) is skipped unless no member answers; then its error is returned.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from starlette.responses import Response

from waystation_common.errors import Forbidden, NotFound, WaystationError
from waystation_common.events import CacheState
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.nuget import protocol
from waystation_server.formats.nuget.common import SAFE_HEADERS, generated, nuget_error
from waystation_server.formats.nuget.protocol import Op, Route, SearchQuery
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.security.actions import Action

if TYPE_CHECKING:
    from waystation_server.formats.nuget.plugin import NugetPlugin


class NugetGroup:
    def __init__(self, plugin: NugetPlugin) -> None:
        self._plugin = plugin

    async def members(self, req: FormatRequest) -> list[RepoConfig]:
        """The group's online NuGet members, in order."""
        group = req.repo.group
        out: list[RepoConfig] = []
        for name in group.members if group is not None else ():
            member = await req.repository(name)
            if member is None or member.format != 'nuget' or member.type is RepoType.GROUP or not member.online:
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
        if member is None or member.type is not RepoType.HOSTED or member.format != 'nuget':
            return None
        return member

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
            case Op.SERVICE_INDEX:
                publish = await self.writable(req) is not None
                return generated(req, protocol.service_index(req.repo_url, publish=publish), account=account)
            case Op.FLAT_INDEX:
                versions = await self._collect(req, members, lambda m: self.member_versions(req, m, route.id))
                if not versions:
                    raise missing()
                flat = [v for found in versions for v in found]
                return generated(req, protocol.render_versions(flat), account=account)
            case Op.REGISTRATION:
                docs = await self._collect(req, members, lambda m: self.member_registration(req, m, route.id))
                merged = await asyncio.to_thread(protocol.merge_registrations, docs) if docs else None
                if merged is None:
                    raise missing()
                return generated(req, protocol.dump(merged), account=account)
            case Op.REGISTRATION_PART:
                for member in members:
                    body = await self._member_part(req, member, route)
                    if body is not None:
                        return generated(req, body, account=account)
                raise missing()
            case Op.NUPKG | Op.NUSPEC:
                return await self._file(req, members, route)
            case Op.SEARCH:
                query = SearchQuery.from_params(req.request.query_params)
                answers = await self._collect(req, members, lambda m: self.member_search(req, m, query))
                body = await asyncio.to_thread(protocol.merge_search, answers, query)
                return generated(req, body, account=account)
        raise missing()

    async def _collect[T](
        self, req: FormatRequest, members: list[RepoConfig], ask: Callable[[RepoConfig], Awaitable[T | None]]
    ) -> list[T]:
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

    # ------------------------------------------------------------------ members' documents
    async def member_versions(self, req: FormatRequest, member: RepoConfig, package_id: str) -> list[str] | None:
        sub = self._sub(req, member)
        if member.type is RepoType.HOSTED:
            found = await self._plugin.hosted.versions(sub, member, package_id)
            return [v.flat for v in found] or None
        upstream = await self._plugin.upstream(sub)
        asset = await req.proxy.ensure(sub, self._plugin.versions_spec(sub, upstream, package_id))
        data = await req.proxy.read(asset, max_bytes=protocol.MAX_DOCUMENT)
        return await asyncio.to_thread(protocol.parse_versions, data)

    async def member_registration(self, req: FormatRequest, member: RepoConfig, package_id: str) -> Any:
        """A member's registration index, fully inlined, with the group's URLs (None: the member does not have it)."""
        sub = self._sub(req, member)
        if member.type is RepoType.HOSTED:
            found = await self._plugin.hosted.versions(sub, member, package_id)
            if not found:
                return None
            body = await asyncio.to_thread(protocol.render_registration, req.repo_url, found[0].id, found)
            return json.loads(body)
        doc = await self._plugin.inlined_pages(sub, package_id)
        body = protocol.reprefix(protocol.dump(doc), sub.repo_url, req.repo_url)
        return await asyncio.to_thread(protocol.load, body, 'a member registration')

    async def _member_part(self, req: FormatRequest, member: RepoConfig, route: Route) -> bytes | None:
        sub = self._sub(req, member)
        try:
            if member.type is RepoType.HOSTED:
                leaf = await self._plugin.hosted.leaf(sub, member, route)
                return protocol.render_leaf(req.repo_url, leaf) if leaf is not None else None
            doc = await self._plugin.registration_doc(sub, route)
        except NotFound:
            return None
        except WaystationError:
            return None
        return protocol.reprefix(protocol.dump(doc), sub.repo_url, req.repo_url)

    async def member_search(self, req: FormatRequest, member: RepoConfig, query: SearchQuery) -> Any:
        sub = self._sub(req, member)
        wide = dataclasses.replace(query, skip=0, take=query.skip + query.take)
        if member.type is RepoType.HOSTED:
            return await self._plugin.hosted.search(sub, member, query, req.repo_url, window=False)
        doc = await self._plugin.search_doc(sub, wide)
        if doc is None:
            return None
        body = protocol.reprefix(protocol.dump(doc), sub.repo_url, req.repo_url)
        return json.loads(body)

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

    # ------------------------------------------------------------------ writes
    async def write(self, req: FormatRequest, route: Route) -> Response:
        member = await self.writable(req)
        if member is None:
            return nuget_error(405, 'this group has no writable member: push to a hosted repository')
        action = Action.DELETE if req.method == 'DELETE' else Action.WRITE
        if not req.allows_member(action, member):
            raise Forbidden(f'{action.value} permission required on {member.name}')
        if not member.online:
            return nuget_error(503, 'the writable member is offline')
        req.log(group=req.repo.name)
        response = await self._plugin.hosted.handle_write(self._sub(req, member), route)
        response.headers['X-Ws-Group-Member'] = member.name
        return response
