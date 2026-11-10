"""API tokens and service accounts (T-S04) against real MongoDB/Valkey: REST endpoints, basic/bearer on format
routes, narrowing, expiry, revocation across replicas, docker login with a token, last used, /metrics."""

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from starlette.responses import PlainTextResponse, Response
from ws_testing import asgi_client

from waystation_common.db import seen
from waystation_common.db.collections import API_TOKENS, REPOSITORIES, USERS
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.base import FormatRequest
from waystation_server.security import passwords
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
SERVICE = 'registry.example.com'


class Echo:
    """Answers with who the router authorised (stands in for a format plugin)."""

    def __init__(self, services: Services, fmt: str) -> None:
        descriptor = services.formats.descriptor(fmt)
        assert descriptor is not None
        self.descriptor = descriptor

    async def handle(self, req: FormatRequest) -> Response:
        return PlainTextResponse(f'{req.method} {req.repo.name}/{req.path} as {req.principal.display}')


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        bootstrap_admin_password=SecretStr(PASSWORD), token_failure_limit=5, metrics_allow_cidrs=['192.0.2.0/24']
    )


async def add_repo(services: Services, name: str, fmt: str, repo_type: str = 'proxy', **options: Any) -> None:
    now = datetime.now(UTC)
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': f'id-{name}',
            'name': name,
            'format': fmt,
            'type': repo_type,
            'online': True,
            'proxy': {'upstream_url': 'https://upstream.example.com/'} if repo_type == 'proxy' else None,
            'format_options': options,
            'created_at': now,
            'updated_at': now,
        }
    )


async def add_user(services: Services, username: str, roles: list[str]) -> None:
    await services.db[USERS].insert_one(
        {
            '_id': f'id-{username}',
            'username': username,
            'display_name': username.title(),
            'source': 'local',
            'password_hash': await passwords.hash_password(PASSWORD),
            'roles': roles,
            'created_at': datetime.now(UTC),
        }
    )


@pytest.fixture
async def world(services: Services) -> None:
    await ensure_bootstrap_admin(services.db, services.settings)
    await add_user(services, 'rita', ['reader'])
    await add_user(services, 'dave', ['deployer'])
    await add_repo(services, 'raw-files', 'raw')
    await add_repo(services, 'raw-private', 'raw', repo_type='hosted')
    await add_repo(services, 'npm-proxy', 'npm')
    await add_repo(services, 'docker-hub', 'docker', aliases=['docker.io'], docker_hub=True, default_repo=True)
    services.catalog.invalidate()
    for fmt in ('raw', 'npm', 'docker'):
        services.formats.set_plugin(Echo(services, fmt))  # type: ignore[arg-type]


async def session(client: httpx.AsyncClient, username: str = 'admin') -> httpx.AsyncClient:
    response = await client.post('/api/v1/auth/login', json={'username': username, 'password': PASSWORD})
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return client


@pytest.fixture
async def admin(services: Services, world: None) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(services) as client:
        yield await session(client)


@pytest.fixture
async def anon(services: Services, world: None) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(services) as client:
        yield client


