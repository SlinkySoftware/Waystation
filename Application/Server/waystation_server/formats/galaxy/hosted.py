"""Hosted Ansible Galaxy repositories (T-P5-GALAXY; FORMATS.md "Ansible Galaxy"; SPECIFICATION §4.2).

- **Publish**: ``POST api/v3/artifacts/collections/`` (``ansible-galaxy collection publish``), multipart with the
  artifact in ``file`` (named ``<namespace>-<name>-<version>.tar.gz``) and its ``sha256``. The version must not
  exist yet (versions are immutable; delete first) and no other upload of it may be waiting for its import. The
  artifact streams into the blob store (``MAX_ARTIFACT_BYTES``, hard quota checked first; a live upload transfer),
  the ``sha256`` field is checked at commit, then the import task (``galaxy_imports``) and its ``import.galaxy`` job
  are written in one MongoDB transaction (outbox, ARCHITECTURE §10). The answer is ``202`` with the task URL; the
  worker validates the archive and publishes the version (``waystation_worker.galaxy``). Write policy
  ``read_only`` refuses (403).
- **Import task**: ``GET api/v3/imports/collections/<task>/`` (``state``, ``messages``, ``error``,
  ``finished_at``); served by the plugin for the hosted repository and its groups.
- **Delete**: ``DELETE api/v3/collections/<ns>/<name>/versions/<version>/`` or ``.../<ns>/<name>/`` (every version):
  the records and artifacts, audited ``content.delete`` per version.

Uploads need ``write`` and deletes ``delete`` (checked by the router from the method).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from starlette.responses import JSONResponse, Response

from waystation_common import galaxy
from waystation_common.db import galaxy as galaxy_common_db
from waystation_common.db.client import Session
from waystation_common.errors import (
    DigestMismatch,
    InsufficientStorage,
    NotFound,
    PayloadTooLarge,
    ValidationFailed,
    WaystationError,
)
from waystation_common.events import CacheState, EventType
from waystation_common.ids import new_id
from waystation_common.jobs import GalaxyImport
from waystation_common.storage import BlobWriter
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.common.multipart import MultipartReader, boundary_of
from waystation_server.formats.galaxy import api
from waystation_server.hosted.store import UploadInterrupted
from waystation_server.repos.model import StoredHosted, WritePolicy
from waystation_server.repos.quota import QuotaExceeded

MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


class Refused(Exception):  # noqa: N818 - an answer, not a failure
    def __init__(self, status: int, message: str, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')
    return value if isinstance(value, str) else None


def version_of(doc: Mapping[str, Any]) -> api.Version:
    return api.Version(
        version=str(doc['version']),
        created_at=iso(doc.get('created')),
        updated_at=iso(doc.get('created')),
        requires_ansible=doc.get('requires_ansible'),
    )


def info_of(docs: Sequence[Mapping[str, Any]]) -> api.Info:
    created = [c for d in docs if (c := iso(d.get('created')))]
    return api.Info(
        deprecated=False, created_at=min(created) if created else None, updated_at=max(created) if created else None
    )


def detail_of(doc: Mapping[str, Any]) -> api.Detail:
    metadata = dict(doc.get('metadata') or {})
    metadata.setdefault('dependencies', {})
    return api.Detail(
        namespace=str(doc['namespace']),
        name=str(doc['name']),
        version=str(doc['version']),
        sha256=str(doc.get('sha256') or ''),
        size=doc.get('size'),
        created_at=iso(doc.get('created')),
        updated_at=iso(doc.get('created')),
        requires_ansible=doc.get('requires_ansible'),
        metadata=metadata,
    )


def galaxy_error(status: int, message: str, code: str) -> Response:
    return JSONResponse(api.error_doc(status, message, code), status_code=status)


class GalaxyHosted:
    @staticmethod
    def _policy(req: FormatRequest) -> StoredHosted:
        hosted = req.repo.hosted or StoredHosted()
        if hosted.write_policy is WritePolicy.READ_ONLY:
            raise Refused(403, 'this repository is read-only', 'permission_denied')
        return hosted

    async def serve_artifact(self, req: FormatRequest, artifact: galaxy.Artifact) -> Response:
        asset = await req.hosted.get(req.repo, artifact.path)
        if asset is None:
            req.hosted.account(req, CacheState.HIT, 404, 0, time.perf_counter())
            raise NotFound('Not Found')
        return req.hosted.serve(req, asset)

    # ------------------------------------------------------------------ publish
    async def publish(self, req: FormatRequest, urls: api.Urls) -> Response:
        started = time.perf_counter()
        writer: BlobWriter | None = None
        try:
            self._policy(req)
            length = req.request.headers.get('content-length')
            total = int(length) if length is not None and length.isdigit() else None
            if total is not None and total > MAX_ARTIFACT_BYTES + 1024 * 1024:
                raise Refused(413, 'the collection artifact is too large', 'too_large')
            await req.hosted.admit(req.repo, total)
            fields, artifact, writer = await self._read(req)
            response = await self._store(req, urls, fields, artifact, writer)
        except Refused as exc:
            response = galaxy_error(exc.status, exc.message, exc.code)
        except QuotaExceeded as exc:  # a hard repository quota (SPECIFICATION §4.8)
            response = galaxy_error(exc.status, exc.detail, 'quota_exceeded')
        except InsufficientStorage:  # the low-disk guard
            response = galaxy_error(507, 'not enough free space in the blob store', 'insufficient_storage')
        except (ValidationFailed, PayloadTooLarge, UploadInterrupted) as exc:  # bad form, too big, cut off
            response = galaxy_error(exc.status, exc.detail, exc.code)
        finally:
            if writer is not None and not writer.closed:
                await writer.abort()
        req.hosted.account(req, CacheState.HIT, response.status_code, 0, started)
        return response

    async def _read(self, req: FormatRequest) -> tuple[dict[str, str], galaxy.Artifact, BlobWriter]:
        try:
            boundary = boundary_of(req.request.headers.get('content-type'))
        except WaystationError as exc:
            raise Refused(400, exc.detail, 'invalid') from None
        reader = MultipartReader(req.request.stream(), boundary)
        fields: dict[str, str] = {}
        writer: BlobWriter | None = None
        artifact: galaxy.Artifact | None = None
        try:
            async for part in reader.parts():
                if part.filename is None:
                    fields.setdefault(part.name, await reader.text())
                elif part.name == 'file' and writer is None:
                    artifact = await self._admit_artifact(req, part.filename)
                    writer = await req.hosted.blobs.open_writer(max_size=MAX_ARTIFACT_BYTES)
                    await req.hosted.receive(
                        req,
                        writer,
                        path=artifact.path,
                        artifact=artifact.filename,
                        component=artifact.fqcn,
                        body=reader.chunks(),
                    )
                else:
                    await reader.drain()
        except BaseException:
            if writer is not None:
                await writer.abort()
            raise
        if writer is None or artifact is None:
            raise Refused(400, 'the upload has no collection artifact (form field "file")', 'invalid')
        return fields, artifact, writer

    async def _admit_artifact(self, req: FormatRequest, filename: str) -> galaxy.Artifact:
        base = os.path.basename(filename.replace('\\', '/'))
        artifact = galaxy.parse_filename(base)
        if artifact is None:
            raise Refused(
                400,
                f'invalid artifact file name {base[:200]!r}: expected <namespace>-<name>-<version>.tar.gz '
                '(lower-case names, a SemVer version), as ansible-galaxy collection build writes it',
                'invalid',
            )
        db, repo_id = req.hosted.db, req.repo.id
        if await galaxy_common_db.get_version(db, repo_id, artifact.namespace, artifact.name, artifact.version):
            raise Refused(
                409,
                f'{artifact.fqcn} {artifact.version} exists already: versions are immutable (delete it first)',
                'conflict',
            )
        if await galaxy_common_db.unfinished_import(db, repo_id, artifact.namespace, artifact.name, artifact.version):
            raise Refused(409, f'an upload of {artifact.fqcn} {artifact.version} is being imported', 'conflict')
        return artifact

    async def _store(
        self,
        req: FormatRequest,
        urls: api.Urls,
        fields: dict[str, str],
        artifact: galaxy.Artifact,
        writer: BlobWriter,
    ) -> Response:
        digest = fields.get('sha256', '').strip().lower()
        if digest:
            if not _SHA256.match(digest):
                raise Refused(400, 'invalid sha256 field', 'invalid')
            writer.expected_sha256 = digest
        try:
            ref = await req.hosted.blobs.commit(writer)
        except DigestMismatch:
            raise Refused(400, 'the uploaded artifact does not match its sha256', 'checksum_mismatch') from None
        task_id = new_id()
        now = datetime.now(UTC)
        doc = {
            '_id': task_id,
            'repo_id': req.repo.id,
            'namespace': artifact.namespace,
            'name': artifact.name,
            'version': artifact.version,
            'filename': artifact.filename,
            'sha256': ref.sha256,
            'size': ref.size,
            'uploaded_by': req.principal.display,
        }

        async def write(session: Session) -> None:
            await galaxy_common_db.create_import(req.hosted.db, doc, now=now, session=session)
            if req.hosted.jobs is not None:
                await req.hosted.jobs.enqueue_durable(GalaxyImport(task_id=task_id), session=session)

        await req.hosted.transaction(write)
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('galaxy_collection', None, f'{req.repo.name}/{artifact.fqcn}:{artifact.version}'),
            details={
                'repository': req.repo.name,
                'collection': artifact.fqcn,
                'version': artifact.version,
                'size': ref.size,
                'sha256': ref.sha256,
                'task': task_id,
            },
        )
        req.hosted.activity(req, EventType.ASSET_CREATED, path=artifact.path, artifact=artifact.filename, size=ref.size)
        return JSONResponse({'task': urls.task(task_id)}, status_code=202)

    # ------------------------------------------------------------------ delete
    async def delete(self, req: FormatRequest, namespace: str, name: str, version: str | None) -> Response:
        started = time.perf_counter()
        try:
            self._policy(req)
        except Refused as exc:
            return galaxy_error(exc.status, exc.message, exc.code)
        db, repo = req.hosted.db, req.repo
        if version is not None:
            found = await galaxy_common_db.get_version(db, repo.id, namespace, name, version)
            docs = [found] if found is not None else []
        else:
            docs = await galaxy_common_db.collection_versions(db, [repo.id], namespace, name)
        if not docs:
            req.hosted.account(req, CacheState.HIT, 404, 0, started)
            raise NotFound('Not Found')
        for doc in docs:
            await galaxy_common_db.delete_version(db, repo.id, namespace, name, str(doc['version']))
            removed = await req.hosted.remove(repo, str(doc['path']))
            size = removed.size if removed is not None else doc.get('size')
            audit.record(
                AuditAction.CONTENT_DELETE,
                target=audit.target('galaxy_collection', None, f'{repo.name}/{namespace}.{name}:{doc["version"]}'),
                details={'repository': repo.name, 'collection': f'{namespace}.{name}', 'version': doc['version'],
                         'size': size},
            )
            req.hosted.activity(
                req, EventType.ASSET_DELETED, path=str(doc['path']), artifact=str(doc['path']).rsplit('/', 1)[-1],
                size=size,
            )
        req.hosted.account(req, CacheState.HIT, 204, 0, started)
        return Response(status_code=204)
