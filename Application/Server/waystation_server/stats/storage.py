"""Storage figures (SPECIFICATION §4.7 "Storage"): bytes per repository and format, blobs, de-duplication savings,
free space on the data directory. Cached in-process for ``TTL_S`` (hourly ``storage_snapshots`` replace this with
T-F09's full scope).

Per repository the figures are the ``repo_usage`` counters (bytes and artifacts, kept by every asset write and
reconciled hourly; ``waystation_common.db.usage``): one small read, whatever the number of assets. The blob totals
are an aggregation over ``blobs``. Only the first call waits for the figures; afterwards an expired snapshot is
answered at once and refreshed in the background (one refresh at a time), so no request pays for an aggregation
(T-P2-PERF: the repository list's p99 was over a second at 1M assets while each process recounted ``assets``)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from waystation_common.db.client import Database
from waystation_server.db import stats as stats_db

log = logging.getLogger('waystation.stats.storage')

TTL_S = 30.0


@dataclass
class RepoStorage:
    bytes: int = 0
    artifacts: int = 0


@dataclass
class Storage:
    used_bytes: int  # bytes held by blobs (each stored once)
    free_bytes: int
    total_bytes: int
    blob_count: int
    asset_bytes: int  # sum over assets (what it would take without de-duplication)
    by_repo: dict[str, RepoStorage] = field(default_factory=dict)  # repo_id -> figures

    @property
    def dedup_saved_bytes(self) -> int:
        return max(0, self.asset_bytes - self.used_bytes)

    @property
    def artifacts(self) -> int:
        return sum(r.artifacts for r in self.by_repo.values())


def _disk(path: Path) -> tuple[int, int]:
    try:
        st = os.statvfs(path)
    except OSError:
        return 0, 0
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


class StorageStats:
    def __init__(self, db: Database, data_dir: Path) -> None:
        self._db = db
        self._data_dir = data_dir
        self._cached: Storage | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()
        self._refresh: asyncio.Task[None] | None = None

    async def get(self) -> Storage:
        cached = self._cached
        if cached is not None:
            if time.monotonic() - self._at >= TTL_S and (self._refresh is None or self._refresh.done()):
                self._refresh = asyncio.create_task(self._refresh_quietly(), name='storage-stats-refresh')
            return cached
        async with self._lock:
            if self._cached is None:
                self._cached = await self._load()
            return self._cached

    async def _refresh_quietly(self) -> None:
        try:
            async with self._lock:
                self._cached = await self._load()
        except Exception as exc:  # keep answering with the previous figures
            log.warning('storage figures not refreshed', extra={'error': type(exc).__name__})

    async def _load(self) -> Storage:
        per_repo = await stats_db.repo_totals(self._db)
        count, used = await stats_db.blob_totals(self._db)
        free, total = await asyncio.to_thread(_disk, self._data_dir)
        by_repo = {repo_id: RepoStorage(size, artifacts) for repo_id, (size, artifacts) in per_repo.items()}
        self._at = time.monotonic()
        return Storage(
            used_bytes=used,
            free_bytes=free,
            total_bytes=total,
            blob_count=count,
            asset_bytes=sum(r.bytes for r in by_repo.values()),
            by_repo=by_repo,
        )

    def invalidate(self) -> None:
        self._cached = None

    async def aclose(self) -> None:
        if self._refresh is not None:
            self._refresh.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._refresh
            self._refresh = None
