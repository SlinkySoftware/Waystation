"""Long-term stats (T-F09) against real MongoDB/Valkey: hour and day rollups exact against synthetic minutes and
written once, storage snapshots (de-duplication per repository), the component index (T-S09) with download counters,
and the upstream health script."""

import json
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis

from waystation_common.db import components as components_db
from waystation_common.db.client import Database
from waystation_common.db.collections import (
    ASSETS,
    BLOBS,
    COMPONENTS,
    DOCKER_MANIFESTS,
    DOCKER_TAGS,
    DOWNLOADS_DAILY,
    REPO_USAGE,
    REPOSITORIES,
    STATS_DAY,
    STATS_HOUR,
    STATS_MINUTE,
    STORAGE_SNAPSHOTS,
)
from waystation_common.db.schema import ensure_schema
from waystation_common.events import EventEmitter, keys
from waystation_worker import history, rollup

pytestmark = pytest.mark.integration

DAY0 = (int(datetime.now(UTC).timestamp()) // 86400 - 3) * 86400  # three days ago, midnight UTC


def minute_doc(repo: str, ts: int, rng: random.Random) -> dict[str, Any]:
    doc: dict[str, Any] = {'ts': datetime.fromtimestamp(ts, UTC), 'm': {'repo_id': repo, 'format': 'npm'}}
    for field in keys.COUNTERS:
        doc[field] = rng.randint(0, 50)
    doc['lat_hist'] = {'le_5': rng.randint(0, 9), 'le_500': rng.randint(0, 3)}
    doc['ulat_hist'] = {'le_100': rng.randint(0, 4)}
    return doc


async def test_hour_and_day_rollups_are_exact_and_written_once(valkey: Redis, mongo_db: Database) -> None:
    await ensure_schema(mongo_db)
    rng = random.Random(7)
    # Two repositories, minutes spread over 26 hours (crossing a day boundary), some minutes without traffic.
    docs = [
        minute_doc(repo, DAY0 + m * 60, rng)
        for m in range(0, 26 * 60, 7)
        for repo in ('repo-a', 'repo-b')
        if not (repo == 'repo-b' and m % 3 == 0)
    ]
    await mongo_db[STATS_MINUTE].insert_many(docs)
    now = DAY0 + 2 * 86400 + 3 * 3600  # day 0 and day 1 closed (day 1 has no traffic after 02:00)

    for _ in range(5):  # MAX_PERIODS per run: a few runs catch up
        await history.rollup_periods(valkey, mongo_db, now=now)
    hours = [d async for d in mongo_db[STATS_HOUR].find({})]
    days = [d async for d in mongo_db[STATS_DAY].find({})]

    def expected(step: int) -> dict[tuple[str, int], dict[str, int]]:
        out: dict[tuple[str, int], dict[str, int]] = {}
        for doc in docs:
            ts = int(doc['ts'].timestamp())
            acc = out.setdefault((doc['m']['repo_id'], ts - ts % step), {})
            for field in keys.COUNTERS:
                acc[field] = acc.get(field, 0) + doc[field]
            for bucket, n in doc['lat_hist'].items():
                acc['lat.' + bucket] = acc.get('lat.' + bucket, 0) + n
        return {k: {f: n for f, n in acc.items() if n or not f.startswith('lat.')} for k, acc in out.items()}

    def actual(rolled: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, int]]:
        out = {}
        for doc in rolled:
            acc = {field: doc[field] for field in keys.COUNTERS}
            acc.update({'lat.' + b: n for b, n in doc['lat_hist'].items()})
            out[(doc['m']['repo_id'], int(doc['ts'].replace(tzinfo=UTC).timestamp()))] = acc
        return out

    assert actual(hours) == expected(3600)
    assert len(hours) == len(expected(3600))  # one document per repository and hour
    assert actual(days) == expected(86400)
    assert int(await valkey.get(keys.ROLLUP_DAY) or 0) == DAY0 + 2 * 86400

    # Valkey loses its data: the rollup resumes after the newest rolled-up documents, nothing is written twice.
    await valkey.flushdb()
    assert await history.rollup_periods(valkey, mongo_db, now=now) == history.Periods()
    assert await mongo_db[STATS_HOUR].count_documents({}) == len(hours)
    assert await mongo_db[STATS_DAY].count_documents({}) == len(days)
    # An open hour is never rolled up.
    open_hour = DAY0 + 2 * 86400 + 3 * 3600
    await mongo_db[STATS_MINUTE].insert_one(minute_doc('repo-a', open_hour + 60, rng))
    await history.rollup_periods(valkey, mongo_db, now=open_hour + 3600 + 30)
    assert await mongo_db[STATS_HOUR].count_documents({'ts': datetime.fromtimestamp(open_hour, UTC)}) == 0
    await history.rollup_periods(valkey, mongo_db, now=open_hour + 3600 + history.HOUR_GRACE_S)
    assert await mongo_db[STATS_HOUR].count_documents({'ts': datetime.fromtimestamp(open_hour, UTC)}) == 1


