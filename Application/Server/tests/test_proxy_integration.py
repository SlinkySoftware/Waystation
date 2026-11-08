"""Proxy engine (T-S07) against real MongoDB/Valkey and a scriptable local upstream, including fault injection."""

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from ws_fake_upstream import FakeUpstream, Respond, Seen
from ws_testing import FilesPlugin, add_repo, asgi_client

from waystation_common.db.collections import ASSETS, BLOBS, REPOSITORIES
from waystation_common.errors import DigestMismatch
from waystation_common.events import keys
from waystation_common.settings import Settings
from waystation_server.problems import ResponseAborted
from waystation_server.proxy.engine import AssetKind, AssetSpec, Expected, UpstreamInfo
from waystation_server.proxy.http import Timeouts
from waystation_server.repos.model import secret_aad
from waystation_server.services import Services

pytestmark = pytest.mark.integration

DATA = bytes(range(256)) * 3000  # 768000 bytes: several 256 KiB chunks
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'])


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        yield fake


@pytest.fixture
def plugin(services: Services) -> FilesPlugin:
    plugin = FilesPlugin(services)
    services.formats.set_plugin(plugin)  # type: ignore[arg-type]
    services.upstream.timeouts = Timeouts(connect=2.0, read=1.0)
    return plugin


@pytest.fixture
async def repo(services: Services, upstream: FakeUpstream, plugin: FilesPlugin) -> str:
    return await add_repo(services, upstream.url)


async def activity(services: Services) -> list[dict[str, Any]]:
    await services.emitter.flush()
    entries = await services.valkey.xrange(keys.ACTIVITY)
    return [json.loads(fields['data']) for _, fields in entries]


async def counters(services: Services, repo_id: str) -> dict[str, int]:
    await services.emitter.flush()
    total: dict[str, int] = {}
    for member in await services.valkey.smembers(keys.STATS_PENDING):
        minute, rid = keys.parse_pending(member)
        if rid == repo_id:
            for k, v in (await services.valkey.hgetall(keys.cnt_key(minute, rid))).items():
                if k != 'format':
                    total[k] = total.get(k, 0) + int(v)
    return total


