"""Hosted apt repositories (T-P5-APTH; FORMATS.md "apt"; SPECIFICATION §4.2).

- **Upload**: ``PUT /repository/<repo>/[<anything>.deb]`` (``curl --upload-file``) or ``POST /repository/<repo>/``
  with the package as the body (Nexus' ``--data-binary``) or as the file part of a ``multipart/form-data`` form
  (``curl -F``). ``?component=`` picks one of the repository's ``components`` (default: the first). The body is
  streamed into the blob store (bounded by ``WAYSTATION_MAX_UPLOAD_GB``; the hard quota is checked first), then the
  package is read in a thread (``formats.apt.deb``: ``ar`` layout, bounded control-archive decompression, tar names
  checked, one control paragraph with valid ``Package``/``Version``/``Architecture``) and stored at its pool path
  ``pool/<component>/<prefix>/<source>/<name>_<version>_<arch>.deb``, whatever the request path was.
- **Write policy**: ``allow_redeploy`` replaces the package at its path, ``disable_redeploy`` refuses an existing one
  (409), ``read_only`` refuses every change (403). The same package version and architecture in another component is
  refused (409) whatever the policy: apt would see it twice.
- **Delete**: ``DELETE <pool path>``.
- **Indexes**: each upload or delete writes the package's ``apt_hosted_packages`` record (with its ``Packages``
  paragraph, rendered at upload) and an ``index.apt`` job into the outbox in one MongoDB transaction; a worker
  rebuilds ``dists/<distribution>/`` and signs it with the repository's key (``waystation_worker.apt_index``), usually
  within seconds. The public key is served as ``public.asc`` (armored) and ``public.gpg`` (binary). Until the first
  build ``dists/<distribution>/InRelease`` answers 404.

Uploads need ``write``, deletes ``delete`` (the router checked them from the method); both are audited
(``content.upload`` / ``content.delete``) and shown in the activity feed. Everything else is served from the stored
assets by X-Accel. Groups are not supported for apt.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from starlette.responses import JSONResponse, Response

from waystation_common.db import apt_hosted as apt_db
from waystation_common.db.client import Session
from waystation_common.errors import Conflict, Forbidden, NotFound, ValidationFailed
from waystation_common.events import CacheState, EventType
from waystation_common.jobs import AptIndex
from waystation_common.storage import BlobRef
from waystation_common.versionsort import version_sort
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.formats.apt import deb
from waystation_server.formats.apt.descriptor import AptOptions
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.common.multipart import MultipartReader, boundary_of
from waystation_server.problems import native_error
from waystation_server.repos.model import RepoConfig, StoredHosted, WritePolicy

DEB_TYPE = 'application/vnd.debian.binary-package'


class AptHosted:
    async def handle(self, req: FormatRequest) -> Response:
        method, path = req.method, req.path
        if method in ('GET', 'HEAD'):
            return await self.get(req)
        if method == 'PUT' and (path == '' or path.endswith('.deb')):
            return await self.upload(req)
        if method == 'POST' and path == '':
            return await self.upload(req)
        if method == 'DELETE' and path.startswith('pool/') and path.endswith('.deb'):
            return await self.delete(req)
        return native_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD, PUT, POST, DELETE'})

    # ------------------------------------------------------------------ reading
    async def get(self, req: FormatRequest) -> Response:
        asset = await req.hosted.get(req.repo, req.path) if req.path else None
        if asset is not None:
            return req.hosted.serve(req, asset)
        req.hosted.account(req, CacheState.HIT, 404, 0, time.perf_counter())
        raise NotFound('Not Found')

    # ------------------------------------------------------------------ writing
    @staticmethod
    def _policy(req: FormatRequest) -> StoredHosted:
        hosted = req.repo.hosted or StoredHosted()
        if hosted.write_policy is WritePolicy.READ_ONLY:
            raise Forbidden('this repository is read-only')
        return hosted

    @staticmethod
    def _component(req: FormatRequest, opts: AptOptions) -> str:
        wanted = req.request.query_params.get('component')
        if wanted is None:
            return opts.components[0]
        if wanted not in opts.components:
            raise ValidationFailed(
                f'unknown component {wanted[:40]!r}: this repository has {", ".join(opts.components)}'
            )
        return wanted

    @staticmethod
    def _length(req: FormatRequest) -> int | None:
        length = req.request.headers.get('content-length')
        return int(length) if length is not None and length.isdigit() else None

    async def _receive(self, req: FormatRequest) -> BlobRef:
        """Stream the package (raw body, or the first file part of a multipart form) into the blob store."""
        content_type = req.request.headers.get('content-type', '')
        multipart = content_type.lower().startswith('multipart/form-data')
        body: AsyncIterator[bytes] | None = None
        total = None if multipart else self._length(req)  # a form's length is not the package's
        await req.hosted.admit(req.repo, total)  # a hard quota refuses before any byte is stored
        artifact = req.path.rsplit('/', 1)[-1] or 'package.deb'
        parts: AsyncIterator[Any] | None = None
        if multipart:
            reader = MultipartReader(req.request.stream(), boundary_of(content_type))
            parts = reader.parts()
            async for part in parts:
                if part.filename is not None:
                    artifact = part.filename.rsplit('/', 1)[-1][:200] or artifact
                    body = reader.chunks()
                    break
                await reader.drain()
            if body is None:
                raise ValidationFailed('the form has no file part (send the package as a file)')
        async with await req.hosted.blobs.open_writer(expected_size=total) as writer:
            await req.hosted.receive(req, writer, path=req.path or artifact, artifact=artifact, total=total, body=body)
            ref = await req.hosted.blobs.commit(writer)
        if parts is not None:
            async for _ in parts:  # the rest of the form (nothing else is used)
                pass
        return ref

    async def upload(self, req: FormatRequest) -> Response:
        hosted = self._policy(req)
        opts = AptOptions.model_validate(req.repo.format_options)
        component = self._component(req, opts)
        ref = await self._receive(req)
        info = await asyncio.to_thread(deb.read_deb, req.hosted.blobs.blob_path(ref.sha256), sha256=ref.sha256)
        path = info.pool_path(component)
        db, repo = req.hosted.db, req.repo
        existing = await apt_db.by_path(db, repo.id, path)
        if existing is not None and hosted.write_policy is WritePolicy.DISABLE_REDEPLOY:
            raise Conflict(f'{path} exists and this repository does not allow redeploying')
        if existing is not None and existing.get('version') != info.version:
            raise Conflict(f'{path} holds version {existing.get("version")} (the file name has no epoch)')
        twin = await apt_db.by_package(db, repo.id, info.package, info.version, info.architecture)
        if twin is not None and twin['path'] != path:
            raise Conflict(f'{info.filename} is already in this repository at {twin["path"]}')
        stanza = await asyncio.to_thread(info.stanza, path)
        await self.save(req, repo, path, component, info, ref, stanza, replacing=existing is not None)
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('apt_package', None, f'{repo.name}/{info.package}_{info.version}_{info.architecture}'),
            details={
                'repository': repo.name,
                'package': info.package,
                'version': info.version,
                'arch': info.architecture,
                'distribution': opts.distribution,
                'component': component,
                'path': path,
                'size': ref.size,
                'sha256': ref.sha256,
                'replaced': existing is not None,
            },
        )
        req.hosted.activity(req, EventType.ASSET_CREATED, path=path, artifact=info.filename, size=ref.size)
        req.hosted.account(req, CacheState.HIT, 201, 0, time.perf_counter())
        headers = {'ETag': f'"sha256:{ref.sha256}"', 'X-Checksum-Sha256': ref.sha256, 'Location': path}
        body = {
            'saved': True,
            'path': path,
            'package': info.package,
            'version': info.version,
            'architecture': info.architecture,
            'distribution': opts.distribution,
            'component': component,
        }
        return JSONResponse(body, status_code=201, headers=headers)

    async def save(
        self,
        req: FormatRequest,
        repo: RepoConfig,
        path: str,
        component: str,
        info: deb.Deb,
        ref: BlobRef,
        stanza: str,
        *,
        replacing: bool,
    ) -> None:
        """Record the package file, then its ``apt_hosted_packages`` record and the index job in one transaction (so
        an index build never sees a record without its file)."""
        await req.hosted.record(repo, path, ref, content_type=DEB_TYPE)
        record: dict[str, Any] = {
            'path': path,
            'component': component,
            'name': info.package,
            'version': info.version,
            'version_sort': version_sort('apt', info.version),
            'architecture': info.architecture,
            'section': info.section,
            'sha256': ref.sha256,
            'size': ref.size,
            'stanza': stanza,
            'uploaded_at': datetime.now(UTC),
            'uploaded_by': req.principal.display,
        }

        async def write(session: Session) -> None:
            await apt_db.save(req.hosted.db, repo.id, record, session=session)
            await self._reindex(req, repo, session)

        try:
            await req.hosted.transaction(write)
        except Conflict:  # the same version and architecture landed at another path meanwhile
            if not replacing:
                await req.hosted.remove(repo, path)
            raise Conflict(f'{info.filename} is already in this repository') from None

    @staticmethod
    async def _reindex(req: FormatRequest, repo: RepoConfig, session: Session) -> None:
        if req.hosted.jobs is not None:
            await req.hosted.jobs.enqueue_durable(AptIndex(repo_id=repo.id), session=session)

    async def delete(self, req: FormatRequest) -> Response:
        self._policy(req)
        db, repo = req.hosted.db, req.repo
        found = await apt_db.by_path(db, repo.id, req.path)
        if found is None:
            raise NotFound('Not Found')

        async def write(session: Session) -> None:
            await apt_db.delete(db, repo.id, req.path, session=session)
            await self._reindex(req, repo, session)

        await req.hosted.transaction(write)
        removed = await req.hosted.remove(repo, req.path)
        size = removed.size if removed is not None else found.get('size')
        name, version, arch = str(found['name']), str(found['version']), str(found['architecture'])
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target('apt_package', None, f'{repo.name}/{name}_{version}_{arch}'),
            details={'repository': repo.name, 'package': name, 'version': version, 'path': req.path, 'size': size},
        )
        req.hosted.activity(
            req, EventType.ASSET_DELETED, path=req.path, artifact=req.path.rsplit('/', 1)[-1], size=size
        )
        req.hosted.account(req, CacheState.HIT, 204, 0, time.perf_counter())
        return Response(status_code=204)
