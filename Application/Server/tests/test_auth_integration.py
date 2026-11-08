from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from ws_testing import asgi_client

from waystation_common.db.client import Database
from waystation_common.db.collections import SESSIONS, USERS
from waystation_common.settings import Settings
from waystation_server.bootstrap import banner, ensure_bootstrap_admin
from waystation_server.security import passwords
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(PASSWORD), login_lockout_failures=3, login_rate_limit=50)


@pytest.fixture
async def admin(services: Services) -> None:
    result = await ensure_bootstrap_admin(services.db, services.settings)
    assert result.created


async def login(client: httpx.AsyncClient, username: str = 'admin', password: str = PASSWORD) -> httpx.Response:
    return await client.post('/api/v1/auth/login', json={'username': username, 'password': password})


async def test_bootstrap_generated_password_is_idempotent(
    settings_factory: Callable[..., Settings], services: Services
) -> None:
    settings = settings_factory()
    first = await ensure_bootstrap_admin(services.db, settings)
    assert first.created and first.generated_password
    assert first.generated_password in banner(first.generated_password)
    doc = await services.db[USERS].find_one({'username': 'admin'})
    assert doc is not None and doc['must_change_password'] is True and doc['roles'] == ['admin']
    assert await passwords.verify_password(doc['password_hash'], first.generated_password)
    second = await ensure_bootstrap_admin(services.db, settings)
    assert not second.created and second.generated_password is None
    assert await services.db[USERS].count_documents({}) == 1


@pytest.mark.usefixtures('admin')
async def test_login_me_logout(client: httpx.AsyncClient, services: Services) -> None:
    response = await login(client)
    assert response.status_code == 200
    body = response.json()
    assert body['user']['username'] == 'admin'
    assert 'admin' in body['capabilities']
    cookies = response.headers.get_list('set-cookie')
    session_cookie = next(c for c in cookies if c.startswith('ws_session='))
    assert 'HttpOnly' in session_cookie and 'Secure' in session_cookie and 'samesite=lax' in session_cookie.lower()
    csrf_cookie = next(c for c in cookies if c.startswith('ws_csrf='))
    assert 'HttpOnly' not in csrf_cookie

    me = await client.get('/api/v1/auth/me')
    assert me.status_code == 200
    assert me.json()['csrf_token'] == body['csrf_token']
    # the session id is stored hashed
    token = client.cookies['ws_session']
    assert await services.db[SESSIONS].find_one({'_id': token}) is None

    assert (await client.post('/api/v1/auth/logout')).status_code == 403  # no CSRF header
    out = await client.post('/api/v1/auth/logout', headers={'X-CSRF-Token': body['csrf_token']})
    assert out.status_code == 200
    assert out.json() == {'end_session_url': None}
    assert await services.db[SESSIONS].count_documents({}) == 0
    assert (await client.get('/api/v1/auth/me')).status_code == 401


async def test_logout_without_session(client: httpx.AsyncClient) -> None:
    out = await client.post('/api/v1/auth/logout')
    assert out.status_code == 200
    assert out.json() == {'end_session_url': None}


@pytest.mark.usefixtures('admin')
async def test_bad_credentials_are_generic(client: httpx.AsyncClient) -> None:
    wrong = await login(client, password='nope')
    unknown = await login(client, username='ghost')
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()['detail'] == unknown.json()['detail']
    assert wrong.headers['content-type'] == 'application/problem+json'


@pytest.mark.usefixtures('admin')
async def test_lockout(client: httpx.AsyncClient) -> None:
    for _ in range(3):
        assert (await login(client, password='nope')).status_code == 401
    assert (await login(client)).status_code == 401  # locked, even with the right password


@pytest.mark.usefixtures('admin')
async def test_login_rate_limit(settings_factory: Callable[..., Settings], services: Services) -> None:
    services.settings = settings_factory(login_rate_limit=2)
    async with asgi_client(services) as client:
        assert (await login(client)).status_code == 200
        assert (await login(client)).status_code == 200
        limited = await login(client)
    assert limited.status_code == 429
    assert int(limited.headers['retry-after']) > 0


@pytest.mark.usefixtures('admin')
async def test_login_replaces_existing_session(client: httpx.AsyncClient, services: Services) -> None:
    await login(client)
    first = client.cookies['ws_session']
    await login(client)
    assert client.cookies['ws_session'] != first
    assert await services.db[SESSIONS].count_documents({}) == 1


@pytest.mark.usefixtures('admin')
async def test_session_expiry_and_idle(client: httpx.AsyncClient, services: Services, mongo_db: Database) -> None:
    await login(client)
    await mongo_db[SESSIONS].update_many({}, {'$set': {'expires_at': datetime.now(UTC) - timedelta(seconds=1)}})
    assert (await client.get('/api/v1/auth/me')).status_code == 401

    await login(client)
    services.sessions.idle_s = -1
    assert (await client.get('/api/v1/auth/me')).status_code == 401


@pytest.mark.usefixtures('admin')
async def test_preferences(client: httpx.AsyncClient) -> None:
    csrf = (await login(client)).json()['csrf_token']
    prefs = {'theme': 'dark', 'density': 'compact'}
    assert (await client.put('/api/v1/auth/me/preferences', json=prefs)).status_code == 403
    saved = await client.put('/api/v1/auth/me/preferences', json=prefs, headers={'X-CSRF-Token': csrf})
    assert saved.json() == prefs
    assert (await client.get('/api/v1/auth/me')).json()['preferences'] == prefs


async def test_config_and_providers(client: httpx.AsyncClient) -> None:
    config = (await client.get('/api/v1/config')).json()
    assert config['docker'] == {'registry_host': 'registry.example.com', 'default_repo': None}
    assert config['instance_name'] == 'Waystation'
    providers = (await client.get('/api/v1/auth/providers')).json()
    assert providers[0]['kind'] == 'local'
