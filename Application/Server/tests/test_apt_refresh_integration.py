"""The worker's apt refresh (``apt.refresh``, T-P2-APT) against real MongoDB/Valkey, the server's apt proxy filling
the cache and a scriptable upstream: unverified pool files verified (and a contradicted one dropped) by fetching the
index that lists them, stale Release revalidation (200 and 304) with the used index refreshed by-hash and then
served as a cache hit, a mismatching index refused, a lost indexing redone from the cache with no upstream request,
offline and auto-blocked repositories, idempotency (a second run changes nothing and calls no upstream)."""

import hashlib
import lzma
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from ws_fake_upstream import FakeUpstream, Respond, Seen

from waystation_common.breaker import AutoBlockBreaker
from waystation_common.db.collections import APT_PACKAGES, APT_POOL_INDEX, ASSETS, REPOSITORIES
from waystation_common.events import keys as event_keys
from waystation_common.settings import Settings
from waystation_server.formats.apt.plugin import AptPlugin
from waystation_server.proxy.http import Timeouts
from waystation_server.services import Services
from waystation_worker import apt
from waystation_worker.upstream import Fetcher

pytestmark = pytest.mark.integration

DEB = b'!<arch>\n' + bytes(range(256)) * 40
DEB_PATH = 'pool/main/h/hello/hello_2.10-5_amd64.deb'
ALL_PATH = 'pool/main/t/tzdata/tzdata_2025a-1_all.deb'
INRELEASE = 'dists/trixie/InRelease'
INDEX = 'main/binary-amd64/Packages.xz'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def packages(*entries: tuple[str, bytes]) -> bytes:
    stanzas = [
        f'Package: p{i}\nVersion: 1\nArchitecture: amd64\nFilename: {name}\nSize: {len(data)}\nSHA256: {sha(data)}\n'
        for i, (name, data) in enumerate(entries)
    ]
    return lzma.compress('\n'.join(stanzas).encode())


def release(files: dict[str, bytes]) -> bytes:
    lines = ['Origin: Test', 'Suite: trixie', 'Acquire-By-Hash: yes', 'SHA256:']
    lines += [f' {sha(data)} {len(data)} {path}' for path, data in files.items()]
    text = '\n'.join(lines) + '\n'
    return (
        f'-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n{text}'
        '-----BEGIN PGP SIGNATURE-----\n\nZmFrZQ==\n-----END PGP SIGNATURE-----\n'
    ).encode()


def publish(upstream: FakeUpstream, files: dict[str, bytes]) -> bytes:
    """Serve suite trixie: InRelease (answering 304 to its own ETag) + every file at its by-hash path."""
    inrelease = release(files)
    etag = f'"{sha(inrelease)}"'

    def answer(seen: Seen) -> Respond:
        if seen.headers.get('if-none-match') == etag:
            return Respond(304, {'ETag': etag}, b'')
        return Respond(200, {'ETag': etag}, inrelease)

    upstream.routes[f'/{INRELEASE}'] = answer
    for path, data in files.items():
        directory = path.rsplit('/', 1)[0]
        upstream.routes[f'/dists/trixie/{directory}/by-hash/SHA256/{sha(data)}'] = Respond(200, {}, data)
    return inrelease


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'])


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        yield fake


@pytest.fixture
def plugin(services: Services) -> AptPlugin:
    services.upstream.timeouts = Timeouts(connect=2.0, read=1.0)
    plugin = services.formats.plugin('apt')
    assert isinstance(plugin, AptPlugin)
    return plugin


@pytest.fixture
async def repo(services: Services, upstream: FakeUpstream, plugin: AptPlugin) -> str:
    now = datetime.now(UTC)
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': 'repo-debian',
            'name': 'debian',
            'format': 'apt',
            'type': 'proxy',
            'online': True,
            'proxy': {'upstream_url': upstream.url, 'metadata_max_age_s': 3600},
            'format_options': {'distribution': 'trixie'},
            'created_at': now,
            'updated_at': now,
        }
    )
    services.catalog.invalidate()
    return 'repo-debian'


@pytest.fixture
def breaker_clock() -> list[float]:
    """Seconds added to the job's breaker clock (the test moves time past a block period)."""
    return [0.0]


