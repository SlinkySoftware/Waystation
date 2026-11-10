"""A cache fill outlives the client that triggered it (T-S07, ARCHITECTURE §6.3 "Fills outlive their client").

The client of a streaming content fill goes away mid-transfer (an aborted ``docker pull``, a client timeout on a large
layer): the fill keeps running in the engine, commits and indexes the blob, its followers (attached before or after
the disconnect, on either replica) are served from it, and the next request is a cache hit. Shutting the engine down
cancels such fills cleanly (``aborted``, upload file removed, nothing indexed).
"""

import asyncio
import contextlib
import hashlib
import random
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import FilesPlugin, add_repo, asgi_client

from waystation_common.db.collections import ASSETS
from waystation_common.settings import Settings
from waystation_server.proxy.coalesce import Coalescer
from waystation_server.proxy.http import Timeouts
from waystation_server.services import Services

pytestmark = pytest.mark.integration

MiB = 1024 * 1024
DATA = random.Random(11).randbytes(6 * MiB)
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
    services.upstream.timeouts = Timeouts(connect=2.0, read=30.0)
    return plugin


@pytest.fixture
async def replica_b(settings: Settings, services: Services) -> AsyncIterator[Services]:
    other = Services.create(settings)
    await other.start()
    try:
        yield other
    finally:
        await other.stop()


@pytest.fixture
async def client_b(replica_b: Services) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(replica_b, client=('127.0.0.2', 50001)) as c:
        yield c


@pytest.fixture
async def repo(services: Services, replica_b: Services, upstream: FakeUpstream) -> str:
    _plugin(services)
    _plugin(replica_b)
    return await add_repo(services, upstream.url)


async def eventually(check: Callable[[], Awaitable[bool]], within: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f'condition not reached within {within} s')
        await asyncio.sleep(0.02)


async def fill(services: Services, repo_id: str) -> dict[str, str]:
    raw: dict[str, str] = await services.valkey.hgetall(Coalescer.key(repo_id, 'big.bin'))
    return raw


def written(services: Services, repo_id: str, at_least: int) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        state = await fill(services, repo_id)
        return state.get('status') == 'streaming' and int(state.get('written') or 0) >= at_least

    return check


def status(services: Services, repo_id: str, value: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return (await fill(services, repo_id)).get('status') == value

    return check


async def disconnect(task: asyncio.Task[httpx.Response]) -> None:
    """The client goes away mid-response (httpx's ASGI transport runs the app in the client's task)."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_a_fill_finishes_after_its_client_disconnects(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    gate = asyncio.Event()
    upstream.routes['/big.bin'] = Respond(200, {}, DATA, chunk=256 * 1024, pause_at=3 * MiB, gate=gate)
    owner = asyncio.create_task(client.get(URL))
    await eventually(written(services, repo, 2 * MiB))
    await disconnect(owner)
    assert (await fill(services, repo))['status'] == 'streaming'  # still owned, still filling
    late = asyncio.create_task(client.get(URL))  # a request after the disconnect follows the running fill

    async def attached() -> bool:
        return int((await fill(services, repo)).get('followers') or 0) == 1

    await eventually(attached)
    gate.set()
    response = await asyncio.wait_for(late, 30)
    assert response.status_code == 200
    assert response.content == DATA
    assert response.headers['x-ws-cache'] == 'COALESCED'
    await eventually(status(services, repo, 'done'))
    assert (await fill(services, repo))['sha256'] == SHA
    assert upstream.hits('/big.bin') == 1
    assert await services.db[ASSETS].count_documents({'path': 'big.bin', 'blob_sha256': SHA}) == 1
    assert list(services.blobs.uploads_dir.iterdir()) == []
    await eventually(lambda: _idle(services))  # the lease was released after the commit, the fill deregistered
    hit = await client.get(URL)
    assert hit.headers['x-ws-cache'] == 'HIT'
    assert hit.headers['x-accel-redirect'].endswith(SHA)


async def test_followers_of_a_disconnected_owner_succeed_from_its_fill(
    client: httpx.AsyncClient,
    client_b: httpx.AsyncClient,
    services: Services,
    replica_b: Services,
    upstream: FakeUpstream,
    repo: str,
) -> None:
    gate = asyncio.Event()
    upstream.routes['/big.bin'] = Respond(200, {}, DATA, chunk=256 * 1024, pause_at=3 * MiB, gate=gate)
    owner = asyncio.create_task(client.get(URL))
    await eventually(written(services, repo, 2 * MiB))
    followers = [asyncio.create_task((client if i % 2 else client_b).get(URL)) for i in range(6)]

    async def attached() -> bool:
        return int((await fill(services, repo)).get('followers') or 0) == 6

    await eventually(attached)
    await disconnect(owner)
    gate.set()
    results = await asyncio.wait_for(asyncio.gather(*followers), 30)
    assert all(r.status_code == 200 and r.content == DATA for r in results)
    assert all(r.headers['x-ws-cache'] == 'COALESCED' for r in results)
    assert upstream.hits('/big.bin') == 1  # nobody had to fetch it again
    await eventually(status(services, repo, 'done'))
    assert await services.db[ASSETS].count_documents({'path': 'big.bin', 'blob_sha256': SHA}) == 1
    assert list(services.blobs.uploads_dir.iterdir()) == []
    await eventually(lambda: _idle(services))
    assert (await client_b.get(URL)).headers['x-ws-cache'] == 'HIT'


async def test_shutdown_cancels_a_fill_whose_client_left(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    upstream.routes['/big.bin'] = Respond(200, {}, DATA, chunk=256 * 1024, hang_after=3 * MiB)
    owner = asyncio.create_task(client.get(URL))
    await eventually(written(services, repo, 2 * MiB))
    await disconnect(owner)
    assert (await fill(services, repo))['status'] == 'streaming'
    await asyncio.wait_for(services.proxy.aclose(), 10)
    state = await fill(services, repo)
    assert state['status'] == 'aborted'  # followers elsewhere would take it over
    assert await services.db[ASSETS].count_documents({}) == 0
    assert list(services.blobs.uploads_dir.iterdir()) == []
    assert await _idle(services)


async def _idle(services: Services) -> bool:
    return not services.proxy.fills.running and not services.proxy.coalescer._leases
