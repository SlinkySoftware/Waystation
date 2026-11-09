"""Repository REST API (T-S05) against real MongoDB/Valkey."""

from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream
from ws_testing import asgi_client

from waystation_common.db.collections import REPOSITORIES
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.repos.service import secret_aad
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
API = '/api/v1/repositories'


@pytest.fixture
def settings(settings_factory: Any) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(PASSWORD))  # type: ignore[no-any-return]


@pytest.fixture
async def admin(services: Services) -> httpx.AsyncClient:
    await ensure_bootstrap_admin(services.db, services.settings)
    client = asgi_client(services)
    response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return client


def docker_hub(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        'name': 'docker-hub',
        'format': 'docker',
        'type': 'proxy',
        'online': True,
        'description': 'Docker Hub',
        'proxy': {'upstream_url': 'https://registry-1.docker.io'},
        'format_options': {'aliases': ['docker.io'], 'docker_hub': True, 'default_repo': True},
    }
    body.update(overrides)
    return body


def fields(response: httpx.Response) -> dict[str, str]:
    return {e['field']: e.get('code', '') for e in response.json().get('errors', [])}


async def test_create_get_list_delete(admin: httpx.AsyncClient, client: httpx.AsyncClient) -> None:
    created = await admin.post(API, json=docker_hub())
    assert created.status_code == 201, created.text
    assert created.headers['location'] == '/api/v1/repositories/docker-hub'
    body = created.json()
    assert body['status'] == 'ok'
    assert body['url'] == 'https://registry.example.com/repository/docker-hub/'
    assert body['format_options'] == {'aliases': ['docker.io'], 'docker_hub': True, 'default_repo': True}
    assert body['proxy']['metadata_max_age_s'] == 1800
    assert body['stats']['requests_spark_24h'] == [0] * 24

    assert (await admin.get(f'{API}/docker-hub')).json()['name'] == 'docker-hub'
    # anonymous may browse proxy repositories
    listing = (await client.get(API)).json()
    assert [r['name'] for r in listing['items']] == ['docker-hub']
    assert listing['total'] == 1
    assert listing['next_cursor'] is None
    # /config reports the default docker repository
    assert (await client.get('/api/v1/config')).json()['docker']['default_repo'] == 'docker-hub'

    assert (await client.delete(f'{API}/docker-hub')).status_code == 401
    assert (await admin.delete(f'{API}/docker-hub')).status_code == 204
    assert (await admin.get(f'{API}/docker-hub')).status_code == 404


async def test_anonymous_cannot_create(client: httpx.AsyncClient) -> None:
    assert (await client.post(API, json=docker_hub())).status_code == 401


async def test_name_rules(admin: httpx.AsyncClient) -> None:
    assert (await admin.post(API, json=docker_hub())).status_code == 201
    taken = await admin.post(API, json=docker_hub(name='DOCKER-HUB', format_options={}))
    assert taken.status_code == 409
    assert taken.headers['content-type'] == 'application/problem+json'
    assert fields(taken) == {'name': 'conflict'}
    reserved = await admin.post(API, json=docker_hub(name='V2', format_options={}))
    assert reserved.status_code == 422
    assert fields(reserved) == {'name': 'reserved'}
    alias = await admin.post(API, json=docker_hub(name='Docker.io', format_options={}))
    assert fields(alias) == {'name': 'alias'}
    clash = await admin.post(API, json=docker_hub(name='other', format_options={'aliases': ['DOCKER.IO']}))
    assert fields(clash) == {'format_options.aliases.0': 'taken'}
    bad = await admin.post(API, json=docker_hub(name='-bad'))
    assert bad.status_code == 422
    assert 'name' in fields(bad)


