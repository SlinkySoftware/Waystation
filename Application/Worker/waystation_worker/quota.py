"""Quota accounting and the low-disk alert (SPECIFICATION §4.8): the ``quota.reconcile`` and ``storage.check`` jobs.

- ``reconcile``: recount every repository's stored bytes from ``assets`` into ``repo_usage`` (fixing drift of the
  incremental counters the server keeps), then announce quota thresholds the recount reached (``quota.warning`` at
  80 %, ``quota.exceeded`` at 100 %) and re-arm the ones usage fell below.
- ``check_storage``: free space of the data filesystem against the low-disk guard (settings document ``storage``, or
  ``WAYSTATION_MIN_FREE_GB``). A change of state is announced once across workers (``storage.low`` when the Valkey
  key ``storage:low`` is created, ``storage.ok`` when it is removed).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis

from waystation_common import quota
from waystation_common.db import storage_guard
from waystation_common.db import usage as usage_db
from waystation_common.db.client import Database
from waystation_common.events import EventEmitter, keys
from waystation_common.storage import BlobStore

log = logging.getLogger('waystation.worker.quota')

STATE_TTL_S = 7 * 86400  # the state key outlives any sane check interval; a lost key only repeats an alert


@dataclass(frozen=True)
class ReconcileResult:
    repositories: int
    drifted: int
    drift_bytes: int
    announced: int

    def as_dict(self) -> dict[str, int]:
        return {
            'repositories': self.repositories,
            'drifted': self.drifted,
            'drift_bytes': self.drift_bytes,
            'announced': self.announced,
        }


async def reconcile(db: Database, emitter: EventEmitter | None, *, now: datetime) -> ReconcileResult:
    repos = await usage_db.repositories(db)
    results = await usage_db.reconcile(db, [r.repo_id for r in repos], now=now)
    drifted = [r for r in results if r.drift_bytes]
    current = await usage_db.get_many(db, [r.repo_id for r in repos])
    announced = 0
    for repo in repos:
        if repo.quota_bytes is None and current[repo.repo_id].alert_level == 0:
            continue
        event = await quota.evaluate(
            db,
            current[repo.repo_id],
            repo=repo.name,
            format=repo.format,
            quota=repo.quota_bytes,
            mode='hard' if repo.quota_mode == 'hard' else 'soft',
            now=now,
        )
        if event is not None and emitter is not None:
            emitter.activity(event)
            announced += 1
    for r in drifted:
        log.info('repository usage corrected', extra={'repo_id': r.repo_id, 'drift_bytes': r.drift_bytes})
    return ReconcileResult(
        repositories=len(results),
        drifted=len(drifted),
        drift_bytes=sum(abs(r.drift_bytes) for r in drifted),
        announced=announced,
    )


@dataclass(frozen=True)
class StorageState:
    low: bool
    free_bytes: int
    total_bytes: int
    floor_bytes: int
    changed: bool

    def as_dict(self) -> dict[str, int | bool]:
        return {
            'low': self.low,
            'free_bytes': self.free_bytes,
            'total_bytes': self.total_bytes,
            'floor_bytes': self.floor_bytes,
            'changed': self.changed,
        }


async def check_storage(
    db: Database,
    valkey: Redis,
    store: BlobStore,
    emitter: EventEmitter | None,
    *,
    default_min_free_bytes: int,
    now: datetime,
) -> StorageState:
    guard = await storage_guard.load(db, default_min_free_bytes=default_min_free_bytes)
    free, total = await store.space()
    floor = guard.floor(total)
    low = free < floor
    if low:
        changed = bool(await valkey.set(keys.STORAGE_LOW, now.isoformat(), nx=True, ex=STATE_TTL_S))
        if not changed:
            await valkey.expire(keys.STORAGE_LOW, STATE_TTL_S)
    else:
        changed = bool(await valkey.delete(keys.STORAGE_LOW))
    if changed:
        log.warning('low-disk guard %s', 'tripped' if low else 'cleared', extra={'free_bytes': free, 'floor': floor})
        if emitter is not None:
            emitter.activity(quota.storage_event(low=low, free=free, floor=floor, total=total, now=now))
    return StorageState(low=low, free_bytes=free, total_bytes=total, floor_bytes=floor, changed=changed)
