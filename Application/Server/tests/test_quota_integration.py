"""Quotas and the low-disk guard (T-O03, SPECIFICATION §4.8) against real MongoDB/Valkey: usage accounting, hard
quotas refusing hosted pushes (OCI ``DENIED``) and passing proxy downloads through uncached while hits are still
served, soft quotas only warning, the guard settings API and the low-disk pass-through."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import FilesPlugin, add_repo, asgi_client

from waystation_common.db import usage as usage_db
from waystation_common.db.collections import ASSETS, REPOSITORIES
from waystation_common.events import keys
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.problems import ResponseAborted
from waystation_server.proxy.engine import AssetKind, AssetSpec, Expected
from waystation_server.proxy.http import Timeouts
from waystation_server.services import Services
from waystation_server.stats.prometheus import ClusterMetrics

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
SERVICE = 'registry.example.com'
DATA = bytes(range(256)) * 3000  # 768000 bytes
SHA = hashlib.sha256(DATA).hexdigest()
OTHER = b'other content ' * 1000
OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
OCI_CONFIG = 'application/vnd.oci.image.config.v1+json'
LAYER_TYPE = 'application/vnd.oci.image.layer.v1.tar+gzip'
CONFIG = json.dumps({'architecture': 'amd64', 'os': 'linux'}).encode()


def digest(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


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
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


async def set_quota(services: Services, repo_id: str, quota: int | None, mode: str = 'hard') -> None:
    await services.db[REPOSITORIES].update_one(
        {'_id': repo_id}, {'$set': {'storage': {'quota_bytes': quota, 'quota_mode': mode}}}
    )
    services.catalog.invalidate()


async def activity(services: Services) -> list[dict[str, Any]]:
    await services.emitter.flush()
    return [json.loads(fields['data']) for _, fields in await services.valkey.xrange(keys.ACTIVITY)]


# ---------------------------------------------------------------------------- proxy repositories
async def test_usage_is_counted_incrementally(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin
) -> None:
    repo = await add_repo(services, upstream.url)
    other = await add_repo(services, upstream.url, name='mirror')
    upstream.routes['/a.bin'] = Respond(200, {}, DATA)
    upstream.routes['/b.bin'] = Respond(200, {}, OTHER)
    assert (await client.get('/repository/files/a.bin')).content == DATA
    assert (await client.get('/repository/files/b.bin')).content == OTHER
    assert (await client.get('/repository/mirror/a.bin')).content == DATA  # same blob, another repository
    usage = await usage_db.get(services.db, repo)
    assert (usage.bytes, usage.assets) == (len(DATA) + len(OTHER), 2)
    assert (await usage_db.get(services.db, other)).bytes == len(DATA)  # a shared blob counts in each repository

    await services.hosted.remove(await services.catalog.get('files'), 'b.bin')  # type: ignore[arg-type]
    assert (await usage_db.get(services.db, repo)).bytes == len(DATA)


async def test_hard_quota_passes_new_content_through_and_still_serves_hits(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin
) -> None:
    repo = await add_repo(services, upstream.url)
    upstream.routes['/cached.bin'] = Respond(200, {}, OTHER)
    upstream.routes['/new.bin'] = Respond(200, {}, DATA, chunk=100_000)
    assert (await client.get('/repository/files/cached.bin')).headers['x-ws-cache'] == 'FETCHING'
    await set_quota(services, repo, len(OTHER) + 1000)  # room left, but not for DATA (Content-Length known)

    hit = await client.get('/repository/files/cached.bin')
    assert hit.headers['x-ws-cache'] == 'HIT'

    for _ in range(2):
        response = await client.get('/repository/files/new.bin')
        assert response.status_code == 200
        assert response.content == DATA
        assert response.headers['x-ws-not-stored'] == 'quota'
    assert upstream.hits('/new.bin') == 2  # nothing cached: every request goes upstream
    assert await services.db[ASSETS].find_one({'repo_id': repo, 'path': 'new.bin'}) is None
    assert list(services.blobs.uploads_dir.iterdir()) == []
    assert (await usage_db.get(services.db, repo)).bytes == len(OTHER)

    # Metadata is still cached under a hard quota (small; the repository needs it).
    upstream.routes['/index.json'] = Respond(200, {'Content-Type': 'application/json'}, b'{"a": 1}')
    plugin.make_spec = lambda path: AssetSpec(path=path, kind=AssetKind.METADATA)
    meta = await client.get('/repository/files/index.json')
    assert meta.status_code == 200 and 'x-ws-not-stored' not in meta.headers
    assert meta.headers['x-accel-redirect'].endswith(hashlib.sha256(b'{"a": 1}').hexdigest())  # stored, served by nginx
    assert await services.db[ASSETS].find_one({'repo_id': repo, 'path': 'index.json'}) is not None

    # Raising the quota lets new content in again.
    await set_quota(services, repo, 10 * len(DATA))
    plugin.make_spec = lambda path: AssetSpec(path=path)
    assert 'x-ws-not-stored' not in (await client.get('/repository/files/new.bin')).headers
    assert (await client.get('/repository/files/new.bin')).headers['x-ws-cache'] == 'HIT'


async def test_hard_quota_pass_through_verifies_digests(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin
) -> None:
    repo = await add_repo(services, upstream.url)
    await set_quota(services, repo, 1)
    upstream.routes['/good'] = Respond(200, {}, DATA)
    upstream.routes['/bad'] = Respond(200, {}, DATA)
    plugin.make_spec = lambda path: AssetSpec(path=path, expected=Expected(sha256=SHA if path == 'good' else 'f' * 64))
    good = await client.get('/repository/files/good')
    assert good.content == DATA and good.headers['x-ws-not-stored'] == 'quota'
    with pytest.raises(ResponseAborted):
        await client.get('/repository/files/bad')


async def test_concurrent_requests_for_unstored_content_all_succeed(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin
) -> None:
    repo = await add_repo(services, upstream.url)
    await set_quota(services, repo, 1)
    upstream.routes['/big.bin'] = Respond(200, {}, DATA, chunk=50_000, chunk_delay=0.01)
    responses = await asyncio.gather(*(client.get('/repository/files/big.bin') for _ in range(4)))
    assert all(r.status_code == 200 and r.content == DATA for r in responses)
    assert await services.db[ASSETS].find_one({'repo_id': repo}) is None


async def test_soft_quota_only_warns_once_per_threshold(
    admin: httpx.AsyncClient,
    client: httpx.AsyncClient,
    services: Services,
    upstream: FakeUpstream,
    plugin: FilesPlugin,
) -> None:
    repo = await add_repo(services, upstream.url)
    await set_quota(services, repo, len(DATA) + len(OTHER) // 2, mode='soft')
    upstream.routes['/a.bin'] = Respond(200, {}, DATA)
    upstream.routes['/b.bin'] = Respond(200, {}, OTHER)
    upstream.routes['/c.bin'] = Respond(200, {}, b'c' * 1000)
    assert 'x-ws-not-stored' not in (await client.get('/repository/files/a.bin')).headers  # 98 %: warning
    assert 'x-ws-not-stored' not in (await client.get('/repository/files/b.bin')).headers  # over: still stored
    assert 'x-ws-not-stored' not in (await client.get('/repository/files/c.bin')).headers
    events = [e for e in await activity(services) if e['type'].startswith('quota.')]
    assert [(e['type'], e['detail']['level']) for e in events] == [('quota.warning', 80), ('quota.exceeded', 100)]
    assert events[1]['repo'] == 'files' and 'soft quota' in events[1]['detail']['summary']
    assert (await usage_db.get(services.db, repo)).alert_level == 100

    listing = await admin.get('/api/v1/activity', params={'kind': 'storage'})
    assert {e['outcome'] for e in listing.json()['items']} == {'info', 'failure'}


async def test_metrics_report_quotas_and_refusals(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin
) -> None:
    repo = await add_repo(services, upstream.url)
    await set_quota(services, repo, 1)
    upstream.routes['/a.bin'] = Respond(200, {}, DATA)
    await client.get('/repository/files/a.bin')
    families = {f.name: f for f in await ClusterMetrics(services).families()}
    assert families['waystation_repository_quota_bytes'].samples[0].labels == {'repo': 'files', 'mode': 'hard'}
    assert families['waystation_storage_low'].samples[0].value == 0
    rendered = services.metrics.render().decode()
    assert 'waystation_fills_not_stored_total{reason="quota",repo="files"} 1.0' in rendered
    assert 'waystation_quota_refusals_total{kind="fill",repo="files"} 1.0' in rendered


# ---------------------------------------------------------------------------- low-disk guard
async def test_low_disk_guard_refuses_new_writes_and_serves_hits(
    admin: httpx.AsyncClient, client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: FilesPlugin
) -> None:
    repo = await add_repo(services, upstream.url)
    upstream.routes['/cached.bin'] = Respond(200, {}, OTHER)
    upstream.routes['/new.bin'] = Respond(200, {}, DATA)
    await client.get('/repository/files/cached.bin')

    state = (await admin.get('/api/v1/storage/guard')).json()
    assert state['low'] is False and state['min_free_bytes'] == 0
    assert (await client.get('/api/v1/storage/guard')).status_code == 401
    saved = await admin.put('/api/v1/storage/guard', json={'min_free_bytes': 2**53, 'min_free_percent': 5})
    assert saved.status_code == 200, saved.text
    assert saved.json()['low'] is True and saved.json()['floor_bytes'] == 2**53
    bad = await admin.put('/api/v1/storage/guard', json={'min_free_bytes': -1, 'min_free_percent': 95})
    assert bad.status_code == 422

    assert (await client.get('/repository/files/cached.bin')).headers['x-ws-cache'] == 'HIT'
    response = await client.get('/repository/files/new.bin')
    assert response.content == DATA and response.headers['x-ws-not-stored'] == 'low_disk'
    upstream.routes['/index.json'] = Respond(200, {'Content-Type': 'application/json'}, b'{"a": 1}')
    plugin.make_spec = lambda path: AssetSpec(path=path, kind=AssetKind.METADATA)
    meta = await client.get('/repository/files/index.json')
    assert meta.json() == {'a': 1} and meta.headers['x-ws-not-stored'] == 'low_disk'
    assert await services.db[ASSETS].count_documents({'repo_id': repo}) == 1

    await admin.put('/api/v1/storage/guard', json={'min_free_bytes': 0})
    plugin.make_spec = lambda path: AssetSpec(path=path)
    assert 'x-ws-not-stored' not in (await client.get('/repository/files/new.bin')).headers


# ---------------------------------------------------------------------------- hosted repositories (docker)
class Registry:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def request(self, method: str, name: str, suffix: str, **kwargs: Any) -> httpx.Response:
        params = [('service', SERVICE), ('scope', f'repository:{name}:pull,push,delete')]
        issued = await self.client.get('/auth/token', params=params, auth=('admin', PASSWORD))
        headers = {'Authorization': f'Bearer {issued.json()["token"]}', **kwargs.pop('headers', {})}
        return await self.client.request(method, f'/v2/{name}/{suffix}', headers=headers, **kwargs)

    async def push_blob(self, name: str, data: bytes) -> httpx.Response:
        return await self.request('POST', name, 'blobs/uploads/', params={'digest': digest(data)}, content=data)


async def test_hard_quota_refuses_docker_pushes_with_denied(admin: httpx.AsyncClient, services: Services) -> None:
    body = {
        'name': 'apps',
        'format': 'docker',
        'type': 'hosted',
        'online': True,
        'storage': {'quota_bytes': len(DATA) + 1000, 'quota_mode': 'hard'},
    }
    created = await admin.post('/api/v1/repositories', json=body)
    assert created.status_code == 201, created.text
    assert created.json()['storage'] == {'quota_bytes': len(DATA) + 1000, 'quota_mode': 'hard'}
    assert created.json()['quota']['state'] == 'ok'
    async with asgi_client(services) as v2:
        reg = Registry(v2)
        assert (await reg.push_blob('apps/web', DATA)).status_code == 201
        assert (await reg.push_blob('apps/web', CONFIG)).status_code == 201  # 768000 + 52 < quota
        refused = await reg.push_blob('apps/web', OTHER)  # would go over the quota
        assert refused.status_code == 403
        error = refused.json()['errors'][0]
        assert error['code'] == 'DENIED' and 'quota' in error['message']
        manifest = json.dumps(
            {
                'schemaVersion': 2,
                'mediaType': OCI_MANIFEST,
                'config': {'mediaType': OCI_CONFIG, 'digest': digest(CONFIG), 'size': len(CONFIG)},
                'layers': [{'mediaType': LAYER_TYPE, 'digest': digest(DATA), 'size': len(DATA)}],
            }
        ).encode()
        pushed = await reg.request(
            'PUT', 'apps/web', 'manifests/1.0', content=manifest, headers={'Content-Type': OCI_MANIFEST}
        )
        assert pushed.status_code == 201, pushed.text  # still under the quota
        session = await reg.request('POST', 'apps/web', 'blobs/uploads/')
        assert session.status_code == 202
        assert (await reg.request('GET', 'apps/web', f'blobs/{digest(DATA)}')).status_code == 200  # reads work
        repo = await services.catalog.get('apps')
        assert repo is not None
        await services.db[REPOSITORIES].update_one(
            {'_id': repo.id}, {'$set': {'storage.quota_bytes': len(DATA)}}
        )  # now over it
        services.catalog.invalidate()
        upload = 'blobs/uploads/' + session.headers['docker-upload-uuid']
        assert (await reg.request('PATCH', 'apps/web', upload, content=b'x')).status_code == 403
        again = await reg.request(
            'PUT', 'apps/web', 'manifests/1.1', content=manifest, headers={'Content-Type': OCI_MANIFEST}
        )
        assert again.status_code == 403
        assert (await reg.request('DELETE', 'apps/web', 'manifests/1.0')).status_code == 202  # deletes still work
    view = (await admin.get('/api/v1/repositories/apps')).json()
    assert view['quota']['state'] == 'exceeded' and view['quota']['mode'] == 'hard'
    assert view['quota']['used_bytes'] == len(DATA) + len(CONFIG) + len(manifest)


async def test_quota_validation(admin: httpx.AsyncClient) -> None:
    group = await admin.post(
        '/api/v1/repositories',
        json={
            'name': 'all',
            'format': 'docker',
            'type': 'group',
            'online': True,
            'group': {'members': ['nope']},
            'storage': {'quota_bytes': 100},
        },
    )
    assert group.status_code == 422
    assert 'storage.quota_bytes' in {e['field'] for e in group.json()['errors']}
    zero = await admin.post(
        '/api/v1/repositories',
        json={'name': 'h', 'format': 'docker', 'type': 'hosted', 'online': True, 'storage': {'quota_bytes': 0}},
    )
    assert zero.status_code == 422
    plain = await admin.post(
        '/api/v1/repositories', json={'name': 'h2', 'format': 'docker', 'type': 'hosted', 'online': True}
    )
    assert plain.json()['quota'] is None
    assert plain.json()['storage'] == {'quota_bytes': None, 'quota_mode': 'soft'}
