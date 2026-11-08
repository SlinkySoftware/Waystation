"""Storage figures (SPECIFICATION §4.7 "Storage"): bytes per repository and format, blobs, de-duplication savings,
free space on the data directory. Computed by aggregation and cached in-process for ``TTL_S`` (hourly
``storage_snapshots`` replace this with T-F09's full scope)."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from waystation_common.db.client import Database
from waystation_server.db import stats as stats_db

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

    async def get(self) -> Storage:
        if self._cached is not None and time.monotonic() - self._at < TTL_S:
            return self._cached
        async with self._lock:
            if self._cached is not None and time.monotonic() - self._at < TTL_S:
                return self._cached
            per_repo = await stats_db.asset_totals(self._db)
            count, used = await stats_db.blob_totals(self._db)
            free, total = await asyncio.to_thread(_disk, self._data_dir)
            by_repo = {str(r['_id']): RepoStorage(int(r['bytes']), int(r['artifacts'])) for r in per_repo}
            self._cached = Storage(
                used_bytes=used,
                free_bytes=free,
                total_bytes=total,
                blob_count=count,
                asset_bytes=sum(r.bytes for r in by_repo.values()),
                by_repo=by_repo,
            )
            self._at = time.monotonic()
            return self._cached

    def invalidate(self) -> None:
        self._cached = None
