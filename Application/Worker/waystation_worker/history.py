"""Long-term stats (ARCHITECTURE §8; T-F09), run by the stats rollup after the minutes:

- **Hour and day rollups**: every closed hour (ended ``HOUR_GRACE_S`` ago, so its minutes are in ``stats_minute``)
  becomes one ``stats_hour`` document per repository with traffic in it, and every closed day whose hours are all
  rolled up one ``stats_day`` document; each written once (``waystation_common.db.stats.insert_period``). Progress is
  kept in Valkey (``stats:rollup:hour`` / ``stats:rollup:day``, the start of the next period to roll); without it
  (first run, Valkey lost its data) the rollup resumes after the newest rolled-up document, or from the oldest source
  document. At most ``MAX_PERIODS`` periods per run, so a backlog is caught up over a few runs.
- **Storage snapshots** (``storage_snapshots``, hourly): per repository ``bytes`` / ``assets`` / ``artifacts``
  (``repo_usage``), ``components`` and ``dedup_saved_bytes`` (its stored bytes minus its share of the blobs it uses:
  a blob used by n repositories counts 1/n in each), plus the instance-wide document (``m.repo_id`` = ''): blob
  bytes and count, stored bytes, artifacts and components. Written once per hour (the instance-wide document last).
- **Component index**: a batch of ``waystation_common.db.components.index_batch`` every run and, every
  ``SWEEP_EVERY_S``, a batch of the rolling sweep.

Retention: the time-series collections expire their documents (minutes 7 days, hours 90 days, days and snapshots 2
years; ``downloads_daily`` 400 days), see ``waystation_common.db.collections``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from waystation_common.db import components as components_db
from waystation_common.db import stats as stats_db
from waystation_common.db.client import Database
from waystation_common.db.collections import (
    ASSETS,
    BLOBS,
    COMPONENTS,
    REPO_USAGE,
    REPOSITORIES,
    STATS_DAY,
    STATS_HOUR,
    STATS_MINUTE,
    STORAGE_SNAPSHOTS,
)
from waystation_common.events import keys

log = logging.getLogger('waystation.worker.history')

HOUR = 3600
DAY = 86400
HOUR_GRACE_S = 120  # minutes close 5 s after their end and are rolled up within 10 s: two minutes is ample
DAY_GRACE_S = 2 * HOUR  # the day's last hour is rolled up first
MAX_PERIODS = 48
SWEEP_EVERY_S = 30  # one batch of the component sweep (``components_db.SWEEP_BATCH``) every 30 s


@dataclass(frozen=True)
class Periods:
    hours: int = 0
    days: int = 0


def _dt(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def _epoch(value: datetime) -> int:
    return int(value.replace(tzinfo=UTC).timestamp() if value.tzinfo is None else value.timestamp())


async def _get_int(redis: Redis, key: str) -> int | None:
    raw = await redis.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _start(redis: Redis, db: Database, key: str, target: str, source: str, step: int) -> int | None:
    """The first period not rolled up yet (aligned to ``step``), or None when there is nothing to roll."""
    stored = await _get_int(redis, key)
    if stored is not None:
        return stored
    newest = await stats_db.newest(db, target)
    if newest is not None:
        return (_epoch(newest) // step) * step + step
    oldest = await stats_db.oldest(db, source)
    if oldest is not None:
        return (_epoch(oldest) // step) * step
    return None


async def _roll(redis: Redis, db: Database, *, key: str, target: str, source: str, step: int, until: int) -> int:
    """Roll ``source`` periods of ``step`` seconds into ``target`` up to ``until`` (exclusive end)."""
    start = await _start(redis, db, key, target, source, step)
    written = 0
    rolled = 0
    while start is not None and start + step <= until and rolled < MAX_PERIODS:
        docs = await stats_db.aggregate_period(db, source, _dt(start), _dt(start + step))
        written += await stats_db.insert_period(db, target, docs)
        start += step
        rolled += 1
        await redis.set(key, str(start))
    return written


async def rollup_periods(redis: Redis, db: Database, *, now: float) -> Periods:
    """Closed hours → ``stats_hour``, then closed days → ``stats_day``."""
    hours = await _roll(
        redis,
        db,
        key=keys.ROLLUP_HOUR,
        target=STATS_HOUR,
        source=STATS_MINUTE,
        step=HOUR,
        until=int(now) - HOUR_GRACE_S,
    )
    hours_done = await _get_int(redis, keys.ROLLUP_HOUR) or 0
    days = await _roll(
        redis,
        db,
        key=keys.ROLLUP_DAY,
        target=STATS_DAY,
        source=STATS_HOUR,
        step=DAY,
        until=min(int(now) - DAY_GRACE_S, hours_done),
    )
    return Periods(hours=hours, days=days)


# ---------------------------------------------------------------------------- storage snapshots
async def _component_counts(db: Database) -> dict[str, int]:
    pipeline = [{'$group': {'_id': '$repo_id', 'n': {'$sum': 1}}}]
    return {str(doc['_id']): int(doc['n']) async for doc in await db[COMPONENTS].aggregate(pipeline)}


async def _attributed(db: Database) -> dict[str, float]:
    """Per repository: its share of the blob bytes it uses (a blob used by n repositories: size / n each)."""
    pipeline: list[dict[str, Any]] = [
        {'$group': {'_id': '$blob_sha256', 'size': {'$max': '$size'}, 'repos': {'$addToSet': '$repo_id'}}},
        {'$project': {'size': 1, 'repos': 1, 'n': {'$size': '$repos'}}},
        {'$unwind': '$repos'},
        {'$group': {'_id': '$repos', 'bytes': {'$sum': {'$divide': ['$size', '$n']}}}},
    ]
    cursor = await db[ASSETS].aggregate(pipeline, allowDiskUse=True)
    return {str(doc['_id']): float(doc['bytes']) async for doc in cursor}


async def _blobs(db: Database) -> tuple[int, int]:
    pipeline = [{'$group': {'_id': None, 'count': {'$sum': 1}, 'bytes': {'$sum': '$size'}}}]
    async for doc in await db[BLOBS].aggregate(pipeline):
        return int(doc['count']), int(doc['bytes'])
    return 0, 0


async def take_snapshot(db: Database, ts: datetime) -> int:
    """Write the snapshot documents of ``ts`` (skipping repositories that have one); returns the documents written."""
    formats = {str(doc['_id']): str(doc.get('format') or '') async for doc in db[REPOSITORIES].find({}, {'format': 1})}
    usage = {str(doc['_id']): doc async for doc in db[REPO_USAGE].find({})}
    counts = await _component_counts(db)
    attributed = await _attributed(db)
    docs: list[dict[str, Any]] = []
    totals = {'bytes': 0, 'assets': 0, 'artifacts': 0, 'components': 0, 'dedup_saved_bytes': 0}
    for repo_id, fmt in sorted(formats.items()):
        used = usage.get(repo_id, {})
        stored = max(0, int(used.get('bytes') or 0))
        figures = {
            'bytes': stored,
            'assets': max(0, int(used.get('assets') or 0)),
            'artifacts': max(0, int(used.get('artifacts') or 0)),
            'components': counts.get(repo_id, 0),
            'dedup_saved_bytes': max(0, stored - round(attributed.get(repo_id, stored))),
        }
        for field in totals:
            totals[field] += figures[field]
        docs.append({'ts': ts, 'm': {'repo_id': repo_id, 'format': fmt}, **figures})
    written = await stats_db.insert_period(db, STORAGE_SNAPSHOTS, docs) if docs else 0
    blob_count, blob_bytes = await _blobs(db)
    instance = {'ts': ts, 'm': {'repo_id': '', 'format': ''}, **totals, 'blobs': blob_count, 'blob_bytes': blob_bytes}
    written += await stats_db.insert_period(db, STORAGE_SNAPSHOTS, [instance])
    return written


async def snapshot_storage(redis: Redis, db: Database, *, now: float) -> bool:
    """Take this hour's storage snapshot unless it is taken; True when one was written."""
    hour = (int(now) // HOUR) * HOUR
    if await _get_int(redis, keys.SNAPSHOT_HOUR) == hour:
        return False
    ts = _dt(hour)
    taken = await stats_db.snapshot_exists(db, ts)
    if not taken:
        await take_snapshot(db, ts)
    await redis.set(keys.SNAPSHOT_HOUR, str(hour))
    return not taken


# ---------------------------------------------------------------------------- components
async def index_components(redis: Redis, db: Database) -> components_db.IndexResult:
    """Link new assets to their components, then recompute the next batch of the rolling sweep."""
    result = await components_db.index_batch(db)
    if not await redis.set(f'{keys.COMPONENTS_SWEEP}:due', '1', nx=True, ex=SWEEP_EVERY_S):
        return result
    raw = await redis.get(keys.COMPONENTS_SWEEP)
    after = (raw.decode() if isinstance(raw, bytes) else str(raw)) if raw else None
    cursor, _updated, removed = await components_db.sweep_batch(db, after)
    if cursor is None:
        await redis.delete(keys.COMPONENTS_SWEEP)
    else:
        await redis.set(keys.COMPONENTS_SWEEP, cursor)
    if removed:
        log.info('components without files removed', extra={'removed': removed})
    return result


__all__ = ['Periods', 'index_components', 'rollup_periods', 'snapshot_storage', 'take_snapshot']
