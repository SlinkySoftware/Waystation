"""Blob GC (T-O02) against real MongoDB/Valkey and a real blob store.

- mark-and-sweep across runs, the orphan upload sweep, adoption of untracked files, the handler's result/metrics;
- property test (acceptance): GC never removes a blob that is referenced, claimed within the grace period, or
  marked within the grace period, and it does remove every blob that is none of these;
- race (acceptance): a real cache fill through the proxy engine (server ``Services``, fake upstream) of content
  whose old, unreferenced blob GC is deleting at the same moment, in every interleaving: the fill's asset always
  ends up with its blob on disk.
"""

import asyncio
import hashlib
import os
import random
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from redis.asyncio import Redis
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import FilesPlugin, add_repo, asgi_client, make_settings

from waystation_common.db import blobs as blobs_db
from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, BLOBS
from waystation_common.db.migrations import apply_migrations
from waystation_common.db.schema import ensure_schema
from waystation_common.events import EventEmitter, keys
from waystation_common.jobs import BlobGc, JobEnvelope
from waystation_common.settings import Settings
from waystation_common.storage import BlobStore
from waystation_server.services import Services
from waystation_worker import gc
from waystation_worker.jobs import BUILTIN_DEFINITIONS, HANDLERS, JobContext

pytestmark = pytest.mark.integration

GRACE = 24 * 3600
HOUR = timedelta(hours=1)


def new_store(root: Path, db: Database | None = None) -> BlobStore:
    root.mkdir(parents=True, exist_ok=True)
    return BlobStore(root, guard=blobs_db.MongoBlobGuard(db) if db is not None else None)


async def put(store: BlobStore, data: bytes) -> str:
    async with await store.open_writer() as writer:
        await writer.write(data)
        ref = await store.commit(writer)
    return ref.sha256


async def reference(db: Database, sha: str, path: str) -> None:
    now = datetime.now(UTC)
    await db[ASSETS].insert_one({'_id': path, 'repo_id': 'r', 'path': path, 'blob_sha256': sha, 'created_at': now})


@pytest.fixture
async def db(mongo_db: Database) -> Database:
    await ensure_schema(mongo_db)
    return mongo_db


@pytest.fixture
def store(tmp_path: Path, db: Database) -> BlobStore:
    return new_store(tmp_path / 'data', db)


# ---------------------------------------------------------------------------- behaviour
async def test_mark_then_sweep_after_grace(db: Database, store: BlobStore) -> None:
    kept = await put(store, b'referenced')
    gone = await put(store, b'orphan')
    await reference(db, kept, 'a')
    now = datetime.now(UTC)

    first = await gc.collect(db, store, grace_s=GRACE, upload_age_s=GRACE, now=now)
    assert (first.marked, first.freed_blobs) == (1, 0)
    # within the grace period: nothing is deleted
    second = await gc.collect(db, store, grace_s=GRACE, upload_age_s=GRACE, now=now + 23 * HOUR)
    assert second.freed_blobs == 0 and await store.exists(gone)
    # the claim made by the commit protects it for a grace period too, even once marked long ago
    await db[BLOBS].update_one({'_id': gone}, {'$set': {'gc_marked_at': now - 48 * HOUR}})
    third = await gc.collect(db, store, grace_s=GRACE, upload_age_s=GRACE, now=now + 23 * HOUR)
    assert third.freed_blobs == 0
    fourth = await gc.collect(db, store, grace_s=GRACE, upload_age_s=GRACE, now=now + 25 * HOUR)
    assert (fourth.freed_blobs, fourth.freed_bytes) == (1, len(b'orphan'))
    assert not await store.exists(gone) and await store.exists(kept)
    assert await db[BLOBS].find_one({'_id': gone}) is None
    # referenced again after being marked: unmarked, never deleted
    await db[BLOBS].update_one({'_id': kept}, {'$set': {'gc_marked_at': now - 48 * HOUR}})
    fifth = await gc.collect(db, store, grace_s=0, upload_age_s=GRACE, now=now + 49 * HOUR)
    assert fifth.unmarked == 1 and await store.exists(kept)


