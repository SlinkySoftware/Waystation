"""T-S08 acceptance: request coalescing across two real server replicas (separate processes, uvicorn) that share
MongoDB, Valkey and the blob dir, in front of a slow local upstream.

- 200 concurrent requests for one uncached large blob, spread over both replicas → exactly one upstream fetch and
  identical bytes for every client (size: ``WAYSTATION_TEST_COALESCE_MIB``, default 64 MiB, scaled down from the
  1 GiB of the task's acceptance for CI);
- the owner replica is killed (SIGKILL) mid-transfer → after its lease expires a follower on the other replica takes
  the fetch over and every follower still gets the complete, identical file.
"""

import asyncio
import contextlib
import hashlib
import os
import random
import signal
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from ws_fake_upstream import FakeUpstream, Respond, Seen
from ws_testing import TEST_KEY, add_repo

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS
from waystation_common.db.migrations import apply_migrations
from waystation_common.db.schema import ensure_schema
from waystation_server.proxy.coalesce import Coalescer
from waystation_server.services import Services

pytestmark = pytest.mark.integration

MiB = 1024 * 1024
SIZE = int(os.environ.get('WAYSTATION_TEST_COALESCE_MIB', '64')) * MiB
CLIENTS = 200
PATH = 'big.bin'
URL = f'/repository/files/{PATH}'


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope='module')
def data() -> bytes:
    return random.Random(14).randbytes(SIZE)


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        yield fake


@pytest.fixture
async def repo_id(services: Services, upstream: FakeUpstream) -> str:
    return await add_repo(services, upstream.url)


@pytest.fixture
def services_settings_env(tmp_path: Path, mongo_db: Database) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            'WAYSTATION_MONGO_URI': os.environ['WAYSTATION_TEST_MONGO_URI'],
            'WAYSTATION_MONGO_DB': mongo_db.name,
            'WAYSTATION_VALKEY_URL': os.environ['WAYSTATION_TEST_VALKEY_URL'],
            'WAYSTATION_DATA_DIR': str(tmp_path / 'data'),
            'WAYSTATION_MIN_FREE_GB': '0',
            'WAYSTATION_SECRET_KEYS': TEST_KEY,
            'WAYSTATION_BASE_URL': 'https://registry.example.com',
            'WAYSTATION_SSRF_ALLOW_CIDRS': '127.0.0.0/8',
            'WAYSTATION_COALESCE_LEASE_S': '2',
            'WAYSTATION_LOG_LEVEL': 'WARNING',
        }
    )
    return env


@pytest.fixture
async def services(services_settings_env: dict[str, str], mongo_db: Database, tmp_path: Path) -> Services:
    """Only to prepare the database (schema, repository); the replicas are separate processes."""
    from ws_testing import make_settings

    await ensure_schema(mongo_db)
    await apply_migrations(mongo_db)
    settings = make_settings(
        tmp_path,
        mongo_uri=os.environ['WAYSTATION_TEST_MONGO_URI'],
        mongo_db=mongo_db.name,
        valkey_url=os.environ['WAYSTATION_TEST_VALKEY_URL'],
    )
    return Services.create(settings, check_schema_on_start=False)


class Replica:
    def __init__(self, env: dict[str, str], log: Path) -> None:
        self.env = env
        self.port = _free_port()
        self.log = log
        self.proc: asyncio.subprocess.Process | None = None

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    async def __aenter__(self) -> Replica:
        stream = self.log.open('wb')
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            '-m',
            'uvicorn',
            'waystation_server.app:create_app',
            '--factory',
            '--host',
            '127.0.0.1',
            '--port',
            str(self.port),
            '--loop',
            'uvloop',
            '--http',
            'httptools',
            '--no-access-log',
            '--log-level',
            'warning',
            env=self.env,
            stdout=stream,
            stderr=stream,
        )
        stream.close()
        async with httpx.AsyncClient() as http:
            for _ in range(300):
                if self.proc.returncode is not None:
                    raise AssertionError(f'replica exited: {self.log.read_text()[-2000:]}')
                with contextlib.suppress(httpx.HTTPError):
                    if (await http.get(f'{self.url}/healthz')).status_code == 200:
                        return self
                await asyncio.sleep(0.1)
        raise AssertionError(f'replica did not start: {self.log.read_text()[-2000:]}')

    def kill(self) -> None:
        assert self.proc is not None
        self.proc.send_signal(signal.SIGKILL)

    async def __aexit__(self, *exc: object) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 15)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()


