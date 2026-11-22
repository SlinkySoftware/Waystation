"""NuGet hosted repositories (T-P6-NUGET; SPECIFICATION §4.2; FORMATS.md "NuGet v3").

- **Push** (``PUT /repository/<repo>/api/v2/package``, ``dotnet nuget push`` / ``nuget push``): the package is the
  first file part of a ``multipart/form-data`` body (or the raw body), streamed into the blob store (a live upload
  transfer, at most ``MAX_PACKAGE_BYTES``, hard quota checked first), then read in a thread (``formats.nuget.package``:
  zip-slip and zip-bomb guards, one root ``.nuspec`` parsed with defusedxml, bounded) and hashed (SHA-512, the
  ``packageHash`` registrations publish). Stored as ``v3-flatcontainer/<id>/<version>/<id>.<version>.nupkg`` (the
  details on the asset, ``assets.nuget``) and its ``<id>.nuspec`` next to it. Authentication: ``X-NuGet-ApiKey`` = an
  API token (``security.authn``), or basic credentials. Write policy: ``read_only`` refuses (403),
  ``disable_redeploy`` answers an existing version 409 (``--skip-duplicate`` understands it), ``allow_redeploy``
  replaces it.
- **Delete** (``DELETE api/v2/package/<id>/<version>``, ``dotnet nuget delete``): by the ``delete_mode`` option,
  unlists the version (default, as nuget.org: hidden from search and from registrations' listed versions, still
  restorable by exact version) or removes both files. ``POST`` on the same path relists. Unlisting and relisting are
  also in the REST API (``browse``).
- **Reading**: the version list, registration (one inlined page; leaves too), search and package files, rendered per
  request from the assets (``ETag``, ``304``, ``HEAD``); files are served by X-Accel.

Pushes need ``write`` and deletes ``delete`` (the router checked them from the method); both are audited and shown in
the activity feed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.responses import Response

from waystation_common import nuget
from waystation_common.db.client import Database
from waystation_common.errors import (
    Conflict,
    Forbidden,
    InsufficientStorage,
    NotFound,
    PayloadTooLarge,
    ValidationFailed,
    WaystationError,
)
from waystation_common.events import CacheState, EventType
from waystation_common.storage import BlobWriter
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.db import nuget as nuget_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.common.multipart import MultipartReader, boundary_of
from waystation_server.formats.nuget import package, protocol
from waystation_server.formats.nuget.common import SAFE_HEADERS, generated, hosted_version, latest_of, nuget_error
from waystation_server.formats.nuget.descriptor import NugetOptions
from waystation_server.formats.nuget.protocol import HostedVersion, Op, Route, SearchQuery
from waystation_server.repos.model import RepoConfig, StoredHosted, WritePolicy
from waystation_server.repos.quota import QuotaExceeded

MAX_PACKAGE_BYTES = 512 * 1024 * 1024
HASH_CHUNK = 1024 * 1024


def _inspect(path: Path) -> tuple[package.Nuspec, bytes, str]:
    """The package's nuspec (parsed and raw) and SHA-512 (hex) (blocking: run in a thread)."""
    with path.open('rb') as file:
        raw = package.read_nuspec_from(file)
        file.seek(0)
        digest = hashlib.sha512()
        while chunk := file.read(HASH_CHUNK):
            digest.update(chunk)
    return package.parse_nuspec(raw), raw, digest.hexdigest()


@dataclass(frozen=True)
class Pushed:
    spec: package.Nuspec
    path: str
    size: int
    replaced: bool


