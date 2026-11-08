"""Chaos and end-to-end tests of the job system (T-F08 acceptance) against real MongoDB and Valkey.

Workers run in-process with short timings (``FAST``) and, where time matters, an injected clock. "Killing" a worker
cancels its tasks without any cleanup: nothing is acked or recorded, exactly like a SIGKILL.
"""

import asyncio
import dataclasses
import inspect
import math
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from redis.asyncio import Redis

from waystation_common.db import jobs as jobs_db
from waystation_common.db import stats as stats_db
from waystation_common.db.client import Database
from waystation_common.db.collections import WORKERS
from waystation_common.db.schema import ensure_schema
from waystation_common.events import CacheState, EventEmitter
from waystation_common.events import keys as event_keys
from waystation_common.jobs import JobClient, JobEnvelope, JobPayload, JobQueue, Queue, StatsRollup, keys
from waystation_worker.jobs import BUILTIN_DEFINITIONS, HANDLERS, Handler, JobConfig, JobContext, JobSystem

pytestmark = pytest.mark.integration

VALKEY_URL = os.environ.get('WAYSTATION_TEST_VALKEY_URL', '')
FAST = JobConfig(
    visibility_s=0.6, block_ms=50, tick_s=0.05, lease_ttl_s=1.0, retry_base_s=0.0, shutdown_grace_s=2.0, presence_s=0.1
)
SLOW_STOP = dataclasses.replace(FAST, visibility_s=5.0, lease_ttl_s=5.0, shutdown_grace_s=30.0)
STREAM = keys.stream('maintenance')


class Work(JobPayload):
    kind: ClassVar[str] = 'test.worker.work'
    queue: ClassVar[Queue] = 'maintenance'
    key: str

    def dedupe_key(self) -> str | None:
        return f'work:{self.key}'


class Flaky(JobPayload):
    kind: ClassVar[str] = 'test.worker.flaky'
    max_attempts: ClassVar[int] = 2


class Fragile(JobPayload):
    kind: ClassVar[str] = 'test.worker.fragile'
    max_attempts: ClassVar[int] = 1


