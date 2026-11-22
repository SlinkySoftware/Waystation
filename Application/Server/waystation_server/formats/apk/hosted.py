"""Hosted apk repositories (T-P6-APK; FORMATS.md "Alpine apk"; SPECIFICATION §4.2).

- **Upload**: ``PUT /repository/<repo>/<branch>/<repository>/<arch>/<file>.apk`` (``curl --upload-file``). The body
  is streamed into the blob store (bounded by ``WAYSTATION_MAX_UPLOAD_GB``, hard quota checked first), then the
  package is read in a thread (``waystation_common.apk.read_package``: v2 gzip segments, ``.PKGINFO`` bounded, the
  data segment checked against ``datahash``). Its architecture must be the directory's (or ``noarch``: it is listed
  with the directory's, as ``apk index --rewrite-arch`` does). Stored as ``<dir>/<pkgname>-<pkgver>.apk`` whatever the
  file name in the URL (apk fetches that name).
- **Write policy**: ``allow_redeploy`` replaces the package, ``disable_redeploy`` refuses an existing one (409),
  ``read_only`` refuses every change (403).
- **Delete**: ``DELETE <dir>/<name>-<version>.apk``.
- **Index**: each upload or delete writes the package's ``apk_packages`` record (with its ``APKINDEX`` stanza) and an
  ``index.apk`` job into the outbox in one MongoDB transaction; a worker rebuilds and signs every directory's
  ``APKINDEX.tar.gz`` with the repository's RSA key (``waystation_worker.apk``), usually within seconds, and publishes
  the public key as ``keys/<name>-<id>.rsa.pub``. Until the first build the index answers 404.

Uploads need ``write``, deletes ``delete`` (the router checked them from the method); both are audited
(``content.upload`` / ``content.delete``) and shown in the activity feed.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse, Response

from waystation_common import apk
from waystation_common.db.client import Session
from waystation_common.errors import Conflict, Forbidden, NotFound, ValidationFailed
from waystation_common.events import CacheState, EventType
from waystation_common.jobs import ApkIndex
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.db import apk as apk_db
from waystation_server.formats.apk.common import details
from waystation_server.formats.base import FormatRequest
from waystation_server.problems import native_error
from waystation_server.repos.model import RepoConfig, StoredHosted, WritePolicy

APK_TYPE = 'application/octet-stream'
MAX_DEPTH = 8


def upload_dir(path: str) -> str:
    """The ``<branch>/<repository>/<arch>`` directory of an upload path (raises ``ValidationFailed``)."""
    directory, _, name = path.rpartition('/')
    parts = directory.split('/') if directory else []
    if not name.endswith('.apk') or len(parts) < 3 or len(parts) > MAX_DEPTH:
        raise ValidationFailed('upload to <branch>/<repository>/<arch>/<name>-<version>.apk')
    if not all(apk.SEGMENT.match(p) and p not in ('.', '..') for p in parts):
        raise ValidationFailed('branch and repository names are letters, digits, ".", "_" and "-"')
    if not apk.ARCH.match(parts[-1]) or parts[-1] == 'noarch':
        raise ValidationFailed('the folder before the file name must be an architecture (x86_64, aarch64...)')
    return directory


def _read(path: Path, size: int) -> apk.PackageInfo:
    try:
        with path.open('rb') as fh:
            return apk.read_package(fh, size)
    except apk.ApkError as exc:
        raise ValidationFailed(f'not a valid apk package: {exc}') from None


class ApkHosted:
    async def handle(self, req: FormatRequest) -> Response:
        method, path = req.method, req.path
        if method in ('GET', 'HEAD'):
            return await self.get(req)
        if method == 'PUT' and path.endswith('.apk'):
            return await self.upload(req)
        if method == 'DELETE' and path.endswith('.apk'):
            return await self.delete(req)
        return native_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD, PUT, DELETE'})

    async def get(self, req: FormatRequest) -> Response:
        asset = await req.hosted.get(req.repo, req.path) if req.path else None
        if asset is not None and req.path.endswith(f'/{apk.INDEX_NAME}'):
            # apk revalidates its cached index with If-Modified-Since = the local time it saved it: an index
            # republished within that same second would look unchanged. Indexes answer by ETag only (always a 200
            # for apk; they are small).
            return req.hosted.serve_generated(req, asset.blob_sha256, asset.size, content_type=asset.content_type)
        if asset is not None:
            return req.hosted.serve(req, asset)
        req.hosted.account(req, CacheState.HIT, 404, 0, time.perf_counter())
        raise NotFound('Not Found')

    @staticmethod
    def _policy(req: FormatRequest) -> StoredHosted:
        hosted = req.repo.hosted or StoredHosted()
        if hosted.write_policy is WritePolicy.READ_ONLY:
            raise Forbidden('this repository is read-only')
        return hosted

    async def upload(self, req: FormatRequest) -> Response:
        hosted = self._policy(req)
        directory = upload_dir(req.path)
        arch = apk.Directory(directory).arch
        length = req.request.headers.get('content-length')
        total = (
            int(length) if length is not None and length.isascii() and length.isdigit() and len(length) <= 20 else None
        )
        await req.hosted.admit(req.repo, total)  # a hard quota refuses before any byte is accepted (507)
        artifact = req.path.rsplit('/', 1)[-1]
        async with await req.hosted.blobs.open_writer(expected_size=total) as writer:
            await req.hosted.receive(req, writer, path=req.path, artifact=artifact, total=total)
            ref = await req.hosted.blobs.commit(writer)
        info = await asyncio.to_thread(_read, req.hosted.blobs.blob_path(ref.sha256), ref.size)
        if info.arch not in (arch, 'noarch'):
            raise ValidationFailed(f'the package is for {info.arch}, not {arch}')
        path = f'{directory}/{apk.file_name(info.name, info.version)}'
        db, repo = req.hosted.db, req.repo
        existing = await apk_db.hosted_by_path(db, repo.id, path)
        if existing is not None and hosted.write_policy is WritePolicy.DISABLE_REDEPLOY:
            raise Conflict(f'{path} exists and this repository does not allow redeploying')
        uploaded = datetime.now(UTC)
        stanza = apk.stanza(info, arch=arch)
        fields = dict(line.split(':', 1) for line in stanza.splitlines() if ':' in line)
        record: dict[str, Any] = {
            'path': path,
            'dir': directory,
            'name': info.name,
            'version': info.version,
            'arch': arch,
            'package_arch': info.arch,
            'checksum': info.checksum,
            'size': info.size,
            'version_sort': apk.sort_string(info.version),
            'stanza': stanza,
            **details(fields),
            'uploaded_at': uploaded,
            'uploaded_by': req.principal.display,
        }
        await req.hosted.record(repo, path, ref, content_type=APK_TYPE)

        async def write(session: Session) -> None:
            await apk_db.save_hosted(db, repo.id, record, session=session)
            await self._reindex(req, repo, session)

        await req.hosted.transaction(write)
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('apk_package', None, f'{repo.name}/{path}'),
            details={
                'repository': repo.name,
                'package': info.name,
                'version': info.version,
                'arch': arch,
                'path': path,
                'size': ref.size,
                'sha256': ref.sha256,
                'replaced': existing is not None,
            },
        )
        req.hosted.activity(req, EventType.ASSET_CREATED, path=path, artifact=path.rsplit('/', 1)[-1], size=ref.size)
        req.hosted.account(req, CacheState.HIT, 201, 0, time.perf_counter())
        headers = {'ETag': f'"sha256:{ref.sha256}"', 'X-Checksum-Sha256': ref.sha256, 'Location': path}
        body = {'saved': True, 'path': path, 'name': info.name, 'version': info.version, 'checksum': info.checksum}
        return JSONResponse(body, status_code=201, headers=headers)

    @staticmethod
    async def _reindex(req: FormatRequest, repo: RepoConfig, session: Session) -> None:
        if req.hosted.jobs is not None:
            await req.hosted.jobs.enqueue_durable(ApkIndex(repo_id=repo.id), session=session)

    async def delete(self, req: FormatRequest) -> Response:
        self._policy(req)
        db, repo = req.hosted.db, req.repo
        found = await apk_db.hosted_by_path(db, repo.id, req.path)
        if found is None:
            raise NotFound('Not Found')

        async def write(session: Session) -> None:
            await apk_db.delete_hosted(db, repo.id, req.path, session=session)
            await self._reindex(req, repo, session)

        await req.hosted.transaction(write)
        removed = await req.hosted.remove(repo, req.path)
        size = removed.size if removed is not None else found.get('size')
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target('apk_package', None, f'{repo.name}/{req.path}'),
            details={
                'repository': repo.name,
                'package': found['name'],
                'version': found['version'],
                'path': req.path,
                'size': size,
            },
        )
        req.hosted.activity(
            req, EventType.ASSET_DELETED, path=req.path, artifact=req.path.rsplit('/', 1)[-1], size=size
        )
        req.hosted.account(req, CacheState.HIT, 204, 0, time.perf_counter())
        return Response(status_code=204)
