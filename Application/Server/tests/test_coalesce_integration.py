"""Request coalescing (T-S08, ARCHITECTURE §6.3) with two app instances sharing MongoDB, Valkey and the blob dir.

Each test runs two ``Services`` ("replicas" A and B, the same process) behind ASGI clients and a scriptable upstream
that can pause mid-body until the test lets it go, so every follower is attached before the owner finishes.
The cross-process acceptance test (real servers, 200 clients, owner killed) is ``test_coalesce_replicas``.
"""

import asyncio
import hashlib
import json
import os
import random
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from ws_fake_upstream import FakeUpstream, Respond, Seen
from ws_testing import FilesPlugin, add_repo, asgi_client

from waystation_common.db.collections import ASSETS
from waystation_common.events import keys
from waystation_common.settings import Settings
from waystation_server.problems import ResponseAborted
from waystation_server.proxy.coalesce import Coalescer, Follower, Lease
from waystation_server.proxy.engine import AssetKind, AssetSpec, Expected
from waystation_server.proxy.http import Timeouts
from waystation_server.services import Services

pytestmark = pytest.mark.integration

MiB = 1024 * 1024
DATA = random.Random(8).randbytes(6 * MiB)
SHA = hashlib.sha256(DATA).hexdigest()
URL = '/repository/files/big.bin'


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'], coalesce_lease_s=2)


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        yield fake


def _plugin(services: Services) -> FilesPlugin:
    plugin = FilesPlugin(services)
    services.formats.set_plugin(plugin)  # type: ignore[arg-type]
    services.upstream.timeouts = Timeouts(connect=2.0, read=5.0)
    return plugin


@pytest.fixture
def plugin(services: Services) -> FilesPlugin:
    return _plugin(services)


@pytest.fixture
async def replica_b(settings: Settings, services: Services) -> AsyncIterator[Services]:
    """A second app instance on the same MongoDB, Valkey and data dir."""
    other = Services.create(settings)
    await other.start()
    try:
        yield other
    finally:
        await other.stop()


@pytest.fixture
def plugin_b(replica_b: Services) -> FilesPlugin:
    return _plugin(replica_b)


@pytest.fixture
async def client_b(replica_b: Services) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(replica_b, client=('127.0.0.2', 50001)) as c:
        yield c


@pytest.fixture
async def repo(services: Services, upstream: FakeUpstream, plugin: FilesPlugin, plugin_b: FilesPlugin) -> str:
    return await add_repo(services, upstream.url)


async def eventually(
    check: Callable[[], Awaitable[bool]], within: float = 10.0, what: Callable[[], str] = lambda: ''
) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f'condition not reached within {within} s {what()}'.rstrip())
        await asyncio.sleep(0.02)


async def fill(services: Services, repo_id: str, path: str) -> dict[str, str]:
    raw: dict[str, str] = await services.valkey.hgetall(Coalescer.key(repo_id, path))
    return raw


async def counters(services: Services, *others: Services) -> dict[str, int]:
    for s in (services, *others):
        await s.emitter.flush()
    total: dict[str, int] = {}
    for member in await services.valkey.smembers(keys.STATS_PENDING):
        minute, rid = keys.parse_pending(member)
        for k, v in (await services.valkey.hgetall(keys.cnt_key(minute, rid))).items():
            if k != 'format':
                total[k] = total.get(k, 0) + int(v)
    return total


async def live(services: Services) -> list[dict[str, Any]]:
    return [json.loads(v) for v in (await services.valkey.hgetall(keys.TRANSFERS)).values()]


