"""The Valkey state machine of the job queue (``waystation_common.jobs.queue``) against a real Valkey."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from redis.asyncio import Redis

from waystation_common.db import jobs as jobs_db
from waystation_common.db.client import Database
from waystation_common.db.schema import ensure_schema
from waystation_common.jobs import JobClient, JobEnvelope, JobPayload, JobQueue, Queue, keys
from waystation_common.jobs.queue import QueueStats

pytestmark = pytest.mark.integration


class Rebuild(JobPayload):
    kind: ClassVar[str] = 'test.queue.rebuild'
    queue: ClassVar[Queue] = 'index'
    repo_id: str

    def dedupe_key(self) -> str | None:
        return f'rebuild:{self.repo_id}'


async def read_one(q: JobQueue, consumer: str = 'c1') -> list:  # type: ignore[type-arg]
    await q.ensure_groups(['index'])
    return await q.read(consumer, ['index'], count=10, block_ms=10)


async def test_dirty_flag_pending_merge_and_single_rerun(valkey: Redis) -> None:
    q = JobQueue(valkey)
    first = JobEnvelope.for_payload(Rebuild(repo_id='r1'))
    assert (await q.enqueue(first, outbox_id='o1')).status == 'enqueued'
    second = await q.enqueue(JobEnvelope.for_payload(Rebuild(repo_id='r1')), outbox_id='o2')
    assert (second.status, second.job_id) == ('merged', first.id)
    assert await valkey.xlen(keys.stream('index')) == 1

    (delivery,) = await read_one(q)
    started = await q.start(delivery, 'c1')
    assert started.run
    assert started.outbox_ids == ['o1', 'o2']

    # three uploads while it runs: one rerun, carrying their outbox ids
    results = [await q.enqueue(JobEnvelope.for_payload(Rebuild(repo_id='r1')), outbox_id=f'u{i}') for i in range(3)]
    assert {r.status for r in results} == {'rerun'}
    # the same job re-dispatched while it runs changes nothing
    assert (await q.enqueue(first, outbox_id='o1')).status == 'running'

    outcome, rerun_id = await q.finish(delivery, 'done')
    assert outcome == 'rerun'
    (rerun,) = await read_one(q)
    assert rerun.envelope is not None and rerun.envelope.id == rerun_id
    assert (await q.start(rerun, 'c1')).outbox_ids == ['u0', 'u1', 'u2']
    assert await q.finish(rerun, 'done') == ('done', '')
    assert await valkey.exists(keys.dedupe('rebuild:r1')) == 0
    assert await valkey.xlen(keys.stream('index')) == 0
    assert await read_one(q) == []


async def test_retry_dead_letter_and_delayed(valkey: Redis) -> None:
    q = JobQueue(valkey)
    env = JobEnvelope.for_payload(Rebuild(repo_id='r2'))
    await q.enqueue(env)
    (d,) = await read_one(q)
    await q.start(d, 'c1')
    upload = await q.enqueue(JobEnvelope.for_payload(Rebuild(repo_id='r2')), outbox_id='late')
    assert upload.status == 'rerun'
    due = datetime.now(UTC) + timedelta(seconds=30)
    assert await q.finish(d, 'retry', envelope=env.model_copy(update={'attempt': 1}), retry_at=due) == ('retry', env.id)
    assert await valkey.hget(keys.dedupe('rebuild:r2'), 'outbox') == 'late'  # the retry covers the late upload
    assert await q.promote_due('index', now=due.timestamp() - 1) == 0
    assert await q.promote_due('index', now=due.timestamp()) == 1
    (retry,) = await read_one(q)
    assert retry.envelope is not None and (retry.envelope.id, retry.envelope.attempt) == (env.id, 1)
    assert (await q.start(retry, 'c1')).outbox_ids == ['late']
    await q.finish(retry, 'dead', envelope=retry.envelope, error='boom')
    (dead,) = await q.dead_letters()
    assert dead['error'] == 'boom'
    assert dead['job']['id'] == env.id
    assert await valkey.exists(keys.dedupe('rebuild:r2')) == 0


async def test_run_after_goes_to_delayed_set(valkey: Redis) -> None:
    q = JobQueue(valkey)
    later = datetime.now(UTC) + timedelta(hours=1)
    env = JobEnvelope.for_payload(Rebuild(repo_id='r3'), run_after=later)
    assert (await q.enqueue(env)).status == 'enqueued'
    assert await valkey.zcard(keys.delayed('index')) == 1
    assert await read_one(q) == []
    assert (await q.enqueue(JobEnvelope.for_payload(Rebuild(repo_id='r3')))).status == 'merged'


async def test_reclaim_touch_and_stale_duplicates(valkey: Redis) -> None:
    q = JobQueue(valkey)
    env = JobEnvelope.for_payload(Rebuild(repo_id='r4'))
    await q.enqueue(env)
    (d,) = await read_one(q, 'dead-worker')
    await q.start(d, 'dead-worker')
    assert await q.reclaim('c2', 'index', min_idle_ms=60_000, count=10) == []
    assert await q.touch(d, 'dead-worker')
    (reclaimed,) = await q.reclaim('c2', 'index', min_idle_ms=0, count=10)
    assert (reclaimed.entry_id, reclaimed.deliveries) == (d.entry_id, 2)
    assert not await q.touch(d, 'dead-worker')  # ownership moved: the old worker's heartbeat fails
    assert await q.touch(reclaimed, 'c2')
    assert (await q.start(reclaimed, 'c2')).run

    # an entry whose key belongs to another job is a stale duplicate: skipped, and it asks for one rerun
    other = env.model_copy(update={'id': 'OTHER'})
    await valkey.xadd(keys.stream('index'), {'job': other.to_json()})
    (stale,) = await read_one(q, 'c2')
    assert not (await q.start(stale, 'c2')).run
    await q.drop(stale)
    assert await q.finish(reclaimed, 'done') == ('rerun', 'OTHER')

    # a malformed entry is dead-lettered as is
    await valkey.xadd(keys.stream('index'), {'job': '{not json'})
    deliveries = await read_one(q, 'c2')
    bad = next(x for x in deliveries if x.envelope is None)
    await q.drop(bad, dead_reason='malformed job entry')
    assert (await q.dead_letters())[0]['job'] == '{not json'


async def test_groups_epoch_and_consumers(valkey: Redis) -> None:
    q = JobQueue(valkey)
    await q.ensure_groups(['index'])
    await q.ensure_groups(['index'])  # idempotent
    epoch = await q.epoch()
    assert await q.epoch() == epoch
    await valkey.flushdb()
    assert await q.read('c1', ['index'], count=1, block_ms=10) == []  # NOGROUP: groups recreated
    assert await q.read('c1', ['index'], count=1, block_ms=10) == []
    assert await q.epoch() != epoch
    await asyncio.sleep(0.02)
    assert await q.prune_consumers('index', idle_ms=0, keep='other') == 1
    assert await q.prune_consumers('maintenance', idle_ms=0, keep='other') == 0
    assert await q.read('c1', [], count=1, block_ms=10) == []


async def test_job_client_durable_outbox_in_transaction(valkey: Redis, mongo_db: Database) -> None:
    await ensure_schema(mongo_db)
    jobs = JobClient(valkey, mongo_db)
    client = mongo_db.client
    async with client.start_session() as session, await session.start_transaction():
        outbox_id = await jobs.enqueue_durable(Rebuild(repo_id='t1'), session=session)
        await session.abort_transaction()
    assert await jobs_db.outbox_get(mongo_db, outbox_id) is None  # rolled back with the change
    async with client.start_session() as session, await session.start_transaction():
        outbox_id = await jobs.enqueue_durable(Rebuild(repo_id='t1'), session=session)
    doc = await jobs_db.outbox_get(mongo_db, outbox_id)
    assert doc is not None and doc['sent_at'] is None and doc['dedupe_key'] == 'rebuild:t1'
    assert (await jobs.enqueue(Rebuild(repo_id='t1'))).status == 'enqueued'


async def test_queue_stats_and_dead_count(valkey: Redis) -> None:
    q = JobQueue(valkey)
    assert await q.stats(['index', 'maintenance']) == {'index': QueueStats(), 'maintenance': QueueStats()}
    assert await q.dead_count() == 0
    await q.ensure_groups(['index'])
    for i in range(3):
        await q.enqueue(JobEnvelope.for_payload(Rebuild(repo_id=f's{i}')))
    later = datetime.now(UTC) + timedelta(hours=1)
    await q.enqueue(JobEnvelope.for_payload(Rebuild(repo_id='later'), run_after=later))
    (first,) = await q.read('c1', ['index'], count=1, block_ms=10)
    oldest = int(first.entry_id.split('-')[0])  # the next waiting entry is younger than or as old as this one
    now = oldest / 1000 + 5
    stats = await q.stats(['index'], now=now)
    assert stats['index'].waiting == 2
    assert stats['index'].running == 1
    assert stats['index'].delayed == 1
    assert 0 < stats['index'].oldest_waiting_age_s <= 5
    await q.start(first, 'c1')
    assert first.envelope is not None
    await q.finish(first, 'dead', envelope=first.envelope, error='boom')
    assert await q.dead_count() == 1
    assert (await q.stats(['index'], now=now))['index'].running == 0


async def test_worker_presence(mongo_db: Database) -> None:
    now = datetime.now(UTC)
    await jobs_db.worker_heartbeat(mongo_db, 'w1', {'leader': True}, now=now)
    await jobs_db.worker_heartbeat(mongo_db, 'w2', {'leader': False}, now=now - timedelta(minutes=3))
    assert await jobs_db.worker_presence(mongo_db, alive_since=now - timedelta(seconds=60)) == (1, 1)
    assert await jobs_db.worker_presence(mongo_db, alive_since=now + timedelta(seconds=1)) == (0, 0)
