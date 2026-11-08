"""Per-minute stats rollup (ARCHITECTURE §8): Valkey counters of **closed** minutes -> ``stats_minute``, exactly once.

Each run, holding the ``lease:rollup`` lease (one roller across replicas):

1. reads ``stats:pending``; for each ``<minute>:<repo_id>`` whose minute ended more than ``GRACE_S`` ago, builds
   the document from ``cnt:``/``lat:`` hashes and inserts it unless one already exists for (repo, minute);
2. only then deletes the hashes and the pending member.

A crash between 1 and 2 re-runs the minute, finds the document and skips the insert: no duplicates. Late
increments for an already rolled-up minute (beyond the grace) are discarded the same way.

Also sweeps ``transfers`` entries not updated for ``STALE_TRANSFER_S`` (their process died) and flushes the
batched API token "last used" and session "last seen" times from Valkey to MongoDB (``flush_last_seen``).

Runs as the scheduled job ``stats.rollup`` (``waystation_worker.jobs.builtin``, every 10 s on the ``stats``
queue). The job system already runs one rollup at a time; the lease stays as a second guard because two concurrent
rollups could both insert a minute (the existence check is not atomic). Hour/day rollups and snapshots are T-F09
(full).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from waystation_common.db import seen
from waystation_common.db import stats as stats_db
from waystation_common.db.client import Database
from waystation_common.events import keys
from waystation_common.leases import hold_lease, release_lease

log = logging.getLogger('waystation.worker.rollup')

GRACE_S = 5.0
LEASE_KEY = 'lease:rollup'
LEASE_TTL_MS = 30_000
STALE_TRANSFER_S = 300.0


def _str(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


async def _hgetall(redis: Redis, key: str) -> dict[str, str]:
    return {_str(k): _str(v) for k, v in (await redis.hgetall(key)).items()}


def build_minute_doc(minute: int, repo_id: str, cnt: dict[str, str], lat: dict[str, str]) -> dict[str, Any]:
    doc: dict[str, Any] = {
        'ts': datetime.fromtimestamp(minute * 60, UTC),
        'm': {'repo_id': repo_id, 'format': cnt.get('format', '')},
    }
    for field in keys.COUNTERS:
        doc[field] = int(cnt.get(field, 0))
    doc['lat_hist'] = {field: int(lat[field]) for field in keys.LATENCY_FIELDS if field in lat}
    return doc


async def rollup_closed_minutes(redis: Redis, db: Database, *, now: float | None = None) -> int:
    """Roll every closed minute; returns the number of documents inserted."""
    now = time.time() if now is None else now
    inserted = 0
    for member in sorted(_str(m) for m in await redis.smembers(keys.STATS_PENDING)):
        minute, repo_id = keys.parse_pending(member)
        if (minute + 1) * 60 + GRACE_S > now:
            continue
        cnt_key, lat_key = keys.cnt_key(minute, repo_id), keys.lat_key(minute, repo_id)
        cnt = await _hgetall(redis, cnt_key)
        lat = await _hgetall(redis, lat_key)
        if cnt or lat:
            doc = build_minute_doc(minute, repo_id, cnt, lat)
            if not await stats_db.minute_exists(db, repo_id, doc['ts']):
                await stats_db.insert_minute(db, doc)
                inserted += 1
        await redis.delete(cnt_key, lat_key)
        await redis.srem(keys.STATS_PENDING, member)
    return inserted


async def sweep_stale_transfers(redis: Redis, *, now: float | None = None) -> int:
    now = time.time() if now is None else now
    stale: list[str] = []
    entries = await _hgetall(redis, keys.TRANSFERS)
    for transfer_id, raw in entries.items():
        try:
            updated = datetime.fromisoformat(json.loads(raw)['updated_at']).timestamp()
        except ValueError, KeyError, TypeError:
            updated = 0.0
        if now - updated > STALE_TRANSFER_S:
            stale.append(transfer_id)
    if stale:
        await redis.hdel(keys.TRANSFERS, *stale)
    return len(stale)


async def _drain(redis: Redis, key: str) -> dict[str, str]:
    """Atomically take the hash ``key`` (``RENAME`` to ``<key>:flushing``): writes after this go to a fresh hash.
    A ``:flushing`` hash left by a crashed cycle is taken first."""
    flushing = f'{key}:flushing'
    if not await redis.exists(flushing):
        try:
            await redis.rename(key, flushing)
        except ResponseError:  # no such key: nothing was used
            return {}
    return await _hgetall(redis, flushing)


async def flush_last_seen(redis: Redis, db: Database) -> int:
    """``seen:tokens`` / ``seen:sessions`` → MongoDB in bulk (ARCHITECTURE §8). Returns the records updated."""
    updated = 0
    for key, flush in ((seen.SEEN_TOKENS, seen.flush_tokens), (seen.SEEN_SESSIONS, seen.flush_sessions)):
        entries = await _drain(redis, key)
        if entries:
            updated += await flush(db, entries)
        await redis.delete(f'{key}:flushing')
    return updated


async def rollup_once(redis: Redis, db: Database, owner: str, *, now: float | None = None) -> int | None:
    """One cycle if we hold the lease (released afterwards); None when another worker holds it."""
    if not await hold_lease(redis, owner, key=LEASE_KEY, ttl_ms=LEASE_TTL_MS):
        return None
    try:
        inserted = await rollup_closed_minutes(redis, db, now=now)
        swept = await sweep_stale_transfers(redis, now=now)
        flushed = await flush_last_seen(redis, db)
    finally:
        await release_lease(redis, owner, key=LEASE_KEY)
    if inserted or swept or flushed:
        log.info('stats rolled up', extra={'minutes': inserted, 'stale_transfers': swept, 'last_seen': flushed})
    return inserted
