import asyncio
import json
import time
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis

from waystation_common.db import seen
from waystation_common.db import stats as stats_db
from waystation_common.db.client import Database
from waystation_common.db.collections import API_TOKENS, SESSIONS
from waystation_common.db.schema import ensure_schema
from waystation_common.events import CacheState, EventEmitter, Transfer, keys
from waystation_common.leases import hold_lease, release_lease
from waystation_worker import rollup

pytestmark = pytest.mark.integration

MINUTE = keys.minute_of(time.time()) - 10  # a minute in the recent past (inside the 7-day TTL)
TS = MINUTE * 60 + 1.0


class FixedClock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


async def emit_requests(redis: Redis, repo_id: str, n: int, ts: float = TS) -> None:
    em = EventEmitter(redis, clock=FixedClock(ts))
    for i in range(n):
        em.request(
            repo_id=repo_id,
            format='npm',
            cache=CacheState.HIT if i % 2 else CacheState.FETCHING,
            duration_ms=float(i),
            bytes_out=100,
        )
    await em.flush()


async def test_rollup_closed_minutes_exactly_once(valkey: Redis, mongo_db: Database) -> None:
    await ensure_schema(mongo_db)
    await emit_requests(valkey, 'repo-a', 10)
    await emit_requests(valkey, 'repo-b', 3)

    # minute not closed yet (inside the grace period): nothing written, keys kept
    assert await rollup.rollup_closed_minutes(valkey, mongo_db, now=(MINUTE + 1) * 60 + 1) == 0
    assert await valkey.scard(keys.STATS_PENDING) == 2

    assert await rollup.rollup_closed_minutes(valkey, mongo_db, now=(MINUTE + 1) * 60 + 6) == 2
    since = datetime.fromtimestamp(MINUTE * 60, UTC)
    docs = await stats_db.minutes(mongo_db, 'repo-a', since)
    assert len(docs) == 1
    doc = docs[0]
    assert doc['ts'] == since
    assert doc['m'] == {'repo_id': 'repo-a', 'format': 'npm'}
    assert (doc['requests'], doc['hits'], doc['misses']) == (10, 5, 5)
    assert doc['bytes_out_cache'] == doc['bytes_out_upstream'] == 500
    assert sum(doc['lat_hist'].values()) == 10
    assert keys.percentile(doc['lat_hist'], 0.5) == 5.0
    assert await valkey.exists(keys.cnt_key(MINUTE, 'repo-a'), keys.lat_key(MINUTE, 'repo-a')) == 0
    assert await valkey.scard(keys.STATS_PENDING) == 0

    # a crash after the insert but before the cleanup replays the same minute: no duplicate
    await emit_requests(valkey, 'repo-a', 4)
    assert await rollup.rollup_closed_minutes(valkey, mongo_db) == 0
    assert len(await stats_db.minutes(mongo_db, 'repo-a', since)) == 1
    assert await valkey.scard(keys.STATS_PENDING) == 0


async def test_lease_single_roller(valkey: Redis, mongo_db: Database) -> None:
    await ensure_schema(mongo_db)
    lease = {'key': rollup.LEASE_KEY, 'ttl_ms': rollup.LEASE_TTL_MS}
    assert await hold_lease(valkey, 'w1', **lease)
    assert await hold_lease(valkey, 'w1', **lease)  # renew
    assert not await hold_lease(valkey, 'w2', **lease)
    assert await rollup.rollup_once(valkey, mongo_db, 'w2') is None
    await emit_requests(valkey, 'repo-c', 2)
    assert await rollup.rollup_once(valkey, mongo_db, 'w1') == 1
    assert await valkey.exists(rollup.LEASE_KEY) == 0  # released after the cycle
    assert not await release_lease(valkey, 'w2', key=rollup.LEASE_KEY)


