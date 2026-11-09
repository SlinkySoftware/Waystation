"""Maven group repositories (FORMATS.md "Maven"; SPECIFICATION §4.3): ordered proxy and hosted members.

- **Files** (artifacts, signatures, checksum files): the first member that has the file answers, as that member
  would (a proxy member asks its upstream). A member answering 404 is skipped, and so is one that fails (upstream
  error) while a later member can still answer; when none can, the first failure is returned, else 404. Members
  whose version policy excludes the path's version are not asked.
- **maven-metadata.xml**: every member's document (proxies: cached and revalidated as usual; failing members are
  left out) merged: versions united and ``latest``/``release`` recomputed in Maven order, the newest SNAPSHOT build
  per classifier and extension, plugin prefixes united. Built per request, deterministic (its checksum files are
  computed from the same bytes), with an ETag and conditional requests.
- Groups are read-only (``405``): deploy to a hosted repository.

Reading through a group needs ``read`` on the group only (its members' content is served through it).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable

from starlette.responses import Response

from waystation_common.errors import NotFound, ValidationFailed, WaystationError
from waystation_common.events import CacheState
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.maven import coords
from waystation_server.formats.maven import metadata as md
from waystation_server.formats.maven.common import XML_TYPE, generated, options_of, policy_allows
from waystation_server.formats.maven.proxy import MavenProxy
from waystation_server.problems import native_error
from waystation_server.repos.model import RepoConfig, RepoType

log = logging.getLogger('waystation.formats.maven')

ServeMember = Callable[[FormatRequest], Awaitable[Response]]


class MavenGroup:
    def __init__(self, serve_member: ServeMember) -> None:
        self._serve = serve_member

    async def members(self, req: FormatRequest) -> list[RepoConfig]:
        """The group's online Maven members, in order (missing ones and groups are skipped)."""
        group = req.repo.group
        out: list[RepoConfig] = []
        for name in group.members if group is not None else ():
            member = await req.repository(name)
            if member is None or member.format != 'maven' or member.type is RepoType.GROUP or not member.online:
                continue
            out.append(member)
        return out

    async def handle(self, req: FormatRequest) -> Response:
        if req.method not in ('GET', 'HEAD'):
            return native_error(
                405, 'A group repository is read-only: deploy to a hosted repository', headers={'Allow': 'GET, HEAD'}
            )
        path = req.path
        if not path or req.request.url.path.endswith('/'):
            raise NotFound('Not Found')
        req.log(group=req.repo.name)
        version = coords.version_of_path(path)
        members = [m for m in await self.members(req) if policy_allows(options_of(m), version)]
        split = coords.split_checksum(path)
        if coords.is_metadata(path) or (split is not None and coords.is_metadata(split[0])):
            return await self.metadata(req, members, split[1] if split is not None else None)
        first_error: WaystationError | None = None
        for member in members:
            try:
                return await self._serve(dataclasses.replace(req, repo=member))
            except NotFound:
                continue
            except WaystationError as exc:
                first_error = first_error or exc
        raise first_error or NotFound('Not Found')

    async def metadata(self, req: FormatRequest, members: list[RepoConfig], alg: str | None) -> Response:
        started = time.perf_counter()
        path = req.path if alg is None else req.path[: -len(alg) - 1]
        documents: list[md.Metadata] = []
        for member in members:
            data = await self._member_document(dataclasses.replace(req, repo=member, path=path))
            if data is None:
                continue
            try:
                documents.append(await asyncio.to_thread(md.parse, data))
            except ValidationFailed:
                log.warning('member maven-metadata.xml unreadable', extra={'member': member.name, 'path': path})
        if not documents:
            req.hosted.account(req, CacheState.HIT, 404, 0, started)
            raise NotFound('Not Found')
        merged = await asyncio.to_thread(lambda: md.render(md.merge(documents)))
        if alg is None:
            response = generated(req.request, merged, XML_TYPE)
        else:
            response = generated(req.request, hashlib.new(alg, merged).hexdigest().encode(), 'text/plain')
        sent = len(response.body) if response.status_code == 200 and req.method == 'GET' else 0
        req.hosted.account(req, CacheState.HIT, response.status_code, sent, started)
        return response

    async def _member_document(self, req: FormatRequest) -> bytes | None:
        """A member's ``maven-metadata.xml`` (None when it has none or cannot answer now)."""
        try:
            if req.repo.type is RepoType.HOSTED:
                asset = await req.hosted.get(req.repo, req.path)
                return await req.hosted.read(asset, max_bytes=md.MAX_BYTES) if asset is not None else None
            asset = await req.proxy.ensure(req, MavenProxy.metadata_spec(req.path))
            return await req.proxy.read(asset, max_bytes=md.MAX_BYTES)
        except NotFound:
            return None
        except (WaystationError, ValueError) as exc:
            log.warning('member maven-metadata.xml unavailable', extra={'member': req.repo.name, 'error': str(exc)})
            return None