async def test_orphan_uploads_adoption_and_missing(db: Database, tmp_path: Path) -> None:
    plain = new_store(tmp_path / 'data')  # no guard: files without blobs documents (pre-claim data)
    untracked = await put(plain, b'untracked')
    store = new_store(tmp_path / 'data', db)
    missing = hashlib.sha256(b'missing').hexdigest()
    await db[BLOBS].insert_one({'_id': missing, 'size': 7, 'first_seen': datetime.now(UTC)})
    await reference(db, missing, 'm')
    store.uploads_dir.mkdir(parents=True, exist_ok=True)
    old, fresh = store.uploads_dir / 'old', store.uploads_dir / 'fresh'
    old.write_bytes(b'x')
    fresh.write_bytes(b'y')
    os.utime(old, (time.time() - 2 * GRACE, time.time() - 2 * GRACE))
    result = await gc.collect(db, store, grace_s=GRACE, upload_age_s=GRACE)
    assert (result.uploads_removed, result.adopted, result.marked, result.missing) == (1, 1, 1, 1)
    assert not old.exists() and fresh.exists()
    doc = await db[BLOBS].find_one({'_id': untracked})
    assert doc is not None and doc['size'] == len(b'untracked') and 'gc_marked_at' in doc
    # an abandoned deletion (GC died after its compare-and-set) does not block writers for long
    await db[BLOBS].update_one(
        {'_id': untracked}, {'$set': {'gc_state': 'deleting', 'gc_deleting_at': datetime.now(UTC) - 2 * HOUR}}
    )
    await put(store, b'untracked')
    doc = await db[BLOBS].find_one({'_id': untracked})
    assert doc is not None and 'gc_state' not in doc and 'gc_marked_at' not in doc


async def test_handler_records_metrics_and_activity(db: Database, valkey: Redis, tmp_path: Path) -> None:
    settings_ = make_settings(tmp_path)
    store = BlobStore.from_settings(settings_)
    await put(store, b'a')  # untracked (no guard): adopted, then marked
    definition = next(d for d in BUILTIN_DEFINITIONS if d.id == 'blob.gc')
    assert definition.kind == BlobGc.kind and definition.params == {'grace_s': GRACE, 'upload_age_s': GRACE}
    job = BlobGc(grace_s=0)
    env = JobEnvelope.for_payload(job)
    async with EventEmitter(valkey) as emitter:
        ctx = JobContext(
            job=env, run_id='r1', attempt=1, worker_id='w', db=db, valkey=valkey, settings=settings_, emitter=emitter
        )
        first = await HANDLERS[BlobGc.kind](job, ctx)
        assert first is not None and (first['adopted'], first['marked'], first['freed_blobs']) == (1, 1, 0)
        second = await HANDLERS[BlobGc.kind](job, ctx)
        assert second is not None and (second['freed_blobs'], second['freed_bytes']) == (1, 1)
    totals = await valkey.hgetall(keys.MAINTENANCE_TOTALS)
    assert totals['gc_runs'] == '2' and totals['gc_freed_blobs'] == '1' and totals['gc_freed_bytes'] == '1'
    events = await valkey.xrange(keys.ACTIVITY)
    assert len(events) == 1 and 'Blob GC freed 1 blobs (1 B)' in events[0][1]['data']
    no_settings = JobContext(job=env, run_id='r2', attempt=1, worker_id='w', db=db, valkey=valkey)
    with pytest.raises(RuntimeError):
        await HANDLERS[BlobGc.kind](job, no_settings)


# ---------------------------------------------------------------------------- property
BLOB = st.fixed_dictionaries(
    {
        'referenced': st.booleans(),
        'claimed_h': st.one_of(st.none(), st.floats(0, 72)),
        'marked_h': st.one_of(st.none(), st.floats(0, 72)),
    }
)


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(blobs=st.lists(BLOB, min_size=1, max_size=10), step_h=st.floats(0, 48))
async def test_gc_never_removes_a_referenced_or_recent_blob(
    db: Database, tmp_path: Path, blobs: list[dict[str, Any]], step_h: float
) -> None:
    for name in (ASSETS, BLOBS):
        await db[name].delete_many({})
    store = new_store(tmp_path / os.urandom(8).hex())  # a fresh store per example
    now = datetime(2026, 11, 9, 12, tzinfo=UTC)
    shas: list[str] = []
    for i, spec in enumerate(blobs):
        sha = await put(store, f'blob {i}'.encode())
        shas.append(sha)
        doc: dict[str, Any] = {'_id': sha, 'size': 6, 'first_seen': now - 100 * HOUR}
        if spec['claimed_h'] is not None:
            doc['claimed_at'] = now - spec['claimed_h'] * HOUR
        if spec['marked_h'] is not None:
            doc['gc_marked_at'] = now - spec['marked_h'] * HOUR
        await db[BLOBS].insert_one(doc)
        if spec['referenced']:
            await reference(db, sha, f'path{i}')
    for run_at in (now, now + step_h * HOUR):
        before = {d['_id']: d async for d in db[BLOBS].find({})}
        await gc.collect(db, store, grace_s=GRACE, upload_age_s=GRACE, now=run_at)
        cutoff = run_at - timedelta(seconds=GRACE)
        for i, sha in enumerate(shas):
            doc = before.get(sha)
            exists = await store.exists(sha)
            if blobs[i]['referenced']:
                assert exists, 'a referenced blob was deleted'
                continue
            if doc is None:
                assert not exists
                continue
            claimed, marked = doc.get('claimed_at'), doc.get('gc_marked_at')
            eligible = marked is not None and marked <= cutoff and (claimed is None or claimed <= cutoff)
            assert exists != eligible, f'blob {i}: eligible={eligible} but exists={exists}'


