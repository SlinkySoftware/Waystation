"""OIDC sign-in against real identity providers (T-A02): Keycloak (``make test-integration``) and authentik
(``make test-integration-authentik``, opt-in: it needs PostgreSQL plus a server and a worker and takes a few minutes to
start). Both run from ``Build/Containers/compose.test.yaml`` with committed throwaway configuration
(``Build/Containers/test/keycloak/waystation-realm.json``, ``Build/Containers/test/authentik/waystation.yaml``).

The test is the browser: it starts the flow at Waystation (ASGI client), follows the provider's login form (Keycloak)
or flow executor API (authentik) with a real HTTP client, and hands the redirect back to Waystation's callback.
The providers live on the compose network (private addresses, plain http), so the provider config sets
``allow_private``.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from ws_oidc import Browser, query_of
from ws_testing import asgi_client

from waystation_common.db.collections import USERS
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.security import oidc
from waystation_server.services import Services

pytestmark = pytest.mark.integration

ADMIN_PASSWORD = 'local-admin-password-1'
KEYCLOAK = os.environ.get('WAYSTATION_TEST_KEYCLOAK_URL', '')
AUTHENTIK = os.environ.get('WAYSTATION_TEST_AUTHENTIK_URL', '')
AUTHENTIK_TOKEN = os.environ.get('WAYSTATION_TEST_AUTHENTIK_TOKEN', '')
CLIENT_SECRET = 'waystation-test-client-secret'
PASSWORDS = {'alice': 'Alice-Passw0rd1', 'carol': 'Carol-Passw0rd1', 'admin': 'Admin-Passw0rd1'}
KEYCLOAK_ADMIN_PASSWORD = 'Keycloak-Admin-Passw0rd1'
keycloak = pytest.mark.skipif(not KEYCLOAK, reason='Keycloak test provider not running')
authentik = pytest.mark.skipif(
    not AUTHENTIK, reason='authentik test provider not running (make test-integration-authentik)'
)

Driver = Callable[[httpx.AsyncClient, str, str, str], Awaitable[str | None]]


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(ADMIN_PASSWORD), login_rate_limit=10_000)


@pytest.fixture
async def admin(services: Services, client: httpx.AsyncClient) -> dict[str, str]:
    await ensure_bootstrap_admin(services.db, services.settings)
    response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    return {'X-CSRF-Token': response.json()['csrf_token']}


def provider_body(issuer: str, name: str, **oidc_overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        'issuer': issuer,
        'client_id': 'waystation',
        'client_secret': CLIENT_SECRET,
        'scopes': ['openid', 'profile', 'email'],
        'groups_claim': 'groups',
        'allow_private': True,
        'timeout_s': 10,
    }
    config.update(oidc_overrides)
    return {
        'kind': 'oidc',
        'name': name,
        'order': 1,
        'oidc': config,
        'group_mapping': [{'external_group': 'ops', 'roles': ['viewer']}],
    }


async def create(client: httpx.AsyncClient, headers: dict[str, str], body: dict[str, Any]) -> str:
    response = await client.post('/api/v1/auth-providers', json=body, headers=headers)
    assert response.status_code == 201, response.text
    provider_id: str = response.json()['id']
    return provider_id


async def sign_in(
    services: Services, provider_id: str, driver: Driver, username: str, password: str
) -> tuple[httpx.Response | None, Browser, Browser]:
    """Start at Waystation, log in at the provider, deliver the redirect to Waystation. Returns the callback response
    (None when the provider did not send the browser back) and both clients (caller closes them)."""
    browser = asgi_client(services)
    idp = httpx.AsyncClient(timeout=20)
    start = await browser.get(f'/auth/oidc/{provider_id}/login', params={'next': '/browse'})
    assert start.status_code == 302, start.text
    back = await driver(idp, start.headers['location'], username, password)
    if back is None:
        return None, Browser(browser), Browser(idp)
    parts = urlsplit(back)
    assert parts.netloc == 'registry.example.com', back
    return await browser.get(f'{parts.path}?{parts.query}'), Browser(browser), Browser(idp)


# ---------------------------------------------------------------------------- Keycloak
_KC_FORM = re.compile(r'<form[^>]*\bid="kc-form-login"[^>]*>', re.S)
_ACTION = re.compile(r'\baction="([^"]+)"')


async def keycloak_login(idp: httpx.AsyncClient, url: str, username: str, password: str) -> str | None:
    page = await idp.get(url)
    if page.status_code in (302, 303):  # already signed in at the provider (SSO session)
        return page.headers['location']
    assert page.status_code == 200, page.text[:500]
    form = _KC_FORM.search(page.text)
    assert form is not None, page.text[:500]
    action = _ACTION.search(form.group(0))
    assert action is not None
    done = await idp.post(
        html.unescape(action.group(1)), data={'username': username, 'password': password, 'credentialId': ''}
    )
    return done.headers['location'] if done.status_code in (302, 303) else None


async def keycloak_admin_token(idp: httpx.AsyncClient) -> str:
    response = await idp.post(
        f'{KEYCLOAK}/realms/master/protocol/openid-connect/token',
        data={
            'grant_type': 'password',
            'client_id': 'admin-cli',
            'username': 'admin',
            'password': KEYCLOAK_ADMIN_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    token: str = response.json()['access_token']
    return token


def keycloak_issuer() -> str:
    return f'{KEYCLOAK}/realms/waystation'


@keycloak
async def test_keycloak_discovery_and_client_check(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    body = provider_body(keycloak_issuer(), 'Keycloak')
    result = (
        await client.post('/api/v1/auth-providers/test-connection', json={'provider': body}, headers=admin)
    ).json()
    assert result['ok'] is True, result
    discovery = result['discovery']
    assert discovery['issuer'] == keycloak_issuer() and discovery['keys'] >= 1
    assert discovery['end_session_endpoint'].endswith('/protocol/openid-connect/logout')
    wrong = provider_body(keycloak_issuer(), 'Keycloak', client_secret='not-the-secret')
    result = (
        await client.post('/api/v1/auth-providers/test-connection', json={'provider': wrong}, headers=admin)
    ).json()
    assert result['ok'] is False
    assert next(c for c in result['servers'][0]['checks'] if c['name'] == 'client')['ok'] is False
    # the SSRF guard: a private provider without allow_private is refused (the issuer must then be https too)
    private = provider_body(keycloak_issuer().replace('http://', 'https://'), 'Keycloak', allow_private=False)
    result = (
        await client.post('/api/v1/auth-providers/test-connection', json={'provider': private}, headers=admin)
    ).json()
    assert result['ok'] is False and 'private' in result['servers'][0]['checks'][0]['message']


@keycloak
async def test_keycloak_sign_in_groups_and_logout(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services
) -> None:
    pid = await create(client, admin, provider_body(keycloak_issuer(), 'Keycloak'))
    callback, browser, idp = await sign_in(services, pid, keycloak_login, 'alice', PASSWORDS['alice'])
    async with browser, idp:
        assert callback is not None and callback.status_code == 303, callback and callback.text
        assert callback.headers['location'] == '/ui/browse'
        me = (await browser.get('/api/v1/auth/me')).json()
        assert me['user']['username'] == 'alice' and me['user']['source'] == f'oidc:{pid}'
        assert me['user']['email'] == 'alice@example.com' and me['user']['display_name'] == 'Alice Example'
        assert me['capabilities'] == ['stats.read']  # /platform/ops → "ops" → viewer
        user = await services.db[USERS].find_one({'username': 'alice'})
        assert user is not None
        assert {g['id'] for g in user['external_groups']} == {'/developers', '/platform/ops'}
        # RP-initiated logout: Keycloak ends its session and sends the browser back to the sign-in page
        out = await browser.post('/api/v1/auth/logout', headers={'X-CSRF-Token': me['csrf_token']})
        url = out.json()['end_session_url']
        assert url.startswith(f'{keycloak_issuer()}/protocol/openid-connect/logout?') and 'id_token_hint=' in url
        bye = await idp.get(url)
        assert bye.status_code == 302 and bye.headers['location'] == 'https://registry.example.com/ui/sign-in'
        # the provider session is gone: signing in again shows the login form
        again = await browser.get(f'/auth/oidc/{pid}/login')
        assert (await idp.get(again.headers['location'])).status_code == 200


@keycloak
async def test_keycloak_wrong_password_and_same_username_rules(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services
) -> None:
    pid = await create(client, admin, provider_body(keycloak_issuer(), 'Keycloak'))
    callback, browser, idp = await sign_in(services, pid, keycloak_login, 'alice', 'not-the-password')
    async with browser, idp:
        assert callback is None
    # Keycloak's "admin" is not Waystation's local administrator (local password: never linked)
    callback, browser, idp = await sign_in(services, pid, keycloak_login, 'admin', PASSWORDS['admin'])
    async with browser, idp:
        assert callback is not None and callback.headers['location'] == '/ui/sign-in?error=oidc_failed'
    local = await services.db[USERS].find_one({'username': 'admin'})
    assert local is not None and local['source'] == 'local' and not local.get('identities')
    # auto-provision off: an unknown user is refused
    body = provider_body(keycloak_issuer(), 'Keycloak', auto_provision=False)
    await client.put(f'/api/v1/auth-providers/{pid}', json=body, headers=admin)
    callback, browser, idp = await sign_in(services, pid, keycloak_login, 'carol', PASSWORDS['carol'])
    async with browser, idp:
        assert callback is not None and callback.headers['location'] == '/ui/sign-in?error=oidc_failed'
    assert await services.db[USERS].find_one({'username': 'carol'}) is None


@keycloak
async def test_keycloak_public_client_with_userinfo(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services
) -> None:
    body = provider_body(
        keycloak_issuer(), 'Keycloak public', client_id='waystation-public', client_auth='none', client_secret=None
    )
    body['oidc']['use_userinfo'] = True
    pid = await create(client, admin, body)
    callback, browser, idp = await sign_in(services, pid, keycloak_login, 'carol', PASSWORDS['carol'])
    async with browser, idp:
        assert callback is not None and callback.status_code == 303, callback and callback.text
        assert callback.headers['location'] == '/ui/browse'
        assert (await browser.get('/api/v1/auth/me')).json()['user']['username'] == 'carol'


@keycloak
async def test_keycloak_key_rotation_refreshes_jwks(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oidc, 'JWKS_MIN_REFRESH_S', 0.0)
    pid = await create(client, admin, provider_body(keycloak_issuer(), 'Keycloak'))
    callback, browser, idp = await sign_in(services, pid, keycloak_login, 'carol', PASSWORDS['carol'])
    async with browser, idp:
        assert callback is not None and callback.status_code == 303
        # Rotate: a new RSA key with a higher priority becomes the realm's signing key.
        token = await keycloak_admin_token(idp)
        auth = {'Authorization': f'Bearer {token}'}
        realm = (await idp.get(f'{KEYCLOAK}/admin/realms/waystation', headers=auth)).json()
        component = {
            'name': 'rotated-rsa',
            'providerId': 'rsa-generated',
            'providerType': 'org.keycloak.keys.KeyProvider',
            'parentId': realm['id'],
            'config': {'priority': ['500'], 'keySize': ['2048'], 'algorithm': ['RS256'], 'active': ['true']},
        }
        added = await idp.post(f'{KEYCLOAK}/admin/realms/waystation/components', json=component, headers=auth)
        assert added.status_code == 201, added.text
    counting = CountingHttp(services.oidc._http)
    services.oidc._http = counting
    callback, browser, idp = await sign_in(services, pid, keycloak_login, 'carol', PASSWORDS['carol'])
    async with browser, idp:
        assert callback is not None and callback.status_code == 303, callback and callback.headers
    assert counting.urls.count(f'{keycloak_issuer()}/protocol/openid-connect/certs') == 1  # the new key id


class CountingHttp:
    def __init__(self, inner: oidc.Http) -> None:
        self.inner = inner
        self.urls: list[str] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> oidc.HttpResult:
        self.urls.append(url)
        return await self.inner.request(method, url, **kwargs)


# ---------------------------------------------------------------------------- authentik
async def authentik_login(idp: httpx.AsyncClient, url: str, username: str, password: str) -> str | None:
    """Drive authentik's flow executor API like its web UI does."""
    location: str | None = url
    for _ in range(20):
        assert location is not None
        if location.startswith('https://registry.example.com/'):
            return location
        target = urljoin(AUTHENTIK + '/', location)
        parts = urlsplit(target)
        match = re.match(r'^/if/flow/([^/]+)/?$', parts.path)
        if match:
            location = await _run_flow(idp, match.group(1), parse_qs(parts.query), username, password)
            continue
        response = await idp.get(target)
        if response.status_code in (301, 302, 303):
            location = response.headers['location']
            continue
        return None
    raise AssertionError('authentik: too many redirects')