async def create(client: httpx.AsyncClient, path: str = '/api/v1/auth/me/tokens', **body: Any) -> dict[str, Any]:
    body.setdefault('name', 'ci')
    body.setdefault('scopes', ['read'])
    response = await client.post(path, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def bearer(secret: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {secret}'}


def basic(username: str, secret: str) -> dict[str, str]:
    return {'Authorization': 'Basic ' + base64.b64encode(f'{username}:{secret}'.encode()).decode()}


# ---------------------------------------------------------------------------- REST
async def test_create_shows_secret_once_and_stores_a_keyed_hash(admin: httpx.AsyncClient, services: Services) -> None:
    created = await create(admin, name='laptop', scopes=['read', 'write'], repositories=['raw-*'])
    secret, token = created['secret'], created['token']
    assert secret.startswith('wst_') and token['prefix'] == secret[:12]
    assert token['status'] == 'active' and token['owner']['name'] == 'admin' and token['owner']['kind'] == 'user'
    assert token['scopes'] == ['read', 'write'] and token['repositories'] == ['raw-*']
    doc = await services.db[API_TOKENS].find_one({'_id': token['id']})
    assert doc is not None and secret not in json.dumps(doc, default=str)
    assert doc['hash'] == services.tokens.hasher.hash(secret)
    plan = await services.db[API_TOKENS].find({'hash': {'$in': [doc['hash']]}}).explain()
    assert 'hash_unique' in json.dumps(plan, default=str)  # index-backed lookup (ARCHITECTURE §9)

    listing = (await admin.get('/api/v1/auth/me/tokens')).json()
    assert [t['id'] for t in listing['items']] == [token['id']]
    assert 'secret' not in json.dumps(listing) and secret not in json.dumps(listing)
    assert 'admin' in listing['available_scopes']


async def test_create_validation(admin: httpx.AsyncClient, anon: httpx.AsyncClient) -> None:
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    response = await admin.post(
        '/api/v1/auth/me/tokens', json={'name': 'x', 'scopes': ['read'], 'repositories': ['a b'], 'expires_at': past}
    )
    assert response.status_code == 422
    fields = {e['field'] for e in response.json()['errors']}
    assert fields == {'repositories.0', 'expires_at'}
    assert (await admin.post('/api/v1/auth/me/tokens', json={'name': 'x', 'scopes': []})).status_code == 422
    assert (await admin.post('/api/v1/auth/me/tokens', json={'name': 'x', 'scopes': ['root']})).status_code == 422
    assert (await anon.get('/api/v1/auth/me/tokens')).status_code == 401
    assert (await anon.post('/api/v1/auth/me/tokens', json={'name': 'x', 'scopes': ['read']})).status_code == 401


async def test_scopes_cannot_exceed_the_owner(services: Services, world: None) -> None:
    async with asgi_client(services) as client:
        rita = await session(client, 'rita')
        listing = (await rita.get('/api/v1/auth/me/tokens')).json()
        assert listing['available_scopes'] == ['read']
        response = await rita.post('/api/v1/auth/me/tokens', json={'name': 'x', 'scopes': ['read', 'write']})
        assert response.status_code == 422
        assert response.json()['errors'][0]['field'] == 'scopes.1'
        assert (await rita.get('/api/v1/service-accounts')).status_code == 403
        assert (await rita.get('/api/v1/tokens')).status_code == 403


async def test_tokens_cannot_mint_tokens_and_only_see_their_own(
    admin: httpx.AsyncClient, anon: httpx.AsyncClient, services: Services
) -> None:
    secret = (await create(admin, scopes=['admin']))['secret']
    response = await anon.post('/api/v1/auth/me/tokens', json={'name': 'x', 'scopes': ['read']}, headers=bearer(secret))
    assert response.status_code == 403
    listing = await anon.get('/api/v1/auth/me/tokens', headers=bearer(secret))
    assert listing.status_code == 200 and len(listing.json()['items']) == 1
    other = await create(admin)
    async with asgi_client(services) as dave_client:
        dave = await session(dave_client, 'dave')
        assert (await dave.delete(f'/api/v1/auth/me/tokens/{other["token"]["id"]}')).status_code == 404
        assert (await dave.delete('/api/v1/auth/me/tokens/nope')).status_code == 404


# ---------------------------------------------------------------------------- format routes
async def test_basic_and_bearer_on_format_routes(admin: httpx.AsyncClient, anon: httpx.AsyncClient) -> None:
    secret = (await create(admin, scopes=['read', 'write']))['secret']
    for headers in (bearer(secret), basic('admin', secret), basic('anything', secret), basic('', secret)):
        response = await anon.get('/repository/raw-private/a/b.txt', headers=headers)
        assert response.status_code == 200, response.text
        assert response.text == 'GET raw-private/a/b.txt as admin'
    put = await anon.put('/repository/raw-private/a/b.txt', headers=bearer(secret), content=b'x')
    assert put.status_code == 200
    npm = await anon.get('/repository/npm-proxy/left-pad', headers={'Authorization': f'Token {secret}'})
    assert npm.text.endswith('as admin')
    # anonymous cannot see the hosted repository at all: the challenge, as for a missing one (ADR 0005)
    hidden = await anon.get('/repository/raw-private/a/b.txt')
    missing = await anon.get('/repository/raw-missing/a/b.txt')
    assert hidden.status_code == missing.status_code == 401 and hidden.content == missing.content
    assert hidden.headers['www-authenticate'] == missing.headers['www-authenticate']


async def test_bad_credentials_are_refused_not_downgraded(anon: httpx.AsyncClient, services: Services) -> None:
    wrong = 'wst_' + 'A' * 43
    for headers in (bearer(wrong), basic('admin', wrong), basic('admin', PASSWORD)):
        response = await anon.get('/repository/raw-files/a.txt', headers=headers)
        assert response.status_code == 401, headers
        assert response.headers['www-authenticate'].startswith('Basic realm="Waystation"')
    assert (await anon.get('/api/v1/repositories', headers=bearer(wrong))).status_code == 401
    # too many rejected tokens from one address: 429 (limit 5 per 5 minutes in this test)
    statuses = [(await anon.get('/repository/raw-files/a.txt', headers=bearer(wrong))).status_code for _ in range(4)]
    assert statuses[-1] == 429


async def test_scope_and_repository_narrowing(admin: httpx.AsyncClient, anon: httpx.AsyncClient) -> None:
    read_only = (await create(admin, scopes=['read']))['secret']
    response = await anon.put('/repository/raw-private/a.txt', headers=bearer(read_only), content=b'x')
    assert response.status_code == 403
    assert (await anon.get('/api/v1/service-accounts', headers=bearer(read_only))).status_code == 403
    only_npm = (await create(admin, scopes=['read'], repositories=['format:npm']))['secret']
    assert (await anon.get('/repository/npm-proxy/x', headers=bearer(only_npm))).status_code == 200
    assert (await anon.get('/repository/raw-private/a.txt', headers=bearer(only_npm))).status_code == 404
    names = [r['name'] for r in (await anon.get('/api/v1/repositories', headers=bearer(only_npm))).json()['items']]
    assert names == ['npm-proxy']


async def test_expired_token_is_refused(admin: httpx.AsyncClient, anon: httpx.AsyncClient, services: Services) -> None:
    soon = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    created = await create(admin, expires_at=soon)
    assert (await anon.get('/repository/raw-private/a', headers=bearer(created['secret']))).status_code == 200
    past = datetime.now(UTC) - timedelta(seconds=1)
    await services.db[API_TOKENS].update_one({'_id': created['token']['id']}, {'$set': {'expires_at': past}})
    services.tokens.clear()
    assert (await anon.get('/repository/raw-private/a', headers=bearer(created['secret']))).status_code == 401
    listing = (await admin.get('/api/v1/auth/me/tokens')).json()
    assert listing['items'][0]['status'] == 'expired'


async def test_revoked_token_is_refused_at_once(admin: httpx.AsyncClient, anon: httpx.AsyncClient) -> None:
    created = await create(admin)
    secret, token_id = created['secret'], created['token']['id']
    assert (await anon.get('/repository/raw-private/a', headers=bearer(secret))).status_code == 200
    assert (await admin.delete(f'/api/v1/auth/me/tokens/{token_id}')).status_code == 204
    assert (await anon.get('/repository/raw-private/a', headers=bearer(secret))).status_code == 401
    listing = (await admin.get('/api/v1/auth/me/tokens')).json()
    assert listing['items'][0]['status'] == 'revoked' and listing['items'][0]['revoked_at']


async def test_revocation_reaches_other_replicas_within_a_second(
    admin: httpx.AsyncClient, services: Services, settings: Settings
) -> None:
    created = await create(admin)
    secret = created['secret']
    replica = Services.create(settings)
    replica.formats.set_plugin(Echo(replica, 'raw'))  # type: ignore[arg-type]
    await replica.start()
    try:
        await asyncio.wait_for(replica.tokens.subscribed.wait(), timeout=5)
        async with asgi_client(replica) as other:
            # warm the other replica's cache: it now answers from memory
            assert (await other.get('/repository/raw-private/a', headers=bearer(secret))).status_code == 200
            start = time.monotonic()
            assert (await admin.delete(f'/api/v1/auth/me/tokens/{created["token"]["id"]}')).status_code == 204
            while (await other.get('/repository/raw-private/a', headers=bearer(secret))).status_code == 200:
                assert time.monotonic() - start < 1.0, 'revocation not effective within 1 s'
                await asyncio.sleep(0.01)
            assert time.monotonic() - start < 1.0
    finally:
        await replica.stop()


# ---------------------------------------------------------------------------- docker
def claims(token: str) -> dict[str, Any]:
    body = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))  # type: ignore[no-any-return]


