"""Blob verify (T-O07) against real MongoDB/Valkey and a real blob store: missing and damaged blobs, re-fetch marks on
proxy assets, hosted losses (result, log, activity event, totals, the /metrics gauge's counts), marks cleared when
the blob is back, deep (sha256) checks, report-only mode, and ``run_task`` (enqueue + wait)."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis
from ws_testing import make_settings

from waystation_common.db import blobs as blobs_db
from waystation_common.db import jobs as jobs_db
from waystation_common.db import verify as verify_db
from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, JOB_OUTBOX, JOB_RUNS, REPOSITORIES
from waystation_common.db.schema import ensure_schema
from waystation_common.events import EventEmitter, keys
from waystation_common.jobs import JobEnvelope
from waystation_common.jobs.model import BlobVerify
from waystation_common.storage import BlobStore
from waystation_worker import run_task, verify
from waystation_worker.jobs import BUILTIN_DEFINITIONS, HANDLERS, JobContext

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


@pytest.fixture
async def db(mongo_db: Database) -> Database:
    await ensure_schema(mongo_db)
    await mongo_db[REPOSITORIES].insert_many(
        [
            {'_id': 'rh', 'name': 'files-hosted', 'type': 'hosted', 'format': 'raw'},
            {'_id': 'rp', 'name': 'files-proxy', 'type': 'proxy', 'format': 'raw'},
        ]
    )
    return mongo_db


@pytest.fixture
def store(tmp_path: Path, db: Database) -> BlobStore:
    root = tmp_path / 'data'
    root.mkdir()
    return BlobStore(root, guard=blobs_db.MongoBlobGuard(db))


async def put(store: BlobStore, data: bytes) -> str:
    async with await store.open_writer() as writer:
        await writer.write(data)
        ref = await store.commit(writer)
    return ref.sha256


async def asset(db: Database, asset_id: str, repo_id: str, sha: str, size: int) -> None:
    await db[ASSETS].insert_one(
        {'_id': asset_id, 'repo_id': repo_id, 'path': f'{asset_id}.txt', 'blob_sha256': sha, 'size': size}
    )


async def mark_of(db: Database, asset_id: str) -> dict[str, Any] | None:
    doc = await db[ASSETS].find_one({'_id': asset_id})
    assert doc is not None
    return doc.get('blob_missing')


async def seed(db: Database, store: BlobStore) -> dict[str, str]:
    """hosted-ok, hosted-gone (file deleted), proxy-gone (file deleted), proxy-bad (truncated), shared (one blob,
    both repos, fine)."""
    shas = {name: await put(store, f'content of {name}'.encode() * 10) for name in ('ok', 'hgone', 'pgone', 'bad')}
    size = len(b'content of ok' * 10)
    for name, sha in shas.items():
        hosted = name in {'ok', 'hgone'}
        data_size = len(f'content of {name}'.encode() * 10)
        await asset(db, f'h-{name}' if hosted else f'p-{name}', 'rh' if hosted else 'rp', sha, data_size)
    await asset(db, 'p-shared', 'rp', shas['ok'], size)
    store.blob_path(shas['hgone']).unlink()
    store.blob_path(shas['pgone']).unlink()
    store.blob_path(shas['bad']).write_bytes(b'trunc')
    return shas


async def test_verify_marks_proxies_and_reports_hosted_losses(db: Database, store: BlobStore) -> None:
    shas = await seed(db, store)
    lines: list[str] = []

    async def log_line(line: str) -> None:
        lines.append(line)

    result = await verify.verify(db, store, now=NOW, log_line=log_line)
    assert (result.assets, result.blobs) == (5, 4)
    assert (result.missing_blobs, result.damaged_blobs, result.moved_blobs) == (2, 1, 1)
    assert (result.proxy_marked, result.hosted_lost, result.cleared) == (2, 1, 0)
    assert result.lost_repositories == {'files-hosted': 1}
    assert lines == ['lost: files-hosted/h-hgone.txt (blob missing)']
    assert await mark_of(db, 'h-hgone') == {'reason': 'missing', 'hosted': True, 'detected_at': NOW.replace(tzinfo=None)}
    assert await mark_of(db, 'p-pgone') == {'reason': 'missing', 'hosted': False, 'detected_at': NOW.replace(tzinfo=None)}
    assert (await mark_of(db, 'p-bad') or {})['reason'] == 'damaged'
    assert await mark_of(db, 'h-ok') is None and await mark_of(db, 'p-shared') is None
    # The damaged file was moved out of blobs/ (into uploads/, where GC sweeps it): a proxy request re-fetches it.
    assert not await store.exists(shas['bad'])
    assert [p.name.endswith('.damaged') for p in store.uploads_dir.iterdir()] == [True]
    assert await verify_db.marked_counts(db) == {'hosted': 1, 'proxy': 2}

    # A second run: nothing new to mark, the loss is still reported; the first detection time is kept.
    again = await verify.verify(db, store, now=NOW + timedelta(days=7))
    assert (again.proxy_marked, again.hosted_lost, again.damaged_blobs, again.missing_blobs) == (0, 1, 0, 3)
    assert (await mark_of(db, 'h-hgone') or {})['detected_at'] == NOW.replace(tzinfo=None)

    # The blobs come back (a re-fetch, a re-upload or a restore): the marks go.
    for name in ('hgone', 'pgone', 'bad'):
        await put(store, f'content of {name}'.encode() * 10)
    healed = await verify.verify(db, store, now=NOW)
    assert (healed.cleared, healed.hosted_lost, healed.missing_blobs) == (3, 0, 0)
    assert await verify_db.marked_counts(db) == {'hosted': 0, 'proxy': 0}


async def test_report_only_and_deep(db: Database, store: BlobStore) -> None:
    sha = await put(store, b'0123456789')
    await asset(db, 'p-x', 'rp', sha, 10)
    store.blob_path(sha).write_bytes(b'9876543210')  # same size, different bytes: only a deep check sees it
    shallow = await verify.verify(db, store, now=NOW)
    assert shallow.damaged_blobs == 0 and await mark_of(db, 'p-x') is None
    report = await verify.verify(db, store, deep=True, repair=False, now=NOW)
    assert (report.damaged_blobs, report.moved_blobs, report.proxy_marked) == (1, 0, 1)
    assert await store.exists(sha)  # report only: left in place
    deep = await verify.verify(db, store, deep=True, now=NOW)
    assert (deep.damaged_blobs, deep.moved_blobs) == (1, 1) and not await store.exists(sha)


async def test_asset_size_differs_from_a_sound_blob(db: Database, store: BlobStore) -> None:
    sha = await put(store, b'abc')  # blobs record: 3 bytes, like the file
    await asset(db, 'h-x', 'rh', sha, 99)
    result = await verify.verify(db, store, now=NOW)
    assert (result.inconsistent, result.damaged_blobs, result.hosted_lost) == (1, 0, 0)
    assert await store.exists(sha)


async def test_handler_records_totals_and_activity(db: Database, valkey: Redis, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = BlobStore.from_settings(settings)
    await seed(db, store)
    definition = next(d for d in BUILTIN_DEFINITIONS if d.id == 'blob.verify')
    assert definition.kind == BlobVerify.kind and definition.params == {'deep': False, 'repair': True}
    job = BlobVerify()
    env = JobEnvelope.for_payload(job)
    async with EventEmitter(valkey) as emitter:
        ctx = JobContext(
            job=env, run_id='r1', attempt=1, worker_id='w', db=db, valkey=valkey, settings=settings, emitter=emitter
        )
        result = await HANDLERS[BlobVerify.kind](job, ctx)
    assert result is not None and (result['hosted_lost'], result['proxy_marked']) == (1, 2)
    totals = await valkey.hgetall(keys.MAINTENANCE_TOTALS)
    assert totals['verify_runs'] == '1' and totals['verify_hosted_lost'] == '1'
    assert totals['verify_missing_blobs'] == '2' and totals['verify_damaged_blobs'] == '1'
    assert totals['verify_refetch_marked'] == '2'
    events = await valkey.xrange(keys.ACTIVITY)
    assert len(events) == 1 and events[0][1]['type'] == 'storage.blobs_missing'
    assert 'Blob verify: 1 hosted assets lost (files-hosted (1)); 2 cached assets will be re-fetched' in events[0][1][
        'data'
    ]
    with pytest.raises(RuntimeError):
        await HANDLERS[BlobVerify.kind](job, JobContext(job=env, run_id='r2', attempt=1, worker_id='w', db=db, valkey=valkey))


async def test_run_task_enqueues_and_waits(db: Database) -> None:
    with pytest.raises(run_task.TaskError, match='no scheduled task'):
        await run_task.run(db, 'blob.verify', wait=False, timeout_s=0.05, poll_s=0.01)
    await jobs_db.ensure_definitions(db, BUILTIN_DEFINITIONS)
    queued = await run_task.run(db, 'blob.verify', wait=False, timeout_s=1)
    assert queued['status'] == 'queued'
    outbox = await db[JOB_OUTBOX].find_one({})
    assert outbox is not None and outbox['job']['trigger'] == 'manual'
    assert outbox['job']['definition_id'] == 'blob.verify'

    async def fake_worker() -> None:  # a run of the same task (dedupe key) finishing after the request
        await asyncio.sleep(0.05)
        now = datetime.now(UTC)
        await db[JOB_RUNS].insert_one(
            {
                '_id': 'run1',
                'job_id': 'other',
                'kind': 'blob.verify',
                'dedupe_key': 'blob.verify',
                'status': 'succeeded',
                'started_at': now,
                'finished_at': now,
                'result': {'hosted_lost': 0},
            }
        )

    task = asyncio.create_task(fake_worker())
    done = await run_task.run(db, 'blob.verify', wait=True, timeout_s=10, poll_s=0.02)
    await task
    assert (done['status'], done['run_id'], done['result']) == ('succeeded', 'run1', {'hosted_lost': 0})
    with pytest.raises(run_task.TaskError, match='did not finish'):
        await run_task.run(db, 'blob.gc', wait=True, timeout_s=0.1, poll_s=0.02)


async def test_a_fill_clears_the_mark(db: Database, store: BlobStore) -> None:
    """The proxy engine's fill (``waystation_server.db.assets.save``) brings the blob back and drops the mark."""
    from waystation_server.db import assets as assets_db
    from waystation_server.proxy.records import AssetKind, StoredAsset, UpstreamMeta

    sha = await put(store, b'payload')
    await asset(db, 'p-1', 'rp', sha, 7)
    store.blob_path(sha).unlink()
    await verify.verify(db, store, now=NOW)
    assert (await mark_of(db, 'p-1') or {})['reason'] == 'missing'
    await put(store, b'payload')
    await assets_db.save(
        db,
        StoredAsset(
            _id='p-1',
            repo_id='rp',
            path='p-1.txt',
            kind=AssetKind.CONTENT,
            blob_sha256=sha,
            size=7,
            content_type='text/plain',
            upstream=UpstreamMeta(url='https://upstream.example.com/p-1.txt', fetched_at=NOW),
            created_at=NOW,
            updated_at=NOW,
        ),
    )
    assert await mark_of(db, 'p-1') is None
