"""Composer hosted repositories (T-P6-COMPOSER; SPECIFICATION §4.2; FORMATS.md "Composer").

- **Upload** (``PUT /repository/<repo>/packages/upload/<vendor>/<name>/<version>``, the raw zip as the body, e.g.
  ``curl --upload-file``; the same endpoint as Nexus's Composer plugin): streamed into the blob store (a live upload
  transfer, at most ``MAX_PACKAGE_BYTES``, hard quota checked first), then read in a thread
  (``formats.composer.archive``: zip-slip and zip-bomb guards, ``composer.json`` at the root or in the single
  top-level folder, bounded, its ``name`` and ``version`` (when given) must be the uploaded ones) and hashed (SHA-1:
  the ``shasum`` and ``reference`` clients see). The version is normalised as Composer does
  (``waystation_common.composer``). Stored as ``dists/<vendor>/<name>/<version>/<name>-<version>.zip`` with the
  package's details on the asset (``assets.composer``). Write policy: ``read_only`` refuses (403),
  ``disable_redeploy`` answers an existing version 409, ``allow_redeploy`` replaces it.
- **Delete** (``DELETE`` on the same path): removes the version.
- **Reading**: ``packages.json`` (with ``available-packages``), ``p2`` documents rendered per request from the assets
  (``ETag``, ``304``, ``HEAD``; tagged versions and ``~dev`` branches apart, newest first); archives by X-Accel.

Uploads need ``write`` and deletes ``delete`` (the router checked them from the method); both are audited and shown
in the activity feed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.responses import Response

from waystation_common import composer
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
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.db import composer as composer_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.composer import archive, protocol
from waystation_server.formats.composer.common import SAFE_HEADERS, composer_error, generated
from waystation_server.formats.composer.protocol import Op, Route
from waystation_server.repos.model import RepoConfig, StoredHosted, WritePolicy
from waystation_server.repos.quota import QuotaExceeded

MAX_PACKAGE_BYTES = 256 * 1024 * 1024
HASH_CHUNK = 1024 * 1024
ZIP_TYPE = protocol.ARCHIVE_TYPES['zip']


def _inspect(path: Path) -> tuple[dict[str, Any], str]:
    """The archive's composer.json and SHA-1 (hex) (blocking: run in a thread)."""
    with path.open('rb') as file:
        doc = archive.read_composer_json(file)
        file.seek(0)
        digest = hashlib.sha1(usedforsecurity=False)
        while chunk := file.read(HASH_CHUNK):
            digest.update(chunk)
    return doc, digest.hexdigest()


def _package(text: Any) -> dict[str, Any]:
    try:
        found = json.loads(text) if isinstance(text, str) else {}
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def entry_of(doc: Mapping[str, Any], repo_url: str) -> dict[str, Any]:
    """A hosted archive's ``p2`` entry (from its asset with ``composer`` details)."""
    details: Mapping[str, Any] = doc.get('composer') or {}
    sha1 = str(details.get('sha1') or '')
    published = details.get('published')
    entry: dict[str, Any] = {
        **_package(details.get('package_json')),
        'name': details.get('name'),
        'version': details.get('version'),
        'version_normalized': details.get('version_normalized'),
        'dist': {'type': 'zip', 'url': repo_url + str(doc['path']), 'reference': sha1, 'shasum': sha1},
    }
    if isinstance(published, datetime):
        entry['time'] = published.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    return entry


async def stored_entries(db: Database, repo: RepoConfig, name: str, repo_url: str) -> list[dict[str, Any]]:
    """A hosted repository's entries of a package (tagged and branches), newest first."""
    docs = await composer_db.package_files(db, repo.id, name)
    return protocol.newest_first(entry_of(d, repo_url) for d in docs)


def split(entries: list[dict[str, Any]], *, dev: bool) -> list[dict[str, Any]]:
    """The entries of the tagged (``dev=False``) or ``~dev`` document."""
    return [e for e in entries if composer.is_dev(str(e['version_normalized'])) == dev]


async def summarise(db: Database, repo: RepoConfig, name: str) -> None:
    """Refresh the package's ``composer_packages`` summary from its stored versions (removed when none is left)."""
    docs = await composer_db.package_files(db, repo.id, name)
    if not docs:
        await composer_db.delete_package(db, repo.id, name)
        return
    entries = protocol.newest_first(entry_of(d, '') for d in docs)
    by_path = {str(d['path']): d for d in docs}
    latest = composer.newest(str(e['version_normalized']) for e in entries)
    newest = next((e for e in entries if e['version_normalized'] == latest), entries[0])
    versions = []
    for entry in entries:
        path = str(entry['dist']['url'])
        details = by_path[path].get('composer') or {}
        versions.append(
            {
                'version': entry['version'],
                'normalized': entry['version_normalized'],
                'reference': entry['dist']['reference'],
                'time': entry.get('time'),
                'path': path,
                'uploaded_by': details.get('uploaded_by'),
            }
        )
    description = newest.get('description')
    kind = newest.get('type')
    await composer_db.upsert_package(
        db,
        repo.id,
        name,
        {
            'versions': versions,
            'version_count': len(entries),
            'latest': newest.get('version'),
            'description': description[:1024] if isinstance(description, str) else None,
            'type': kind[:100] if isinstance(kind, str) else None,
        },
    )


