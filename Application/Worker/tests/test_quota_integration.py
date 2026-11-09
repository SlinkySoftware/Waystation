"""Quota recount and the low-disk alert (T-O03) against real MongoDB/Valkey: ``quota.reconcile`` repairs drifted usage
and announces thresholds once; ``storage.check`` announces ``storage.low`` / ``storage.ok`` once per change."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from redis.asyncio import Redis
from ws_testing import make_settings

from waystation_common.db import storage_guard
from waystation_common.db import usage as usage_db
from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, REPO_USAGE, REPOSITORIES
from waystation_common.db.schema import ensure_schema
from waystation_common.events import EventEmitter, keys
from waystation_common.jobs import JobEnvelope, QuotaReconcile, StorageCheck
from waystation_worker.jobs import BUILTIN_DEFINITIONS, HANDLERS, JobContext

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


@pytest.fixture
async def db(mongo_db: Database) -> Database:
    await ensure_schema(mongo_db)
    return mongo_db


async def repo(db: Database, repo_id: str, quota: int | None = None, mode: str = 'soft') -> None:
    await db[REPOSITORIES].insert_one(
        {
            '_id': repo_id,
            'name': repo_id,
            'format': 'raw',
            'type': 'hosted',
            'storage': {'quota_bytes': quota, 'quota_mode': mode},
        }
    )


async def asset(db: Database, repo_id: str, path: str, size: int, kind: str = 'content') -> None:
    await db[ASSETS].insert_one(
        {
            '_id': f'{repo_id}:{path}',
            'repo_id': repo_id,
            'path': path,
            'kind': kind,
            'size': size,
            'blob_sha256': 'a' * 64,
        }
    )


async def activity(valkey: Redis) -> list[dict[str, object]]:
    return [json.loads(fields['data']) for _, fields in await valkey.xrange(keys.ACTIVITY)]


def context(db: Database, valkey: Redis, emitter: EventEmitter, tmp_path: Path, payload: object) -> JobContext:
    return JobContext(
        job=JobEnvelope.for_payload(payload),  # type: ignore[arg-type]
        run_id='r1',
        attempt=1,
        worker_id='w',
        db=db,
        valkey=valkey,
        settings=make_settings(tmp_path),
        emitter=emitter,
        clock=lambda: NOW.timestamp(),
    )


async def test_reconcile_fixes_drift_and_announces_thresholds(db: Database, valkey: Redis, tmp_path: Path) -> None:
    await repo(db, 'big', quota=1000, mode='hard')
    await repo(db, 'small', quota=1000)
    await repo(db, 'free')
    await asset(db, 'big', 'a', 700)
    await asset(db, 'big', 'b', 400, kind='metadata')  # 1100: over the quota
    await asset(db, 'small', 'a', 850)  # 85 %: warning
    await asset(db, 'free', 'a', 10)
    await usage_db.add(db, 'big', 5)  # drifted (e.g. content from before usage was counted)
    await usage_db.add(db, 'gone', 99, 1)  # a deleted repository's leftover
    definition = next(d for d in BUILTIN_DEFINITIONS if d.id == 'quota.reconcile')
    assert definition.kind == QuotaReconcile.kind

    async with EventEmitter(valkey) as emitter:
        result = await HANDLERS[QuotaReconcile.kind](
            QuotaReconcile(), context(db, valkey, emitter, tmp_path, QuotaReconcile())
        )
        again = await HANDLERS[QuotaReconcile.kind](
            QuotaReconcile(), context(db, valkey, emitter, tmp_path, QuotaReconcile())
        )
    assert result == {'repositories': 3, 'drifted': 3, 'drift_bytes': 1095 + 850 + 10, 'announced': 2}
    assert again is not None and again['drifted'] == 0 and again['announced'] == 0
    usage = await usage_db.get_many(db, ['big', 'small', 'free'])
    assert (usage['big'].bytes, usage['big'].assets, usage['big'].alert_level) == (1100, 2, 100)
    assert (usage['big'].artifacts, usage['small'].artifacts) == (1, 1)  # metadata is not an artifact
    assert (usage['small'].bytes, usage['small'].alert_level) == (850, 80)
    assert usage['free'].bytes == 10 and usage['free'].reconciled_at is not None
    assert await db[REPO_USAGE].find_one({'_id': 'gone'}) is None
    events = await activity(valkey)
    assert sorted((e['type'], e['repo']) for e in events) == [('quota.exceeded', 'big'), ('quota.warning', 'small')]
    exceeded = next(e for e in events if e['type'] == 'quota.exceeded')
    assert 'refused' in exceeded['detail']['summary']  # type: ignore[index]

    # Usage back under the warning level re-arms it.
    await db[ASSETS].delete_one({'_id': 'small:a'})
    async with EventEmitter(valkey) as emitter:
        await HANDLERS[QuotaReconcile.kind](QuotaReconcile(), context(db, valkey, emitter, tmp_path, QuotaReconcile()))
    assert (await usage_db.get(db, 'small')).alert_level == 0


async def test_storage_check_announces_changes_once(db: Database, valkey: Redis, tmp_path: Path) -> None:
    job = StorageCheck()
    async with EventEmitter(valkey) as emitter:
        ctx = context(db, valkey, emitter, tmp_path, job)
        ok = await HANDLERS[StorageCheck.kind](job, ctx)
        assert ok is not None and ok['low'] is False and ok['changed'] is False
        await storage_guard.save(db, storage_guard.GuardSettings(min_free_bytes=2**53), by='test')
        low = await HANDLERS[StorageCheck.kind](job, ctx)
        still = await HANDLERS[StorageCheck.kind](job, ctx)
        assert low is not None and low['low'] is True and low['changed'] is True
        assert still is not None and still['changed'] is False
        assert await valkey.get(keys.STORAGE_LOW) == NOW.isoformat()
        await storage_guard.save(db, storage_guard.GuardSettings(min_free_bytes=0, min_free_percent=0.001), by='t')
        recovered = await HANDLERS[StorageCheck.kind](job, ctx)
        assert recovered is not None and recovered['low'] is False and recovered['changed'] is True
    assert await valkey.get(keys.STORAGE_LOW) is None
    assert [e['type'] for e in await activity(valkey)] == ['storage.low', 'storage.ok']
    no_settings = JobContext(
        job=JobEnvelope.for_payload(job), run_id='r', attempt=1, worker_id='w', db=db, valkey=valkey
    )
    with pytest.raises(RuntimeError):
        await HANDLERS[StorageCheck.kind](job, no_settings)