async def _run_flow(
    idp: httpx.AsyncClient, slug: str, query: dict[str, list[str]], username: str, password: str
) -> str | None:
    api = f'{AUTHENTIK}/api/v3/flows/executor/{slug}/?' + urlencode(
        {'query': urlencode({k: v[0] for k, v in query.items()})}
    )
    challenge = (await idp.get(api, follow_redirects=True)).json()
    for _ in range(10):
        component = challenge.get('component')
        if component == 'xak-flow-redirect':
            to: str = challenge['to']
            return to
        headers = {'X-authentik-CSRF': idp.cookies.get('authentik_csrf') or ''}
        if component == 'ak-stage-identification':
            body: dict[str, Any] = {'component': component, 'uid_field': username}
            if challenge.get('password_fields'):
                body['password'] = password
        elif component == 'ak-stage-password':
            body = {'component': component, 'password': password}
        elif component == 'ak-stage-consent':
            body = {'component': component, 'token': challenge.get('token', '')}
        else:  # access denied, wrong password...
            return None
        answered = await idp.post(api, json=body, headers=headers, follow_redirects=True)
        challenge = answered.json()
        if challenge.get('response_errors'):
            return None
    raise AssertionError('authentik: flow did not finish')


async def authentik_ready() -> str:
    """Wait for authentik and apply the committed blueprint (default flows and keys must exist first)."""
    headers = {'Authorization': f'Bearer {AUTHENTIK_TOKEN}'}
    async with httpx.AsyncClient(base_url=AUTHENTIK, headers=headers, timeout=20) as api:
        for _ in range(120):
            try:
                found = await api.get('/api/v3/managed/blueprints/', params={'path': 'waystation/waystation.yaml'})
                if found.status_code == 200 and found.json()['results']:
                    blueprint = found.json()['results'][0]
                    if blueprint['status'] != 'successful':
                        await api.post(f'/api/v3/managed/blueprints/{blueprint["pk"]}/apply/')
                    app = await api.get('/api/v3/core/applications/waystation/')
                    if app.status_code == 200:
                        return f'{AUTHENTIK}/application/o/waystation/'
            except httpx.HTTPError:
                pass
            await asyncio.sleep(5)
    raise AssertionError('authentik did not become ready')