class ComposerHosted:
    # ------------------------------------------------------------------ reading
    async def entries(self, req: FormatRequest, repo: RepoConfig, name: str, repo_url: str) -> list[dict[str, Any]]:
        return await stored_entries(req.hosted.db, repo, name, repo_url)

    async def names(self, req: FormatRequest, repo: RepoConfig) -> list[str] | None:
        return await composer_db.names(req.hosted.db, repo.id)

    async def serve(self, req: FormatRequest, route: Route) -> Response:
        started = time.perf_counter()

        def account(status: int, sent: int) -> None:
            req.hosted.account(req, CacheState.HIT, status, sent, started)

        def missing() -> NotFound:
            account(404, 0)
            return NotFound('Not Found')

        match route.op:
            case Op.ROOT:
                names = await self.names(req, req.repo)
                return generated(req, protocol.render_root(req.repo_url, available=names), account=account)
            case Op.P2:
                found = split(await self.entries(req, req.repo, route.name, req.repo_url), dev=route.branches)
                if not found:
                    raise missing()
                body = await asyncio.to_thread(protocol.render_p2, route.name, found)
                return generated(req, body, account=account)
            case Op.DIST:
                asset = await req.hosted.get(req.repo, route.key)
                if asset is None:
                    raise missing()
                return req.hosted.serve(req, asset, headers=SAFE_HEADERS)
        raise missing()

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
            if req.method == 'PUT':
                response = await self.upload(req, route)
            elif req.method == 'DELETE':
                response = await self.delete(req, route)
            else:
                response = composer_error(405, 'Method Not Allowed', headers={'Allow': 'PUT, DELETE'})
        except QuotaExceeded as exc:  # a hard repository quota (SPECIFICATION §4.8)
            response = composer_error(403, exc.detail)
        except InsufficientStorage:  # the low-disk guard
            response = composer_error(507, 'not enough free space in the blob store')
        except WaystationError as exc:
            response = composer_error(exc.status, exc.detail)
        req.hosted.account(req, CacheState.HIT, response.status_code, 0, started)
        return response

    @staticmethod
    def _version(route: Route) -> str:
        normalised = composer.normalise(route.version)
        if normalised is None:
            raise ValidationFailed(f'{route.version[:100]!r} is not a Composer version (1.2.3, v2.0.0-beta1, dev-main)')
        return normalised

    async def upload(self, req: FormatRequest, route: Route) -> Response:
        hosted = self._policy(req)
        normalised = self._version(route)
        segment = composer.path_segment(normalised)
        path = protocol.dist_path(route.name, segment, protocol.hosted_file(route.name, segment))
        existing = await req.hosted.get(req.repo, path)
        if existing is not None and hosted.write_policy is WritePolicy.DISABLE_REDEPLOY:
            raise Conflict(
                f'{route.name} {route.version} already exists and this repository does not allow redeploying'
            )
        length = req.request.headers.get('content-length')
        total = int(length) if length is not None and length.isdigit() else None
        if total is not None and total > MAX_PACKAGE_BYTES:
            raise PayloadTooLarge('the package is too large')
        await req.hosted.admit(req.repo, total)
        writer = await req.hosted.blobs.open_writer(max_size=MAX_PACKAGE_BYTES)
        try:
            await req.hosted.receive(req, writer, path=path, artifact=path.rsplit('/', 1)[-1], component=route.name)
            await writer.flush()
            if writer.size == 0:
                raise ValidationFailed('the upload is empty: send the package zip as the request body')
            doc, sha1 = await asyncio.to_thread(_inspect, writer.path)
            package = archive.package_details(doc, route.name, route.version)
            ref = await req.hosted.blobs.commit(writer)
        finally:
            if not writer.closed:
                await writer.abort()
        await req.hosted.record(req.repo, path, ref, content_type=ZIP_TYPE)
        details = {
            'name': route.name,
            'version': route.version.strip(),
            'version_normalized': normalised,
            'version_sort': composer.sort_string(normalised),
            'package_json': json.dumps(package),  # a string: composer.json keys may hold dots or start with $
            'sha1': sha1,
            'published': datetime.now(UTC),
            'uploaded_by': req.principal.display,
        }
        await composer_db.set_details(req.hosted.db, req.repo.id, path, details)
        await summarise(req.hosted.db, req.repo, route.name)
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('composer_package', None, f'{req.repo.name}/{route.name}@{normalised}'),
            details={
                'repository': req.repo.name,
                'package': route.name,
                'version': route.version,
                'size': ref.size,
                'replaced': existing is not None,
            },
        )
        req.hosted.activity(req, EventType.ASSET_CREATED, path=path, artifact=path.rsplit('/', 1)[-1], size=ref.size)
        req.log(package=route.name)
        return Response(status_code=201, headers={'Content-Length': '0', 'Location': path})

    async def delete(self, req: FormatRequest, route: Route) -> Response:
        self._policy(req)
        segment = composer.path_segment(self._version(route))
        path = protocol.dist_path(route.name, segment, protocol.hosted_file(route.name, segment))
        removed = await req.hosted.remove(req.repo, path)
        if removed is None:
            raise NotFound(f'{route.name} {route.version} is not in this repository')
        await summarise(req.hosted.db, req.repo, route.name)
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target('composer_package', None, f'{req.repo.name}/{route.name}@{segment}'),
            details={
                'repository': req.repo.name,
                'package': route.name,
                'version': route.version,
                'size': removed.size,
            },
        )
        req.hosted.activity(
            req, EventType.ASSET_DELETED, path=path, artifact=path.rsplit('/', 1)[-1], size=removed.size
        )
        return Response(status_code=204)