@pytest.fixture
async def fetcher(settings: Settings, services: Services, breaker_clock: list[float]) -> AsyncIterator[Fetcher]:
    breaker = AutoBlockBreaker(services.valkey, clock=lambda: time.time() + breaker_clock[0])
    async with Fetcher(settings, None, breaker=breaker) as f:
        yield f


Run = Callable[..., Any]


@pytest.fixture
def run(services: Services, fetcher: Fetcher, repo: str) -> Run:
    async def go(*, now: datetime | None = None, repo_id: str | None = repo) -> dict[str, Any]:
        lines: list[str] = []

        async def log_line(line: str) -> None:
            lines.append(line)

        result = await apt.run(
            services.db,
            services.blobs,
            fetcher,
            repo_id,
            valkey=services.valkey,
            now=now or datetime.now(UTC),
            log_line=log_line,
        )
        result['lines'] = lines
        return result

    return go


async def pool_entry(services: Services, repo: str, path: str) -> dict[str, Any]:
    entry = await services.db[APT_POOL_INDEX].find_one({'repo_id': repo, 'filename': path})
    assert entry is not None
    return entry


async def age(services: Services, repo: str, path: str, hours: int = 2) -> None:
    old = datetime.now(UTC) - timedelta(hours=hours)
    await services.db[ASSETS].update_one({'repo_id': repo, 'path': path}, {'$set': {'upstream.fetched_at': old}})


async def test_unverified_pool_files_are_verified_from_the_index_that_lists_them(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: AptPlugin, repo: str, run: Run
) -> None:
    other = b'what the upstream served'
    index = packages((DEB_PATH, DEB), (ALL_PATH, b'what the listing says'))
    publish(upstream, {INDEX: index})
    upstream.routes[f'/{DEB_PATH}'] = Respond(200, {}, DEB)
    upstream.routes[f'/{ALL_PATH}'] = Respond(200, {}, other)
    # apt had the Packages file already: only the Release and the packages go through the proxy.
    assert (await client.get(f'/repository/debian/{INRELEASE}')).status_code == 200
    assert (await client.get(f'/repository/debian/{DEB_PATH}')).content == DEB
    assert (await client.get(f'/repository/debian/{ALL_PATH}')).content == other
    await plugin.drain()
    assert (await pool_entry(services, repo, DEB_PATH))['verified'] is False

    result = await run()
    assert (result['indexes_fetched'], result['indexes_indexed'], result['verified']) == (1, 1, 0)
    assert result['dropped'] == 1 and result['unverified'] == 0 and result['releases_checked'] == 0
    assert any(ALL_PATH in line for line in result['lines'])
    key = f'dists/trixie/main/binary-amd64/by-hash/SHA256/{sha(index)}'
    assert upstream.hits(f'/{key}') == 1
    stored = await services.db[ASSETS].find_one({'repo_id': repo, 'path': key})
    assert stored is not None and stored['blob_sha256'] == sha(index) and stored['kind'] == 'metadata'
    assert (await pool_entry(services, repo, DEB_PATH))['verified'] is True
    entry = await pool_entry(services, repo, ALL_PATH)
    assert (entry['verified'], entry['sha256']) == (True, sha(b'what the listing says'))
    assert await services.db[ASSETS].find_one({'repo_id': repo, 'path': ALL_PATH}) is None  # contradicted: dropped
    assert await services.db[ASSETS].find_one({'repo_id': repo, 'path': DEB_PATH}) is not None
    assert await services.db[APT_PACKAGES].count_documents({'repo_id': repo, 'suite': 'trixie'}) == 2
    # The index is now a cache hit for clients.
    hit = await client.get(f'/repository/debian/dists/trixie/{INDEX}')
    assert hit.headers['x-ws-cache'] == 'HIT'

    # Idempotent: nothing to do, no upstream request.
    seen = len(upstream.seen)
    again = await run()
    assert (again['indexes_fetched'], again['indexes_indexed'], again['verified'], again['dropped']) == (0, 0, 0, 0)
    assert len(upstream.seen) == seen