class Tick(JobPayload):
    kind: ClassVar[str] = 'test.worker.tick'


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@dataclass
class Running:
    system: JobSystem
    stop: asyncio.Event
    task: asyncio.Task[None]

    async def kill(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


@asynccontextmanager
async def worker(
    db: Database,
    handlers: Mapping[str, Handler],
    worker_id: str,
    *,
    queues: Sequence[str] = ('maintenance',),
    definitions: Sequence[jobs_db.Definition] = (),
    clock: Callable[[], float] = time.time,
    emitter: EventEmitter | None = None,
    config: JobConfig = FAST,
) -> AsyncIterator[Running]:
    redis: Redis = Redis.from_url(VALKEY_URL, decode_responses=True)
    system = JobSystem(
        db,
        redis,
        handlers=handlers,
        worker_id=worker_id,
        queues=queues,
        definitions=definitions,
        config=config,
        clock=clock,
        emitter=emitter,
    )
    stop = asyncio.Event()
    running = Running(system, stop, asyncio.create_task(system.run(stop)))
    try:
        yield running
    finally:
        if not running.task.done():
            stop.set()
            await asyncio.wait_for(running.task, 30)
        await redis.aclose()


async def until(
    check: Callable[[], Any], limit_s: float = 30.0
) -> None:  # generous: only a failing test waits this long
    deadline = time.monotonic() + limit_s
    while True:
        result = check()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError('condition not met in time')
        await asyncio.sleep(0.02)


async def statuses(db: Database, job_id: str) -> list[str]:
    return [run['status'] for run in await jobs_db.runs_for_job(db, job_id)]


async def queue_empty(valkey: Redis, dedupe_key: str) -> bool:
    pending = await valkey.xpending(STREAM, keys.GROUP)
    return (
        await valkey.xlen(STREAM) == 0 and pending['pending'] == 0 and not await valkey.exists(keys.dedupe(dedupe_key))
    )


@pytest.fixture
async def db(mongo_db: Database, valkey: Redis) -> Database:
    await ensure_schema(mongo_db)
    return mongo_db


# ---------------------------------------------------------------- acceptance: chaos
async def test_kill_worker_mid_job_reruns_exactly_once(db: Database, valkey: Redis) -> None:
    calls: list[str] = []
    entered = asyncio.Event()

    async def work(job: Work, ctx: JobContext) -> dict[str, Any]:
        calls.append(ctx.worker_id)
        entered.set()
        if ctx.worker_id == 'w-a':
            await asyncio.Event().wait()  # hangs until the worker is killed
        return {'key': job.key}

    handlers: dict[str, Handler] = {Work.kind: work}
    jobs = JobClient(valkey, db)
    async with worker(db, handlers, 'w-a') as a:
        job = await jobs.enqueue(Work(key='k1'))
        await asyncio.wait_for(entered.wait(), 30)
        await a.kill()
    assert await statuses(db, job.job_id) == ['running']  # the killed worker recorded nothing more

    async with worker(db, handlers, 'w-b'):
        await until(lambda: _eq(statuses(db, job.job_id), ['interrupted', 'succeeded']))
        await asyncio.sleep(FAST.visibility_s * 2)  # long enough for any further (wrong) reclaim
    assert calls == ['w-a', 'w-b']
    runs = await jobs_db.runs_for_job(db, job.job_id)
    assert [r['attempt'] for r in runs] == [1, 2]
    assert runs[1]['result'] == {'key': 'k1'}
    assert await queue_empty(valkey, 'work:k1')


async def _epoch_changed(db: Database, outbox_id: str, epoch: Any) -> bool:
    doc = await jobs_db.outbox_get(db, outbox_id)
    return doc is not None and doc['sent_epoch'] != epoch


async def test_flushed_valkey_redispatches_outbox_jobs(db: Database, valkey: Redis) -> None:
    calls: list[str] = []

    async def work(job: Work, ctx: JobContext) -> None:
        calls.append(ctx.job.id)

    handlers: dict[str, Handler] = {Work.kind: work}
    jobs = JobClient(valkey, db)
    outbox_id = await jobs.enqueue_durable(Work(key='k2'))
    async with worker(db, handlers, 'w-leader', queues=()):  # dispatches, consumes nothing
        await until(lambda: _sent(db, outbox_id))
        first = await jobs_db.outbox_get(db, outbox_id)
        assert first is not None
        assert await valkey.xlen(STREAM) == 1
        await valkey.flushdb()  # Valkey loses the queued job, its dedupe key, the epoch and the lease
        await until(lambda: _xlen(valkey, STREAM, 1))
        # The dispatcher adds to the stream first and records the new epoch just after: wait for both.
        await until(lambda: _epoch_changed(db, outbox_id, first['sent_epoch']))
        again = await jobs_db.outbox_get(db, outbox_id)
        assert again is not None and again['sent_epoch'] != first['sent_epoch']
        assert again['done_at'] is None
        async with worker(db, handlers, 'w-c'):
            await until(lambda: _done(db, outbox_id))
        await asyncio.sleep(0.3)  # the leader keeps ticking: nothing is dispatched again
    assert calls == [first['job']['id']]
    assert await queue_empty(valkey, 'work:k2')


async def test_upload_during_running_rebuild_runs_exactly_one_rerun(db: Database, valkey: Redis) -> None:
    uploads = 0
    seen: list[int] = []
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def rebuild(job: Work, ctx: JobContext) -> dict[str, Any]:
        seen.append(uploads)
        entered.set()
        if len(seen) == 1:
            await gate.wait()
        return {'uploads': uploads}

    jobs = JobClient(valkey, db)
    async with worker(db, {Work.kind: rebuild}, 'w-d'):
        await jobs.enqueue(Work(key='repo1'))
        await asyncio.wait_for(entered.wait(), 30)
        results = []
        for _ in range(5):  # uploads land while the index rebuild runs
            uploads += 1
            results.append((await jobs.enqueue(Work(key='repo1'))).status)
        uploads += 1
        outbox_id = await jobs.enqueue_durable(Work(key='repo1'))  # and one through the outbox
        await until(lambda: _sent(db, outbox_id))
        assert results == ['rerun'] * 5
        gate.set()
        await until(lambda: _done(db, outbox_id))
        await asyncio.sleep(0.5)
    assert seen == [0, 6]  # exactly one rerun, and it saw every upload
    assert await queue_empty(valkey, 'work:repo1')


async def test_schedule_fires_once_with_three_workers(db: Database, valkey: Redis) -> None:
    minute0 = (math.floor(time.time() / 60) + 1) * 60
    clock = Clock(minute0 + 10)
    fired: list[tuple[str, str | None]] = []

    async def tick(job: Tick, ctx: JobContext) -> None:
        fired.append((ctx.job.id, ctx.job.definition_id))

    handlers: dict[str, Handler] = {Tick.kind: tick}
    definition = jobs_db.Definition(id='every-minute', kind=Tick.kind, schedule='* * * * *', queue='maintenance')
    async with (
        worker(db, handlers, 'w-1', definitions=[definition], clock=clock),
        worker(db, handlers, 'w-2', definitions=[definition], clock=clock),
        worker(db, handlers, 'w-3', definitions=[definition], clock=clock),
    ):
        await until(lambda: _next_run(db, 'every-minute', minute0 + 60))
        for n in (1, 2, 3):
            clock.t = minute0 + 60 * n + 1
            await until(lambda n=n: len(fired) == n)
            await asyncio.sleep(0.3)  # every worker ticks several times more
            assert len(fired) == n
            await until(lambda n=n: _next_run(db, 'every-minute', minute0 + 60 * (n + 1)))
    assert len({job_id for job_id, _ in fired}) == 3
    assert {definition_id for _, definition_id in fired} == {'every-minute'}


async def test_overlapping_schedulers_fire_once(db: Database, valkey: Redis) -> None:
    """Even without the leader lease (split brain), the compare-and-set on next_run_at lets one fire through."""
    definition = jobs_db.Definition(id='overlap', kind=Tick.kind, schedule='@every 10s', queue='maintenance')
    systems = [
        JobSystem(db, valkey, handlers={}, worker_id=f's{i}', definitions=[definition], config=FAST) for i in range(3)
    ]
    await jobs_db.ensure_definitions(db, [definition])
    await systems[0].coordinator.fire_schedules()  # first sight: computes next_run_at only
    doc = await jobs_db.get_definition(db, 'overlap')
    assert doc is not None and doc['next_run_at'] is not None
    for system in systems:
        system.coordinator.clock = lambda: doc['next_run_at'].timestamp() + 1
    results = await asyncio.gather(*(s.coordinator.fire_schedules() for s in systems))
    assert sorted(results) == [0, 0, 1]
    assert await valkey.xlen(STREAM) == 1


# ---------------------------------------------------------------- retries, dead letters, recording
async def test_failures_retry_then_dead_letter(db: Database, valkey: Redis) -> None:
    attempts: list[int] = []

    async def flaky(job: Flaky, ctx: JobContext) -> None:
        attempts.append(ctx.attempt)
        raise RuntimeError(f'boom {ctx.attempt}')

    jobs = JobClient(valkey, db)
    outbox_id = await jobs.enqueue_durable(Flaky())
    async with worker(db, {Flaky.kind: flaky}, 'w-e'):
        await until(lambda: _done(db, outbox_id))
    doc = await jobs_db.outbox_get(db, outbox_id)
    assert doc is not None
    job_id = doc['job']['id']
    assert attempts == [1, 2]
    assert await statuses(db, job_id) == ['failed', 'dead']
    (dead,) = await JobQueue(valkey).dead_letters()
    assert dead['error'] == 'RuntimeError: boom 2'
    assert (dead['job']['id'], dead['job']['attempt']) == (job_id, 2)
    assert await queue_empty(valkey, f'id:{job_id}')


async def test_worker_lost_too_often_is_dead_lettered(db: Database, valkey: Redis) -> None:
    calls: list[str] = []
    entered = asyncio.Event()

    async def fragile(job: Fragile, ctx: JobContext) -> None:
        calls.append(ctx.worker_id)
        entered.set()
        await asyncio.Event().wait()

    jobs = JobClient(valkey, db)
    async with worker(db, {Fragile.kind: fragile}, 'w-f') as f:
        job = await jobs.enqueue(Fragile())
        await asyncio.wait_for(entered.wait(), 30)
        await f.kill()
    async with worker(db, {Fragile.kind: fragile}, 'w-g'):
        await until(lambda: _eq(statuses(db, job.job_id), ['interrupted', 'dead']))
    assert calls == ['w-f']
    assert 'lost' in (await JobQueue(valkey).dead_letters())[0]['error']


async def test_unknown_kind_is_dead_lettered(db: Database, valkey: Redis) -> None:
    env = JobEnvelope.for_payload(Tick()).model_copy(update={'kind': 'test.nobody.handles'})
    await JobQueue(valkey).enqueue(env)
    async with worker(db, {}, 'w-h'):
        await until(lambda: _eq(statuses(db, env.id), ['dead']))
    assert 'no handler' in (await JobQueue(valkey).dead_letters())[0]['error']


async def test_progress_log_events_presence_and_graceful_stop(db: Database, valkey: Redis) -> None:
    async def work(job: Work, ctx: JobContext) -> dict[str, Any]:
        await ctx.log('starting')
        await ctx.progress(5, 10, 'half way')
        await asyncio.sleep(0.4)  # still running when the worker is asked to stop
        await ctx.progress(10, 10, 'done')
        return {'files': 10}

    jobs = JobClient(valkey, db)
    async with (
        EventEmitter(valkey, flush_interval_s=0.05) as emitter,
        # Generous lease and grace: on a loaded machine the 2 s of FAST could abandon the job, which is correct
        # behaviour but not what this test is about.
        worker(db, {Work.kind: work}, 'w-i', emitter=emitter, config=SLOW_STOP) as w,
    ):
        job = await jobs.enqueue(Work(key='p'))
        await until(lambda: _eq(statuses(db, job.job_id), ['running']))
        await until(lambda: db[WORKERS].find_one({'_id': 'w-i', 'running_jobs.job_id': job.job_id}))
        w.stop.set()  # graceful: the running job finishes first
        await asyncio.wait_for(w.task, 35)
    (run,) = await jobs_db.runs_for_job(db, job.job_id)
    assert run['status'] == 'succeeded'
    assert run['progress'] == {'done': 10, 'total': 10, 'message': 'done'}
    assert run['log'][0].endswith(' starting')
    assert run['result'] == {'files': 10}
    assert run['worker_id'] == 'w-i'
    assert run['expires_at'] - run['started_at'] == timedelta(days=90)
    assert await db[WORKERS].find_one({'_id': 'w-i'}) is None
    types = [fields['type'] for _, fields in await valkey.xrange(event_keys.ACTIVITY)]
    assert types == ['worker.online', 'job.started', 'job.finished', 'worker.offline']


async def test_delayed_job_runs_when_due(db: Database, valkey: Redis) -> None:
    clock = Clock(time.time())
    ran = asyncio.Event()

    async def tick(job: Tick, ctx: JobContext) -> None:
        ran.set()

    jobs = JobClient(valkey, db)
    later = datetime.fromtimestamp(clock.t + 30, UTC)
    assert (await jobs.enqueue(Tick(), run_after=later)).status == 'enqueued'
    async with worker(db, {Tick.kind: tick}, 'w-j', clock=clock):
        await asyncio.sleep(0.3)
        assert not ran.is_set()
        clock.t += 31
        await asyncio.wait_for(ran.wait(), 30)


# ---------------------------------------------------------------- the stats rollup as a scheduled job
async def test_stats_rollup_runs_as_a_job_exactly_once(db: Database, valkey: Redis) -> None:
    minute = event_keys.minute_of(time.time()) - 10
    emitter = EventEmitter(valkey, clock=lambda: minute * 60 + 1.0)
    for _ in range(4):
        emitter.request(repo_id='repo-j', format='npm', cache=CacheState.HIT, duration_ms=1.0, bytes_out=10)
    await emitter.flush()
    jobs = JobClient(valkey, db)
    since = datetime.fromtimestamp(minute * 60, UTC)
    async with worker(db, HANDLERS, 'w-k', queues=('stats',), definitions=BUILTIN_DEFINITIONS):
        await until(lambda: jobs_db.get_definition(db, 'stats.rollup'))
        first = await jobs.enqueue(StatsRollup())
        await until(lambda: _eq(statuses(db, first.job_id), ['succeeded']))
        second = await jobs.enqueue(StatsRollup())
        await until(lambda: _eq(statuses(db, second.job_id), ['succeeded']))
    docs = await stats_db.minutes(db, 'repo-j', since)
    assert len(docs) == 1
    assert docs[0]['requests'] == 4
    definition = await jobs_db.get_definition(db, 'stats.rollup')
    assert definition is not None
    assert (definition['schedule'], definition['queue'], definition['enabled']) == ('@every 10s', 'stats', True)
    runs = await jobs_db.recent_runs(db, kind=StatsRollup.kind)
    assert sum(run.get('result', {}).get('minutes', 0) for run in runs) == 1  # incl. any scheduled fire meanwhile
    assert {run['expires_at'] - run['started_at'] for run in runs} == {timedelta(days=1)}  # routine: kept 1 day


# ---------------------------------------------------------------- helpers
async def _eq(value: Awaitable[Any], expected: Any) -> bool:
    return await value == expected


async def _sent(db: Database, outbox_id: str) -> bool:
    doc = await jobs_db.outbox_get(db, outbox_id)
    return doc is not None and doc['sent_at'] is not None


async def _done(db: Database, outbox_id: str) -> bool:
    doc = await jobs_db.outbox_get(db, outbox_id)
    return doc is not None and doc['done_at'] is not None


async def _xlen(valkey: Redis, stream: str, n: int) -> bool:
    return bool(await valkey.xlen(stream) == n)


async def _next_run(db: Database, definition_id: str, expected: float) -> bool:
    doc = await jobs_db.get_definition(db, definition_id)
    return doc is not None and doc['next_run_at'] is not None and doc['next_run_at'].timestamp() == expected