async def test_docker_login_with_an_api_token(admin: httpx.AsyncClient, anon: httpx.AsyncClient) -> None:
    created = await create(admin, scopes=['read'])
    secret = created['secret']
    params = [('service', SERVICE), ('scope', 'repository:docker-hub/library/nginx:pull,push')]
    issued = await anon.get('/auth/token', params=params, headers=basic('ci-bot', secret))
    assert issued.status_code == 200, issued.text
    jwt = issued.json()['token']
    payload = claims(jwt)
    assert payload['tid'] == created['token']['id']
    assert payload['access'][0]['actions'] == ['pull']  # the read-only token narrows the admin's push
    pulled = await anon.get('/v2/docker-hub/library/nginx/manifests/latest', headers=bearer(jwt))
    assert pulled.status_code == 200 and pulled.text.endswith('as admin')
    # API tokens are not registry tokens: /v2/ answers the challenge
    assert (await anon.get('/v2/', headers=bearer(secret))).status_code == 401
    # revoking the API token kills the registry token it minted at once
    assert (await admin.delete(f'/api/v1/auth/me/tokens/{created["token"]["id"]}')).status_code == 204
    after = await anon.get('/v2/docker-hub/library/nginx/manifests/latest', headers=bearer(jwt))
    assert after.status_code == 401 and after.headers['www-authenticate'].startswith('Bearer ')
    refused = await anon.get('/auth/token', params=params, headers=basic('ci-bot', secret))
    assert refused.status_code == 401 and refused.json()['errors'][0]['code'] == 'UNAUTHORIZED'


