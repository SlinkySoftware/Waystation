"""Top-N, storage history, latency percentiles, hour/day series, digest "where used" and browse counters (T-S09,
T-S11) through the API, against real MongoDB/Valkey with the worker's stats rollup run in between."""

import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import FilesPlugin, add_repo, asgi_client

from waystation_common.db.collections import ASSETS, STATS_HOUR, STATS_MINUTE
from waystation_common.events import keys
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.services import Services
from waystation_worker import history as worker_history
from waystation_worker import rollup

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
DATA = b'x' * 300_000
OTHER = b'y' * 1000


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD))


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream({'/a.bin': Respond(200, {}, DATA), '/sub/b.bin': Respond(200, {}, OTHER)}) as fake:
        yield fake


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


async def get(client: httpx.AsyncClient, url: str, **params: Any) -> Any:
    response = await client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_stats_from_the_component_index_and_snapshots(
    admin: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    services.formats.set_plugin(FilesPlugin(services))  # type: ignore[arg-type]
    files = await add_repo(services, upstream.url)
    await add_repo(services, upstream.url, name='mirror')
    for url in ('/repository/files/a.bin', '/repository/files/a.bin', '/repository/mirror/a.bin'):
        assert (await admin.get(url)).status_code == 200  # hits are X-Accel-Redirects (no body here)
    assert (await admin.get('/repository/files/sub/b.bin')).content == OTHER
    await services.emitter.flush()
    services.storage.invalidate()
    # One worker cycle: closed minutes, the component index, the download flush, the hourly storage snapshot.
    assert await rollup.rollup_once(services.valkey, services.db, 'test-worker', now=time.time() + 66) is not None
    await worker_history.snapshot_storage(services.valkey, services.db, now=time.time())  # this hour's, in any case

    top = await get(admin, '/api/v1/stats/top/artifacts', by='downloads', window='all')
    first = top['items'][0]
    assert (first['repository'], first['display'], first['downloads']) == ('files', 'a.bin', 2)
    assert first['bytes_served'] == 2 * len(DATA) and first['size_bytes'] == len(DATA)
    assert first['path'] == 'a.bin' and len(first['sha256']) == 64
    week = await get(admin, '/api/v1/stats/top/artifacts', by='bytes', window='7d')
    best = week['items'][0]
    assert (best['repository'], best['display'], best['downloads']) == ('files', 'a.bin', 2)
    largest = await get(admin, '/api/v1/stats/top/artifacts', by='size', repo='mirror')
    assert [i['display'] for i in largest['items']] == ['a.bin']
    recent = await get(admin, '/api/v1/stats/top/artifacts', by='recent', limit=1)
    assert len(recent['items']) == 1
    assert (await admin.get('/api/v1/stats/top/artifacts', params={'repo': 'nope'})).status_code == 404

    repos = await get(admin, '/api/v1/stats/top/repositories', range='1h')
    assert [(r['repository'], r['requests']) for r in repos['items']] == [('files', 3), ('mirror', 1)]
    assert repos['items'][0]['latency_p95_ms'] is not None and repos['items'][0]['hit_ratio'] == round(1 / 3, 4)

    summary = await get(admin, '/api/v1/stats/summary', range='1h')
    assert summary['requests'] == 4
    assert summary['latency_p50_ms'] is not None and summary['latency_p95_ms'] >= summary['latency_p50_ms']
    upstreams = {u['repository']: u for u in await get(admin, '/api/v1/stats/upstreams')}
    assert upstreams['files']['p95_latency_ms'] is not None

    history = await get(admin, '/api/v1/stats/storage/history', range='7d')
    assert history['step_s'] == 3600 and len(history['timestamps']) == 168
    assert history['used_bytes'][-1] == len(DATA) + len(OTHER)  # the blob is stored once
    assert history['stored_bytes'][-1] == 2 * len(DATA) + len(OTHER)
    assert history['artifacts'][-1] == 3
    assert all(v is None for v in history['used_bytes'][:-1])

    storage = await get(admin, '/api/v1/stats/storage')
    by_repo = {r['repository']: r for r in storage['by_repository']}
    # a.bin is shared by both repositories: each holds half of it.
    assert by_repo['files']['dedup_saved_bytes'] == len(DATA) // 2
    assert by_repo['mirror']['dedup_saved_bytes'] == len(DATA) // 2
    assert by_repo['files']['components'] == 2 and by_repo['mirror']['components'] == 1

    sha = first['sha256']
    usage = await get(admin, f'/api/v1/digests/sha256:{sha}/usage')
    assert sorted((u['repository'], u['path'], u['kind']) for u in usage['items']) == [
        ('files', 'a.bin', 'asset'),
        ('mirror', 'a.bin', 'asset'),
    ]
    assert usage['size'] == len(DATA) and usage['truncated'] is False
    assert (await admin.get('/api/v1/digests/not-a-digest/usage')).status_code == 400
    unknown = await get(admin, '/api/v1/digests/' + '0' * 64 + '/usage')
    assert unknown == {'digest': 'sha256:' + '0' * 64, 'size': None, 'items': [], 'truncated': False}

    root = await get(admin, '/api/v1/repositories/files/browse')
    entries = {e['name']: e for e in root['entries']}
    assert entries['sub']['child_count'] == 1
    assert entries['a.bin']['download_count'] == 2 and entries['a.bin']['last_downloaded_at'] is not None
    asset = await services.db[ASSETS].find_one({'repo_id': files, 'path': 'a.bin'})
    assert asset is not None and asset['component_id']


async def test_long_ranges_read_the_hour_rollups(
    admin: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    services.formats.set_plugin(FilesPlugin(services))  # type: ignore[arg-type]
    repo = await add_repo(services, upstream.url)
    hour = (int(time.time()) // 3600) * 3600
    zero = dict.fromkeys(keys.COUNTERS, 0)
    meta = {'m': {'repo_id': repo, 'format': 'raw'}, 'lat_hist': {'le_10': 7}, 'ulat_hist': {}}
    two_days = datetime.fromtimestamp(hour - 2 * 86400, UTC)
    await services.db[STATS_HOUR].insert_one({**meta, **zero, 'ts': two_days, 'requests': 7, 'hits': 7})
    # A minute of that hour is still in stats_minute: the hourly document covers it, it is not counted twice.
    await services.db[STATS_MINUTE].insert_one(
        {**meta, **zero, 'ts': two_days + timedelta(minutes=5), 'requests': 100, 'hits': 100}
    )
    await services.valkey.set(keys.ROLLUP_HOUR, str(hour))  # every hour before the current one is rolled up
    assert (await admin.get('/repository/files/a.bin')).status_code == 200
    await services.emitter.flush()
    for range_name, step in (('7d', 3600), ('30d', 21600)):
        traffic = await get(admin, '/api/v1/stats/traffic', range=range_name, metric='requests')
        assert traffic['step_s'] == step
        assert sum(sum(s['values']) for s in traffic['series']) == 8, range_name
    # Without the rollup's progress in Valkey, the newest hourly document is the boundary.
    await services.valkey.delete(keys.ROLLUP_HOUR)
    summary = await get(admin, '/api/v1/stats/summary', range='30d')
    assert summary['requests'] == 8
    yearly = await get(admin, '/api/v1/stats/traffic', range='1y', metric='requests')
    assert yearly['step_s'] == 86400 and len(yearly['timestamps']) == 365
    assert sum(sum(s['values']) for s in yearly['series']) == 8
