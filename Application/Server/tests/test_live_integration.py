"""Stats, live transfers, activity and the SSE stream (T-S11) against real MongoDB/Valkey."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, TypeAdapter
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import FilesPlugin, add_repo, asgi_client

from waystation_common.db.collections import STATS_MINUTE
from waystation_common.events import CacheState, Transfer, keys
from waystation_common.settings import Settings
from waystation_server.api.live import LiveEvent
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.security.principal import ANONYMOUS
from waystation_server.services import Services
from waystation_server.sse.hub import StatsTicker, Subscriber
from waystation_server.sse.stream import event_stream
from waystation_server.stats import series

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
DATA = b'x' * 300_000
EVENTS: TypeAdapter[Any] = TypeAdapter(LiveEvent)


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD))


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream({'/a.bin': Respond(200, {}, DATA)}) as fake:
        yield fake


@pytest.fixture
async def repo(services: Services, upstream: FakeUpstream) -> str:
    services.formats.set_plugin(FilesPlugin(services))  # type: ignore[arg-type]
    return await add_repo(services, upstream.url)


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


async def traffic(client: httpx.AsyncClient, services: Services) -> None:
    assert (await client.get('/repository/files/a.bin')).content == DATA  # miss
    assert (await client.get('/repository/files/a.bin')).status_code == 200  # hit
    await services.emitter.flush()


async def test_stats_need_permission(client: httpx.AsyncClient) -> None:
    for path in (
        '/api/v1/stats/summary',
        '/api/v1/transfers',
        '/api/v1/activity',
        '/api/v1/events/stream?topics=stats',
    ):
        assert (await client.get(path)).status_code == 401, path


async def test_summary_traffic_storage_and_upstreams(admin: httpx.AsyncClient, services: Services, repo: str) -> None:
    await traffic(admin, services)
    summary = (await admin.get('/api/v1/stats/summary', params={'range': '1h'})).json()
    assert summary['requests'] == 2
    assert summary['hit_ratio'] == 0.5
    assert summary['bytes_served'] == 2 * len(DATA)
    assert summary['bytes_saved'] == len(DATA)
    assert summary['bytes_upstream'] == len(DATA)
    assert summary['storage']['used_bytes'] == len(DATA)
    assert summary['artifacts_cached'] == 1
    assert summary['step_s'] == 60
    assert len(summary['sparks']['requests_per_s']) == 60
    assert summary['requests_per_s'] > 0

    cache = (await admin.get('/api/v1/stats/traffic', params={'range': '1h', 'metric': 'requests'})).json()
    assert {s['key']: sum(s['values']) for s in cache['series']} == {'hit': 1, 'upstream': 1}
    assert len(cache['timestamps']) == 60
    by_format = (await admin.get('/api/v1/stats/traffic', params={'range': '24h', 'split': 'format'})).json()
    assert [s['key'] for s in by_format['series']] == ['raw']
    assert by_format['step_s'] == 600
    one = (await admin.get('/api/v1/repositories/files/stats/traffic', params={'range': '15m'})).json()
    assert sum(one['series'][0]['values']) == len(DATA)

    storage = (await admin.get('/api/v1/stats/storage')).json()
    assert storage['blob_count'] == 1
    assert storage['by_repository'] == [{'repository': 'files', 'format': 'raw', 'bytes': len(DATA), 'artifacts': 1}]
    assert storage['by_format'] == [{'format': 'raw', 'bytes': len(DATA), 'artifacts': 1}]

    upstreams = (await admin.get('/api/v1/stats/upstreams')).json()
    assert upstreams[0]['repository'] == 'files'
    assert upstreams[0]['status'] == 'ok'
    assert upstreams[0]['requests_1h'] == 1
    assert upstreams[0]['error_rate'] == 0

    listed = (await admin.get('/api/v1/repositories')).json()['items'][0]
    assert listed['stats']['requests_24h'] == 2
    assert listed['stats']['size_bytes'] == len(DATA)
    assert listed['stats']['hit_ratio_24h'] == 0.5
    assert sum(listed['stats']['requests_spark_24h']) == 2
    assert listed['stats']['last_activity_at'] is not None


async def test_closed_minutes_merge_without_double_counting(
    admin: httpx.AsyncClient, services: Services, repo: str
) -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    earlier = now - timedelta(minutes=30)
    base = {'m': {'repo_id': repo, 'format': 'raw'}, 'lat_hist': {}}
    zero = dict.fromkeys(keys.COUNTERS, 0)
    await services.db[STATS_MINUTE].insert_one({**base, **zero, 'ts': earlier, 'requests': 5, 'hits': 5})
    # the current minute already rolled up AND still in Valkey: counted once
    minute = int(now.timestamp()) // 60
    await services.valkey.hset(keys.cnt_key(minute, repo), mapping={'requests': 3, 'hits': 3, 'format': 'raw'})
    await services.valkey.sadd(keys.STATS_PENDING, keys.pending_member(minute, repo))
    await services.db[STATS_MINUTE].insert_one({**base, **zero, 'ts': now, 'requests': 3, 'hits': 3})
    summary = (await admin.get('/api/v1/stats/summary', params={'range': '1h'})).json()
    assert summary['requests'] == 8


async def test_degraded_status_after_upstream_errors(
    admin: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    upstream.routes['/bad'] = Respond(500, {}, b'')
    assert (await admin.get('/repository/files/bad')).status_code == 502
    await services.emitter.flush()
    assert (await admin.get('/api/v1/repositories/files')).json()['status'] == 'degraded'
    health = (await admin.get('/api/v1/stats/upstreams')).json()[0]
    assert health['status'] == 'degraded'
    assert health['last_error'] == 'upstream answered 500'
    assert health['error_rate'] == 1.0


async def test_transfers_and_activity(admin: httpx.AsyncClient, services: Services, repo: str) -> None:
    now = datetime.now(UTC)
    services.emitter.transfer_started(
        Transfer(
            id='t1',
            repo_id=repo,
            repo='files',
            format='raw',
            path='big/file.iso',
            cache=CacheState.FETCHING,
            principal='anonymous',
            client_ip='192.0.2.10',
            client_agent='curl/8',
            bytes_total=1000,
            started_at=now,
            updated_at=now,
        )
    )
    await services.emitter.flush()
    listed = (await admin.get('/api/v1/transfers')).json()
    assert [t['artifact'] for t in listed['items']] == ['file.iso']
    assert listed['items'][0]['client'] == {'ip': '192.0.2.10', 'user': 'anonymous', 'agent': 'curl/8'}
    assert (await admin.get('/api/v1/transfers', params={'repo': 'other'})).json()['items'] == []
    services.emitter.transfer_finished('t1', status=200, bytes_done=1000)
    await traffic(admin, services)

    page = (await admin.get('/api/v1/activity', params={'limit': 2})).json()
    assert len(page['items']) == 2
    assert page['next_cursor']
    rest = (await admin.get('/api/v1/activity', params={'cursor': page['next_cursor']})).json()
    ids = [e['id'] for e in page['items'] + rest['items']]
    assert len(ids) == len(set(ids)) == 3  # finished t1, fetched a.bin, downloaded a.bin (starts are not shown)
    assert {e['kind'] for e in page['items'] + rest['items']} == {'download'}
    downloads = (await admin.get('/api/v1/activity', params={'kind': 'error'})).json()
    assert downloads['items'] == []
    assert (await admin.get('/api/v1/activity', params={'cursor': 'nope'})).status_code == 400


async def _collect(gen: AsyncIterator[bytes], frames: list[tuple[str | None, str, dict[str, Any]]]) -> None:
    buffer = ''
    async for chunk in gen:
        buffer += chunk.decode()
        while '\n\n' in buffer:
            block, buffer = buffer.split('\n\n', 1)
            fields = dict(
                line.split(': ', 1) for line in block.split('\n') if ': ' in line and not line.startswith(':')
            )
            if 'event' in fields:
                frames.append((fields.get('id'), fields['event'], json.loads(fields['data'])))


async def _wait(frames: list[Any], predicate: Callable[[], bool], limit_s: float = 5.0) -> None:
    async with asyncio.timeout(limit_s):
        while not predicate():  # noqa: ASYNC110 - polling the collector is simplest here
            await asyncio.sleep(0.05)


async def test_event_stream_snapshot_live_and_resume(admin: httpx.AsyncClient, services: Services, repo: str) -> None:
    now = datetime.now(UTC)
    services.emitter.transfer_started(
        Transfer(
            id='old',
            repo_id=repo,
            repo='files',
            format='raw',
            path='x',
            cache=CacheState.FETCHING,
            started_at=now,
            updated_at=now,
        )
    )
    await services.emitter.flush()
    sub = Subscriber(principal=ANONYMOUS, topics=frozenset({'transfers', 'activity'}))
    frames: list[tuple[str | None, str, dict[str, Any]]] = []
    task = asyncio.create_task(_collect(event_stream(services.hub, sub, last_event_id=None), frames))
    try:
        await _wait(frames, lambda: any(f[1] == 'transfers.snapshot' for f in frames))
        snapshot = next(f[2] for f in frames if f[1] == 'transfers.snapshot')
        assert [t['id'] for t in snapshot['transfers']] == ['old']
        await asyncio.sleep(0.2)  # let the hub's subscriptions settle
        await traffic(admin, services)
        await _wait(frames, lambda: sum(1 for f in frames if f[1] == 'activity') >= 2)
        kinds = [f[1] for f in frames]
        assert 'transfer.started' in kinds
        assert 'transfer.finished' in kinds
        for _, _, data in frames:
            EVENTS.validate_python(data)  # every payload matches the contract's LiveEvent
        activity = [f for f in frames if f[1] == 'activity']
        assert all(f[0] for f in activity)  # durable events carry an id
    finally:
        await _stop(task)
    assert sub not in services.hub.subscribers

    # resume after the first activity event: only the later ones are replayed
    first_id = activity[0][0]
    resumed: list[tuple[str | None, str, dict[str, Any]]] = []
    sub2 = Subscriber(principal=ANONYMOUS, topics=frozenset({'activity'}))
    task = asyncio.create_task(_collect(event_stream(services.hub, sub2, last_event_id=first_id), resumed))
    try:
        await _wait(resumed, lambda: len(resumed) >= 1)
        await asyncio.sleep(0.2)
        assert [f[0] for f in resumed] == [f[0] for f in activity[1:]][: len(resumed)]
        assert first_id not in [f[0] for f in resumed]
    finally:
        await _stop(task)


async def test_event_stream_hides_repositories(services: Services, upstream: FakeUpstream, repo: str) -> None:
    hidden = await add_repo(services, upstream.url, name='internal', repo_type='hosted')
    sub = Subscriber(principal=ANONYMOUS, topics=frozenset({'transfers'}))
    frames: list[tuple[str | None, str, dict[str, Any]]] = []
    task = asyncio.create_task(_collect(event_stream(services.hub, sub, last_event_id=None), frames))
    try:
        await _wait(frames, lambda: bool(frames))
        await asyncio.sleep(0.2)
        now = datetime.now(UTC)
        for rid, name in ((hidden, 'internal'), (repo, 'files')):
            services.emitter.transfer_started(
                Transfer(
                    id=name,
                    repo_id=rid,
                    repo=name,
                    format='raw',
                    path='p',
                    cache=CacheState.FETCHING,
                    started_at=now,
                    updated_at=now,
                )
            )
        await services.emitter.flush()
        await _wait(frames, lambda: any(f[1] == 'transfer.started' for f in frames))
        await asyncio.sleep(0.2)
        started = [f[2]['transfer']['id'] for f in frames if f[1] == 'transfer.started']
        assert started == ['files']
    finally:
        await _stop(task)


async def test_stats_ticker(services: Services, repo: str) -> None:
    clock = [1_000_000.0]
    ticker = StatsTicker(services.valkey, lambda: _storage(), clock=lambda: clock[0])
    minute = int(clock[0] // 60)
    key = keys.cnt_key(minute, repo)
    await services.valkey.sadd(keys.STATS_PENDING, keys.pending_member(minute, repo))
    await services.valkey.hset(key, mapping={'requests': 10, 'hits': 10, 'bytes_out_cache': 1000})
    first = await ticker.tick()
    assert first is not None
    assert first['requests_per_s'] == 0  # the first read is the baseline
    clock[0] += 1
    await services.valkey.hincrby(key, 'requests', 5)
    await services.valkey.hincrby(key, 'misses', 5)
    await services.valkey.hincrby(key, 'bytes_out_upstream', 500)
    second = await ticker.tick()
    assert second is not None
    assert second['requests_per_s'] == 5.0
    assert second['bytes_out_per_s'] == 500.0
    assert second['hit_ratio_1m'] == 0.0
    assert second['storage_used_bytes'] == 42
    EVENTS.validate_python(second)


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)  # the generator's finalizer (unsubscribe) runs on the loop


async def _storage() -> tuple[int, int]:
    return 42, 1


def test_window_alignment() -> None:
    win = series.window('24h', now=86400 * 100 + 1234)
    assert win.step_s == 600
    assert len(win.buckets) == 144
    assert win.end % 600 == 0