async def test_stale_release_is_revalidated_and_the_used_index_refreshed(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: AptPlugin, repo: str, run: Run
) -> None:
    old_index = packages(('pool/main/o/old/old_1_amd64.deb', b'old'))
    publish(upstream, {INDEX: old_index})
    assert (await client.get(f'/repository/debian/{INRELEASE}')).status_code == 200
    assert (await client.get(f'/repository/debian/dists/trixie/{INDEX}')).status_code == 200
    await plugin.drain()
    # Fresh: nothing happens.
    first = await run()
    assert (first['releases_checked'], first['indexes_fetched'], first['indexes_indexed']) == (0, 0, 0)

    new_index = packages((DEB_PATH, DEB))
    new_inrelease = publish(upstream, {INDEX: new_index})
    await age(services, repo, INRELEASE)
    result = await run()
    assert (result['releases_checked'], result['releases_changed'], result['indexes_fetched']) == (1, 1, 1)
    cached = await services.db[ASSETS].find_one({'repo_id': repo, 'path': INRELEASE})
    assert cached is not None and cached['blob_sha256'] == sha(new_inrelease)
    assert cached['upstream']['etag'] == f'"{sha(new_inrelease)}"'
    assert upstream.seen[-2].headers.get('if-none-match') == f'"{sha(release({INDEX: old_index}))}"'
    entry = await pool_entry(services, repo, DEB_PATH)
    assert (entry['verified'], entry['suite'], entry['component'], entry['arch']) == (True, 'trixie', 'main', 'amd64')
    gens = await services.db[APT_PACKAGES].distinct('gen', {'repo_id': repo})
    assert gens == [sha(new_index)]
    # Clients get the new Release and the new index from the cache.
    seen = len(upstream.seen)
    response = await client.get(f'/repository/debian/{INRELEASE}')
    assert response.headers['x-accel-redirect'].rsplit('/', 1)[-1] == sha(new_inrelease)
    assert (await client.get(f'/repository/debian/dists/trixie/{INDEX}')).headers['x-ws-cache'] == 'HIT'
    assert len(upstream.seen) == seen

    # Stale again but unchanged upstream: 304, only the timestamp moves.
    await age(services, repo, INRELEASE)
    before = datetime.now(UTC) - timedelta(seconds=1)
    unchanged = await run()
    assert (unchanged['releases_checked'], unchanged['releases_changed'], unchanged['indexes_fetched']) == (1, 0, 0)
    cached = await services.db[ASSETS].find_one({'repo_id': repo, 'path': INRELEASE})
    assert cached is not None and cached['blob_sha256'] == sha(new_inrelease)
    assert cached['upstream']['fetched_at'].replace(tzinfo=UTC) >= before


async def test_an_index_that_does_not_match_the_release_is_refused(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: AptPlugin, repo: str, run: Run
) -> None:
    index = packages((DEB_PATH, DEB))
    publish(upstream, {INDEX: index})
    key = f'dists/trixie/main/binary-amd64/by-hash/SHA256/{sha(index)}'
    upstream.routes[f'/{key}'] = Respond(200, {}, b'tampered' + index[8:])
    upstream.routes[f'/{DEB_PATH}'] = Respond(200, {}, DEB)
    assert (await client.get(f'/repository/debian/{INRELEASE}')).status_code == 200
    assert (await client.get(f'/repository/debian/{DEB_PATH}')).status_code == 200
    await plugin.drain()
    result = await run()
    assert result['indexes_fetched'] == 0 and result['unverified'] == 1
    assert any(INDEX in error for error in result['errors'])
    assert await services.db[ASSETS].find_one({'repo_id': repo, 'path': key}) is None
    assert (await pool_entry(services, repo, DEB_PATH))['verified'] is False


