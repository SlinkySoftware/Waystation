"""PyPI hosted repositories (T-P5-PYPI; SPECIFICATION §4.2; FORMATS.md "PyPI").

- **Upload** (``POST /repository/<repo>/``, also ``.../legacy/``): the legacy upload API twine and ``uv publish`` use,
  ``multipart/form-data`` with ``:action=file_upload``, the metadata fields and the file in ``content``. The file
  streams into the blob store (``formats.common.multipart``; a live upload transfer), then its core metadata is read
  from the archive in a thread (``formats.pypi.distmeta``: bounded, zip/tar-slip checks) and must agree with the file
  name and the form (project and version); ``sha256_digest`` must match. Wheels and sdists (``.tar.gz``, ``.zip``)
  only. The file becomes ``packages/<project>/<file>``; a wheel's ``METADATA`` is also stored as the PEP 658
  ``packages/<project>/<file>.metadata`` file. The project's details are kept on the asset (``assets.pypi``) and its
  summary in ``pypi_packages``.
- **Write policy**: ``read_only`` refuses uploads (403); ``disable_redeploy`` refuses a file name that exists with
  other content (400 "File already exists", which ``twine --skip-existing`` understands) and accepts the same bytes
  again; ``allow_redeploy`` replaces the file.
- **Pages**: ``simple/`` and ``simple/<project>/`` are rendered per request from the stored files (index-backed
  range of ``assets``), either representation, ``ETag`` + conditional requests; files are served by X-Accel.
- **Yank** (PEP 592): ``yank`` marks every file of a version yanked (with a reason) or not; through the REST API.

Uploads need ``write`` (checked by the router); they are audited (``content.upload``; yanks ``content.update``) and
shown in the activity feed.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from starlette.responses import PlainTextResponse, Response

from waystation_common.cleanup.versions import pep440_parts, pep440_sort_string
from waystation_common.errors import (
    DigestMismatch,
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
from waystation_server.db import pypi as pypi_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.common.multipart import MultipartReader, boundary_of
from waystation_server.formats.pypi import simple
from waystation_server.formats.pypi.common import (
    FILE_TYPE,
    METADATA_TYPE,
    SAFE_HEADERS,
    Op,
    Route,
    file_path,
    generated,
    page_from_assets,
    pypi_error,
    summary_fields,
)
from waystation_server.formats.pypi.distmeta import CoreMetadata, read_distribution
from waystation_server.hosted.store import UploadInterrupted
from waystation_server.repos.model import RepoConfig, StoredHosted, WritePolicy
from waystation_server.repos.quota import QuotaExceeded

UPLOAD_EXTENSIONS = ('.whl', '.tar.gz', '.zip')
MAX_INDEX = 1_000_000
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


class Refused(Exception):  # noqa: N818 - reads as an outcome
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def same_version(a: str, b: str) -> bool:
    """Equal as PEP 440 versions (``1.0`` == ``1.0.0``, ``1.0-1`` == ``1.0.post1``), else as strings."""
    pa, pb = pep440_parts(a), pep440_parts(b)
    return pa == pb if pa is not None and pb is not None else a == b


@dataclass
class _Received:
    writer: BlobWriter
    filename: str
    dist: simple.DistName


class PypiHosted:
    # ------------------------------------------------------------------ reading
    async def serve(self, req: FormatRequest, route: Route, chosen: str) -> Response:
        started = time.perf_counter()

        def account(status: int, sent: int) -> None:
            req.hosted.account(req, CacheState.HIT, status, sent, started)

        match route.op:
            case Op.INDEX:
                names = await pypi_db.project_names(req.hosted.db, req.repo.id, limit=MAX_INDEX)
                body = await asyncio.to_thread(simple.render_index, names, as_json=simple.is_json(chosen))
                return generated(req, body, chosen, account=account)
            case Op.PROJECT:
                page = await self.page(req, req.repo, simple.normalise(route.project))
                if page is None:
                    account(404, 0)
                    raise NotFound('Not Found')
                project = simple.normalise(route.project)
                if simple.is_json(chosen):
                    body = await asyncio.to_thread(simple.render_json, page, project)
                else:
                    body = await asyncio.to_thread(simple.render_html, page, project)
                return generated(req, body, chosen, account=account)
            case Op.FILE | Op.METADATA:
                path = file_path(route.project, route.filename, metadata=route.op is Op.METADATA)
                asset = await req.hosted.get(req.repo, path)
                if asset is None:
                    account(404, 0)
                    raise NotFound('Not Found')
                return req.hosted.serve(req, asset, headers=SAFE_HEADERS)
        raise NotFound('Not Found')

    async def page(self, req: FormatRequest, repo: RepoConfig, project: str) -> simple.Page | None:
        """A hosted repository's page of ``project`` (None when it has no files)."""
        docs = await pypi_db.project_files(req.hosted.db, repo.id, project)
        page = await asyncio.to_thread(page_from_assets, docs, project)
        return page if page.files else None

    # ------------------------------------------------------------------ uploading
    async def upload(self, req: FormatRequest) -> Response:
        started = time.perf_counter()
        hosted = req.repo.hosted or StoredHosted()
        if hosted.write_policy is WritePolicy.READ_ONLY:
            return pypi_error(403, 'this repository is read-only')
        received: _Received | None = None
        length = req.request.headers.get('content-length')
        try:
            await req.hosted.admit(req.repo, int(length) if length is not None and length.isdigit() else None)
            fields, received = await self._read(req)
            response = await self._store(req, hosted, fields, received)
        except Refused as exc:
            response = pypi_error(exc.status, exc.message)
        except QuotaExceeded as exc:  # a hard repository quota (SPECIFICATION §4.8)
            response = pypi_error(403, exc.detail)
        except InsufficientStorage:  # the low-disk guard
            response = pypi_error(507, 'not enough free space in the blob store')
        except (ValidationFailed, PayloadTooLarge, UploadInterrupted) as exc:  # bad form/archive, too big, cut off
            response = pypi_error(exc.status, exc.detail)
        finally:
            if received is not None and not received.writer.closed:
                await received.writer.abort()
        req.hosted.account(req, CacheState.HIT, response.status_code, 0, started)
        return response

    async def _read(self, req: FormatRequest) -> tuple[dict[str, str], _Received]:
        try:
            boundary = boundary_of(req.request.headers.get('content-type'))
        except WaystationError as exc:
            raise Refused(400, exc.detail) from None
        reader = MultipartReader(req.request.stream(), boundary)
        fields: dict[str, str] = {}
        received: _Received | None = None
        try:
            async for part in reader.parts():
                if part.filename is None:
                    value = await reader.text()
                    fields.setdefault(part.name, value)  # repeated fields (classifiers...) keep the first
                elif part.name == 'content' and received is None:
                    received = await self._receive(req, reader, part.filename)
                else:
                    await reader.drain()  # gpg_signature, attestations: not kept
        except BaseException:
            if received is not None:
                await received.writer.abort()
            raise
        if received is None:
            raise Refused(400, 'the upload has no distribution file (form field "content")')
        return fields, received

    async def _receive(self, req: FormatRequest, reader: MultipartReader, filename: str) -> _Received:
        filename = filename.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
        if not simple.valid_filename(filename) or not filename.endswith(UPLOAD_EXTENSIONS):
            raise Refused(400, f'invalid distribution file name {filename!r}: upload wheels or sdists (.tar.gz, .zip)')
        dist = simple.parse_filename(filename)
        if dist is None or not simple.valid_project(dist.project):
            raise Refused(400, f'cannot read the project and version from the file name {filename!r}')
        writer = await req.hosted.blobs.open_writer()
        try:
            await req.hosted.receive(
                req,
                writer,
                path=file_path(dist.project, filename),
                artifact=filename,
                component=dist.project,
                body=reader.chunks(),
            )
        except BaseException:
            await writer.abort()
            raise
        return _Received(writer, filename, dist)

    async def _store(
        self, req: FormatRequest, hosted: StoredHosted, fields: dict[str, str], received: _Received
    ) -> Response:
        action = fields.get(':action', 'file_upload')
        if action != 'file_upload':
            raise Refused(400, f'unsupported action {action!r}')
        writer, filename, dist = received.writer, received.filename, received.dist
        await writer.flush()
        meta = await asyncio.to_thread(read_distribution, writer.path, filename)
        self._check(fields, meta, dist)
        digest = fields.get('sha256_digest', '').strip().lower()
        if digest:
            if not _SHA256.match(digest):
                raise Refused(400, 'invalid sha256_digest')
            writer.expected_sha256 = digest
        path = file_path(dist.project, filename)
        existing = await req.hosted.get(req.repo, path)
        if existing is not None and hosted.write_policy is WritePolicy.DISABLE_REDEPLOY:
            if existing.blob_sha256 == writer.hexdigest():
                await writer.abort()
                return PlainTextResponse('OK (already uploaded)\n')
            raise Refused(400, f'File already exists: {filename} (this repository does not allow redeploying)')
        try:
            ref = await req.hosted.blobs.commit(writer)
        except DigestMismatch:
            raise Refused(400, 'the uploaded file does not match its sha256_digest') from None
        await req.hosted.record(req.repo, path, ref, content_type=FILE_TYPE)
        metadata_sha256 = None
        if dist.kind == 'wheel':
            metadata_sha256 = await self._store_metadata(req, path, meta)
        now = datetime.now(UTC)
        details = {
            'project': dist.project,
            'name': meta.name,
            'version': dist.version,
            'version_sort': pep440_sort_string(dist.version),
            'filetype': 'bdist_wheel' if dist.kind == 'wheel' else 'sdist',
            'python_version': (fields.get('pyversion') or '')[:64] or None,
            'requires_python': meta.requires_python,
            'summary': meta.summary,
            'license': meta.license,
            'metadata_sha256': metadata_sha256,
            'yanked': False,
            'uploaded_by': req.principal.display,
            'uploaded_at': now,
        }
        await pypi_db.set_file(req.hosted.db, req.repo.id, path, details)
        await self.summarise(req, req.repo, dist.project, display_name=meta.name, summary=meta.summary)
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('pypi_file', ref.sha256, f'{req.repo.name}/{filename}'),
            details={
                'repository': req.repo.name,
                'project': dist.project,
                'version': dist.version,
                'filename': filename,
                'sha256': ref.sha256,
                'size': ref.size,
                'replaced': existing is not None,
            },
        )
        req.hosted.activity(req, EventType.ASSET_CREATED, path=path, artifact=filename, size=ref.size)
        req.log(project=dist.project)
        return PlainTextResponse('OK\n')

    @staticmethod
    def _check(fields: dict[str, str], meta: CoreMetadata, dist: simple.DistName) -> None:
        if simple.normalise(meta.name) != dist.project:
            raise Refused(400, f'the file name says {dist.project}, its metadata says {meta.name}')
        if not same_version(meta.version, dist.version):
            raise Refused(400, f'the file name says version {dist.version}, its metadata says {meta.version}')
        name, version = fields.get('name'), fields.get('version')
        if name and simple.normalise(name) != dist.project:
            raise Refused(400, f'the form says project {name}, the file is {dist.project}')
        if version and not same_version(version, dist.version):
            raise Refused(400, f'the form says version {version}, the file is {dist.version}')

    @staticmethod
    async def _store_metadata(req: FormatRequest, path: str, meta: CoreMetadata) -> str:
        """The wheel's METADATA as the PEP 658 ``<file>.metadata`` file; returns its sha256."""
        async with await req.hosted.blobs.open_writer() as writer:
            await writer.write(meta.raw)
            ref = await req.hosted.blobs.commit(writer)
        await req.hosted.record(req.repo, f'{path}.metadata', ref, content_type=METADATA_TYPE)
        return ref.sha256

    async def summarise(
        self,
        req: FormatRequest,
        repo: RepoConfig,
        project: str,
        *,
        display_name: str | None = None,
        summary: str | None = None,
    ) -> None:
        """Refresh the project's ``pypi_packages`` summary from its stored files."""
        docs = await pypi_db.project_files(req.hosted.db, repo.id, project)
        page = await asyncio.to_thread(page_from_assets, docs, project)
        versions = simple.versions_of(page)
        fields = summary_fields(display_name or page.name, versions)
        if summary:
            fields['summary'] = summary
        await pypi_db.upsert_package(req.hosted.db, repo.id, project, fields)