async def test_followers_on_two_replicas_tail_one_fetch(
    client: httpx.AsyncClient,
    client_b: httpx.AsyncClient,
    services: Services,
    replica_b: Services,
    upstream: FakeUpstream,
    repo: str,
) -> None:
    gate = asyncio.Event()
    upstream.routes['/big.bin'] = Respond(200, {'Content-Type': 'application/x-big'}, DATA, chunk=256 * 1024,
                                          pause_at=3 * MiB, gate=gate)  # fmt: skip
    owner = asyncio.create_task(client.get(URL))
    await eventually(lambda: _status(services, repo, 'streaming'))
    followers = [asyncio.create_task((client if i % 2 else client_b).get(URL)) for i in range(20)]

    async def all_attached() -> bool:
        return int((await fill(services, repo, 'big.bin')).get('followers', 0)) == 20

    await eventually(all_attached)

    async def owner_shows_followers() -> bool:
        await services.emitter.flush()
        return any(t['cache'] == 'FETCHING' and t['followers'] == 20 for t in await live(services))

    await eventually(owner_shows_followers)
    gate.set()
    responses = await asyncio.gather(owner, *followers)
    assert upstream.hits('/big.bin') == 1
    assert all(r.status_code == 200 and r.content == DATA for r in responses)
    assert [r.headers['x-ws-cache'] for r in responses].count('FETCHING') == 1
    assert [r.headers['x-ws-cache'] for r in responses].count('COALESCED') == 20
    assert all(r.headers['content-type'] == 'application/x-big' for r in responses)
    assert all(r.headers['content-length'] == str(len(DATA)) for r in responses)
    assert await services.db[ASSETS].count_documents({'path': 'big.bin', 'blob_sha256': SHA}) == 1
    stats = await counters(services, replica_b)
    assert stats['misses'] == 1
    assert stats['coalesced'] == 20
    assert stats['upstream_requests'] == 1
    assert (await fill(services, repo, 'big.bin'))['status'] == 'done'
    assert list(services.blobs.uploads_dir.iterdir()) == []
    hit = await client_b.get(URL)
    assert hit.headers['x-ws-cache'] == 'HIT'


async def _status(services: Services, repo: str, status: str, path: str = 'big.bin') -> bool:
    return (await fill(services, repo, path)).get('status') == status


async def test_mismatch_fails_every_follower_and_caches_nothing(
    client: httpx.AsyncClient,
    client_b: httpx.AsyncClient,
    services: Services,
    upstream: FakeUpstream,
    plugin: FilesPlugin,
    plugin_b: FilesPlugin,
    repo: str,
) -> None:
    gate = asyncio.Event()
    upstream.routes['/big.bin'] = Respond(200, {}, DATA, chunk=256 * 1024, pause_at=3 * MiB, gate=gate)
    plugin.make_spec = plugin_b.make_spec = lambda path: AssetSpec(path=path, expected=Expected(sha256='f' * 64))
    owner = asyncio.create_task(client.get(URL))
    await eventually(lambda: _status(services, repo, 'streaming'))
    followers = [asyncio.create_task((client if i % 2 else client_b).get(URL)) for i in range(10)]

    async def attached() -> bool:
        return int((await fill(services, repo, 'big.bin')).get('followers', 0)) == 10

    await eventually(attached)
    gate.set()
    results = await asyncio.gather(owner, *followers, return_exceptions=True)
    assert all(isinstance(r, ResponseAborted) for r in results), results
    assert upstream.hits('/big.bin') == 1
    assert await services.db[ASSETS].count_documents({}) == 0
    state = await fill(services, repo, 'big.bin')
    assert state['status'] == 'failed'
    assert state['error_code'] == 'mismatch'
    assert list(services.blobs.uploads_dir.iterdir()) == []


