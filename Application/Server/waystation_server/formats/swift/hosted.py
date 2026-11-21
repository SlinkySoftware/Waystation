"""Hosted Swift package registries: publishing (SE-0391 ``swift package-registry publish``, SE-0292 §4.6) and
removing releases.

- **Publish**: ``PUT <scope>/<name>/<version>``, multipart with ``source-archive`` (the zip), optional
  ``source-archive-signature`` (with ``X-Swift-Package-Signature-Format: cms-1.0.0``), ``metadata`` (JSON, appendix
  B) and ``metadata-signature``. A version can be published once: an existing or removed release is ``409`` (checked
  before the body is read and again, atomically, when the record is written). The archive streams into the blob store
  (``MAX_ARCHIVE_BYTES``, the hard quota checked first, a live upload transfer), then is inspected in a thread
  (``archive.inspect_zip``: safe entry names, one top-level folder, bounded size and count, a ``Package.swift`` with
  a tools version: else ``422``) before it is committed; its manifests are stored next to it. The answer is
  synchronous: ``201`` with ``Location``. The signer's certificate subject is recorded (the signature is passed to
  clients, which verify it; Waystation does not). Write policy ``read_only`` refuses (``403``). Audited
  ``content.upload``.
- **Remove**: ``DELETE <scope>/<name>/<version>`` unpublishes: the archive and manifests go, the record stays as
  ``removed`` (the version is never reused; lists show it with a ``410`` problem, its endpoints answer ``410``).
  Audited ``content.delete``.

Uploads need ``write`` and removals ``delete`` (the router checks them from the method).
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.serialization import pkcs7
from starlette.responses import Response

from waystation_common.errors import InsufficientStorage, PayloadTooLarge, ValidationFailed, WaystationError
from waystation_common.events import CacheState, EventType
from waystation_common.ids import new_id
from waystation_common.storage import BlobRef, BlobStore, BlobWriter
from waystation_common.swift import PackageId
from waystation_common.versionsort import version_sort
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.db import swift as swift_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.common.multipart import MultipartReader, boundary_of
from waystation_server.formats.swift import api
from waystation_server.formats.swift.archive import ArchiveRefused, ArchiveSummary, inspect_zip
from waystation_server.hosted.store import UploadInterrupted
from waystation_server.repos.model import RepoConfig, StoredHosted, WritePolicy
from waystation_server.repos.quota import QuotaExceeded

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_SIGNATURE = 64 * 1024
MAX_SIGNER = 512


async def read_bytes(chunks: AsyncIterator[bytes], limit: int, what: str) -> bytes:
    out = bytearray()
    async for chunk in chunks:
        out += chunk
        if len(out) > limit:
            raise api.Problem(413, f'{what} is larger than {limit} bytes')
    return bytes(out)


def signer_of(signature: bytes) -> str | None:
    """The subject of the first certificate in a CMS signature (recorded for display; not a verification)."""
    try:
        certificates = pkcs7.load_der_pkcs7_certificates(signature)
    except ValueError:
        raise ArchiveRefused('the source archive signature is not a CMS (PKCS #7) structure') from None
    if not certificates:
        return None
    return certificates[0].subject.rfc4514_string()[:MAX_SIGNER]


async def store_bytes(blobs: BlobStore, data: bytes) -> BlobRef:
    writer = await blobs.open_writer(max_size=max(len(data), 1))
    try:
        await writer.write(data)
        return await blobs.commit(writer)
    finally:
        if not writer.closed:
            await writer.abort()


async def record_release_files(
    req: FormatRequest, repo: RepoConfig, pkg: PackageId, version: str, archive: BlobRef, summary: ArchiveSummary
) -> list[dict[str, Any]]:
    """Store the archive and its manifests as the release's assets; the ``manifests`` field of its record."""
    await req.hosted.record(
        repo,
        pkg.archive_path(version),
        archive,
        content_type=api.ZIP_TYPE,
        headers={'Content-Disposition': f'attachment; filename="{pkg.name}-{version}.zip"'},
    )
    manifests: list[dict[str, Any]] = []
    for manifest in summary.manifests:
        ref = await store_bytes(req.hosted.blobs, manifest.data)
        path = pkg.manifest_path(version, manifest.filename)
        await req.hosted.record(
            repo,
            path,
            ref,
            content_type=api.SWIFT_TYPE,
            headers={'Content-Disposition': f'attachment; filename="{manifest.filename}"'},
        )
        manifests.append(
            {
                'filename': manifest.filename,
                'swift_version': manifest.swift_version,
                'tools_version': manifest.tools_version,
                'path': path,
                'size': len(manifest.data),
            }
        )
    return manifests


