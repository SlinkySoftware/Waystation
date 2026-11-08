import json
import math
from datetime import UTC, datetime
from typing import Any

import pytest

from waystation_common.events import ActivityEvent, CacheState, Direction, EventEmitter, EventType, Transfer, keys


class FakePipe:
    def __init__(self, owner: FakeRedis) -> None:
        self.owner = owner
        self.cmds: list[tuple[Any, ...]] = []

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.cmds.append((name, *args, *sorted(kwargs.items())))

        return record

    async def execute(self) -> None:
        if self.owner.fail:
            raise ConnectionError('valkey down')
        self.owner.batches.append(self.cmds)

    async def __aenter__(self) -> FakePipe:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeRedis:
    def __init__(self) -> None:
        self.batches: list[list[tuple[Any, ...]]] = []
        self.fail = False

    def pipeline(self, transaction: bool = True) -> FakePipe:
        assert transaction is False
        return FakePipe(self)

    @property
    def cmds(self) -> list[tuple[Any, ...]]:
        return [c for batch in self.batches for c in batch]


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_transfer(clock: Clock, tid: str = 't1', **kw: Any) -> Transfer:
    now = datetime.fromtimestamp(clock.t, UTC)
    base: dict[str, Any] = {
        'id': tid,
        'repo_id': 'r1',
        'repo': 'npm-proxy',
        'format': 'npm',
        'path': 'left-pad/-/left-pad-1.3.0.tgz',
        'cache': CacheState.FETCHING,
        'started_at': now,
        'updated_at': now,
    }
    return Transfer(**(base | kw))


def by_name(cmds: list[tuple[Any, ...]], name: str) -> list[tuple[Any, ...]]:
    return [c for c in cmds if c[0] == name]


def test_latency_buckets_and_percentiles() -> None:
    assert keys.latency_bucket(0) == 'le_1'
    assert keys.latency_bucket(1) == 'le_1'
    assert keys.latency_bucket(1.01) == 'le_2'
    assert keys.latency_bucket(250) == 'le_500'
    assert keys.latency_bucket(10**9) == 'le_inf'
    assert keys.percentile({}, 0.5) is None
    hist = {'le_1': 50, 'le_10': 45, 'le_1000': 4, 'le_inf': 1}
    assert keys.percentile(hist, 0.5) == 1.0
    assert keys.percentile(hist, 0.95) == 10.0
    assert keys.percentile(hist, 0.99) == 1000.0
    assert keys.percentile(hist, 1.0) == math.inf
    assert keys.percentile(hist, 0.0) == 1.0


def test_keys() -> None:
    assert keys.minute_of(119.9) == 1
    assert keys.cnt_key(5, 'r') == 'cnt:5:r'
    assert keys.lat_key(5, 'r') == 'lat:5:r'
    assert keys.parse_pending(keys.pending_member(29_000_000, '01ABC')) == (29_000_000, '01ABC')


async def test_transfer_lifecycle_and_counters() -> None:
    redis, clock = FakeRedis(), Clock()
    em = EventEmitter(redis, clock=clock)  # type: ignore[arg-type]
    em.transfer_started(make_transfer(clock, bytes_total=1000))
    assert em.live_transfers == 1
    clock.t += 0.5
    em.transfer_progress('t1', 400, upstream_latency_ms=12.0)
    clock.t += 0.5
    em.transfer_progress('t1', 900, bytes_total=1000, cache=CacheState.FETCHING)
    em.transfer_progress('unknown', 1)  # ignored
    await em.flush()
    first = redis.batches[0]
    # started: hset + publish + xadd; then one coalesced progress (latest state only)
    assert [c[0] for c in first] == ['hset', 'publish', 'xadd', 'hset', 'publish']
    progress = json.loads(first[3][3])
    assert progress['bytes_done'] == 900
    assert progress['rate_bps'] > 0
    assert progress['upstream_latency_ms'] == 12.0
    message = json.loads(first[4][2])
    assert message['type'] == 'transfer.progress'

    clock.t += 1.0
    em.transfer_finished('t1', status=200)
    em.transfer_finished('t1', status=200)  # second call ignored
    await em.flush()
    cmds = redis.batches[1]
    assert by_name(cmds, 'hdel') == [('hdel', keys.TRANSFERS, 't1')]
    assert json.loads(by_name(cmds, 'publish')[0][2])['type'] == 'transfer.finished'
    activity = json.loads(by_name(cmds, 'xadd')[0][2]['data'])
    assert activity['type'] == 'transfer.finished'
    assert activity['duration_ms'] == pytest.approx(2000, abs=1)
    minute = keys.minute_of(clock.t)
    incr = {c[2]: c[3] for c in by_name(cmds, 'hincrby') if c[1].startswith('cnt:')}
    assert incr == {'requests': 1, 'misses': 1, 'bytes_out_upstream': 900, 'bytes_in_upstream': 900}
    assert ('hincrby', keys.lat_key(minute, 'r1'), 'le_2000', 1) in cmds
    assert ('sadd', keys.STATS_PENDING, f'{minute}:r1') in cmds
    assert ('hsetnx', keys.cnt_key(minute, 'r1'), 'format', 'npm') in cmds
    assert em.live_transfers == 0


