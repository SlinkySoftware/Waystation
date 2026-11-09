"""The hosted store: what a format plugin uses to accept uploads into, serve from and delete from a hosted repository
(SPECIFICATION §4.2; ARCHITECTURE §4, §5, §8). The counterpart of the proxy engine for content clients push.

- **Receiving**: ``receive`` streams a request body into a ``BlobWriter`` (``waystation_common.storage``: hashed
  incrementally in a thread, bounded by ``WAYSTATION_MAX_UPLOAD_GB``, free-space guard) and publishes it as a live
  upload transfer (``Direction.UPLOAD``; per-minute ``bytes_in_upload``, never a cache hit or miss). Resumable
  uploads (OCI chunked) use ``BlobStore.start_upload`` / ``resume_writer`` with the format's own session records.
- **Recording**: ``record`` stores the committed blob as an asset of the repository (``assets``; hosted assets carry
  ``upstream.url == ''`` and ``fetched_at`` = upload time). ``link`` makes an asset for a blob that is already in the
  store (cross-repository mount, a manifest already pushed elsewhere) after claiming it against GC (ARCH §5).
- **Serving**: ``serve`` answers with the X-Accel redirect (``dataplane.xaccel``), the server handling conditional
  requests and ``HEAD``; cached bytes never pass through Python. Downloads are counted as cache hits.
- **Deleting**: ``remove`` drops the asset; blob GC frees the file once nothing references it.

Audit records (``content.upload`` / ``content.delete``) are written by the format, once per client-visible action
(a pushed image manifest, a deleted tag), not per blob.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from starlette.requests import ClientDisconnect
from starlette.responses import Response

from waystation_common.db.client import Database
from waystation_common.errors import WaystationError
from waystation_common.events import ActivityEvent, CacheState, Direction, EventEmitter, EventType, Transfer
from waystation_common.ids import new_id
from waystation_common.storage import BlobRef, BlobStore, BlobWriter
from waystation_server.dataplane.xaccel import ServedBlob, serve_blob
from waystation_server.db import assets as assets_db
from waystation_server.metrics import Metrics
from waystation_server.proxy.records import AssetKind, StoredAsset, UpstreamMeta
from waystation_server.repos.model import RepoConfig

if TYPE_CHECKING:
    from waystation_server.formats.base import FormatRequest


class UploadInterrupted(WaystationError):  # noqa: N818 - reads as an outcome
    """The client went away (or the body ended early) in the middle of an upload."""

    code = 'upload_interrupted'
    status = 400


class HostedStore:
    def __init__(self, *, db: Database, blobs: BlobStore, emitter: EventEmitter, metrics: Metrics) -> None:
        self.db = db
        self.blobs = blobs
        self.emitter = emitter
        self.metrics = metrics

    # ------------------------------------------------------------------ receiving
    async def receive(
        self,
        req: FormatRequest,
        writer: BlobWriter,
        *,
        path: str,
        artifact: str,
        component: str | None = None,
        total: int | None = None,
        body: AsyncIterator[bytes] | None = None,
    ) -> int:
        """Stream the request body (or ``body``) into ``writer`` as a live upload transfer; returns the bytes this
        request added. Raises ``UploadInterrupted`` when the client goes away, ``PayloadTooLarge`` past the limit
        (the writer is then aborted by the store). The caller commits, suspends or rolls back the writer."""
        tid = self._start(req, path=path, artifact=artifact, component=component, total=total)
        start = writer.size
        source = body if body is not None else req.request.stream()
        try:
            async for chunk in source:
                if chunk:
                    await writer.write(chunk)
                    self.emitter.transfer_progress(tid, writer.size - start)
        except ClientDisconnect:
            self.emitter.transfer_finished(tid, status=400, bytes_done=writer.size - start, error='client went away')
            raise UploadInterrupted('the client went away during the upload') from None
        except WaystationError as exc:
            self.emitter.transfer_finished(tid, status=exc.status, bytes_done=writer.size - start, error=exc.detail)
            raise
        except BaseException:
            self.emitter.transfer_finished(tid, status=500, bytes_done=writer.size - start, error='upload failed')
            raise
        received = writer.size - start
        self.emitter.transfer_finished(tid, status=202, bytes_done=received)
        self.metrics.add_bytes(repo=req.repo.name, direction='in_upload', count=received)
        return received

    def _start(self, req: FormatRequest, *, path: str, artifact: str, component: str | None, total: int | None) -> str:
        now = datetime.now(UTC)
        tid = new_id()
        self.emitter.transfer_started(
            Transfer(
                id=tid,
                repo_id=req.repo.id,
                repo=req.repo.name,
                format=req.repo.format,
                path=path,
                direction=Direction.UPLOAD,
                cache=CacheState.FETCHING,
                principal=req.principal.display,
                client_ip=req.client_ip or None,
                client_agent=(req.request.headers.get('user-agent') or '')[:120] or None,
                artifact=artifact,
                component=component,
                bytes_total=total,
                started_at=now,
                updated_at=now,
            )
        )
        return tid

    # ------------------------------------------------------------------ recording
    async def record(
        self,
        repo: RepoConfig,
        path: str,
        ref: BlobRef,
        *,
        content_type: str,
        headers: Mapping[str, str] | None = None,
        kind: AssetKind = AssetKind.CONTENT,
    ) -> StoredAsset:
        """Make ``path`` in ``repo`` the committed blob ``ref`` (replacing what was there)."""
        now = datetime.now(UTC)
        asset = StoredAsset(
            id=new_id(),
            repo_id=repo.id,
            path=path,
            kind=kind,
            blob_sha256=ref.sha256,
            size=ref.size,
            content_type=content_type,
            checksums={'sha256': ref.sha256},
            headers=dict(headers or {}),
            upstream=UpstreamMeta(url='', fetched_at=now, status=201),
            created_at=now,
            updated_at=now,
        )
        return await assets_db.save(self.db, asset)

    async def link(
        self,
        repo: RepoConfig,
        path: str,
        sha256: str,
        size: int,
        *,
        content_type: str,
        headers: Mapping[str, str] | None = None,
        kind: AssetKind = AssetKind.CONTENT,
    ) -> StoredAsset | None:
        """Reference a blob already in the store from ``repo`` (no bytes copied). Claims it against GC first; None
        when the file is gone (GC removed it meanwhile): the client uploads it again."""
        if self.blobs.guard is not None:
            await self.blobs.guard.claim(sha256, size)
        if not await self.blobs.exists(sha256):
            return None
        return await self.record(
            repo, path, BlobRef(sha256, size), content_type=content_type, headers=headers, kind=kind
        )

    async def get(self, repo: RepoConfig, path: str) -> StoredAsset | None:
        return await assets_db.get(self.db, repo.id, path)

    async def sizes(self, repo: RepoConfig, paths: list[str]) -> dict[str, int]:
        """Which of ``paths`` the repository holds, with their sizes."""
        return await assets_db.sizes(self.db, repo.id, paths)

    async def read(self, asset: StoredAsset, *, max_bytes: int) -> bytes:
        """A small stored file (manifests, configs), read in a thread."""
        path = self.blobs.blob_path(asset.blob_sha256)

        def load() -> bytes:
            with path.open('rb') as fh:
                return fh.read(max_bytes + 1)

        data = await asyncio.to_thread(load)
        if len(data) > max_bytes:
            raise ValueError('stored file is larger than expected')
        return data

    async def remove(self, repo: RepoConfig, path: str) -> StoredAsset | None:
        return await assets_db.delete_path(self.db, repo.id, path)

    # ------------------------------------------------------------------ serving and accounting
    def serve(self, req: FormatRequest, asset: StoredAsset, *, headers: Mapping[str, str] | None = None) -> Response:
        started = time.perf_counter()
        blob = ServedBlob(
            sha256=asset.blob_sha256,
            size=asset.size,
            content_type=asset.content_type,
            last_modified=asset.created_at,
            headers={**asset.headers, **(headers or {})},
        )
        response = serve_blob(req.request, blob, cache=CacheState.HIT.value)
        sent = asset.size if response.status_code == 200 and req.method == 'GET' else 0
        if sent:
            self.emitter.asset_downloaded(asset.id)
        self.account(req, CacheState.HIT, response.status_code, sent, started)
        return response

    def account(self, req: FormatRequest, cache: CacheState, status: int, sent: int, started: float) -> None:
        """Count a hosted request that is not an upload transfer (served files, metadata, errors)."""
        duration_ms = (time.perf_counter() - started) * 1000
        self.emitter.request(
            repo_id=req.repo.id,
            format=req.repo.format,
            cache=cache,
            duration_ms=duration_ms,
            bytes_out=sent,
            upload=req.method not in ('GET', 'HEAD'),
        )
        self.metrics.request(repo=req.repo.name, format=req.repo.format, status=status, cache=cache.value)
        if sent:
            self.metrics.add_bytes(repo=req.repo.name, direction='out_cache', count=sent)
        req.log(cache=cache.value, bytes=sent)

    def activity(
        self, req: FormatRequest, event: EventType, *, path: str, artifact: str, size: int | None = None
    ) -> None:
        """An ``asset.created`` / ``asset.deleted`` entry in the activity feed (pushed manifests, deletes)."""
        self.emitter.activity(
            ActivityEvent(
                type=event,
                ts=datetime.now(UTC),
                repo_id=req.repo.id,
                repo=req.repo.name,
                format=req.repo.format,
                path=path,
                artifact=artifact,
                principal=req.principal.display,
                client_ip=req.client_ip or None,
                status=201 if event is EventType.ASSET_CREATED else 202,
                bytes=size,
            )
        )
