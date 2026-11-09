"""PyPI group repositories (T-P5-PYPI; SPECIFICATION §4.3; FORMATS.md "PyPI").

Members (PyPI proxy and hosted repositories, in order; offline or missing ones are skipped) answer through the group:

- **Project pages**: merged. Each member's page (a proxy's cached JSON page, refreshed as needed; a hosted
  repository's stored files) contributes the files the earlier members do not have: the first member wins per file
  name. A member that does not know the project is skipped; one that fails (upstream down, nothing cached) too,
  unless no member answers, then its error is returned. Rendered per request in the negotiated representation, with
  the same relative file URLs (``../../packages/<project>/<file>``), so files are fetched through the group.
- **Files** (and ``.metadata``): the first member that stores or caches the file serves it (X-Accel, no upstream
  request); otherwise the proxy members are asked in order (each looks the file up in its own page).
- **Index** (``simple/``): the union of the members' project names.
- **Uploads** go to the ``writable_member`` (a hosted PyPI repository): the principal needs ``write`` on the group
  (router) and on the member; a group without one refuses uploads (405).
- The legacy JSON API answers from the first proxy member that has it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import TYPE_CHECKING

from starlette.responses import Response

from waystation_common.errors import NotFound, WaystationError
from waystation_common.events import CacheState
from waystation_server.db import pypi as pypi_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.pypi import simple
from waystation_server.formats.pypi.common import SAFE_HEADERS, Op, Route, file_path, generated, pypi_error
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.security.actions import Action

if TYPE_CHECKING:
    from waystation_server.formats.pypi.plugin import PypiPlugin


def merge(pages: list[simple.Page], project: str) -> simple.Page:
    """Union of the members' files by file name, the first member winning; versions merged."""
    merged = simple.Page(name=next((p.name for p in pages if p.name), project))
    seen: set[str] = set()
    versions: set[str] = set()
    for page in pages:
        versions.update(page.versions or ())
        for entry in page.files:
            if entry.filename not in seen:
                seen.add(entry.filename)
                merged.files.append(dataclasses.replace(entry, url=''))
    merged.versions = sorted(versions) if versions else None
    return merged


class PypiGroup:
    def __init__(self, plugin: PypiPlugin) -> None:
        self._plugin = plugin

    async def members(self, req: FormatRequest) -> list[RepoConfig]:
        """The group's online PyPI members, in order."""
        group = req.repo.group
        out: list[RepoConfig] = []
        for name in group.members if group is not None else ():
            member = await req.repository(name)
            if member is None or member.format != 'pypi' or member.type is RepoType.GROUP or not member.online:
                continue
            out.append(member)
        return out

    @staticmethod
    def _sub(req: FormatRequest, member: RepoConfig) -> FormatRequest:
        return dataclasses.replace(req, repo=member)

    async def serve(self, req: FormatRequest, route: Route, chosen: str) -> Response:
        members = await self.members(req)
        req.log(group=req.repo.name)
        match route.op:
            case Op.INDEX:
                return await self._index(req, members, chosen)
            case Op.PROJECT:
                return await self._page(req, members, simple.normalise(route.project), chosen)
            case Op.FILE | Op.METADATA:
                return await self._file(req, members, route)
            case Op.LEGACY_JSON:
                for member in members:
                    if member.type is RepoType.PROXY:
                        try:
                            return await self._plugin.serve(self._sub(req, member), route)
                        except NotFound:
                            continue
        raise NotFound('Not Found')

    async def member_page(self, req: FormatRequest, member: RepoConfig, project: str) -> simple.Page | None:
        """One member's page of ``project`` (None: the member does not know it)."""
        sub = self._sub(req, member)
        if member.type is RepoType.HOSTED:
            return await self._plugin.hosted.page(sub, member, project)
        try:
            return await self._plugin.project_page(sub, project)
        except NotFound:
            return None

    async def _page(self, req: FormatRequest, members: list[RepoConfig], project: str, chosen: str) -> Response:
        started = time.perf_counter()
        pages: list[simple.Page] = []
        failure: WaystationError | None = None
        for member in members:
            try:
                page = await self.member_page(req, member, project)
            except WaystationError as exc:  # upstream down and nothing cached: the others may still answer
                failure = failure or exc
                continue
            if page is not None:
                pages.append(page)

        def account(status: int, sent: int) -> None:
            req.hosted.account(req, CacheState.HIT, status, sent, started)

        if not pages:
            if failure is not None:
                raise failure
            account(404, 0)
            raise NotFound('Not Found')
        page = await asyncio.to_thread(merge, pages, project)
        if simple.is_json(chosen):
            body = await asyncio.to_thread(simple.render_json, page, project)
        else:
            body = await asyncio.to_thread(simple.render_html, page, project)
        return generated(req, body, chosen, account=account)

    async def _index(self, req: FormatRequest, members: list[RepoConfig], chosen: str) -> Response:
        started = time.perf_counter()
        names: list[str] = []
        for member in members:
            sub = self._sub(req, member)
            if member.type is RepoType.HOSTED:
                names += await pypi_db.project_names(req.hosted.db, member.id, limit=1_000_000)
                continue
            try:
                names += await self._plugin.index_names(sub)
            except WaystationError:
                continue
        body = await asyncio.to_thread(simple.render_index, names, as_json=simple.is_json(chosen))
        return generated(req, body, chosen, account=lambda s, n: req.hosted.account(req, CacheState.HIT, s, n, started))

    async def _file(self, req: FormatRequest, members: list[RepoConfig], route: Route) -> Response:
        path = file_path(route.project, route.filename, metadata=route.op is Op.METADATA)
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
                response = await self._plugin.serve(self._sub(req, member), route)
            except NotFound:
                continue
            except WaystationError as exc:
                failure = failure or exc
                continue
            response.headers['X-Ws-Group-Member'] = member.name
            return response
        if failure is not None:
            raise failure
        raise NotFound('Not Found')

    async def upload(self, req: FormatRequest) -> Response:
        group = req.repo.group
        name = group.writable_member if group is not None else None
        member = await req.repository(name) if name else None
        if member is None or member.type is not RepoType.HOSTED or member.format != 'pypi':
            return pypi_error(405, 'this group has no writable member: upload to a hosted repository')
        if not req.allows_member(Action.WRITE, member):
            return pypi_error(403, f'write permission required on {member.name}')
        if not member.online:
            return pypi_error(503, 'the writable member is offline')
        req.log(group=req.repo.name)
        response = await self._plugin.hosted.upload(self._sub(req, member))
        response.headers['X-Ws-Group-Member'] = member.name
        return response
