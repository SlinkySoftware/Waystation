"""The ``cleanup.run`` job handler (T-O01): result, progress in ``job_runs``, activity event, metric totals."""

from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from waystation_common.db import cleanup as cleanup_db
from waystation_common.db import jobs as jobs_db
from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, JOB_DEFINITIONS, REPOSITORIES
from waystation_common.db.schema import ensure_schema
from waystation_common.events import EventEmitter, keys
from waystation_common.jobs import CleanupRun, JobEnvelope
from waystation_worker.jobs import BUILTIN_DEFINITIONS, HANDLERS, JobContext

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


async def seed(db: Database) -> None:
    await ensure_schema(db)
    await db[REPOSITORIES].insert_one(
        {'_id': 'r-npm', 'name': 'npm', 'format': 'npm', 'type': 'proxy', 'cleanup_policy_ids': ['p1']}
    )
    await db[REPOSITORIES].insert_one(
        {'_id': 'r-raw', 'name': 'files', 'format': 'raw', 'type': 'proxy', 'cleanup_policy_ids': ['p1']}
    )
    old = NOW - timedelta(days=60)
    await db[ASSETS].insert_many(
        [
            {
                '_id': f'a{i}',
                'repo_id': 'r-npm',
                'path': f'pkg/-/pkg-1.0.{i}.tgz',
                'kind': 'content',
                'size': 10,
                'blob_sha256': f'{i:064x}',
                'created_at': old,
            }
            for i in range(7)
        ]
    )
    policy = {
        '_id': 'p1',
        'name': 'npm-keep-2',
        'format': 'npm',
        'criteria': {'keep_latest_n_versions': 2},
        'schedule': '@daily',
        'enabled': True,
        'created_at': NOW,
        'updated_at': NOW,
    }
    await cleanup_db.insert_policy(db, policy)
    await cleanup_db.sync_schedule(db, policy)


async def test_handler_removes_records_and_reports(mongo_db: Database, valkey: Redis) -> None:
    await seed(mongo_db)
    definition = await mongo_db[JOB_DEFINITIONS].find_one({'_id': 'cleanup:p1'})
    assert definition is not None and definition['schedule'] == '@daily' and definition['next_run_at'] is None
    env = JobEnvelope.for_payload(CleanupRun(policy_id='p1'))
    await jobs_db.run_start(mongo_db, {'_id': 'run1', 'job_id': env.id, 'status': 'running'}, now=NOW)
    async with EventEmitter(valkey) as emitter:
        ctx = JobContext(job=env, run_id='run1', attempt=1, worker_id='w', db=mongo_db, valkey=valkey, emitter=emitter)
        result = await HANDLERS[CleanupRun.kind](CleanupRun(policy_id='p1'), ctx)
        await ctx.flush_progress()
    assert result is not None
    assert (result['removed'], result['bytes'], result['by_kind']) == (5, 50, {'asset': 5})
    assert result['repositories'] == {'npm': {'removed': 5, 'bytes': 50}}  # the raw repo is skipped (format)
    left = sorted([d['path'] async for d in mongo_db[ASSETS].find({})])
    assert left == ['pkg/-/pkg-1.0.5.tgz', 'pkg/-/pkg-1.0.6.tgz']
    run = await mongo_db['job_runs'].find_one({'_id': 'run1'})
    assert run is not None and run['progress']['done'] == 5 and run['progress']['total'] == 5
    assert any('npm: removed 5 of 5' in line for line in run['log'])
    totals = await valkey.hgetall(keys.MAINTENANCE_TOTALS)
    assert totals == {'cleanup_removed': '5', 'cleanup_bytes': '50'}
    events = await valkey.xrange(keys.ACTIVITY)
    assert len(events) == 1 and 'removed 5 items (50 B) from npm' in events[0][1]['data']
    # idempotent: nothing left to do
    again = await HANDLERS[CleanupRun.kind](CleanupRun(policy_id='p1'), ctx)
    assert again is not None and again['removed'] == 0


async def test_deleted_policy_is_skipped(mongo_db: Database, valkey: Redis) -> None:
    await seed(mongo_db)
    assert await cleanup_db.delete_policy(mongo_db, 'p1')
    assert await mongo_db[JOB_DEFINITIONS].find_one({'_id': 'cleanup:p1'}) is None
    repo = await mongo_db[REPOSITORIES].find_one({'_id': 'r-npm'})
    assert repo is not None and repo['cleanup_policy_ids'] == []
    env = JobEnvelope.for_payload(CleanupRun(policy_id='p1'))
    ctx = JobContext(job=env, run_id='run2', attempt=1, worker_id='w', db=mongo_db, valkey=valkey)
    result = await HANDLERS[CleanupRun.kind](CleanupRun(policy_id='p1'), ctx)
    assert result is not None and result['skipped'] == 'the policy was deleted'
    assert not await cleanup_db.delete_policy(mongo_db, 'p1')


def test_builtin_definitions_are_unique() -> None:
    ids = [d.id for d in BUILTIN_DEFINITIONS]
    assert len(ids) == len(set(ids))
