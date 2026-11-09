"""Docker / OCI group repositories (T-P2-DOCKERG; SPECIFICATION §4.3; FORMATS.md "Docker / OCI").

A group answers the registry API from its members, in order (proxy and hosted mixed; groups cannot contain groups):

- **Manifests** (by tag or digest): the first member that has it answers; a member answering 404 is skipped, and so is
  one that fails (upstream error, rate limit) while a later member can still answer; when none can, the first failure
  is returned, else 404. A proxy member fetches from its upstream as usual, so the order decides which registry a tag
  comes from.
- **Blobs**: content-addressed, so every member's stored or cached copy is looked at first (no upstream request);
  only a ``GET`` then asks the proxy members' upstreams in order. A ``HEAD`` never goes upstream (it is how clients
  check what a push still has to upload, and image names pushed to a group must not leak to public registries).
- **tags/list**: the union of the members' tags (hosted: pushed tags; proxies: the upstream's first page, cached like
  ``tags/list``), sorted, with ``n``/``last`` and ``Link``. **referrers**: the union over hosted members.
- **Writes** (pushes, upload sessions, deletes) go to the ``writable_member`` (a hosted docker repository); a group
  without one is read-only (``405 UNSUPPORTED``). The principal needs the action on the group (checked by the router)
  and on the writable member (RBAC; a registry token names only the group).

Requests are answered as the member answers them (``Location`` headers keep the group's name, so a push continues
through the group); traffic and cache statistics count on the member that served them.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode

from starlette.responses import JSONResponse, Response

from waystation_common.errors import WaystationError
from waystation_common.events import CacheState
from waystation_server.db import docker as docker_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.docker import protocol as p
from waystation_server.formats.docker.hosted import API_HEADERS, client_name, client_prefix
from waystation_server.problems import oci_error
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.security.actions import Action

Serve = Callable[[FormatRequest, p.Operation], Awaitable[Response]]
ProxyTags = Callable[[FormatRequest, str], Awaitable[list[str]]]
FAILED = frozenset({401, 403, 429})  # a member's upstream refused: try the next one, report it if none answers


def _error(status: int, message: str, code: str, headers: dict[str, str] | None = None) -> Response:
    return oci_error(status, message, code=code, headers={**API_HEADERS, **(headers or {})})


class DockerGroup:
    def __init__(self, *, serve: Serve, proxy_tags: ProxyTags) -> None:
        self._serve = serve
        self._proxy_tags = proxy_tags

    async def members(self, req: FormatRequest) -> list[RepoConfig]:
        """The group's online docker members, in order (missing ones are skipped)."""
        group = req.repo.group
        out: list[RepoConfig] = []
        for name in group.members if group is not None else ():
            member = await req.repository(name)
            if member is None or member.format != 'docker' or member.type is RepoType.GROUP or not member.online:
                continue
            out.append(member)
        return out

    async def handle(self, req: FormatRequest, op: p.Operation) -> Response:
        if op.op == 'base':
            return JSONResponse({}, headers=API_HEADERS)
        if req.method not in ('GET', 'HEAD') or op.op == 'upload':
            return await self._write(req, op)
        members = await self.members(req)
        req.log(group=req.repo.name)
        match op.op:
            case 'tags':
                return await self._tags(req, op, members)
            case 'referrers':
                return await self._referrers(req, op, members)
            case 'blob':
                cached = await self._cached_blob(req, op, members)
                if cached is not None or req.is_head:
                    return cached or _error(404, 'blob unknown to registry', 'BLOB_UNKNOWN')
        return await self._first(req, op, members)

    @staticmethod
    def _member_request(req: FormatRequest, member: RepoConfig) -> FormatRequest:
        return dataclasses.replace(req, repo=member)

    async def _first(self, req: FormatRequest, op: p.Operation, members: list[RepoConfig]) -> Response:
        failure: Response | None = None
        for member in members:
            response = await self._serve(self._member_request(req, member), op)
            if response.status_code == 404:
                continue
            if response.status_code >= 500 or response.status_code in FAILED:
                failure = failure or response
                continue
            response.headers['X-Ws-Group-Member'] = member.name
            return response
        if failure is not None:
            return failure
        code = 'BLOB_UNKNOWN' if op.op == 'blob' else 'MANIFEST_UNKNOWN'
        return _error(404, f'{op.op} unknown to registry', code)

    async def _cached_blob(self, req: FormatRequest, op: p.Operation, members: list[RepoConfig]) -> Response | None:
        """A blob any member already holds (stored or cached), served from the store without asking an upstream."""
        path = f'blobs/{op.reference}'
        for member in members:
            asset = await req.hosted.get(member, path)
            if asset is None:
                continue
            sub = self._member_request(req, member)
            response = req.hosted.serve(sub, asset, headers={'Docker-Content-Digest': op.reference})
            response.headers.update(API_HEADERS)
            response.headers['content-type'] = p.safe_content_type(asset.content_type)
            response.headers['X-Ws-Group-Member'] = member.name
            return response
        return None

    async def _tags(self, req: FormatRequest, op: p.Operation, members: list[RepoConfig]) -> Response:
        started = time.perf_counter()
        query = p.tags_query(req.request.query_params)
        names: set[str] = set()
        answered = False
        for member in members:
            sub = self._member_request(req, member)
            if member.type is RepoType.HOSTED:
                tags = await docker_db.tag_names(req.hosted.db, member.id, op.name, last=None, limit=p.TAGS_MAX_N)
            else:
                try:
                    tags = await self._proxy_tags(sub, op.name)
                except WaystationError:
                    continue  # unknown upstream, upstream down: the other members still answer
            answered = answered or bool(tags)
            names.update(tags)
        if not answered:
            return _error(404, 'repository name not known to registry', 'NAME_UNKNOWN')
        ordered = sorted(names)
        if 'last' in query:
            ordered = [t for t in ordered if t > query['last']]
        limit = int(query['n']) if 'n' in query else p.TAGS_MAX_N
        prefix = client_prefix(req.request.url.path, '/tags/list')
        headers = dict(API_HEADERS)
        if len(ordered) > limit:
            ordered = ordered[:limit]
            if ordered:
                headers['Link'] = f'<{prefix}/tags/list?{urlencode({"n": limit, "last": ordered[-1]})}>; rel="next"'
        req.hosted.account(req, CacheState.HIT, 200, 0, started)
        return JSONResponse({'name': client_name(prefix), 'tags': ordered}, headers=headers)

    async def _referrers(self, req: FormatRequest, op: p.Operation, members: list[RepoConfig]) -> Response:
        merged: dict[str, dict[str, object]] = {}
        filtered = False
        for member in members:
            if member.type is not RepoType.HOSTED:
                continue
            response = await self._serve(self._member_request(req, member), op)
            if response.status_code != 200:
                continue
            filtered = filtered or 'oci-filters-applied' in response.headers
            body = json.loads(bytes(response.body))
            for entry in body.get('manifests', []):
                merged.setdefault(str(entry.get('digest')), entry)
        headers = dict(API_HEADERS)
        if filtered:
            headers['OCI-Filters-Applied'] = 'artifactType'
        index = {'schemaVersion': 2, 'mediaType': p.OCI_INDEX, 'manifests': list(merged.values())}
        return JSONResponse(index, media_type=p.OCI_INDEX, headers=headers)

    async def _write(self, req: FormatRequest, op: p.Operation) -> Response:
        group = req.repo.group
        name = group.writable_member if group is not None else None
        member = await req.repository(name) if name else None
        if member is None or member.type is not RepoType.HOSTED:
            return _error(405, 'this group has no writable member: push to a hosted repository', 'UNSUPPORTED')
        action = Action.DELETE if req.method == 'DELETE' and op.op != 'upload' else Action.WRITE
        if req.method in ('GET', 'HEAD'):
            action = Action.READ  # upload status
        if not req.allows_member(action, member):
            return _error(403, f'{action.value} permission required on {member.name}', 'DENIED')
        if not member.online:
            return _error(503, 'the writable member is offline', 'UNAVAILABLE')
        req.log(group=req.repo.name)
        return await self._serve(self._member_request(req, member), op)