async def test_flush_last_seen(valkey: Redis, mongo_db: Database) -> None:
    """Token last-used and session last-seen go from Valkey to MongoDB in bulk, only forwards (ARCH §8)."""
    await ensure_schema(mongo_db)
    early, late = datetime(2026, 11, 8, 10, tzinfo=UTC), datetime(2026, 11, 8, 11, tzinfo=UTC)
    await mongo_db[API_TOKENS].insert_many(
        [
            {'_id': 't1', 'hash': 'h1', 'last_used_at': None},
            {'_id': 't2', 'hash': 'h2', 'last_used_at': late, 'last_used_ip': '192.0.2.9'},
        ]
    )
    await mongo_db[SESSIONS].insert_one({'_id': 's1', 'last_seen_at': early})
    await valkey.hset(
        seen.SEEN_TOKENS,
        mapping={'t1': f'{int(late.timestamp())} 192.0.2.1', 't2': f'{int(early.timestamp())} 192.0.2.2', 'tx': 'bad'},
    )
    await valkey.hset(seen.SEEN_SESSIONS, 's1', str(int(late.timestamp())))
    assert await rollup.flush_last_seen(valkey, mongo_db) == 2
    t1 = await mongo_db[API_TOKENS].find_one({'_id': 't1'})
    t2 = await mongo_db[API_TOKENS].find_one({'_id': 't2'})
    s1 = await mongo_db[SESSIONS].find_one({'_id': 's1'})
    assert t1 is not None and t1['last_used_at'] == late and t1['last_used_ip'] == '192.0.2.1'
    assert t2 is not None and t2['last_used_ip'] == '192.0.2.9'  # an older use never moves it back
    assert s1 is not None and s1['last_seen_at'] == late
    assert not await valkey.exists(seen.SEEN_TOKENS, seen.SEEN_SESSIONS, f'{seen.SEEN_TOKENS}:flushing')
    assert await rollup.flush_last_seen(valkey, mongo_db) == 0
    # a hash left by a crashed cycle is flushed first
    await valkey.hset(f'{seen.SEEN_TOKENS}:flushing', 't1', f'{int(late.timestamp()) + 60} 192.0.2.3')
    assert await rollup.flush_last_seen(valkey, mongo_db) == 1


async def test_sweep_stale_transfers(valkey: Redis) -> None:
    now = time.time()
    fresh = {'updated_at': datetime.fromtimestamp(now, UTC).isoformat()}
    old = {'updated_at': datetime.fromtimestamp(now - 3600, UTC).isoformat()}
    await valkey.hset(keys.TRANSFERS, mapping={'fresh': json.dumps(fresh), 'old': json.dumps(old), 'bad': 'x'})
    assert await rollup.sweep_stale_transfers(valkey, now=now) == 2
    assert await valkey.hkeys(keys.TRANSFERS) == ['fresh']
    assert await rollup.sweep_stale_transfers(valkey, now=now) == 0


async def test_emitter_against_valkey_live_rate_and_state(valkey: Redis) -> None:
    pubsub = valkey.pubsub()
    await pubsub.subscribe(keys.LIVE)
    await pubsub.get_message(timeout=1)  # subscribe confirmation
    now = datetime.now(UTC)
    async with EventEmitter(valkey, flush_interval_s=0.25) as em:
        em.transfer_started(
            Transfer(
                id='t1',
                repo_id='r',
                repo='raw',
                format='raw',
                path='f',
                cache=CacheState.FETCHING,
                started_at=now,
                updated_at=now,
            )
        )
        started = time.monotonic()
        while time.monotonic() - started < 1.0:  # ~1000 progress calls per second
            em.transfer_progress('t1', int((time.monotonic() - started) * 1e6))
            await asyncio.sleep(0.001)
        await em.flush()
        state = json.loads(await valkey.hget(keys.TRANSFERS, 't1'))
        assert state['bytes_done'] > 0
        em.transfer_finished('t1', status=200)
    messages = []
    while (msg := await pubsub.get_message(timeout=0.2)) is not None:
        messages.append(json.loads(msg['data']))
    await pubsub.aclose()
    progress = [m for m in messages if m['type'] == 'transfer.progress']
    assert 2 <= len(progress) <= 6  # <= 4 Hz over ~1 s (+ the explicit flush)
    assert messages[0]['type'] == 'transfer.started'
    assert messages[-1]['type'] == 'transfer.finished'
    assert await valkey.hlen(keys.TRANSFERS) == 0
    entries = await valkey.xrange(keys.ACTIVITY)
    assert [e[1]['type'] for e in entries] == ['transfer.started', 'transfer.finished']