async def test_a_lost_indexing_is_redone_from_the_cache_without_upstream(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: AptPlugin, repo: str, run: Run
) -> None:
    index = packages((DEB_PATH, DEB))
    publish(upstream, {INDEX: index})
    assert (await client.get(f'/repository/debian/{INRELEASE}')).status_code == 200
    assert (await client.get(f'/repository/debian/dists/trixie/{INDEX}')).status_code == 200
    await plugin.drain()
    # The server stopped before its background indexing ran, and the Release is stale but the repository is
    # offline: the refresh indexes what is cached and asks no upstream.
    await services.db[APT_PACKAGES].delete_many({'repo_id': repo})
    await services.db[APT_POOL_INDEX].delete_many({'repo_id': repo})
    await services.db[REPOSITORIES].update_one({'_id': repo}, {'$set': {'proxy.offline': True}})
    await age(services, repo, INRELEASE)
    seen = len(upstream.seen)
    result = await run()
    assert (result['upstream'], result['releases_checked'], result['indexes_indexed']) == (False, 0, 1)
    assert len(upstream.seen) == seen
    assert (await pool_entry(services, repo, DEB_PATH))['verified'] is True
    assert await services.db[APT_PACKAGES].count_documents({'repo_id': repo}) == 1


async def test_auto_blocked_upstream_is_not_called_and_all_repositories_run(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: AptPlugin, repo: str, run: Run
) -> None:
    publish(upstream, {INDEX: packages((DEB_PATH, DEB))})
    upstream.routes[f'/{DEB_PATH}'] = Respond(200, {}, DEB)
    assert (await client.get(f'/repository/debian/{INRELEASE}')).status_code == 200
    assert (await client.get(f'/repository/debian/{DEB_PATH}')).status_code == 200
    await plugin.drain()
    until = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp() * 1000)
    await services.valkey.hset(event_keys.upstream_health_key(repo), 'blocked_until', str(until))
    await age(services, repo, INRELEASE)
    await services.db[REPOSITORIES].insert_one(
        {'_id': 'repo-raw', 'name': 'files', 'format': 'raw', 'type': 'proxy', 'proxy': {'upstream_url': upstream.url}}
    )
    seen = len(upstream.seen)
    result = await run(repo_id=None)
    assert result['repositories'] == 1  # apt proxies only
    only = result['results'][0]
    assert (only['upstream'], only['releases_checked'], only['unverified']) == (False, 0, 1)
    assert any('auto-blocked' in line for line in result['lines'])
    assert len(upstream.seen) == seen
    # A raw repository given by id is skipped; a deleted one too.
    assert (await run(repo_id='repo-raw'))['skipped'] == 'not an apt proxy repository'
    assert (await run(repo_id='nosuch'))['skipped'] == 'the repository was deleted'


async def test_refresh_requests_feed_the_shared_auto_block_breaker(
    client: httpx.AsyncClient,
    services: Services,
    upstream: FakeUpstream,
    plugin: AptPlugin,
    repo: str,
    run: Run,
    breaker_clock: list[float],
) -> None:
    # The server's own prober would race the job for the probe once the block period ends; the job is under test.
    await services.proxy.breaker.stop()
    publish(upstream, {INDEX: packages()})
    assert (await client.get(f'/repository/debian/{INRELEASE}')).status_code == 200
    await services.db[REPOSITORIES].update_one(
        {'_id': repo}, {'$set': {'proxy.auto_block': {'enabled': True, 'failures': 2, 'initial_s': 60, 'max_s': 600}}}
    )
    health = event_keys.upstream_health_key(repo)
    upstream.routes[f'/{INRELEASE}'] = Respond(503, {}, b'down')
    for expected_failures in (1, 2):
        await age(services, repo, INRELEASE)
        result = await run()
        assert any('503' in error for error in result['errors'])
        assert await services.valkey.hget(health, 'block_failures') == str(expected_failures)
    # The second failure in a row blocked the upstream, for the server's proxy as well.
    assert await services.valkey.hget(health, 'blocked_until') is not None
    seen = len(upstream.seen)
    assert (await run())['upstream'] is False
    assert len(upstream.seen) == seen
    # The period ends: one probe; the upstream answers again and the block is lifted.
    publish(upstream, {INDEX: packages()})
    # (Moving the clock past the period also ends the job process's short local memory of the block.)
    breaker_clock[0] = 61.0
    result = await run(now=datetime.now(UTC) + timedelta(seconds=61))
    assert result['upstream'] is True and result['releases_checked'] == 1 and not result.get('errors')
    assert await services.valkey.hget(health, 'blocked_until') is None
    assert await services.valkey.hget(health, 'block_failures') == '0'