async def test_owner_gone_mid_transfer_a_follower_takes_over(
    client: httpx.AsyncClient,
    client_b: httpx.AsyncClient,
    services: Services,
    replica_b: Services,
    upstream: FakeUpstream,
    repo: str,
) -> None:
    def route(seen: Seen) -> Respond:
        if upstream.hits('/big.bin') == 1:  # the first fetch stalls after 3 MiB
            return Respond(200, {}, DATA, chunk=256 * 1024, hang_after=3 * MiB)
        return Respond(200, {}, DATA, chunk=256 * 1024)

    upstream.routes['/big.bin'] = route
    # The first fetch must end by its owner going away, not by the 5 s read timeout (on a loaded machine attaching
    # 15 followers can take longer, and a timed-out fetch rightly fails its followers instead of handing over).
    for s in (services, replica_b):
        s.upstream.timeouts = Timeouts(connect=2.0, read=60.0)
    owner = asyncio.create_task(client.get(URL))
    await eventually(lambda: _status(services, repo, 'streaming'))
    followers = [asyncio.create_task((client if i % 3 else client_b).get(URL)) for i in range(15)]

    sent: list[int] = []

    async def all_sending() -> bool:
        for s in (services, replica_b):
            await s.emitter.flush()
        sent[:] = [t['bytes_done'] for t in await live(services) if t['cache'] == 'COALESCED']
        return len(sent) == 15 and all(n >= 2 * MiB for n in sent)

    await eventually(all_sending, within=30, what=lambda: f'(bytes sent per follower: {sorted(sent)})')
    # The owner goes away (as at shutdown: its client leaving alone no longer stops a fill, test_fill_continue): its
    # fill is aborted, a follower takes it over.
    assert len(services.proxy.fills.cancel()) == 1
    with pytest.raises(ResponseAborted):
        await asyncio.wait_for(owner, 10)
    results = await asyncio.wait_for(asyncio.gather(*followers), 60)
    assert upstream.hits('/big.bin') == 2
    assert all(r.status_code == 200 and r.content == DATA for r in results)
    assert await services.db[ASSETS].count_documents({'path': 'big.bin', 'blob_sha256': SHA}) == 1
    assert list(services.blobs.uploads_dir.iterdir()) == []
    stats = await counters(services, replica_b)
    assert stats['misses'] == 1  # the follower that fetched it again
    assert stats['coalesced'] == 14


async def test_metadata_fetch_is_coalesced(
    client: httpx.AsyncClient,
    client_b: httpx.AsyncClient,
    services: Services,
    upstream: FakeUpstream,
    plugin: FilesPlugin,
    plugin_b: FilesPlugin,
    repo: str,
) -> None:
    upstream.routes['/index.json'] = Respond(200, {'ETag': '"v1"'}, b'{"a": 1}', delay=0.5)
    plugin.make_spec = plugin_b.make_spec = lambda path: AssetSpec(path=path, kind=AssetKind.METADATA)
    responses = await asyncio.gather(
        *((client if i % 2 else client_b).get('/repository/files/index.json') for i in range(10))
    )
    assert upstream.hits('/index.json') == 1
    states = sorted(r.headers['x-ws-cache'] for r in responses)
    assert states == ['COALESCED'] * 9 + ['FETCHING']
    assert len({r.headers['x-accel-redirect'] for r in responses}) == 1


async def test_not_found_is_shared(
    client: httpx.AsyncClient, client_b: httpx.AsyncClient, upstream: FakeUpstream, repo: str
) -> None:
    upstream.routes['/gone'] = Respond(404, {}, b'no', delay=0.5)
    responses = await asyncio.gather(*((client if i % 2 else client_b).get('/repository/files/gone') for i in range(8)))
    assert [r.status_code for r in responses] == [404] * 8
    assert upstream.hits('/gone') == 1


async def test_wait_mode_followers_are_served_from_the_cache(
    settings_factory: Callable[..., Settings], upstream: FakeUpstream, mongo_db: Any
) -> None:
    from waystation_common.db.migrations import apply_migrations
    from waystation_common.db.schema import ensure_schema

    await ensure_schema(mongo_db)
    await apply_migrations(mongo_db)
    settings = settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'], coalesce_tail='wait')
    services = Services.create(settings)
    await services.start()
    try:
        _plugin(services)
        await add_repo(services, upstream.url)
        upstream.routes['/big.bin'] = Respond(200, {}, DATA, chunk=512 * 1024, chunk_delay=0.02)
        async with asgi_client(services) as c:
            responses = await asyncio.gather(*(c.get(URL) for _ in range(6)))
        assert upstream.hits('/big.bin') == 1
        owner = [r for r in responses if r.headers['x-ws-cache'] == 'FETCHING']
        assert len(owner) == 1
        assert owner[0].content == DATA
        followers = [r for r in responses if r.headers['x-ws-cache'] == 'COALESCED']
        assert len(followers) == 5
        assert all(r.headers['x-accel-redirect'].endswith(SHA) and r.content == b'' for r in followers)
    finally:
        await services.stop()