@authentik
async def test_authentik_discovery_sign_in_and_logout(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services
) -> None:
    issuer = await authentik_ready()
    body = provider_body(issuer, 'authentik')
    result = (
        await client.post('/api/v1/auth-providers/test-connection', json={'provider': body}, headers=admin)
    ).json()
    assert result['ok'] is True, result
    assert result['discovery']['issuer'] == issuer
    pid = await create(client, admin, body)
    callback, browser, idp = await sign_in(services, pid, authentik_login, 'alice', PASSWORDS['alice'])
    async with browser, idp:
        assert callback is not None and callback.status_code == 303, callback and callback.text
        assert callback.headers['location'] == '/ui/browse'
        me = (await browser.get('/api/v1/auth/me')).json()
        assert me['user']['username'] == 'alice' and me['user']['email'] == 'alice@example.com'
        assert me['capabilities'] == ['stats.read']  # authentik group "ops" → viewer
        out = await browser.post('/api/v1/auth/logout', headers={'X-CSRF-Token': me['csrf_token']})
        url = out.json()['end_session_url']
        assert url and url.startswith(f'{issuer}end-session/') and query_of(url)['client_id'] == 'waystation'
    callback, browser, idp = await sign_in(services, pid, authentik_login, 'alice', 'not-the-password')
    async with browser, idp:
        assert callback is None
