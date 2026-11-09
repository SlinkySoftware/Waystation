"""Auto-block, per-repository CA bundle and outbound HTTP proxy (T-S07) against real MongoDB/Valkey, a scriptable
local upstream (plain and TLS) and a local forward proxy: fault injection for block, backoff, probe and resume."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_proxy import FakeProxy
from ws_fake_upstream import FakeUpstream, Respond, Seen
from ws_testing import FilesPlugin, add_repo, asgi_client
from ws_tls import make_ca

from waystation_common.db.collections import REPOSITORIES
from waystation_common.events import keys
from waystation_common.settings import Settings
from waystation_server.api.live import upstream_health_source
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.proxy.engine import AssetKind, AssetSpec
from waystation_server.proxy.http import Timeouts
from waystation_server.security.principal import ANONYMOUS
from waystation_server.services import Services
from waystation_server.sse.hub import Subscriber
from waystation_server.sse.stream import event_stream

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
BLOCK = {'enabled': True, 'failures': 3, 'initial_s': 30, 'max_s': 100}


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.1/32'], bootstrap_admin_password=SecretStr(PASSWORD))


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        yield fake


@pytest.fixture
async def clock(services: Services) -> Clock:
    await services.proxy.breaker.stop()  # the tests drive the prober themselves
    clock = Clock()
    services.proxy.breaker.clock = clock
    return clock


@pytest.fixture
def plugin(services: Services) -> FilesPlugin:
    plugin = FilesPlugin(services)
    services.formats.set_plugin(plugin)  # type: ignore[arg-type]
    services.upstream.timeouts = Timeouts(connect=2.0, read=1.0)
    return plugin


@pytest.fixture
async def repo(services: Services, upstream: FakeUpstream, plugin: FilesPlugin, clock: Clock) -> str:
    return await add_repo(services, upstream.url, auto_block=BLOCK, stale_on_error=False)


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


async def health(services: Services, repo_id: str) -> dict[str, str]:
    await services.emitter.flush()
    return await services.valkey.hgetall(keys.upstream_health_key(repo_id))


async def upstream_events(services: Services) -> list[dict[str, Any]]:
    await services.emitter.flush()
    events = [json.loads(f['data']) for _, f in await services.valkey.xrange(keys.ACTIVITY)]
    return [e for e in events if e['type'] == 'upstream.state']


def failing(status: int = 503) -> Callable[[Seen], Respond]:
    return lambda seen: Respond(status, {}, b'down')


async def test_blocks_after_consecutive_failures_then_backs_off_probes_and_resumes(
    client: httpx.AsyncClient,
    admin: httpx.AsyncClient,
    services: Services,
    upstream: FakeUpstream,
    repo: str,
    clock: Clock,
) -> None:
    upstream.routes['/a'] = failing()
    for _ in range(3):
        assert (await client.get('/repository/files/a')).status_code == 502
    assert upstream.hits('/a') == 3

    # blocked: answered without contacting the upstream, with Retry-After
    blocked = await client.get('/repository/files/a')
    assert blocked.status_code == 503
    assert blocked.headers['retry-after'] == '30'
    assert (await client.get('/repository/files/b')).status_code == 503
    assert len(upstream.seen) == 3
    state = await health(services, repo)
    assert state['block_failures'] == '3'
    assert state['block_level'] == '0'
    assert int(state['blocked_until']) == int((clock.now + 30) * 1000)
    assert state['block_reason'] == 'upstream answered 503'

    # visible in upstream health, repository status, events and metrics
    [row] = (await admin.get('/api/v1/stats/upstreams')).json()
    assert row['status'] == 'blocked'
    assert row['blocked_until'] is not None
    assert (await admin.get('/api/v1/repositories/files')).json()['status'] == 'blocked'
    [event] = await upstream_events(services)
    assert event['detail']['event'] == 'blocked'
    assert 'auto-blocked for 30 s after 3 consecutive failures' in event['detail']['summary']
    activity = (await admin.get('/api/v1/activity', params={'kind': 'upstream_state'})).json()['items']
    assert activity[0]['outcome'] == 'failure' and 'auto-blocked' in activity[0]['summary']
    metrics = (await admin.get('/metrics')).text
    assert 'waystation_upstream_autoblock_total{repo="files",event="blocked"} 1.0' in metrics
    assert 'waystation_upstream_state{repo="files"} 2.0' in metrics

    # the period ends: the next request probes; it fails, so the block doubles (level 1 = 60 s)
    clock.now += 31
    assert (await client.get('/repository/files/a')).status_code == 502
    assert upstream.hits('/a') == 4
    state = await health(services, repo)
    assert state['block_level'] == '1'
    assert int(state['blocked_until']) == int((clock.now + 60) * 1000)
    assert (await client.get('/repository/files/a')).status_code == 503
    clock.now += 61
    assert (await client.get('/repository/files/a')).status_code == 502  # probe 2 fails: 100 s (max_s caps 120)
    state = await health(services, repo)
    assert state['block_level'] == '2'
    assert int(state['blocked_until']) == int((clock.now + 100) * 1000)
    assert [e['detail']['event'] for e in await upstream_events(services)] == ['blocked', 'reblocked', 'reblocked']

    # the upstream recovers: the next probe succeeds and lifts the block
    upstream.routes['/a'] = Respond(200, {}, b'back')
    clock.now += 101
    response = await client.get('/repository/files/a')
    assert response.status_code == 200 and response.content == b'back'
    state = await health(services, repo)
    assert 'blocked_until' not in state and state['block_failures'] == '0'
    assert [e['detail']['event'] for e in await upstream_events(services)][-1] == 'resumed'
    [row] = (await admin.get('/api/v1/stats/upstreams')).json()
    assert row['status'] != 'blocked' and row['blocked_until'] is None
    upstream.routes['/b'] = Respond(200, {}, b'b')
    assert (await client.get('/repository/files/b')).status_code == 200


async def test_what_counts_as_a_failure(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    upstream.routes['/slow'] = Respond(200, {}, b'x', delay=3.0)  # read timeout 1 s
    upstream.routes['/limited'] = Respond(429, {}, b'slow down')
    upstream.routes['/reset'] = Respond(200, {}, b'y' * 600_000, chunk=100_000, reset_after=200_000)
    for path in ('missing', 'missing', 'missing', 'missing'):  # 404s never block
        assert (await client.get(f'/repository/files/{path}')).status_code == 404
    assert (await health(services, repo)).get('block_failures', '0') == '0'
    assert (await client.get('/repository/files/slow')).status_code == 504
    assert (await client.get('/repository/files/limited')).status_code == 502
    state = await health(services, repo)
    assert state['block_failures'] == '2' and 'blocked_until' not in state
    upstream.routes['/ok'] = Respond(200, {}, b'ok')
    assert (await client.get('/repository/files/ok')).status_code == 200  # a success resets the count
    assert (await health(services, repo))['block_failures'] == '0'
    for _ in range(2):
        assert (await client.get('/repository/files/limited')).status_code == 502
    with pytest.raises(Exception):  # noqa: B017 - the tee aborts the client's transfer (ResponseAborted)
        await client.get('/repository/files/reset')  # a reset mid-body is the third failure in a row
    assert 'blocked_until' in await health(services, repo)


async def test_unreachable_upstream_blocks(services: Services, plugin: FilesPlugin, clock: Clock) -> None:
    async with FakeUpstream() as gone:
        url = gone.url
    await add_repo(services, url, name='gone', auto_block=BLOCK)
    async with asgi_client(services) as client:
        for _ in range(3):
            assert (await client.get('/repository/gone/x')).status_code == 502
        assert (await client.get('/repository/gone/x')).status_code == 503
    assert 'connect' in (await health(services, 'repo-gone'))['block_reason'].lower()


async def test_stale_copy_served_while_blocked(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin, clock: Clock
) -> None:
    await add_repo(services, upstream.url, auto_block=BLOCK)  # stale_on_error on (default)
    plugin.make_spec = lambda path: AssetSpec(path=path, kind=AssetKind.METADATA, max_age_s=0)
    upstream.routes['/index'] = Respond(200, {}, b'index v1')
    assert (await client.get('/repository/files/index')).status_code == 200
    upstream.routes['/index'] = failing(500)
    for _ in range(3):
        assert (await client.get('/repository/files/index')).headers['x-ws-cache'] == 'STALE'
    hits = upstream.hits('/index')
    stale = await client.get('/repository/files/index')
    assert stale.status_code == 200 and stale.headers['x-ws-cache'] == 'STALE'
    assert upstream.hits('/index') == hits  # blocked: served from the cache without asking
    assert (await client.get('/repository/files/never-cached')).status_code == 503


async def test_one_probe_at_a_time_across_requests(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str, clock: Clock
) -> None:
    upstream.routes['/a'] = failing()
    for _ in range(3):
        await client.get('/repository/files/a')
    clock.now += 31
    upstream.routes['/a'] = Respond(200, {}, b'ok', delay=0.5)
    upstream.routes['/b'] = Respond(200, {}, b'b')
    probe = asyncio.create_task(client.get('/repository/files/a'))
    await asyncio.sleep(0.2)
    assert (await client.get('/repository/files/b')).status_code == 503  # someone else is probing
    assert (await probe).status_code == 200
    assert (await client.get('/repository/files/b')).status_code == 200
    assert upstream.hits('/b') == 1


async def test_prober_resumes_without_traffic(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str, clock: Clock
) -> None:
    upstream.routes['/a'] = failing()
    for _ in range(3):
        await client.get('/repository/files/a')
    catalog = await services.catalog.all()
    assert await services.proxy.breaker.probe_due(catalog, services.probe_upstream) == 0  # not due yet
    clock.now += 31
    upstream.routes['/'] = failing(502)  # the raw format probes the upstream root
    assert await services.proxy.breaker.probe_due(catalog, services.probe_upstream) == 1
    assert (await health(services, repo))['block_level'] == '1'
    clock.now += 61
    upstream.routes['/'] = Respond(404, {}, b'')  # any answer but 5xx/429 means the upstream is back
    assert await services.proxy.breaker.probe_due(catalog, services.probe_upstream) == 1
    assert 'blocked_until' not in await health(services, repo)
    assert await services.proxy.breaker.probe_due(catalog, services.probe_upstream) == 0


async def test_disabled_never_blocks_and_saving_resets(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    upstream.routes['/a'] = failing()
    for _ in range(3):
        await client.get('/repository/files/a')
    assert (await client.get('/repository/files/a')).status_code == 503
    body = (await admin.get('/api/v1/repositories/files')).json()
    write = {k: body[k] for k in ('name', 'format', 'type', 'online', 'description', 'proxy', 'format_options')}
    write['proxy']['auto_block']['enabled'] = False
    saved = await admin.put('/api/v1/repositories/files', json=write)
    assert saved.status_code == 200, saved.text
    assert saved.json()['proxy']['auto_block'] == {**BLOCK, 'enabled': False}
    assert 'blocked_until' not in await health(services, repo)
    services.catalog.invalidate()
    for _ in range(6):
        assert (await client.get('/repository/files/a')).status_code == 502
    assert 'blocked_until' not in await health(services, repo)


async def test_upstream_state_event_on_the_stream(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str, clock: Clock
) -> None:
    services.hub.upstream_health = upstream_health_source(services)
    sub = Subscriber(principal=ANONYMOUS, topics=frozenset({'stats'}))
    frames: list[tuple[str, dict[str, Any]]] = []

    async def collect() -> None:
        buffer = ''
        async for chunk in event_stream(services.hub, sub, last_event_id=None):
            buffer += chunk.decode()
            while '\n\n' in buffer:
                block, buffer = buffer.split('\n\n', 1)
                fields = dict(line.split(': ', 1) for line in block.split('\n') if ': ' in line)
                if fields.get('event') == 'upstream.state':
                    frames.append((fields['event'], json.loads(fields['data'])))

    task = asyncio.create_task(collect())
    try:
        await asyncio.sleep(0.3)
        upstream.routes['/a'] = failing()
        for _ in range(3):
            await client.get('/repository/files/a')
        await services.emitter.flush()
        async with asyncio.timeout(10):
            while not frames:  # noqa: ASYNC110 - polling the collector
                await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    _, data = frames[0]
    assert data['health']['repository'] == 'files'
    assert data['health']['status'] == 'blocked'
    assert data['health']['blocked_until']


# ---------------------------------------------------------------------------- CA bundle and outbound proxy
async def test_custom_ca_bundle_only_for_its_repository(
    client: httpx.AsyncClient, services: Services, plugin: FilesPlugin, tmp_path: Path, clock: Clock
) -> None:
    tls = make_ca(tmp_path)
    async with FakeUpstream({'/f': Respond(200, {}, b'secure')}, tls=tls.server_context()) as secure:
        await add_repo(services, secure.url, name='trusted', ca_pem=tls.ca_pem)
        await add_repo(services, secure.url, name='untrusted')
        ok = await client.get('/repository/trusted/f')
        assert ok.status_code == 200 and ok.content == b'secure'
        assert (await client.get('/repository/untrusted/f')).status_code == 502  # same host, no CA: refused
        assert (await client.get('/repository/trusted/f')).headers['x-ws-cache'] == 'HIT'


async def test_test_upstream_checks_the_network_options(
    admin: httpx.AsyncClient, services: Services, tmp_path: Path
) -> None:
    tls = make_ca(tmp_path)
    async with FakeUpstream({'/': Respond(200, {}, b'')}, tls=tls.server_context()) as secure:
        body: dict[str, Any] = {'format': 'raw', 'proxy': {'upstream_url': secure.url}}
        without = (await admin.post('/api/v1/repositories/test-upstream', json=body)).json()
        assert not without['reachable'] and without['checks'][-1]['name'] == 'tls'
        body['proxy']['ca_pem'] = tls.ca_pem
        with_ca = (await admin.post('/api/v1/repositories/test-upstream', json=body)).json()
        assert with_ca['reachable'] and with_ca['http_status'] == 200
        body['proxy'].update(ca_pem='nope', http_proxy='http://user:pw@proxy.example.com:3128')
        refused = await admin.post('/api/v1/repositories/test-upstream', json=body)
        assert refused.status_code == 422
        assert {e['field'] for e in refused.json()['errors']} == {'proxy.ca_pem', 'proxy.http_proxy'}


async def test_repository_api_validates_and_returns_network_options(
    admin: httpx.AsyncClient, services: Services, tmp_path: Path
) -> None:
    pem = make_ca(tmp_path).ca_pem
    body: dict[str, Any] = {
        'name': 'net',
        'format': 'raw',
        'type': 'proxy',
        'online': True,
        'proxy': {
            'upstream_url': 'https://files.example.com/',
            'ca_pem': 'garbage',
            'http_proxy': 'socks5://proxy.example.com:1080',
            'auto_block': {'initial_s': 60, 'max_s': 30},
        },
    }
    bad = await admin.post('/api/v1/repositories', json=body)
    assert bad.status_code == 422
    assert {e['field'] for e in bad.json()['errors']} == {
        'proxy.ca_pem',
        'proxy.http_proxy',
        'proxy.auto_block.max_s',
    }
    body['proxy'].update(ca_pem=pem, http_proxy=' http://proxy.example.com:3128 ', auto_block={'failures': 7})
    created = await admin.post('/api/v1/repositories', json=body)
    assert created.status_code == 201, created.text
    proxy = created.json()['proxy']
    assert proxy['ca_pem'] == pem.strip()
    assert proxy['http_proxy'] == 'http://proxy.example.com:3128'
    assert proxy['auto_block'] == {'enabled': True, 'failures': 7, 'initial_s': 30, 'max_s': 1800}
    stored = await services.db[REPOSITORIES].find_one({'name': 'net'})
    assert stored is not None and stored['proxy']['auto_block']['failures'] == 7


async def test_outbound_proxy_per_repository(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin, clock: Clock
) -> None:
    upstream.routes['/f'] = Respond(200, {}, b'via proxy')
    async with FakeProxy() as proxy:
        await add_repo(services, upstream.url, name='proxied', http_proxy=proxy.url)
        response = await client.get('/repository/proxied/f')
        assert response.status_code == 200 and response.content == b'via proxy'
        assert proxy.seen == [f'GET {upstream.url}f']


async def test_outbound_proxy_https_tunnel_with_custom_ca(
    client: httpx.AsyncClient, services: Services, plugin: FilesPlugin, tmp_path: Path, clock: Clock
) -> None:
    tls = make_ca(tmp_path)
    async with (
        FakeUpstream({'/f': Respond(200, {}, b'tunnelled')}, tls=tls.server_context()) as secure,
        FakeProxy() as proxy,
    ):
        await add_repo(services, secure.url, name='tunnel', http_proxy=proxy.url, ca_pem=tls.ca_pem)
        response = await client.get('/repository/tunnel/f')
        assert response.status_code == 200 and response.content == b'tunnelled'
        assert proxy.seen == [f'CONNECT 127.0.0.1:{secure.port}']


async def test_ssrf_guard_applies_through_the_proxy(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin, clock: Clock
) -> None:
    async with FakeProxy() as proxy, FakeUpstream({'/f': Respond(200, {}, b'inside')}, host='127.0.0.2') as inner:
        # 127.0.0.2 is loopback outside the allowed 127.0.0.1/32: refused before anything reaches the proxy
        private = inner.url
        await add_repo(services, private, name='private', http_proxy=proxy.url, auto_block=BLOCK)
        assert (await client.get('/repository/private/f')).status_code == 502
        assert proxy.seen == []
        # a redirect to a private address through the proxy is refused too (each hop is checked)
        upstream.routes['/hop'] = Respond(302, {'Location': private + 'f'}, b'')
        await add_repo(services, upstream.url, name='hops', http_proxy=proxy.url)
        assert (await client.get('/repository/hops/hop')).status_code == 502
        assert proxy.seen == [f'GET {upstream.url}hop']
        # SSRF refusals are configuration problems, not an upstream outage: they never auto-block
        for _ in range(4):
            assert (await client.get('/repository/private/f')).status_code == 502
        assert 'blocked_until' not in await health(services, 'repo-private')
        # allow_private_upstream lets the proxy reach it
        await add_repo(services, private, name='allowed', http_proxy=proxy.url, allow_private_upstream=True)
        assert (await client.get('/repository/allowed/f')).content == b'inside'
        assert proxy.seen[-1] == f'GET {private}f'
        assert inner.hits('/f') == 1