async def insert_repo(db: Database, repo_id: str, fmt: str) -> None:
    await db[REPOSITORIES].insert_one({'_id': repo_id, 'name': repo_id, 'format': fmt, 'type': 'proxy'})


async def insert_asset(db: Database, repo_id: str, path: str, sha: str, size: int, kind: str = 'content') -> str:
    asset_id = f'{repo_id}:{path}'
    now = datetime.now(UTC)
    await db[ASSETS].insert_one(
        {
            '_id': asset_id,
            'repo_id': repo_id,
            'path': path,
            'kind': kind,
            'blob_sha256': sha,
            'size': size,
            'created_at': now,
            'updated_at': now,
        }
    )
    return asset_id


async def test_storage_snapshot_and_dedup_per_repository(valkey: Redis, mongo_db: Database) -> None:
    await ensure_schema(mongo_db)
    for repo in ('raw-a', 'raw-b'):
        await insert_repo(mongo_db, repo, 'raw')
    shared, own = 'a' * 64, 'b' * 64
    await insert_asset(mongo_db, 'raw-a', 'x/shared.bin', shared, 1000)
    await insert_asset(mongo_db, 'raw-a', 'x/again.bin', shared, 1000)  # same blob twice in one repository
    await insert_asset(mongo_db, 'raw-b', 'shared.bin', shared, 1000)
    await insert_asset(mongo_db, 'raw-b', 'own.bin', own, 300)
    await mongo_db[BLOBS].insert_many([{'_id': shared, 'size': 1000}, {'_id': own, 'size': 300}])
    await mongo_db[REPO_USAGE].insert_many(
        [
            {'_id': 'raw-a', 'bytes': 2000, 'assets': 2, 'artifacts': 2},
            {'_id': 'raw-b', 'bytes': 1300, 'assets': 2, 'artifacts': 2},
        ]
    )
    await components_db.index_batch(mongo_db)
    now = DAY0 + 3 * 86400 + 1800.0
    assert await history.snapshot_storage(valkey, mongo_db, now=now)
    assert not await history.snapshot_storage(valkey, mongo_db, now=now + 60)  # once per hour
    await valkey.delete(keys.SNAPSHOT_HOUR)
    assert not await history.snapshot_storage(valkey, mongo_db, now=now + 60)  # also without Valkey's memory
    docs = {d['m']['repo_id']: d async for d in mongo_db[STORAGE_SNAPSHOTS].find({})}
    assert set(docs) == {'', 'raw-a', 'raw-b'}
    # raw-a stores 2000 bytes and holds half of the shared blob (500): saves 1500; raw-b: 1300 - (500 + 300) = 500.
    assert docs['raw-a']['dedup_saved_bytes'] == 1500 and docs['raw-b']['dedup_saved_bytes'] == 500
    assert docs['raw-a']['components'] == 2 and docs['raw-b']['components'] == 2
    instance = docs['']
    assert (instance['bytes'], instance['blob_bytes'], instance['blobs'], instance['dedup_saved_bytes']) == (
        3300,
        1300,
        2,
        2000,
    )
    assert await history.snapshot_storage(valkey, mongo_db, now=now + 3600)  # the next hour