async def test_valkey_unavailable_means_no_coalescing_not_errors(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    services.proxy.coalescer = Coalescer(
        Redis.from_url('redis://127.0.0.1:1/0', socket_connect_timeout=0.2), data_dir=services.settings.data_dir
    )
    upstream.routes['/big.bin'] = Respond(200, {}, DATA)
    response = await client.get(URL)
    assert response.status_code == 200
    assert response.content == DATA
    assert response.headers['x-ws-cache'] == 'FETCHING'


async def test_a_slow_fdatasync_does_not_cost_the_owner_its_lease(
    valkey: Redis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (T-S08): on a busy disk one ``fdatasync`` took longer than the lease, the renewal waited behind it,
    the lease expired and a follower started a second upstream fetch. The renewal must not wait on the sync."""
    started, release = threading.Event(), threading.Event()
    real_sync = os.fdatasync

    def slow_sync(fd: int) -> None:
        started.set()
        release.wait(10)
        real_sync(fd)

    monkeypatch.setattr(os, 'fdatasync', slow_sync)
    coalescer = Coalescer(valkey, data_dir=tmp_path, lease_s=1)
    key = Coalescer.key('repo', 'big.bin')
    lease = await coalescer.claim('repo', 'big.bin')
    assert isinstance(lease, Lease)
    follower = await coalescer.claim('repo', 'big.bin')  # attached: the owner syncs before publishing ``synced``
    assert isinstance(follower, Follower)
    upload = tmp_path / 'upload'
    fd = os.open(upload, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.write(fd, b'x' * 1000)
        lease.streaming(upload='upload', total=None, content_type='application/octet-stream', headers={}, fd=fd)
        lease.advance(1000)
        assert await asyncio.to_thread(started.wait, 5), 'the owner never synced the upload file'
        await asyncio.sleep(2.5)  # the sync is still stuck, well past the 1 s lease
        state = await valkey.hgetall(key)
        assert state, 'the lease expired while the owner was alive and waiting on a slow fdatasync'
        assert (state['owner'], state['status'], state['written']) == (lease.token, 'streaming', '1000')
        assert state['synced'] == '0' and not lease.lost
        release.set()

        async def synced() -> bool:
            return (await valkey.hget(key, 'synced')) == '1000'

        await eventually(synced)
        await follower.leave()
        await lease.finish(sha256='0' * 64, size=1000)
        assert (await valkey.hgetall(key))['status'] == 'done'
    finally:
        release.set()
        os.close(fd)
        await coalescer.aclose()


async def test_the_upload_descriptor_outlives_a_sync_still_running_at_finish(
    valkey: Redis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fill may end while an ``fdatasync`` is still running: the outcome is published at once, and the sync's
    descriptor is closed only after the sync returns (never under a thread that is using it)."""
    started, release = threading.Event(), threading.Event()
    real_sync = os.fdatasync

    def slow_sync(fd: int) -> None:
        started.set()
        release.wait(10)
        real_sync(fd)

    monkeypatch.setattr(os, 'fdatasync', slow_sync)
    coalescer = Coalescer(valkey, data_dir=tmp_path, lease_s=1)
    key = Coalescer.key('repo', 'big.bin')
    lease = await coalescer.claim('repo', 'big.bin')
    assert isinstance(lease, Lease)
    follower = await coalescer.claim('repo', 'big.bin')
    assert isinstance(follower, Follower)
    upload = tmp_path / 'upload'
    fd = os.open(upload, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.write(fd, b'x' * 1000)
        lease.streaming(upload='upload', total=None, content_type='application/octet-stream', headers={}, fd=fd)
        sync_fd = lease._sync_fd
        assert sync_fd is not None
        lease.advance(1000)
        assert await asyncio.to_thread(started.wait, 5), 'the owner never synced the upload file'
        await asyncio.wait_for(lease.finish(sha256='0' * 64, size=1000), 5)  # not held up by the stuck sync
        assert (await valkey.hgetall(key))['status'] == 'done'
        await asyncio.sleep(0.2)
        os.fstat(sync_fd)  # still open: the sync thread is using it
        release.set()

        async def closed() -> bool:
            try:
                os.fstat(sync_fd)
            except OSError:
                return True
            return False

        await eventually(closed)
        await follower.leave()
    finally:
        release.set()
        os.close(fd)
        await coalescer.aclose()