# ---------------------------------------------------------------------------- service accounts
async def test_service_account_lifecycle(admin: httpx.AsyncClient, anon: httpx.AsyncClient) -> None:
    response = await admin.post('/api/v1/service-accounts', json={'name': 'CI-Bot', 'roles': ['reader']})
    assert response.status_code == 201, response.text
    account = response.json()
    assert account['name'] == 'ci-bot' and account['roles'] == ['reader'] and account['active_tokens'] == 0
    dup = await admin.post('/api/v1/service-accounts', json={'name': 'ci-bot'})
    assert dup.status_code == 409
    bad = await admin.post('/api/v1/service-accounts', json={'name': 'x y', 'roles': ['root']})
    assert bad.status_code == 422

    base = f'/api/v1/service-accounts/{account["id"]}'
    tokens = (await admin.get(f'{base}/tokens')).json()
    assert tokens['available_scopes'] == ['read'] and tokens['items'] == []
    refused = await admin.post(f'{base}/tokens', json={'name': 'push', 'scopes': ['write']})
    assert refused.status_code == 422
    created = await create(admin, f'{base}/tokens', name='pull')
    secret = created['secret']
    assert created['token']['owner'] == {'kind': 'service_account', 'id': account['id'], 'name': 'ci-bot'}
    assert (await anon.get('/repository/raw-private/a', headers=basic('ci-bot', secret))).text.endswith('as ci-bot')
    assert (await anon.put('/repository/raw-private/a', headers=bearer(secret), content=b'x')).status_code == 403

    # roles change and disabling take effect at once
    updated = await admin.patch(base, json={'roles': ['deployer'], 'description': 'pipelines'})
    assert updated.status_code == 200 and updated.json()['active_tokens'] == 1
    assert (await anon.put('/repository/raw-private/a', headers=bearer(secret), content=b'x')).status_code == 403
    assert (await admin.patch(base, json={'disabled': True})).status_code == 200
    assert (await anon.get('/repository/raw-private/a', headers=bearer(secret))).status_code == 401
    assert (await admin.patch(base, json={'disabled': False})).status_code == 200
    assert (await anon.get('/repository/raw-private/a', headers=bearer(secret))).status_code == 200

    all_tokens = (await admin.get('/api/v1/tokens')).json()
    assert [t['owner']['name'] for t in all_tokens] == ['ci-bot']
    assert (await admin.delete(base)).status_code == 204
    assert (await anon.get('/repository/raw-private/a', headers=bearer(secret))).status_code == 401
    assert (await admin.get(f'{base}/tokens')).status_code == 404
    assert (await admin.get('/api/v1/tokens')).json()[0]['status'] == 'revoked'