async def test_component_index_downloads_and_sweep(valkey: Redis, mongo_db: Database) -> None:
    await ensure_schema(mongo_db)
    await insert_repo(mongo_db, 'npm', 'npm')
    await insert_repo(mongo_db, 'hub', 'docker')
    await insert_repo(mongo_db, 'deb', 'apt')
    tgz = await insert_asset(mongo_db, 'npm', 'lodash/-/lodash-4.17.21.tgz', 'c' * 64, 500)
    await insert_asset(mongo_db, 'npm', 'lodash/-/packument.json', 'd' * 64, 50, kind='metadata')
    tag = await insert_asset(mongo_db, 'hub', 'library/nginx/manifests/1.27', 'e' * 64, 900, kind='metadata')
    digest = 'sha256:' + 'f' * 64
    by_digest = await insert_asset(mongo_db, 'hub', f'library/nginx/manifests/{digest}', 'f' * 64, 900, kind='metadata')
    await insert_asset(mongo_db, 'hub', 'blobs/sha256:' + '1' * 64, '1' * 64, 5000)
    await mongo_db[DOCKER_TAGS].insert_one(
        {'repo_id': 'hub', 'image': 'library/nginx', 'tag': '1.27', 'manifest_digest': digest}
    )
    await mongo_db[DOCKER_MANIFESTS].insert_one(
        {'repo_id': 'hub', 'image': 'library/nginx', 'digest': digest, 'is_index': False, 'size_total': 5900}
    )
    deb = await insert_asset(mongo_db, 'deb', 'pool/main/c/curl/curl_8.14.1-2_amd64.deb', '2' * 64, 400)

    # Downloads before the index runs: counted on the assets, added to the component when it is linked.
    em = EventEmitter(valkey)
    for asset_id in (tgz, tgz, tag, by_digest, deb):
        em.asset_downloaded(asset_id)
    await em.flush()
    await rollup.flush_last_seen(valkey, mongo_db)
    result = await components_db.index_batch(mongo_db)
    assert (result.assets, result.linked, result.components) == (6, 4, 3)
    comps = {c['display']: c async for c in mongo_db[COMPONENTS].find({})}
    assert set(comps) == {'lodash@4.17.21', 'library/nginx:1.27', 'curl 8.14.1-2'}
    assert comps['lodash@4.17.21']['download_count'] == 2 and comps['lodash@4.17.21']['bytes_served'] == 1000
    assert comps['library/nginx:1.27']['download_count'] == 2  # by tag and by digest
    assert comps['library/nginx:1.27']['size_bytes'] == 5900  # the image, not the manifest file
    assert comps['curl 8.14.1-2']['version_sort']
    unlinked = await mongo_db[ASSETS].count_documents({'component_id': ''})
    assert unlinked == 2  # the packument and the docker blob belong to no component; never looked at again
    assert (await components_db.index_batch(mongo_db)).assets == 0

    # Later downloads go to the component and the daily counters in bulk.
    em.asset_downloaded(tgz)
    await em.flush()
    await rollup.flush_last_seen(valkey, mongo_db)
    lodash = await mongo_db[COMPONENTS].find_one({'display': 'lodash@4.17.21'})
    assert lodash is not None and lodash['download_count'] == 3 and lodash['last_downloaded_at'] is not None
    daily = [d async for d in mongo_db[DOWNLOADS_DAILY].find({})]
    assert [(d['component_id'], d['count'], d['bytes'], d['repo_id']) for d in daily] == [
        (lodash['_id'], 1, 500, 'npm')
    ]

    # The file goes (cleanup): the sweep recomputes and removes the component once the grace period is over.
    await mongo_db[ASSETS].delete_one({'_id': deb})
    curl = comps['curl 8.14.1-2']['_id']
    assert await components_db.recompute(mongo_db, [curl]) == (0, 0)  # within the grace period
    later = datetime.now(UTC) + components_db.EMPTY_GRACE + timedelta(minutes=1)
    assert await components_db.recompute(mongo_db, [curl], now=later) == (0, 1)
    cursor, updated, removed = await components_db.sweep_batch(mongo_db, None)
    assert (cursor, updated, removed) == (None, 2, 0)
    # A replaced file is linked again (assets.save drops the link) and its component recomputed.
    await mongo_db[ASSETS].update_one({'_id': tgz}, {'$set': {'size': 700}, '$unset': {'component_id': ''}})
    await history.index_components(valkey, mongo_db)
    lodash = await mongo_db[COMPONENTS].find_one({'display': 'lodash@4.17.21'})
    assert lodash is not None and lodash['size_bytes'] == 700


async def test_upstream_state_is_announced_on_changes_only(valkey: Redis) -> None:
    clock = [1_800_000_000.0]
    em = EventEmitter(valkey, clock=lambda: clock[0])

    async def states() -> list[str]:
        entries = await valkey.xrange(keys.ACTIVITY)
        return [json.loads(fields['data'])['detail']['state'] for _, fields in entries]

    em.upstream(repo_id='r1', repo='npmjs', format='npm', ok=True, latency_ms=12)
    em.upstream(repo_id='r1', repo='npmjs', format='npm', ok=False, error='connect timeout')
    em.upstream(repo_id='r1', repo='npmjs', format='npm', ok=False, error='connect timeout')
    await em.flush()
    assert await states() == ['degraded']
    health = await valkey.hgetall(keys.upstream_health_key('r1'))
    assert health['consecutive_failures'] == '2' and health['last_latency_ms'] == '12.0'
    em.upstream(repo_id='r1', repo='npmjs', format='npm', ok=True, latency_ms=10)
    em.upstream(repo_id='r1', repo='npmjs', format='npm', ok=True, latency_ms=10)
    await em.flush()
    assert await states() == ['degraded', 'ok']
    clock[0] += 10  # flapping: failing again within the minute is not announced again
    em.upstream(repo_id='r1', repo='npmjs', format='npm', ok=False, error='502')
    em.upstream(repo_id='r1', repo='npmjs', format='npm', ok=True)
    await em.flush()
    assert await states() == ['degraded', 'ok']
    clock[0] += 60
    em.upstream(repo_id='r1', repo='npmjs', format='npm', ok=False, error='502')
    await em.flush()
    assert await states() == ['degraded', 'ok', 'degraded']
    minute = keys.minute_of(clock[0] - 70)
    assert await valkey.hgetall(keys.ulat_key(minute, 'r1')) == {'le_20': '1', 'le_10': '2'}