async def test_progress_is_coalesced_per_flush() -> None:
    redis, clock = FakeRedis(), Clock()
    em = EventEmitter(redis, clock=clock)  # type: ignore[arg-type]
    em.transfer_started(make_transfer(clock))
    await em.flush()
    for i in range(1000):
        clock.t += 0.001
        em.transfer_progress('t1', i)
    await em.flush()
    assert len(by_name(redis.batches[1], 'publish')) == 1
    await em.flush()  # nothing dirty, nothing sent
    assert len(redis.batches) == 2


@pytest.mark.parametrize(
    ('cache', 'expected'),
    [
        (CacheState.HIT, {'requests': 1, 'hits': 1, 'bytes_out_cache': 10}),
        (CacheState.REVALIDATED, {'requests': 1, 'hits': 1, 'bytes_out_cache': 10}),
        (CacheState.STALE, {'requests': 1, 'stale': 1, 'bytes_out_cache': 10}),
        (CacheState.FETCHING, {'requests': 1, 'misses': 1, 'bytes_out_upstream': 10}),
        (CacheState.COALESCED, {'requests': 1, 'coalesced': 1, 'bytes_out_upstream': 10}),
        (CacheState.ERROR, {'requests': 1, 'errors': 1}),
    ],
)
async def test_request_counting(cache: CacheState, expected: dict[str, int]) -> None:
    redis, clock = FakeRedis(), Clock()
    em = EventEmitter(redis, clock=clock)  # type: ignore[arg-type]
    em.request(repo_id='r', format='raw', cache=cache, duration_ms=3, bytes_out=10)
    await em.flush()
    assert {c[2]: c[3] for c in by_name(redis.cmds, 'hincrby') if c[1].startswith('cnt:')} == expected


async def test_upload_and_failed_transfer() -> None:
    redis, clock = FakeRedis(), Clock()
    em = EventEmitter(redis, clock=clock)  # type: ignore[arg-type]
    em.transfer_started(make_transfer(clock, 'up', direction=Direction.UPLOAD, cache=CacheState.FETCHING))
    em.transfer_finished('up', status=201, bytes_done=50)
    em.transfer_started(make_transfer(clock, 'bad'))
    em.transfer_finished('bad', status=502, error='upstream reset')
    await em.flush()
    cnt = [c for c in by_name(redis.cmds, 'hincrby') if c[1].startswith('cnt:')]
    assert ('hincrby', keys.cnt_key(keys.minute_of(clock.t), 'r1'), 'bytes_in_upload', 50) in cnt
    assert any(c[2] == 'errors' for c in cnt)
    failed = [json.loads(c[2]['data']) for c in by_name(redis.cmds, 'xadd') if c[2]['type'] == 'transfer.failed']
    assert failed[0]['detail'] == {'error': 'upstream reset'}
    assert failed[0]['cache'] == 'ERROR'


async def test_activity_and_queue_bound_and_failures(caplog: pytest.LogCaptureFixture) -> None:
    redis, clock = FakeRedis(), Clock()
    em = EventEmitter(redis, clock=clock, max_queue=2)  # type: ignore[arg-type]
    event = ActivityEvent(type=EventType.ASSET_DELETED, ts=datetime.now(UTC), repo='raw-hosted', path='a/b')
    for _ in range(3):
        em.activity(event)
    assert em.dropped == 1
    redis.fail = True
    await em.flush()  # never raises
    await em.flush()  # nothing queued any more
    assert em.failed_flushes == 1
    assert 'event flush failed' in caplog.text
    redis.fail = False
    em.activity(event)
    await em.flush()
    assert by_name(redis.cmds, 'xadd')[0][1] == keys.ACTIVITY


async def test_background_loop_flushes_and_final_flush() -> None:
    redis, clock = FakeRedis(), Clock()
    async with EventEmitter(redis, clock=clock, flush_interval_s=0.01) as em:  # type: ignore[arg-type]
        await em.start()  # idempotent
        em.request(repo_id='r', format='raw', cache=CacheState.HIT, duration_ms=1)
    assert by_name(redis.cmds, 'sadd')
    await em.aclose()  # idempotent