async def test_content_miss_tees_then_hits(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    upstream.routes['/pkg/a.bin'] = Respond(200, {'Content-Type': 'application/x-thing'}, DATA, chunk=100_000)
    first = await client.get('/repository/files/pkg/a.bin')
    assert first.status_code == 200
    assert first.content == DATA
    assert first.headers['x-ws-cache'] == 'FETCHING'
    assert first.headers['content-length'] == str(len(DATA))

    second = await client.get('/repository/files/pkg/a.bin')
    assert second.status_code == 200
    assert second.content == b''  # bytes come from nginx
    assert second.headers['x-accel-redirect'].endswith(SHA)
    assert second.headers['x-ws-cache'] == 'HIT'
    assert second.headers['content-type'] == 'application/x-thing'
    assert upstream.hits('/pkg/a.bin') == 1

    head = await client.head('/repository/files/pkg/a.bin')
    assert head.headers['content-length'] == str(len(DATA))
    assert (
        await client.get('/repository/files/pkg/a.bin', headers={'If-None-Match': f'"sha256:{SHA}"'})
    ).status_code == 304

    asset = await services.db[ASSETS].find_one({'repo_id': repo, 'path': 'pkg/a.bin'})
    assert asset is not None
    assert asset['blob_sha256'] == SHA
    assert asset['kind'] == 'content'
    assert await services.db[BLOBS].find_one({'_id': SHA}) is not None
    assert await services.blobs.exists(SHA)

    events = await activity(services)
    kinds = [e['type'] for e in events]
    assert kinds[:2] == ['transfer.started', 'transfer.finished']
    assert events[1]['bytes'] == len(DATA)
    assert events[1]['cache'] == 'FETCHING'
    assert 'cache.hit' in kinds
    assert await services.valkey.hlen(keys.TRANSFERS) == 0
    stats = await counters(services, repo)
    assert stats['misses'] == 1
    assert stats['hits'] == 3
    assert stats['bytes_in_upstream'] == len(DATA)
    assert stats['upstream_requests'] == 1


async def test_digest_verified_before_commit(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin, repo: str
) -> None:
    upstream.routes['/good'] = Respond(200, {}, DATA)
    upstream.routes['/bad'] = Respond(200, {}, DATA)
    plugin.make_spec = lambda path: AssetSpec(
        path=path,
        expected=Expected(sha256=SHA if path == 'good' else 'f' * 64, sha512=hashlib.sha512(DATA).hexdigest()),
    )
    assert (await client.get('/repository/files/good')).content == DATA
    with pytest.raises(ResponseAborted):
        await client.get('/repository/files/bad')
    assert await services.db[ASSETS].find_one({'path': 'bad'}) is None
    assert await services.db[ASSETS].find_one({'path': 'good'}) is not None
    failed = [e for e in await activity(services) if e['type'] == 'transfer.failed']
    assert failed[0]['path'] == 'bad'
    assert 'sha256' in failed[0]['detail']['error']
    assert list(services.blobs.uploads_dir.iterdir()) == []


async def test_size_mismatch_from_content_length(
    client: httpx.AsyncClient, upstream: FakeUpstream, plugin: FilesPlugin, repo: str
) -> None:
    upstream.routes['/f'] = Respond(200, {}, DATA)
    plugin.make_spec = lambda path: AssetSpec(path=path, expected=Expected(size=10))
    response = await client.get('/repository/files/f')
    assert response.status_code == 502


async def test_upstream_reset_mid_body_caches_nothing(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    upstream.routes['/f'] = Respond(200, {}, DATA, chunk=65536, reset_after=300_000)
    with pytest.raises(ResponseAborted):
        await client.get('/repository/files/f')
    assert await services.db[ASSETS].count_documents({}) == 0
    assert list(services.blobs.uploads_dir.iterdir()) == []
    assert (await counters(services, repo)).get('upstream_errors') == 1


async def test_upstream_5xx(client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str) -> None:
    upstream.routes['/f'] = Respond(500, {}, b'boom')
    response = await client.get('/repository/files/f')
    assert response.status_code == 502
    assert response.headers['content-type'].startswith('text/plain')
    assert await services.db[ASSETS].count_documents({}) == 0
    await services.emitter.flush()
    health = await services.valkey.hgetall(keys.upstream_health_key(repo))
    assert health['last_error'] == 'upstream answered 500'
    assert (await counters(services, repo))['errors'] == 1


async def test_upstream_timeout(client: httpx.AsyncClient, upstream: FakeUpstream, repo: str) -> None:
    upstream.routes['/slow'] = Respond(hang=True)
    response = await client.get('/repository/files/slow')
    assert response.status_code == 504


async def test_slow_body_times_out(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    upstream.routes['/f'] = Respond(200, {}, DATA, chunk=100_000, chunk_delay=2.0)
    with pytest.raises(ResponseAborted):
        await client.get('/repository/files/f')
    assert await services.db[ASSETS].count_documents({}) == 0


async def test_not_found_is_negative_cached(client: httpx.AsyncClient, upstream: FakeUpstream, repo: str) -> None:
    assert (await client.get('/repository/files/missing')).status_code == 404
    assert (await client.get('/repository/files/missing')).status_code == 404
    assert upstream.hits('/missing') == 1


async def test_head_miss_passes_through(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    upstream.routes['/f'] = Respond(200, {'Content-Type': 'application/x-thing'}, DATA)
    response = await client.head('/repository/files/f')
    assert response.status_code == 200
    assert response.headers['content-length'] == str(len(DATA))
    assert upstream.seen[0].method == 'HEAD'
    assert await services.db[ASSETS].count_documents({}) == 0


def _rewrite(data: bytes, info: UpstreamInfo) -> bytes:
    return data.replace(b'UPSTREAM', b'LOCAL')


async def test_metadata_transform_freshness_and_revalidation(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin, repo: str
) -> None:
    def index(seen: Seen) -> Respond:
        if seen.headers.get('if-none-match') == '"v1"':
            return Respond(304, {'ETag': '"v1"'})
        return Respond(200, {'ETag': '"v1"', 'Content-Type': 'application/json'}, b'{"url": "UPSTREAM"}')

    upstream.routes['/index.json'] = index
    max_age: dict[str, int | None] = {'value': None}
    plugin.make_spec = lambda path: AssetSpec(
        path=path, kind=AssetKind.METADATA, transform=_rewrite, max_age_s=max_age['value']
    )
    first = await client.get('/repository/files/index.json')
    assert first.headers['x-ws-cache'] == 'FETCHING'
    stored = await services.db[ASSETS].find_one({'path': 'index.json'})
    assert stored is not None
    assert stored['upstream']['etag'] == '"v1"'
    blob = services.blobs.blob_path(stored['blob_sha256']).read_bytes()
    assert blob == b'{"url": "LOCAL"}'
    assert first.headers['x-accel-redirect'].endswith(stored['blob_sha256'])

    assert (await client.get('/repository/files/index.json')).headers['x-ws-cache'] == 'HIT'
    max_age['value'] = 0
    revalidated = await client.get('/repository/files/index.json')
    assert revalidated.headers['x-ws-cache'] == 'REVALIDATED'
    assert upstream.seen[-1].headers['if-none-match'] == '"v1"'
    assert upstream.hits('/index.json') == 2

    upstream.routes['/index.json'] = Respond(500, {}, b'down')
    stale = await client.get('/repository/files/index.json')
    assert stale.headers['x-ws-cache'] == 'STALE'
    assert (await counters(services, repo))['stale'] == 1


async def test_metadata_validation_failure_caches_nothing(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin, repo: str
) -> None:
    def refuse(data: bytes, info: UpstreamInfo) -> None:
        raise DigestMismatch('index does not match its Release file')

    upstream.routes['/Packages'] = Respond(200, {}, b'Package: x\n')
    plugin.make_spec = lambda path: AssetSpec(path=path, kind=AssetKind.METADATA, validate=refuse)
    response = await client.get('/repository/files/Packages')
    assert response.status_code == 502
    assert 'Release' in response.text
    assert await services.db[ASSETS].count_documents({}) == 0


async def test_offline_repository_never_calls_upstream(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin
) -> None:
    await add_repo(services, upstream.url, offline=True)
    assert (await client.get('/repository/files/x')).status_code == 404
    assert upstream.seen == []


async def test_credentials_only_go_to_the_upstream_origin(
    client: httpx.AsyncClient, services: Services, plugin: FilesPlugin
) -> None:
    async with FakeUpstream() as origin, FakeUpstream() as cdn:
        cdn.routes['/blob'] = Respond(200, {}, DATA)
        origin.routes['/f'] = Respond(307, {'Location': f'{cdn.url}blob'})
        repo_id = 'repo-private'
        assert services.keyring is not None
        secret = services.keyring.encrypt_str('s3cret', aad=secret_aad(repo_id))
        now = datetime.now(UTC)
        await services.db[REPOSITORIES].insert_one(
            {
                '_id': repo_id,
                'name': 'private',
                'format': 'raw',
                'type': 'proxy',
                'online': True,
                'proxy': {
                    'upstream_url': origin.url,
                    'auth': {'kind': 'basic', 'username': 'ci', 'secret_enc': secret},
                },
                'created_at': now,
                'updated_at': now,
            }
        )
        services.catalog.invalidate()
        response = await client.get('/repository/private/f')
        assert response.content == DATA
        assert origin.seen[0].headers['authorization'].startswith('Basic ')
        assert 'authorization' not in cdn.seen[0].headers


async def test_redirect_to_private_address_is_blocked(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    upstream.routes['/f'] = Respond(302, {'Location': 'http://10.255.255.1/secret'})  # forbidden-strings: allow
    response = await client.get('/repository/files/f')
    assert response.status_code == 502
    await services.emitter.flush()
    health = await services.valkey.hgetall(keys.upstream_health_key(repo))
    assert 'private or reserved' in health['last_error']


async def test_private_upstream_needs_permission(
    client: httpx.AsyncClient, services: Services, plugin: FilesPlugin
) -> None:
    services.upstream.guard.allow = ()  # no WAYSTATION_SSRF_ALLOW_CIDRS
    async with FakeUpstream({'/f': Respond(200, {}, b'x')}) as local:
        await add_repo(services, local.url, name='strict')
        await add_repo(services, local.url, name='lenient', allow_private_upstream=True)
        assert (await client.get('/repository/strict/f')).status_code == 502
        assert local.seen == []
        assert (await client.get('/repository/lenient/f')).content == b'x'


async def test_ensure_and_read(services: Services, upstream: FakeUpstream, repo: str) -> None:
    upstream.routes['/meta'] = Respond(200, {}, b'hello')
    async with asgi_client(services):
        pass
    catalog_repo = await services.catalog.get('files')
    assert catalog_repo is not None

    class Req:
        repo = catalog_repo
        principal = type('P', (), {'display': 'anonymous'})()
        client_ip = '127.0.0.1'
        is_head = False
        method = 'GET'
        request = type('R', (), {'headers': {}})()

        def log(self, **fields: Any) -> None:
            pass

    asset = await services.proxy.ensure(Req(), AssetSpec(path='meta', kind=AssetKind.METADATA))  # type: ignore[arg-type]
    assert await services.proxy.read(asset) == b'hello'
    again = await services.proxy.ensure(Req(), AssetSpec(path='meta', kind=AssetKind.METADATA))  # type: ignore[arg-type]
    assert again.id == asset.id
    assert upstream.hits('/meta') == 1