class NugetHosted:
    # ------------------------------------------------------------------ reading
    async def versions(self, req: FormatRequest, repo: RepoConfig, package_id: str) -> list[HostedVersion]:
        return await stored_versions(req.hosted.db, repo.id, package_id)

    async def serve(self, req: FormatRequest, route: Route) -> Response:
        started = time.perf_counter()

        def account(status: int, sent: int) -> None:
            req.hosted.account(req, CacheState.HIT, status, sent, started)

        def missing() -> NotFound:
            account(404, 0)
            return NotFound('Not Found')

        match route.op:
            case Op.SERVICE_INDEX:
                return generated(req, protocol.service_index(req.repo_url, publish=True), account=account)
            case Op.FLAT_INDEX:
                found = await self.versions(req, req.repo, route.id)
                if not found:
                    raise missing()
                return generated(req, protocol.render_versions(v.flat for v in found), account=account)
            case Op.NUPKG | Op.NUSPEC:
                asset = await req.hosted.get(req.repo, route.key)
                if asset is None:
                    raise missing()
                return req.hosted.serve(req, asset, headers=SAFE_HEADERS)
            case Op.REGISTRATION:
                found = await self.versions(req, req.repo, route.id)
                if not found:
                    raise missing()
                body = await asyncio.to_thread(protocol.render_registration, req.repo_url, found[0].id, found)
                return generated(req, body, account=account)
            case Op.REGISTRATION_PART:
                leaf = await self.leaf(req, req.repo, route)
                if leaf is None:
                    raise missing()
                return generated(req, protocol.render_leaf(req.repo_url, leaf), account=account)
            case Op.SEARCH:
                query = SearchQuery.from_params(req.request.query_params)
                doc = await self.search(req, req.repo, query, req.repo_url, window=True)
                return generated(req, protocol.dump(doc), account=account)
        raise missing()

    async def leaf(self, req: FormatRequest, repo: RepoConfig, route: Route) -> HostedVersion | None:
        """A registration leaf path (``<version>.json``) of a hosted package."""
        version = nuget.flat_version(route.rest.removesuffix('.json')) if '/' not in route.rest else None
        if version is None:
            return None
        return next((v for v in await self.versions(req, repo, route.id) if v.flat == version), None)

    async def search(
        self, req: FormatRequest, repo: RepoConfig, query: SearchQuery, repo_url: str, *, window: bool
    ) -> dict[str, Any]:
        """Local search over a hosted repository's packages (listed, visible versions). ``window``: apply
        ``skip``/``take``; else the first ``skip + take`` hits (a group windows the merge)."""
        summaries = await nuget_db.scan(req.hosted.db, repo.id)
        matched = [s for s in summaries if query.matches(str(s.get('display_name') or s['name']), s)]
        start = query.skip if window else 0
        hits: list[dict[str, Any]] = []
        skipped = 0
        for summary in matched:
            if len(hits) >= query.take + (0 if window else query.skip):
                break
            versions = [
                v for v in await self.versions(req, repo, str(summary['name'])) if v.listed and query.visible(v.version)
            ]
            if not versions:
                continue
            if skipped < start:
                skipped += 1
                continue
            hits.append(protocol.search_hit(repo_url, versions))
        return {'totalHits': len(matched), 'data': hits}

    # ------------------------------------------------------------------ writing
    @staticmethod
    def _policy(req: FormatRequest) -> StoredHosted:
        hosted = req.repo.hosted or StoredHosted()
        if hosted.write_policy is WritePolicy.READ_ONLY:
            raise Forbidden('this repository is read-only')
        return hosted

    async def handle_write(self, req: FormatRequest, route: Route) -> Response:
        started = time.perf_counter()
        try:
            if route.op is Op.PUBLISH and req.method == 'PUT':
                response = await self.push(req)
            elif route.op is Op.PACKAGE and req.method == 'DELETE':
                response = await self.delete(req, route)
            elif route.op is Op.PACKAGE and req.method == 'POST':
                await self.set_listed(req, req.repo, route.id, route.version, listed=True)
                response = Response(status_code=200, headers={'Content-Length': '0'})
            else:
                allow = 'PUT' if route.op is Op.PUBLISH else 'DELETE, POST'
                response = nuget_error(405, 'Method Not Allowed', headers={'Allow': allow})
        except QuotaExceeded as exc:  # a hard repository quota (SPECIFICATION §4.8)
            response = nuget_error(403, exc.detail)
        except InsufficientStorage:  # the low-disk guard
            response = nuget_error(507, 'not enough free space in the blob store')
        except WaystationError as exc:
            response = nuget_error(exc.status, exc.detail)
        req.hosted.account(req, CacheState.HIT, response.status_code, 0, started)
        return response

    async def push(self, req: FormatRequest) -> Response:
        hosted = self._policy(req)
        length = req.request.headers.get('content-length')
        total = int(length) if length is not None and length.isdigit() else None
        if total is not None and total > MAX_PACKAGE_BYTES + 65536:
            raise PayloadTooLarge('the package is too large')
        await req.hosted.admit(req.repo, total)
        writer = await req.hosted.blobs.open_writer(max_size=MAX_PACKAGE_BYTES)
        try:
            await self._receive(req, writer)
            await writer.flush()
            spec, raw_nuspec, sha512 = await asyncio.to_thread(_inspect, writer.path)
            pushed = await self._store(req, hosted, writer, spec, raw_nuspec, sha512)
        finally:
            if not writer.closed:
                await writer.abort()
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('nuget_package', None, f'{req.repo.name}/{pushed.spec.id}@{pushed.spec.version}'),
            details={
                'repository': req.repo.name,
                'package': pushed.spec.id,
                'version': pushed.spec.version,
                'size': pushed.size,
                'replaced': pushed.replaced,
            },
        )
        filename = pushed.path.rsplit('/', 1)[-1]
        req.hosted.activity(req, EventType.ASSET_CREATED, path=pushed.path, artifact=filename, size=pushed.size)
        req.log(package=pushed.spec.id.lower())
        return Response(status_code=201, headers={'Content-Length': '0', 'Location': pushed.path})

    async def _receive(self, req: FormatRequest, writer: BlobWriter) -> None:
        content_type = req.request.headers.get('content-type') or ''

        if not content_type.lower().startswith('multipart/'):
            await req.hosted.receive(req, writer, path=protocol.PUBLISH, artifact='package.nupkg')
            return
        reader = MultipartReader(req.request.stream(), boundary_of(content_type))
        received = False
        async for part in reader.parts():
            if part.filename is not None and not received:
                await req.hosted.receive(
                    req, writer, path=protocol.PUBLISH, artifact='package.nupkg', body=reader.chunks()
                )
                received = True
            else:
                await reader.drain()
        if not received:
            raise ValidationFailed('the push has no package file')

    async def _store(
        self,
        req: FormatRequest,
        hosted: StoredHosted,
        writer: BlobWriter,
        spec: package.Nuspec,
        raw_nuspec: bytes,
        sha512: str,
    ) -> Pushed:
        repo = req.repo
        path = protocol.nupkg_path(spec.id, spec.flat_version)
        existing = await req.hosted.get(repo, path)
        if existing is not None and hosted.write_policy is WritePolicy.DISABLE_REDEPLOY:
            raise Conflict(f'{spec.id} {spec.version} already exists and this repository does not allow redeploying')
        others = await self.versions(req, repo, spec.id)
        if others and others[0].id.lower() == spec.id.lower() and others[0].id != spec.id:
            spec = dataclasses.replace(spec, id=others[0].id)  # the first push decides how the id is spelled
        ref = await req.hosted.blobs.commit(writer)
        await req.hosted.record(repo, path, ref, content_type=protocol.NUPKG_TYPE)
        async with await req.hosted.blobs.open_writer(expected_size=len(raw_nuspec)) as nuspec_writer:
            await nuspec_writer.write(raw_nuspec)
            nuspec_ref = await req.hosted.blobs.commit(nuspec_writer)
        await req.hosted.record(
            repo, protocol.nuspec_path(spec.id, spec.flat_version), nuspec_ref, content_type=protocol.NUSPEC_TYPE
        )
        details = {
            **spec.details(),
            'version_sort': nuget.sort_string(spec.version),
            'listed': True,
            'published': datetime.now(UTC),
            'uploaded_by': req.principal.display,
            'sha512': sha512,
        }
        await nuget_db.set_details(req.hosted.db, repo.id, path, details)
        await self.summarise(req, repo, spec.id)
        return Pushed(spec, path, ref.size, existing is not None)

    async def delete(self, req: FormatRequest, route: Route) -> Response:
        self._policy(req)
        options = NugetOptions.model_validate(req.repo.format_options or {})
        if options.delete_mode == 'unlist':
            await self.set_listed(req, req.repo, route.id, route.version, listed=False)
            return Response(status_code=204)
        repo = req.repo
        path = protocol.nupkg_path(route.id, route.version)
        removed = await req.hosted.remove(repo, path)
        if removed is None:
            raise NotFound(f'{route.id} {route.version} is not in this repository')
        await req.hosted.remove(repo, protocol.nuspec_path(route.id, route.version))
        await self.summarise(req, repo, route.id)
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target('nuget_package', None, f'{repo.name}/{route.id}@{route.version}'),
            details={'repository': repo.name, 'package': route.id, 'version': route.version, 'size': removed.size},
        )
        req.hosted.activity(
            req, EventType.ASSET_DELETED, path=path, artifact=path.rsplit('/', 1)[-1], size=removed.size
        )
        return Response(status_code=204)

    async def set_listed(
        self, req: FormatRequest, repo: RepoConfig, package_id: str, version: str, *, listed: bool
    ) -> str:
        """Unlist or relist a version (audited); returns its flat version. ``NotFound`` when there is none."""
        return await change_listing(req.hosted.db, repo, package_id, version, listed=listed)

    async def summarise(self, req: FormatRequest, repo: RepoConfig, package_id: str) -> None:
        """Refresh the package's ``nuget_packages`` summary from its stored versions (removed when none is left)."""
        await summarise(req.hosted.db, repo.id, package_id)