async def test_field_errors(admin: httpx.AsyncClient) -> None:
    response = await admin.post(
        API,
        json={
            'name': 'apt-proxy',
            'format': 'apt',
            'type': 'hosted',
            'online': True,
            'proxy': {
                'upstream_url': 'ftp://user:pw@deb.example.com/debian',
                'auth': {'kind': 'basic'},
            },
            'format_options': {'flat': 'maybe'},
        },
    )
    assert response.status_code == 422
    assert fields(response) == {
        'type': 'unsupported',
        'proxy': 'invalid',  # hosted repositories have no upstream
        'format_options.distribution': 'missing',
        'format_options.flat': 'bool_parsing',
    }
    missing_proxy = await admin.post(API, json={'name': 'raw-x', 'format': 'raw', 'type': 'proxy', 'online': True})
    assert fields(missing_proxy) == {'proxy': 'required'}
    creds = await admin.post(
        API,
        json={
            'name': 'raw-x',
            'format': 'raw',
            'type': 'proxy',
            'online': True,
            'proxy': {'upstream_url': 'https://user:pw@files.example.com/', 'auth': {'kind': 'basic'}},
        },
    )
    assert fields(creds) == {
        'proxy.upstream_url': 'credentials',
        'proxy.auth.username': 'required',
        'proxy.auth.secret': 'required',
    }
    private = await admin.post(
        API,
        json={
            'name': 'raw-x',
            'format': 'raw',
            'type': 'proxy',
            'online': True,
            'proxy': {'upstream_url': 'http://10.1.2.3/files/'},  # forbidden-strings: allow
        },
    )
    assert fields(private) == {'proxy.upstream_url': 'ssrf'}
    unknown = await admin.post(API, json=docker_hub(name='maven-x', format='maven'))
    assert fields(unknown)['format'] == 'unsupported'


async def test_secret_is_write_only_and_encrypted(admin: httpx.AsyncClient, services: Services) -> None:
    body = {
        'name': 'npm-private',
        'format': 'npm',
        'type': 'proxy',
        'online': True,
        'proxy': {
            'upstream_url': 'https://npm.example.com/',
            'auth': {'kind': 'basic', 'username': 'ci', 'secret': 'hunter2'},
        },
    }
    created = await admin.post(API, json=body)
    assert created.status_code == 201, created.text
    auth = created.json()['proxy']['auth']
    assert auth == {'kind': 'basic', 'username': 'ci', 'secret_set': True}
    assert 'hunter2' not in created.text
    doc = await services.db[REPOSITORIES].find_one({'name': 'npm-private'})
    assert doc is not None
    enc = doc['proxy']['auth']['secret_enc']
    assert 'hunter2' not in str(doc)
    assert services.keyring is not None
    assert services.keyring.decrypt_str(enc, aad=secret_aad(doc['_id'])) == 'hunter2'

    # update without the secret keeps it
    body['description'] = 'changed'
    del body['proxy']['auth']['secret']  # type: ignore[index]
    updated = await admin.put(f'{API}/npm-private', json=body)
    assert updated.status_code == 200, updated.text
    doc = await services.db[REPOSITORIES].find_one({'name': 'npm-private'})
    assert doc is not None
    assert doc['proxy']['auth']['secret_enc'] == enc
    assert doc['config_version'] == 2


async def test_update_rules(admin: httpx.AsyncClient) -> None:
    assert (await admin.post(API, json=docker_hub())).status_code == 201
    renamed = await admin.put(f'{API}/docker-hub', json=docker_hub(name='hub2', format='npm', format_options={}))
    assert fields(renamed) == {'name': 'immutable', 'format': 'immutable'}
    offline = await admin.put(f'{API}/docker-hub', json=docker_hub(online=False))
    assert offline.json()['status'] == 'offline'


async def test_default_docker_repo_is_unique(admin: httpx.AsyncClient, services: Services) -> None:
    assert (await admin.post(API, json=docker_hub())).status_code == 201
    second = docker_hub(name='ghcr', format_options={'aliases': ['ghcr.io'], 'default_repo': True})
    second['proxy'] = {'upstream_url': 'https://ghcr.io'}
    assert (await admin.post(API, json=second)).status_code == 201
    hub = (await admin.get(f'{API}/docker-hub')).json()
    assert hub['format_options']['default_repo'] is False