class SwiftHosted:
    @staticmethod
    def _policy(req: FormatRequest) -> StoredHosted:
        hosted = req.repo.hosted or StoredHosted()
        if hosted.write_policy is WritePolicy.READ_ONLY:
            raise api.Problem(403, 'this repository is read-only')
        return hosted

    # ------------------------------------------------------------------ publish
    async def publish(self, req: FormatRequest, pkg: PackageId, version: str, urls: api.Urls) -> Response:
        started = time.perf_counter()
        writer: BlobWriter | None = None
        try:
            self._policy(req)
            await self._not_taken(req, pkg, version)
            length = req.request.headers.get('content-length')
            total = int(length) if length is not None and length.isdigit() else None
            if total is not None and total > MAX_ARCHIVE_BYTES + 2 * 1024 * 1024:
                raise api.Problem(413, 'the source archive is too large')
            await req.hosted.admit(req.repo, total)
            fields, signatures, writer = await self._read(req, pkg, version)
            response = await self._store(req, pkg, version, urls, fields, signatures, writer)
        except api.Problem as exc:
            response = api.problem(exc.status, exc.detail, headers=exc.headers)
        except ArchiveRefused as exc:
            response = api.problem(422, exc.message)
        except QuotaExceeded as exc:  # a hard repository quota (SPECIFICATION §4.8)
            response = api.problem(exc.status, exc.detail)
        except InsufficientStorage:  # the low-disk guard
            response = api.problem(507, 'not enough free space in the blob store')
        except (ValidationFailed, PayloadTooLarge, UploadInterrupted) as exc:  # bad form, too big, cut off
            response = api.problem(exc.status, exc.detail)
        finally:
            if writer is not None and not writer.closed:
                await writer.abort()
        req.hosted.account(req, CacheState.HIT, response.status_code, 0, started)
        return response

    @staticmethod
    async def _not_taken(req: FormatRequest, pkg: PackageId, version: str) -> None:
        found = await swift_db.get_release(req.hosted.db, req.repo.id, pkg.scope_key, pkg.name_key, version)
        if found is None:
            return
        if found.get('state') == swift_db.REMOVED:
            raise api.Problem(409, f'release {version} was removed: a version cannot be published again')
        raise api.Problem(409, f'a release with version {version} already exists')

    async def _read(
        self, req: FormatRequest, pkg: PackageId, version: str
    ) -> tuple[dict[str, str], dict[str, bytes], BlobWriter]:
        try:
            boundary = boundary_of(req.request.headers.get('content-type'))
        except WaystationError as exc:
            raise api.Problem(400, exc.detail) from None
        reader = MultipartReader(req.request.stream(), boundary)
        fields: dict[str, str] = {}
        signatures: dict[str, bytes] = {}
        writer: BlobWriter | None = None
        try:
            async for part in reader.parts():
                if part.name == 'source-archive' and writer is None:
                    writer = await req.hosted.blobs.open_writer(max_size=MAX_ARCHIVE_BYTES)
                    await req.hosted.receive(
                        req,
                        writer,
                        path=pkg.archive_path(version),
                        artifact=f'{pkg.name}-{version}.zip',
                        component=pkg.id,
                        body=reader.chunks(),
                    )
                elif part.name in ('source-archive-signature', 'metadata-signature') and part.name not in signatures:
                    signatures[part.name] = await read_bytes(reader.chunks(), MAX_SIGNATURE, part.name)
                elif part.name == 'metadata' and 'metadata' not in fields:
                    fields['metadata'] = await reader.text()
                else:
                    await reader.drain()
        except BaseException:
            if writer is not None:
                await writer.abort()
            raise
        if writer is None:
            raise api.Problem(422, 'the upload has no source archive (form field "source-archive")')
        return fields, signatures, writer

    async def _signature(self, req: FormatRequest, signatures: dict[str, bytes]) -> dict[str, Any] | None:
        archive_signature = signatures.get('source-archive-signature')
        declared = (req.request.headers.get('x-swift-package-signature-format') or '').strip().lower()
        if archive_signature is None:
            return None
        if declared not in api.SIGNATURE_FORMATS:
            raise api.Problem(
                422, 'a signed archive needs X-Swift-Package-Signature-Format: cms-1.0.0 (the only supported format)'
            )
        signer = await asyncio.to_thread(signer_of, archive_signature)
        metadata_signature = signatures.get('metadata-signature')
        return {
            'format': declared,
            'base64': base64.b64encode(archive_signature).decode('ascii'),
            'signer': signer,
            'verified': False,
            'metadata_base64': base64.b64encode(metadata_signature).decode('ascii') if metadata_signature else None,
        }

    async def _store(
        self,
        req: FormatRequest,
        pkg: PackageId,
        version: str,
        urls: api.Urls,
        fields: dict[str, str],
        signatures: dict[str, bytes],
        writer: BlobWriter,
    ) -> Response:
        metadata = api.check_metadata(fields.get('metadata', ''))
        signature = await self._signature(req, signatures)
        await writer.flush()
        summary = await asyncio.to_thread(inspect_zip, writer.path)
        archive = await req.hosted.blobs.commit(writer)
        repo, db = req.repo, req.hosted.db
        now = datetime.now(UTC)
        url_keys = sorted({k for u in metadata.get('repositoryURLs') or [] if (k := api.url_key(u)) is not None})
        main = summary.manifest()
        record: dict[str, Any] = {
            '_id': new_id(),
            'repo_id': repo.id,
            'scope': pkg.scope,
            'name': pkg.name,
            'scope_key': pkg.scope_key,
            'name_key': pkg.name_key,
            'version': version,
            'version_sort': version_sort('swift', version),
            'state': swift_db.PUBLISHED,
            'source': 'hosted',
            'archive_path': pkg.archive_path(version),
            'checksum': archive.sha256,
            'size': archive.size,
            'manifests': [],
            'tools_version': main.tools_version if main is not None else None,
            'metadata': metadata,
            'repository_urls': url_keys,
            'signature': signature,
            'published_at': now,
            'published_by': req.principal.display,
        }
        if not await swift_db.insert_release(db, record):  # a concurrent publish of the same version won
            raise api.Problem(409, f'a release with version {version} already exists')
        try:
            manifests = await record_release_files(req, repo, pkg, version, archive, summary)
        except BaseException:
            await swift_db.delete_release(db, repo.id, pkg.scope_key, pkg.name_key, version)
            raise
        await swift_db.set_manifests(db, repo.id, pkg.scope_key, pkg.name_key, version, manifests)
        await swift_db.refresh_package(db, repo.id, pkg.scope, pkg.name)
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('swift_release', None, f'{repo.name}/{pkg.id}@{version}'),
            details={
                'repository': repo.name,
                'package': pkg.id,
                'version': version,
                'size': archive.size,
                'sha256': archive.sha256,
                'signed': signature is not None,
            },
        )
        req.hosted.activity(
            req,
            EventType.ASSET_CREATED,
            path=pkg.archive_path(version),
            artifact=f'{pkg.name}-{version}.zip',
            size=archive.size,
        )
        return Response(
            status_code=201,
            headers={**api.VERSION_HEADERS, 'Location': urls.release(pkg.scope, pkg.name, version)},
        )

    # ------------------------------------------------------------------ remove
    async def remove(self, req: FormatRequest, pkg: PackageId, version: str) -> Response:
        started = time.perf_counter()
        self._policy(req)
        repo, db = req.repo, req.hosted.db
        found = await swift_db.mark_removed(
            db, repo.id, pkg.scope_key, pkg.name_key, version, by=req.principal.display, now=datetime.now(UTC)
        )
        if found is None:
            existing = await swift_db.get_release(db, repo.id, pkg.scope_key, pkg.name_key, version)
            if existing is not None:
                raise api.Problem(410, f'release {version} was removed already')
            raise api.Problem(404, 'release not found')
        removed = await req.hosted.remove(repo, str(found['archive_path']))
        for manifest in found.get('manifests') or []:
            await req.hosted.remove(repo, str(manifest['path']))
        await swift_db.refresh_package(db, repo.id, str(found['scope']), str(found['name']))
        size = removed.size if removed is not None else found.get('size')
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target('swift_release', None, f'{repo.name}/{found["scope"]}.{found["name"]}@{version}'),
            details={
                'repository': repo.name,
                'package': f'{found["scope"]}.{found["name"]}',
                'version': version,
                'size': size,
            },
        )
        req.hosted.activity(
            req,
            EventType.ASSET_DELETED,
            path=str(found['archive_path']),
            artifact=f'{found["name"]}-{version}.zip',
            size=size,
        )
        req.hosted.account(req, CacheState.HIT, 204, 0, started)
        return Response(status_code=204, headers=api.VERSION_HEADERS)