def summary_fields(found: Sequence[HostedVersion]) -> dict[str, Any]:
    listed = [v for v in found if v.listed] or list(found)
    latest_version = latest_of([v.version for v in listed])
    latest = next((v for v in listed if v.version == latest_version), listed[-1])
    d = latest.details
    return {
        'display_name': latest.id,
        'latest': latest.version,
        'version_count': len(found),
        'description': (str(d.get('description') or '')[:1024]) or None,
        'authors': d.get('authors'),
        'title': d.get('title'),
        'tags': list(d.get('tags') or ())[:50],
    }


async def stored_versions(db: Database, repo_id: str, package_id: str) -> list[HostedVersion]:
    """A hosted repository's versions of a package (its ``.nupkg`` assets)."""
    docs = await nuget_db.package_files(db, repo_id, package_id)
    return [hosted_version(d) for d in docs if str(d['path']).endswith('.nupkg')]


async def summarise(db: Database, repo_id: str, package_id: str) -> None:
    """Refresh the package's ``nuget_packages`` summary from its stored versions (removed when none is left)."""
    found = await stored_versions(db, repo_id, package_id)
    name = package_id.lower()
    if not found:
        await nuget_db.delete_package(db, repo_id, name)
        return
    await nuget_db.upsert_package(db, repo_id, name, summary_fields(found))


async def change_listing(db: Database, repo: RepoConfig, package_id: str, version: str, *, listed: bool) -> str:
    """Unlist or relist a hosted version (audited); returns its flat version. ``NotFound`` when there is none,
    ``Forbidden`` for a read-only repository."""
    if (repo.hosted or StoredHosted()).write_policy is WritePolicy.READ_ONLY:
        raise Forbidden('this repository is read-only')
    flat = nuget.flat_version(version)
    path = protocol.nupkg_path(package_id, flat) if flat else ''
    if not flat or not await nuget_db.set_listed(db, repo.id, path, listed):
        raise NotFound(f'{package_id} {version} is not in this repository')
    await summarise(db, repo.id, package_id)
    audit.record(
        AuditAction.CONTENT_UPDATE,
        target=audit.target('nuget_package', None, f'{repo.name}/{package_id.lower()}@{flat}'),
        details={'repository': repo.name, 'package': package_id.lower(), 'version': flat, 'listed': listed},
    )
    return flat
