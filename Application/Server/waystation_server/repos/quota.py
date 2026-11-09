"""Repository quotas and the low-disk guard on the server (SPECIFICATION §4.8; DATA_MODEL ``repo_usage``).

**Usage** is counted incrementally where assets are written and deleted (``db.assets``), recounted hourly by the
worker (``quota.reconcile``). A blob counts towards every repository that references it.

**Quotas** (``repositories.storage``): ``Quotas.refusal`` answers whether a hard quota refuses a new write (one read
by ``_id``, only for repositories with a hard quota; never on a cache hit). Uploads that would be refused get the
format's native error; proxy cache fills are served to the client and not stored (``proxy.engine``). After every
write ``written`` announces a threshold the repository newly reached (``quota.warning`` at 80 %, ``quota.exceeded`` at
100 %, soft and hard alike) once across replicas, and counts it on ``/metrics``.

**Low-disk guard**: ``GuardKeeper`` keeps the blob store's free-space floor (``BlobStore.min_free_bytes`` /
``min_free_percent``) in step with the ``storage`` settings document, re-read every ``REFRESH_S`` seconds (and at
once on the replica that changes it). The store refuses new writers below the floor (``InsufficientStorage``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from waystation_common import quota as quota_mod
from waystation_common.db import storage_guard
from waystation_common.db import usage as usage_db
from waystation_common.db.client import Database
from waystation_common.errors import WaystationError
from waystation_common.events import EventEmitter
from waystation_common.storage import BlobStore
from waystation_server.metrics import Metrics
from waystation_server.repos.model import QuotaMode, RepoConfig

log = logging.getLogger('waystation.quota')

REFRESH_S = 5.0


class QuotaExceeded(WaystationError):  # noqa: N818 - reads as a state, like InsufficientStorage
    """A hard repository quota refuses the write."""

    code = 'quota_exceeded'
    status = 507


@dataclass(frozen=True)
class QuotaStatus:
    quota_bytes: int
    mode: QuotaMode
    used_bytes: int

    @property
    def ratio(self) -> float:
        return self.used_bytes / self.quota_bytes if self.quota_bytes else 0.0

    @property
    def state(self) -> str:
        """``ok``, ``warning`` (80 % or more) or ``exceeded`` (at or over the quota)."""
        level = usage_db.level_of(self.used_bytes, self.quota_bytes)
        return 'exceeded' if level >= 100 else 'warning' if level >= 80 else 'ok'


class Quotas:
    def __init__(self, db: Database, emitter: EventEmitter, metrics: Metrics) -> None:
        self.db = db
        self.emitter = emitter
        self.metrics = metrics

    async def refusal(self, repo: RepoConfig, incoming: int | None = None, *, kind: str = 'upload') -> str | None:
        """Why a hard quota refuses a write of ``incoming`` bytes (unknown: refused only once the quota is reached);
        None when it may go ahead. ``kind`` labels the refusal metric (``upload`` or ``fill``)."""
        storage = repo.storage
        if storage.quota_bytes is None or storage.quota_mode is not QuotaMode.HARD:
            return None
        used = (await usage_db.get(self.db, repo.id)).bytes
        if used < storage.quota_bytes and (incoming is None or used + incoming <= storage.quota_bytes):
            return None
        self.metrics.quota_refused(repo=repo.name, kind=kind)
        return (
            f'repository {repo.name} is over its quota: {quota_mod.human_bytes(used)} of '
            f'{quota_mod.human_bytes(storage.quota_bytes)} stored'
        )

    async def check(self, repo: RepoConfig, incoming: int | None = None) -> None:
        """Raise ``QuotaExceeded`` when a hard quota refuses an upload."""
        if (reason := await self.refusal(repo, incoming)) is not None:
            raise QuotaExceeded(reason)

    async def written(self, repo: RepoConfig) -> None:
        """After an asset write or delete: announce a threshold newly reached (or re-arm one usage fell below).
        Never fails the caller."""
        quota = repo.storage.quota_bytes
        if quota is None:
            return
        try:
            current = await usage_db.get(self.db, repo.id)
            event = await quota_mod.evaluate(
                self.db,
                current,
                repo=repo.name,
                format=repo.format,
                quota=quota,
                mode=repo.storage.quota_mode.value,
                now=datetime.now(UTC),
            )
        except Exception as exc:  # alerts are best effort; the hourly recount evaluates again
            log.warning('quota evaluation failed', extra={'repo': repo.name, 'error': type(exc).__name__})
            return
        if event is not None:
            self.emitter.activity(event)
            self.metrics.quota_alert(repo=repo.name, level=int(event.detail.get('level') or 0))
            log.warning('repository quota threshold reached', extra={'repo': repo.name, 'detail': event.detail})

    async def statuses(self, repos: list[RepoConfig]) -> dict[str, QuotaStatus]:
        """Usage against the quota of every repository in ``repos`` that has one (one query)."""
        limited = [r for r in repos if r.storage.quota_bytes is not None]
        if not limited:
            return {}
        usage = await usage_db.get_many(self.db, [r.id for r in limited])
        return {
            r.id: QuotaStatus(
                quota_bytes=r.storage.quota_bytes or 0, mode=r.storage.quota_mode, used_bytes=usage[r.id].bytes
            )
            for r in limited
        }


class GuardKeeper:
    """Keeps ``BlobStore``'s free-space floor in step with the low-disk guard settings."""

    def __init__(self, db: Database, blobs: BlobStore, *, default_min_free_bytes: int) -> None:
        self.db = db
        self.blobs = blobs
        self.default_min_free_bytes = default_min_free_bytes
        self.current = storage_guard.GuardSettings(min_free_bytes=default_min_free_bytes)
        self._task: asyncio.Task[None] | None = None

    def apply(self, settings: storage_guard.GuardSettings) -> None:
        self.current = settings
        self.blobs.min_free_bytes = settings.min_free_bytes
        self.blobs.min_free_percent = settings.min_free_percent

    async def refresh(self) -> storage_guard.GuardSettings:
        settings = await storage_guard.load(self.db, default_min_free_bytes=self.default_min_free_bytes)
        self.apply(settings)
        return settings

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name='storage-guard')

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the last known settings
                log.warning('could not load the low-disk guard settings', extra={'error': type(exc).__name__})
            await asyncio.sleep(REFRESH_S)