class Download:
    """One streaming client: hashes what it receives (in a thread), counts bytes."""

    def __init__(self, http: httpx.AsyncClient, url: str) -> None:
        self.http = http
        self.url = url
        self.received = 0
        self.status = 0
        self.cache = ''

    async def run(self) -> str:
        digest = hashlib.sha256()
        async with self.http.stream('GET', self.url) as response:
            self.status = response.status_code
            self.cache = response.headers.get('x-ws-cache', '')
            async for chunk in response.aiter_raw(MiB):
                await asyncio.to_thread(digest.update, chunk)
                self.received += len(chunk)
        return digest.hexdigest()


async def eventually(check: Callable[[], Awaitable[bool]], timeout: float = 60.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await check():
        if loop.time() > deadline:
            raise AssertionError('condition not reached in time')
        await asyncio.sleep(0.05)


async def fill_state(valkey: Redis, repo_id: str) -> dict[str, Any]:
    raw: dict[str, Any] = await valkey.hgetall(Coalescer.key(repo_id, PATH))
    return raw


def _client() -> httpx.AsyncClient:
    limits = httpx.Limits(max_connections=CLIENTS + 20, max_keepalive_connections=CLIENTS + 20)
    return httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(300.0, connect=30.0))


async def test_200_clients_on_two_replicas_cause_one_upstream_fetch(
    services_settings_env: dict[str, str],
    services: Services,
    valkey: Redis,
    upstream: FakeUpstream,
    repo_id: str,
    data: bytes,
    tmp_path: Path,
) -> None:
    expected = hashlib.sha256(data).hexdigest()
    gate = asyncio.Event()
    # Slow upstream: it stops after 8 MiB until every follower is attached, then trickles 1 MiB chunks.
    upstream.routes[f'/{PATH}'] = Respond(200, {}, data, chunk=MiB, chunk_delay=0.005, pause_at=8 * MiB, gate=gate)
    async with (
        Replica(services_settings_env, tmp_path / 'a.log') as a,
        Replica(services_settings_env, tmp_path / 'b.log') as b,
        _client() as http,
    ):
        first = Download(http, a.url + URL)
        owner = asyncio.create_task(first.run())

        async def streaming() -> bool:
            return (await fill_state(valkey, repo_id)).get('status') == 'streaming'

        await eventually(streaming)
        downloads = [Download(http, (a if i % 2 else b).url + URL) for i in range(CLIENTS - 1)]
        tasks = [asyncio.create_task(d.run()) for d in downloads]

        async def attached() -> bool:
            return int((await fill_state(valkey, repo_id)).get('followers', 0)) == CLIENTS - 1

        await eventually(attached)
        gate.set()
        digests = await asyncio.gather(owner, *tasks)

    assert upstream.hits(f'/{PATH}') == 1
    assert digests == [expected] * CLIENTS
    everyone = [first, *downloads]
    assert all(d.status == 200 and d.received == SIZE for d in everyone)
    assert first.cache == 'FETCHING'
    assert all(d.cache == 'COALESCED' for d in downloads)
    assert await services.db[ASSETS].count_documents({'path': PATH, 'blob_sha256': expected}) == 1
    assert list((tmp_path / 'data' / 'uploads').iterdir()) == []


async def test_owner_replica_killed_a_follower_takes_over(
    services_settings_env: dict[str, str],
    services: Services,
    valkey: Redis,
    upstream: FakeUpstream,
    repo_id: str,
    data: bytes,
    tmp_path: Path,
) -> None:
    expected = hashlib.sha256(data).hexdigest()

    def route(seen: Seen) -> Respond:
        if upstream.hits(f'/{PATH}') == 1:  # the owner's fetch delivers 8 MiB, then nothing
            return Respond(200, {}, data, chunk=MiB, hang_after=8 * MiB)
        return Respond(200, {}, data, chunk=MiB)

    upstream.routes[f'/{PATH}'] = route
    followers = 50
    async with (
        Replica(services_settings_env, tmp_path / 'a.log') as a,
        Replica(services_settings_env, tmp_path / 'b.log') as b,
        _client() as http,
    ):
        owner = asyncio.create_task(Download(http, a.url + URL).run())

        async def written() -> bool:
            state = await fill_state(valkey, repo_id)
            return state.get('status') == 'streaming' and int(state.get('written', 0)) >= 4 * MiB

        await eventually(written)
        downloads = [Download(http, b.url + URL) for _ in range(followers)]
        tasks = [asyncio.create_task(d.run()) for d in downloads]

        async def receiving() -> bool:
            return all(d.received >= 2 * MiB for d in downloads)

        await eventually(receiving)
        a.kill()
        with pytest.raises(httpx.HTTPError):
            await owner
        digests = await asyncio.gather(*tasks)

    assert upstream.hits(f'/{PATH}') == 2
    assert digests == [expected] * followers
    assert all(d.status == 200 and d.received == SIZE and d.cache == 'COALESCED' for d in downloads)
    assert await services.db[ASSETS].count_documents({'path': PATH, 'blob_sha256': expected}) == 1
