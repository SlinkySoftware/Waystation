"""Cargo hosted repositories (T-P6-CARGO; SPECIFICATION §4.2; FORMATS.md "Cargo").

- **Publish** (``PUT api/v1/crates/new``, ``cargo publish``): the length-prefixed metadata JSON (``MAX_JSON``) and
  ``.crate`` file; the file streams into the blob store as a live upload transfer (bounded by
  ``WAYSTATION_MAX_UPLOAD_GB`` and the declared length), then is checked in a thread (``payload.inspect_crate``). The
  ``.crate`` becomes ``crates/<crate>/<crate>-<version>.crate`` carrying ``assets.cargo`` (the index entry made from
  the metadata with the file's sha256 as ``cksum``, yanked state, publisher); the summary goes to ``cargo_crates``.
  A crate name that differs from an existing crate only by case or ``-``/``_`` is refused (as on crates.io).
- **Write policy**: ``read_only`` refuses (403); ``disable_redeploy`` refuses a version that exists with other
  content (400, the same bytes again answer 200); ``allow_redeploy`` replaces it (the new ``cksum`` breaks lock files
  that recorded the old one). Versions that differ only in build metadata are the same version (refused).
- **Index files** are rendered per request from the stored entries (``ETag``, ``304``); ``.crate`` files are served
  by X-Accel.
- **Yank / unyank** (``DELETE .../yank``, ``PUT .../unyank``): the version stays downloadable; the index marks it.
- **Owners**: ``GET`` lists the publishers of the crate; adding or removing owners is refused (access is Waystation
  RBAC).

Publishing needs ``write`` (router: ``PUT``; yank, unyank and owner changes are mapped to ``write`` by the
descriptor). Uploads are audited (``content.upload``; yanks ``content.update``) and shown in the activity feed.
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from starlette.responses import JSONResponse, Response

from waystation_common.errors import DigestMismatch, InsufficientStorage, NotFound, PayloadTooLarge
from waystation_common.events import CacheState, EventType
from waystation_common.storage import BlobWriter
from waystation_common.versionsort import version_sort
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.db import cargo as cargo_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.cargo import publish as payload
from waystation_server.formats.cargo.common import (
    CRATE_TYPE,
    INDEX_TYPE,
    SAFE_HEADERS,
    Op,
    Route,
    canonical,
    cargo_error,
    crate_path,
    generated,
    render_index,
    summary_fields,
    without_build,
)
from waystation_server.hosted.store import UploadInterrupted
from waystation_server.repos.model import RepoConfig, StoredHosted, WritePolicy
from waystation_server.repos.quota import QuotaExceeded

WARNINGS: dict[str, dict[str, list[str]]] = {'warnings': {'invalid_categories': [], 'invalid_badges': [], 'other': []}}


class Refused(Exception):  # noqa: N818 - reads as an outcome
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


class BodyReader:
    """Exact reads from a request body stream (the publish payload's length-prefixed parts)."""

    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._stream = stream
        self._buffer = bytearray()
        self._done = False

    async def _fill(self) -> bool:
        if self._done:
            return False
        try:
            chunk = await anext(self._stream)
        except StopAsyncIteration:
            self._done = True
            return False
        self._buffer += chunk
        return True

    async def read_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            if not await self._fill():
                raise Refused(400, 'the publish request body ended early')
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    async def u32(self) -> int:
        value: int = struct.unpack('<I', await self.read_exact(4))[0]
        return value

    async def chunks(self, size: int) -> AsyncIterator[bytes]:
        left = size
        while left > 0:
            if not self._buffer and not await self._fill():
                raise Refused(400, 'the publish request body ended early')
            piece = bytes(self._buffer[:left])
            del self._buffer[: len(piece)]
            left -= len(piece)
            yield piece

    async def at_end(self) -> bool:
        while not self._buffer:
            if not await self._fill():
                return True
        return False


def entries_of(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Index entries of a hosted crate's stored files, oldest version first."""
    out: list[tuple[str, dict[str, Any]]] = []
    for doc in docs:
        details = doc.get('cargo')
        if not isinstance(details, dict) or not isinstance(details.get('entry'), dict):
            continue
        entry = {**details['entry'], 'yanked': bool(details.get('yanked'))}
        out.append((str(details.get('version_sort') or ''), entry))
    out.sort(key=lambda pair: pair[0])
    return [entry for _, entry in out]


class CargoHosted:
    # ------------------------------------------------------------------ reading
    async def entries(self, req: FormatRequest, repo: RepoConfig, crate: str) -> list[dict[str, Any]]:
        return entries_of(await cargo_db.crate_files(req.hosted.db, repo.id, crate))

    async def serve(self, req: FormatRequest, route: Route) -> Response:
        started = time.perf_counter()

        def account(status: int, sent: int) -> None:
            req.hosted.account(req, CacheState.HIT, status, sent, started)

        if route.op is Op.INDEX:
            entries = await self.entries(req, req.repo, route.crate)
            if not entries:
                account(404, 0)
                raise NotFound('Not Found')
            body = await asyncio.to_thread(render_index, entries)
            return generated(req, body, INDEX_TYPE, account=account)
        if route.op is Op.DOWNLOAD:
            asset = await req.hosted.get(req.repo, crate_path(route.crate, route.version))
            if asset is None:
                account(404, 0)
                raise NotFound('Not Found')
            return req.hosted.serve(req, asset, headers=SAFE_HEADERS)
        raise NotFound('Not Found')  # pragma: no cover

    # ------------------------------------------------------------------ publishing
    async def publish(self, req: FormatRequest) -> Response:
        started = time.perf_counter()
        hosted = req.repo.hosted or StoredHosted()
        writer: BlobWriter | None = None
        try:
            if hosted.write_policy is WritePolicy.READ_ONLY:
                raise Refused(403, 'this repository is read-only')
            length = req.request.headers.get('content-length')
            await req.hosted.admit(req.repo, int(length) if length is not None and length.isdigit() else None)
            reader = BodyReader(req.request.stream())
            size = await reader.u32()
            if size > payload.MAX_JSON:
                raise Refused(413, 'the publish metadata is too large')
            meta = await asyncio.to_thread(payload.parse_metadata, await reader.read_exact(size))
            crate_size = await reader.u32()
            if length is not None and length.isdigit() and int(length) != 8 + size + crate_size:
                raise Refused(400, 'the publish request length does not match its parts')
            writer = await req.hosted.blobs.open_writer()
            await req.hosted.receive(
                req,
                writer,
                path=crate_path(meta.name, meta.vers),
                artifact=f'{meta.name}-{meta.vers}.crate',
                component=meta.name,
                total=crate_size,
                body=reader.chunks(crate_size),
            )
            if not await reader.at_end():
                raise Refused(400, 'the publish request has data after the crate file')
            response = await self._store(req, hosted, meta, writer)
        except Refused as exc:
            response = cargo_error(exc.status, exc.message, api=True)
        except payload.Invalid as exc:
            response = cargo_error(400, str(exc), api=True)
        except QuotaExceeded as exc:  # a hard repository quota (SPECIFICATION §4.8)
            response = cargo_error(403, exc.detail, api=True)
        except InsufficientStorage:  # the low-disk guard
            response = cargo_error(507, 'not enough free space in the blob store', api=True)
        except (PayloadTooLarge, UploadInterrupted) as exc:
            response = cargo_error(exc.status, exc.detail, api=True)
        finally:
            if writer is not None and not writer.closed:
                await writer.abort()
        req.hosted.account(req, CacheState.HIT, response.status_code, 0, started)
        return response

    async def _store(
        self, req: FormatRequest, hosted: StoredHosted, meta: payload.Published, writer: BlobWriter
    ) -> Response:
        await writer.flush()
        await asyncio.to_thread(payload.inspect_crate, writer.path, meta.name, meta.vers)
        crate = meta.name.lower()
        db = req.hosted.db
        for other in await cargo_db.same_crate(db, req.repo.id, canonical(crate)):
            if other != crate:
                raise Refused(400, f'crate {meta.name} conflicts with the existing crate {other}')
        summary = await cargo_db.get_crate(db, req.repo.id, crate)
        if summary is not None and summary.get('display_name') not in (None, meta.name):
            raise Refused(400, f'crate {meta.name} exists as {summary["display_name"]}: use that name')
        path = crate_path(meta.name, meta.vers)
        docs = await cargo_db.crate_files(db, req.repo.id, crate)
        for doc in docs:
            vers = str((doc.get('cargo') or {}).get('vers') or '')
            if vers and vers != meta.vers and without_build(vers) == without_build(meta.vers):
                raise Refused(400, f'crate version {meta.name}@{vers} is already uploaded')
        existing = next((d for d in docs if d['path'] == path), None)
        if existing is not None and hosted.write_policy is WritePolicy.DISABLE_REDEPLOY:
            if existing['blob_sha256'] == writer.hexdigest():
                await writer.abort()
                return JSONResponse(WARNINGS)
            raise Refused(400, f'crate version {meta.name}@{meta.vers} is already uploaded')
        try:
            ref = await req.hosted.blobs.commit(writer)
        except DigestMismatch:  # pragma: no cover - no expected digest is set
            raise Refused(400, 'the crate file is corrupt') from None
        await req.hosted.record(req.repo, path, ref, content_type=CRATE_TYPE)
        details = {
            'name': meta.name,
            'vers': meta.vers,
            'version_sort': version_sort('cargo', meta.vers),
            'entry': payload.index_entry(meta, ref.sha256),
            'yanked': False,
            'description': meta.description,
            'published_by': req.principal.display,
            'published_at': datetime.now(UTC),
        }
        await cargo_db.set_details(db, req.repo.id, path, details)
        await self.summarise(req, req.repo, crate, description=meta.description)
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('cargo_crate', ref.sha256, f'{req.repo.name}/{meta.name}@{meta.vers}'),
            details={
                'repository': req.repo.name,
                'crate': meta.name,
                'version': meta.vers,
                'sha256': ref.sha256,
                'size': ref.size,
                'replaced': existing is not None,
            },
        )
        req.hosted.activity(
            req, EventType.ASSET_CREATED, path=path, artifact=f'{meta.name}-{meta.vers}.crate', size=ref.size
        )
        req.log(crate=meta.name)
        return JSONResponse(WARNINGS)

    async def summarise(self, req: FormatRequest, repo: RepoConfig, crate: str, *, description: str | None) -> None:
        """Refresh the crate's ``cargo_crates`` summary from its stored files."""
        entries = await self.entries(req, repo, crate)
        if not entries:
            await cargo_db.delete_crate(req.hosted.db, repo.id, crate)
            return
        await cargo_db.upsert_crate(req.hosted.db, repo.id, crate, summary_fields(entries, crate, description))

    # ------------------------------------------------------------------ yank, owners
    async def yank(self, req: FormatRequest, route: Route) -> Response:
        if (req.repo.hosted or StoredHosted()).write_policy is WritePolicy.READ_ONLY:
            return cargo_error(403, 'this repository is read-only', api=True)
        yanked = route.op is Op.YANK
        path = crate_path(route.crate, route.version)
        if not await cargo_db.set_yanked(req.hosted.db, req.repo.id, path, yanked):
            return cargo_error(404, f'crate {route.crate}@{route.version} does not exist', api=True)
        await self.summarise(req, req.repo, route.crate, description=None)
        audit.record(
            AuditAction.CONTENT_UPDATE,
            target=audit.target('cargo_crate', None, f'{req.repo.name}/{route.crate}@{route.version}'),
            details={'repository': req.repo.name, 'crate': route.crate, 'version': route.version, 'yanked': yanked},
        )
        req.log(crate=route.crate)
        return JSONResponse({'ok': True})

    async def owners(self, req: FormatRequest, route: Route) -> Response:
        if req.method != 'GET':
            return cargo_error(
                400, 'owners are not managed through the registry: access is granted by Waystation roles', api=True
            )
        docs = await cargo_db.crate_files(req.hosted.db, req.repo.id, route.crate)
        publishers = sorted(
            {str(d['cargo']['published_by']) for d in docs if (d.get('cargo') or {}).get('published_by')}
        )
        if not publishers:
            return cargo_error(404, f'crate {route.crate} does not exist', api=True)
        users = [{'id': i + 1, 'login': login, 'name': None} for i, login in enumerate(publishers)]
        return JSONResponse({'users': users})
