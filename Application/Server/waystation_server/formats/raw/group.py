"""Raw group repositories (T-P2-RAW; FORMATS.md "Raw"; SPECIFICATION §4.3).

- ``GET``/``HEAD`` of a file: the members in order (proxy and hosted mixed, groups cannot contain groups); the first
  member that has the file answers, as that member would (a proxy member fetches from its upstream). A member
  answering 404 is skipped, and so is one that fails (upstream error) while a later member can still answer; when
  none can, the first failure is returned, else 404.
- Directory listings: the members' listings merged (``listing``).
- ``PUT`` and ``DELETE`` go to the ``writable_member`` (a hosted raw repository); a group without one is read-only
  (405). The principal needs the action on the group (checked by the router) and on the writable member.

Reading through a group needs ``read`` on the group only (its members' content is served through it, as in Nexus).
Answers carry ``X-Ws-Group-Member``; statistics count on the member that served them.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable

from starlette.responses import Response

from waystation_common.errors import Forbidden, NotFound, WaystationError
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.raw import listing
from waystation_server.problems import native_error
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.security.actions import Action

Serve = Callable[[FormatRequest], Awaitable[Response]]


class RawGroup:
    def __init__(self, serve: Serve) -> None:
        self._serve = serve  # a member's own handling (proxy or hosted)

    async def members(self, req: FormatRequest) -> list[RepoConfig]:
        """The group's online raw members, in order (missing ones are skipped)."""
        group = req.repo.group
        out: list[RepoConfig] = []
        for name in group.members if group is not None else ():
            member = await req.repository(name)
            if member is None or member.format != 'raw' or member.type is RepoType.GROUP or not member.online:
                continue
            out.append(member)
        return out

    async def handle(self, req: FormatRequest, *, is_folder: bool) -> Response:
        if req.method not in ('GET', 'HEAD'):
            return await self._write(req)
        members = await self.members(req)
        req.log(group=req.repo.name)
        if is_folder:
            return await listing.serve(req, members)
        failure: WaystationError | Response | None = None
        for member in members:
            try:
                response = await self._serve(dataclasses.replace(req, repo=member))
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

    async def _write(self, req: FormatRequest) -> Response:
        group = req.repo.group
        name = group.writable_member if group is not None else None
        member = await req.repository(name) if name else None
        if member is None or member.type is not RepoType.HOSTED:
            return native_error(
                405, 'This group has no writable member: upload to a hosted repository', headers={'Allow': 'GET, HEAD'}
            )
        action = Action.DELETE if req.method == 'DELETE' else Action.WRITE
        if not req.allows_member(action, member):
            raise Forbidden(f'{action.value} permission required on {member.name}')
        if not member.online:
            return native_error(503, 'The writable member is offline')
        req.log(group=req.repo.name)
        return await self._serve(dataclasses.replace(req, repo=member))