# ---------------------------------------------------------------------------- race: a cache fill against GC
@pytest.fixture
async def services(tmp_path: Path, mongo_db: Database, valkey: Redis) -> AsyncIterator[Services]:
    await ensure_schema(mongo_db)
    await apply_migrations(mongo_db)
    cfg: Settings = make_settings(
        tmp_path,
        mongo_uri=os.environ['WAYSTATION_TEST_MONGO_URI'],
        mongo_db=mongo_db.name,
        valkey_url=os.environ['WAYSTATION_TEST_VALKEY_URL'],
        ssrf_allow_cidrs=['127.0.0.0/8'],
    )
    svc = Services.create(cfg)
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


class SlowGuard:
    """The server's guard with a delay before each claim (moves the claim around GC's critical section)."""

    def __init__(self, inner: blobs_db.MongoBlobGuard) -> None:
        self.inner = inner
        self.delay = 0.0
        self.claimed = asyncio.Event()

    async def claim(self, sha256: str, size: int) -> None:
        await asyncio.sleep(self.delay)
        await self.inner.claim(sha256, size)
        self.claimed.set()


async def test_fill_racing_gc_always_keeps_its_blob(services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    db, store = services.db, services.blobs
    assert isinstance(store.guard, blobs_db.MongoBlobGuard)
    guard = SlowGuard(store.guard)
    store.guard = guard
    plugin = FilesPlugin(services)
    services.formats.set_plugin(plugin)  # type: ignore[arg-type]
    check_delay = {'s': 0.0}
    real_is_referenced = blobs_db.is_referenced

    async def slow_is_referenced(db_: Database, sha256: str) -> bool:  # GC's re-check, after its compare-and-set
        await asyncio.sleep(check_delay['s'])
        return await real_is_referenced(db_, sha256)

    monkeypatch.setattr(blobs_db, 'is_referenced', slow_is_referenced)
    outcomes: dict[str, int] = {}
    async with FakeUpstream() as upstream:
        await add_repo(services, upstream.url)
        async with asgi_client(services) as client:
            # (fill delay, claim delay, GC re-check delay): GC done before the fill commits; the claim lands while
            # GC is deleting (it waits); the claim lands first (GC's compare-and-set misses); then random ones.
            rng = random.Random(7)
            plans: list[tuple[float, float, float]] = [(0.2, 0.0, 0.0), (0.0, 0.0, 0.3), (0.0, 0.0, 0.0)]
            plans += [(rng.uniform(0, 0.05), rng.uniform(0, 0.05), rng.uniform(0, 0.05)) for _ in range(30)]
            for i, (fill_delay, claim_delay, recheck_delay) in enumerate(plans):
                data = f'content {i} '.encode() * 5000
                sha = hashlib.sha256(data).hexdigest()
                # the blob was cached long ago; cleanup removed its asset; GC marked it a day ago
                plain = BlobStore(store.root)
                await put(plain, data)
                old = datetime.now(UTC) - 48 * HOUR
                await db[BLOBS].insert_one(
                    {'_id': sha, 'size': len(data), 'first_seen': old, 'claimed_at': old, 'gc_marked_at': old}
                )
                upstream.routes[f'/race/{i}.bin'] = Respond(200, {}, data, chunk=16384, delay=fill_delay)
                guard.delay, check_delay['s'] = claim_delay, recheck_delay

                guard.claimed = asyncio.Event()

                async def sweep(sha: str = sha, after_claim: bool = i == 2) -> str:
                    if after_claim:  # the claim lands first
                        await guard.claimed.wait()
                    outcome, _ = await gc.sweep_one(db, store, sha, cutoff=datetime.now(UTC) - HOUR)
                    return outcome

                fill, outcome = await asyncio.gather(client.get(f'/repository/files/race/{i}.bin'), sweep())
                assert fill.status_code == 200 and fill.content == data
                asset = await db[ASSETS].find_one({'path': f'race/{i}.bin'})
                assert asset is not None and asset['blob_sha256'] == sha
                assert await store.exists(sha), f'plan {i}: the fill lost its blob ({outcome})'
                doc = await db[BLOBS].find_one({'_id': sha})
                assert doc is not None and 'gc_state' not in doc and 'gc_marked_at' not in doc
                # a later GC run never deletes it: it is referenced
                assert (await gc.sweep_one(db, store, sha, cutoff=datetime.now(UTC) + HOUR))[0] == 'kept'
                hit = await client.get(f'/repository/files/race/{i}.bin')
                assert hit.headers.get('x-ws-cache') == 'HIT'
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
    # both sides won some races: GC deleted the old copy (the fill wrote it again) and GC backed off
    assert outcomes.get('freed', 0) >= 2 and outcomes.get('kept', 0) >= 1, outcomes


def test_job_type_dedupes() -> None:
    assert BlobGc().dedupe_key() == 'blob.gc'
    factory: Callable[[], BlobGc] = BlobGc
    assert factory().grace_s == GRACE