async def test_pagination_and_filters(admin: httpx.AsyncClient) -> None:
    for i in range(5):
        body = {
            'name': f'raw-{i}',
            'format': 'raw',
            'type': 'proxy',
            'online': i != 3,
            'description': 'mirror' if i % 2 else '',
            'proxy': {'upstream_url': 'https://files.example.com/'},
        }
        assert (await admin.post(API, json=body)).status_code == 201
    page1 = (await admin.get(API, params={'limit': 2})).json()
    assert [r['name'] for r in page1['items']] == ['raw-0', 'raw-1']
    assert page1['total'] == 5
    page2 = (await admin.get(API, params={'limit': 2, 'cursor': page1['next_cursor']})).json()
    assert [r['name'] for r in page2['items']] == ['raw-2', 'raw-3']
    assert [r['name'] for r in (await admin.get(API, params={'q': 'MIRROR'})).json()['items']] == ['raw-1', 'raw-3']
    offline = (await admin.get(API, params={'status': 'offline'})).json()
    assert [r['name'] for r in offline['items']] == ['raw-3']
    assert (await admin.get(API, params={'format': 'npm'})).json()['total'] == 0


async def test_setup_guide(admin: httpx.AsyncClient) -> None:
    assert (await admin.post(API, json=docker_hub())).status_code == 201
    guide = (await admin.get(f'{API}/docker-hub/setup')).json()
    assert guide['format'] == 'docker'
    docker = next(c for c in guide['clients'] if c['id'] == 'docker')
    pull = next(s for s in docker['snippets'] if s['id'] == 'docker-pull')
    assert 'registry.example.com/docker-hub/library/nginx:latest' in pull['content']
    assert any(s['id'] == 'docker-mirror' for s in docker['snippets'])


async def test_test_upstream_blocks_private_targets(admin: httpx.AsyncClient) -> None:
    result = await admin.post(
        f'{API}/test-upstream',
        json={'format': 'raw', 'proxy': {'upstream_url': 'http://127.0.0.1:9/'}},
    )
    assert result.status_code == 200
    body = result.json()
    assert body['reachable'] is False
    assert body['checks'] == [{'name': 'ssrf', 'ok': False, 'message': body['checks'][0]['message']}]


async def test_test_upstream_reaches_allowed_upstream(admin: httpx.AsyncClient) -> None:

    async with FakeUpstream({'/v2/': (401, {'WWW-Authenticate': 'Bearer realm="x"'}, b'')}) as upstream:
        result = await admin.post(
            f'{API}/test-upstream',
            json={
                'format': 'docker',
                'proxy': {'upstream_url': upstream.url, 'allow_private_upstream': True},
            },
        )
    body = result.json()
    assert body['reachable'] is True
    assert body['http_status'] == 401
    assert body['auth'] == 'anonymous'
    assert all(c['ok'] for c in body['checks'])


async def test_openapi_documents_format_options(client: httpx.AsyncClient) -> None:
    doc = (await client.get('/api/v1/openapi.json')).json()
    schemas = doc['components']['schemas']
    assert {r['$ref'].rsplit('/', 1)[-1] for r in schemas['FormatOptions']['oneOf']} == {
        'AptOptions',
        'DockerOptions',
        'NpmOptions',
        'RawOptions',
        'YumOptions',
    }
    assert schemas['AptOptions']['required'] == ['distribution']
    assert 'Problem' in schemas


async def test_hidden_repository_is_404_for_anonymous(admin: httpx.AsyncClient, services: Services) -> None:
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': 'hosted-1',
            'name': 'internal',
            'format': 'raw',
            'type': 'hosted',
            'online': True,
            'created_at': '2026-11-08T00:00:00Z',
            'updated_at': '2026-11-08T00:00:00Z',
        }
    )
    services.catalog.invalidate()
    async with asgi_client(services) as anon:
        assert (await anon.get(f'{API}/internal')).status_code == 404
        assert 'internal' not in (await anon.get(API)).text
    assert (await admin.get(f'{API}/internal')).status_code == 200
