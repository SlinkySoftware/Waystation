"""Hosted conda channels (T-P6-CONDA; FORMATS.md "Conda"; SPECIFICATION §4.2).

- **Upload**: ``PUT /repository/<repo>/<subdir>/<name>-<version>-<build>.conda|.tar.bz2`` (``curl --upload-file``).
  The body is streamed into the blob store (hard quota checked first), then read safely in a thread
  (``formats.conda.package``: bounded, nothing extracted); ``info/index.json`` must name the same package, version,
  build and subdir as the path. Write policy: ``allow_redeploy`` replaces a file, ``disable_redeploy`` refuses other
  bytes for an existing one (409; the same bytes: 200), ``read_only`` refuses every change (403).
- **Delete**: ``DELETE <subdir>/<file>``.
- **Index**: each upload or delete writes the file's ``conda_packages`` record and an ``index.conda`` job into the
  outbox in one MongoDB transaction; the worker regenerates the repodata (``waystation_worker.conda``). Until a subdir
  has published repodata, ``<subdir>/repodata.json`` answers an empty listing (conda needs ``noarch`` to exist) and
  ``channeldata.json`` an empty channel.

Uploads need ``write``, deletes ``delete`` (the router checked them from the method); both are audited
(``content.upload`` / ``content.delete``, ``conda_package``) and shown in the activity feed.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse, Response

from waystation_common.conda import PackageFile, sort_string
from waystation_common.conda import repodata as repodata_mod
from waystation_common.db import conda as conda_db
from waystation_common.db.client import Session
from waystation_common.errors import Conflict, Forbidden, NotFound, ValidationFailed
from waystation_common.events import CacheState, EventType
from waystation_common.jobs import CondaIndex
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.conda.common import (
    JSON_TYPE,
    PACKAGE_TYPE,
    REPODATA,
    Op,
    Route,
    generated,
)
from waystation_server.formats.conda.package import read_package
from waystation_server.problems import native_error
from waystation_server.repos.model import RepoConfig, StoredHosted, WritePolicy


class CondaHosted:
    async def handle(self, req: FormatRequest, route: Route) -> Response:
        method = req.method
        if method in ('GET', 'HEAD'):
            return await self.get(req, route)
        if route.op is Op.PACKAGE and route.package is not None:
            if method == 'PUT':
                return await self.upload(req, route.subdir, route.package)
            if method == 'DELETE':
                return await self.delete(req, route.subdir, route.package)
        return native_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD, PUT, DELETE'})

    # ------------------------------------------------------------------ reading
    async def get(self, req: FormatRequest, route: Route) -> Response:
        if route.op is Op.UNSUPPORTED:
            raise NotFound('Not Found')
        asset = await req.hosted.get(req.repo, req.path)
        if asset is not None:
            return req.hosted.serve(req, asset)
        started = time.perf_counter()

        def account(status: int, sent: int) -> None:
            req.hosted.account(req, CacheState.HIT, status, sent, started)

        if route.op is Op.REPODATA and route.name == REPODATA:  # nothing published for this subdir (yet)
            return generated(req, repodata_mod.empty(route.subdir), JSON_TYPE, account=account)
        if route.op is Op.CHANNELDATA:
            return generated(req, repodata_mod.channeldata({}, []), JSON_TYPE, account=account)
        account(404, 0)
        raise NotFound('Not Found')

    # ------------------------------------------------------------------ writing
    @staticmethod
    def _policy(req: FormatRequest) -> StoredHosted:
        hosted = req.repo.hosted or StoredHosted()
        if hosted.write_policy is WritePolicy.READ_ONLY:
            raise Forbidden('this repository is read-only')
        return hosted

    async def upload(self, req: FormatRequest, subdir: str, package: PackageFile) -> Response:
        hosted = self._policy(req)
        length = req.request.headers.get('content-length')
        total = int(length) if length is not None and length.isdigit() else None
        await req.hosted.admit(req.repo, total)  # a hard quota refuses before any byte is accepted (507)
        filename = package.filename
        async with await req.hosted.blobs.open_writer(expected_size=total) as writer:
            await req.hosted.receive(
                req,
                writer,
                path=req.path,
                artifact=filename,
                component=f'{package.name} {package.version}',
                total=total,
            )
            ref = await req.hosted.blobs.commit(writer)
        if ref.size == 0:
            raise ValidationFailed('the upload is empty')
        meta = await asyncio.to_thread(read_package, req.hosted.blobs.blob_path(ref.sha256), package.suffix)
        if (meta.name, meta.version, meta.build) != (package.name, package.version, package.build):
            raise ValidationFailed(
                f'the file name does not match info/index.json ({meta.name}-{meta.version}-{meta.build})'
            )
        if meta.subdir != subdir:
            raise ValidationFailed(f'the package is for the subdir {meta.subdir}, not {subdir}')
        db, repo = req.hosted.db, req.repo
        existing = await conda_db.get_hosted(db, repo.id, subdir, filename)
        if existing is not None and hosted.write_policy is WritePolicy.DISABLE_REDEPLOY:
            if existing.get('sha256') == ref.sha256:
                req.hosted.account(req, CacheState.HIT, 200, 0, time.perf_counter())
                return JSONResponse({'saved': True, 'unchanged': True}, status_code=200)
            raise Conflict(f'{subdir}/{filename} exists and this repository does not allow redeploying')
        md5 = await asyncio.to_thread(_md5, req.hosted.blobs.blob_path(ref.sha256))
        entry = {**meta.entry, 'md5': md5, 'sha256': ref.sha256, 'size': ref.size}
        now = datetime.now(UTC)
        record: dict[str, Any] = {
            'subdir': subdir,
            'filename': filename,
            'path': f'{subdir}/{filename}',
            'name': meta.name,
            'version': meta.version,
            'build': meta.build,
            'build_number': meta.build_number,
            'version_sort': sort_string(meta.version),
            'sha256': ref.sha256,
            'md5': md5,
            'size': ref.size,
            'timestamp': now,
            'verified': True,
            'source': 'hosted',
            'entry': entry,
            'uploaded_by': req.principal.display,
            'created_at': now,
        }
        await req.hosted.record(repo, f'{subdir}/{filename}', ref, content_type=PACKAGE_TYPE)

        async def write(session: Session) -> None:
            await conda_db.save_hosted(db, repo.id, record, session=session)
            await self._reindex(req, repo, session)

        await req.hosted.transaction(write)
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('conda_package', None, f'{repo.name}/{subdir}/{filename}'),
            details={
                'repository': repo.name,
                'subdir': subdir,
                'package': meta.name,
                'version': meta.version,
                'build': meta.build,
                'size': ref.size,
                'sha256': ref.sha256,
                'replaced': existing is not None,
            },
        )
        req.hosted.activity(req, EventType.ASSET_CREATED, path=f'{subdir}/{filename}', artifact=filename, size=ref.size)
        req.hosted.account(req, CacheState.HIT, 201, 0, time.perf_counter())
        headers = {'ETag': f'"sha256:{ref.sha256}"', 'X-Checksum-Sha256': ref.sha256}
        return JSONResponse({'saved': True}, status_code=201, headers=headers)

    @staticmethod
    async def _reindex(req: FormatRequest, repo: RepoConfig, session: Session) -> None:
        if req.hosted.jobs is not None:
            await req.hosted.jobs.enqueue_durable(CondaIndex(repo_id=repo.id), session=session)

    async def delete(self, req: FormatRequest, subdir: str, package: PackageFile) -> Response:
        self._policy(req)
        db, repo, filename = req.hosted.db, req.repo, package.filename
        found = await conda_db.get_hosted(db, repo.id, subdir, filename)
        if found is None:
            raise NotFound('Not Found')

        async def write(session: Session) -> None:
            await conda_db.delete_hosted(db, repo.id, subdir, filename, session=session)
            await self._reindex(req, repo, session)

        await req.hosted.transaction(write)
        removed = await req.hosted.remove(repo, f'{subdir}/{filename}')
        size = removed.size if removed is not None else found.get('size')
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target('conda_package', None, f'{repo.name}/{subdir}/{filename}'),
            details={'repository': repo.name, 'subdir': subdir, 'package': package.name, 'size': size},
        )
        req.hosted.activity(req, EventType.ASSET_DELETED, path=f'{subdir}/{filename}', artifact=filename, size=size)
        req.hosted.account(req, CacheState.HIT, 204, 0, time.perf_counter())
        return Response(status_code=204)


def _md5(path: Path) -> str:

    digest = hashlib.md5(usedforsecurity=False)
    with path.open('rb') as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