async def test_admin_revokes_any_token(services: Services, world: None, anon: httpx.AsyncClient) -> None:
    async with asgi_client(services) as client:
        dave = await session(client, 'dave')
        created = await create(dave, scopes=['write'])
        assert (await dave.get('/api/v1/tokens')).status_code == 403
    async with asgi_client(services) as client:
        admin = await session(client)
        assert (await admin.delete(f'/api/v1/tokens/{created["token"]["id"]}')).status_code == 204
        assert (await admin.delete('/api/v1/tokens/unknown')).status_code == 404
    assert (await anon.get('/repository/raw-private/a', headers=bearer(created['secret']))).status_code == 401


# ---------------------------------------------------------------------------- last used, metrics
async def test_last_used_is_batched_in_valkey(
    admin: httpx.AsyncClient, anon: httpx.AsyncClient, services: Services
) -> None:
    created = await create(admin)
    assert created['token']['last_used_at'] is None
    for _ in range(3):
        await anon.get('/repository/raw-private/a', headers=bearer(created['secret']))
    assert await services.valkey.hlen(seen.SEEN_TOKENS) == 1
    doc = await services.db[API_TOKENS].find_one({'_id': created['token']['id']})
    assert doc is not None and doc['last_used_at'] is None  # never written on the request path
    listing = (await admin.get('/api/v1/auth/me/tokens')).json()
    assert listing['items'][0]['last_used_at'] is not None
    assert listing['items'][0]['last_used_ip'] == '127.0.0.1'
    entries = await services.valkey.hgetall(seen.SEEN_TOKENS)
    assert await seen.flush_tokens(services.db, entries) == 1
    doc = await services.db[API_TOKENS].find_one({'_id': created['token']['id']})
    assert doc is not None and doc['last_used_at'] is not None and doc['last_used_ip'] == '127.0.0.1'


async def test_metrics_with_a_metrics_token(admin: httpx.AsyncClient, anon: httpx.AsyncClient) -> None:
    assert (await anon.get('/metrics')).status_code == 404  # 127.0.0.1 is not in the allowed networks here
    metrics = (await create(admin, scopes=['metrics']))['secret']
    reader = (await create(admin, scopes=['read']))['secret']
    assert (await anon.get('/metrics', headers=bearer(reader))).status_code == 404
    assert (await anon.get('/metrics', headers=bearer('wst_' + 'B' * 43))).status_code == 404
    response = await anon.get('/metrics', headers=bearer(metrics))
    assert response.status_code == 200 and 'waystation' in response.text
