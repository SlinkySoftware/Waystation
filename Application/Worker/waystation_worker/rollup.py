"""Per-minute stats rollup (ARCHITECTURE §8): Valkey counters of **closed** minutes -> ``stats_minute``, exactly once.

Every ``INTERVAL_S`` the worker holding the ``lease:rollup`` lease (one roller across replicas):

1. reads ``stats:pending``; for each ``<minute>:<repo_id>`` whose minute ended more than ``GRACE_S`` ago, builds
   the document from ``cnt:``/``lat:`` hashes and inserts it unless one already exists for (repo, minute);
2. only then deletes the hashes and the pending member.

A crash between 1 and 2 re-runs the minute, finds the document and skips the insert: no duplicates. Late
increments for an already rolled-up minute (beyond the grace) are discarded the same way.

Also sweeps ``transfers`` entries not updated for ``STALE_TRANSFER_S`` (their process died).

Minimal M0 loop; the job framework (T-F08) will host it later, with hour/day rollups, snapshots and last-seen
flushes (T-F09 full).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from waystation_common.db import stats as stats_db
from waystation_common.db.client import Database, create_client, database
from waystation_common.events import keys
from waystation_common.settings import Settings
from waystation_common.valkey import create_valkey

log = logging.getLogger('waystation.worker.rollup')

INTERVAL_S = 10.0
GRACE_S = 5.0
LEASE_KEY = 'lease:rollup'
LEASE_TTL_MS = 30_000
STALE_TRANSFER_S = 300.0

# Acquire the lease, or renew it if we already hold it. Returns 1 when held.
_LEASE_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2]) return 1 end
if v == ARGV[1] then redis.call('PEXPIRE', KEYS[1], ARGV[2]) return 1 end
return 0
"""


def _str(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


async def _hgetall(redis: Redis, key: str) -> dict[str, str]:
    return {_str(k): _str(v) for k, v in (await redis.hgetall(key)).items()}


async def hold_lease(redis: Redis, owner: str, *, key: str = LEASE_KEY, ttl_ms: int = LEASE_TTL_MS) -> bool:
    result = await redis.eval(_LEASE_LUA, 1, key, owner, str(ttl_ms))
    return bool(int(result))


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


async def rollup_once(redis: Redis, db: Database, owner: str, *, now: float | None = None) -> int | None:
    """One cycle if we hold the lease; None when another worker does."""
    if not await hold_lease(redis, owner):
        return None
    inserted = await rollup_closed_minutes(redis, db, now=now)
    swept = await sweep_stale_transfers(redis, now=now)
    if inserted or swept:
        log.info('stats rolled up', extra={'minutes': inserted, 'stale_transfers': swept})
    return inserted


async def rollup_loop(settings: Settings, stop: asyncio.Event) -> None:  # pragma: no cover - wiring, run by the stack
    owner = uuid.uuid4().hex
    redis = create_valkey(settings)
    client = create_client(settings, app_name='waystation-worker')
    db = database(client, settings)
    try:
        while not stop.is_set():
            try:
                await rollup_once(redis, db, owner)
            except Exception:  # Valkey/MongoDB outage: log and retry next cycle
                log.exception('stats rollup failed')
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=INTERVAL_S)
    finally:
        await redis.aclose()
        await client.close()
