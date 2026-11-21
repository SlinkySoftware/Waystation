"""The emitter keeps up with 10,000 progress events per second (T-F09; ARCHITECTURE §8) against a real Valkey: 200
live transfers updated 50 times a second each, plus 1,000 counted requests a second, for 3 seconds.

Checked: the event loop stays responsive (a probe task's wake-up lateness), the emitter calls are cheap, every flush
lands (nothing dropped or failed, the queue drained), progress is published at most 4 times a second per transfer, and
the counters are exact."""

import asyncio
import statistics
import time
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis

from waystation_common.events import CacheState, EventEmitter, Transfer, keys

pytestmark = pytest.mark.integration

TRANSFERS = 200
PROGRESS_PER_S = 10_000
REQUESTS_PER_S = 1_000
SECONDS = 3.0
TICK_S = 0.01


async def test_ten_thousand_progress_events_per_second(valkey: Redis) -> None:
    emitter = EventEmitter(valkey)
    await emitter.start()
    published: dict[str, int] = {}
    pubsub = valkey.pubsub()
    await pubsub.subscribe(keys.LIVE)

    async def listen() -> None:
        async for message in pubsub.listen():
            if message.get('type') == 'message' and '"transfer.progress"' in message['data']:
                tid = message['data'].split('"id":"', 1)[1].split('"', 1)[0]
                published[tid] = published.get(tid, 0) + 1

    listener = asyncio.create_task(listen())
    lateness: list[float] = []
    stop = asyncio.Event()

    async def probe() -> None:
        while not stop.is_set():
            before = time.perf_counter()
            await asyncio.sleep(0.005)
            lateness.append(time.perf_counter() - before - 0.005)

    prober = asyncio.create_task(probe())
    now = datetime.now(UTC)
    for i in range(TRANSFERS):
        emitter.transfer_started(
            Transfer(
                id=f't{i}',
                repo_id='repo-load',
                repo='load',
                format='raw',
                path=f'files/{i}.bin',
                cache=CacheState.FETCHING,
                bytes_total=10**9,
                started_at=now,
                updated_at=now,
            )
        )
    per_tick = int(PROGRESS_PER_S * TICK_S)
    requests_per_tick = int(REQUESTS_PER_S * TICK_S)
    spent = 0.0
    events = requests = 0
    done = [0] * TRANSFERS
    start = time.perf_counter()
    tick = 0
    while time.perf_counter() - start < SECONDS:
        t0 = time.perf_counter()
        for j in range(per_tick):
            k = (tick * per_tick + j) % TRANSFERS
            done[k] += 4096
            emitter.transfer_progress(f't{k}', done[k])
        for _ in range(requests_per_tick):
            emitter.request(repo_id='repo-load', format='raw', cache=CacheState.HIT, duration_ms=2.0, bytes_out=10)
        spent += time.perf_counter() - t0
        events += per_tick
        requests += requests_per_tick
        tick += 1
        await asyncio.sleep(max(0.0, start + tick * TICK_S - time.perf_counter()))
    elapsed = time.perf_counter() - start
    for i in range(TRANSFERS):
        emitter.transfer_finished(f't{i}', status=200)
    await emitter.aclose()
    stop.set()
    await prober
    await asyncio.sleep(0.2)  # let the last messages arrive
    listener.cancel()
    await pubsub.aclose()  # type: ignore[no-untyped-call]

    rate = events / elapsed
    assert rate >= 0.9 * PROGRESS_PER_S, f'only {rate:.0f} progress events/s generated'
    # The emitter's synchronous calls take a small share of the loop (no I/O, O(1) each).
    assert spent / elapsed < 0.25, f'emitter calls used {spent / elapsed:.0%} of the time'
    # The event loop stays responsive: request handling would not wait behind the emitter.
    p99 = statistics.quantiles(lateness, n=100)[98]
    assert p99 < 0.05, f'event loop p99 lateness {p99 * 1000:.1f} ms'
    assert emitter.dropped == 0 and emitter.failed_flushes == 0
    # Progress is coalesced to at most one publish per flush (4 Hz) per transfer.
    assert published, 'no progress was published'
    assert max(published.values()) <= elapsed * 4 + 2
    # Every request was counted, exactly.
    counted = 0
    for member in await valkey.smembers(keys.STATS_PENDING):
        minute, repo_id = keys.parse_pending(member)
        if repo_id == 'repo-load':
            hits = await valkey.hget(keys.cnt_key(minute, repo_id), 'hits')
            counted += int(hits or 0)
    assert counted == requests
    assert await valkey.hlen(keys.TRANSFERS) == 0
