"""OIDC sign-in through the real server, MongoDB and Valkey with an in-process provider (``ws_oidc.FakeIdp``, wired
in through the ``security.oidc.Http`` seam) (T-A02/T-A03).

Covers: the admin API for OIDC providers (write-only encrypted client secret, validation, redirect URIs, test
discovery), the browser flow (state, nonce, PKCE, browser binding, one-time state, RFC 9207 ``iss``), provisioning
and the same-username rules through ``link_identity``, group claim → mapped roles, sessions and RP-initiated logout,
API tokens for OIDC users and no password client auth for them. Real providers: ``test_oidc_providers_integration``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, Browser, FakeIdp, query_of
from ws_testing import asgi_client

from waystation_common.db.collections import AUTH_PROVIDERS, SESSIONS, USERS
from waystation_common.ids import new_id
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.security.oidc import OidcClient
from waystation_server.security.passwords import hash_password
from waystation_server.services import Services

pytestmark = pytest.mark.integration

ADMIN_PASSWORD = 'local-admin-password-1'


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(ADMIN_PASSWORD), login_rate_limit=10_000)


@pytest.fixture
def idp(services: Services) -> FakeIdp:
    provider = FakeIdp()
    services.oidc = OidcClient(provider)
    return provider


@pytest.fixture
async def admin(services: Services, client: httpx.AsyncClient) -> dict[str, str]:
    await ensure_bootstrap_admin(services.db, services.settings)
    response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    return {'X-CSRF-Token': response.json()['csrf_token']}


def oidc_body(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        'issuer': ISSUER,
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'scopes': ['openid', 'profile', 'email'],
        'groups_claim': 'groups',
        'auto_provision': True,
    }
    config.update(overrides.pop('oidc', {}))
    body: dict[str, Any] = {'kind': 'oidc', 'name': 'Example SSO', 'enabled': True, 'order': 5, 'oidc': config}
    body.update(overrides)
    return body


async def create(client: httpx.AsyncClient, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post('/api/v1/auth-providers', json=body, headers=headers)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


async def start(c: httpx.AsyncClient, provider_id: str, next_path: str | None = '/repositories') -> dict[str, str]:
    """Begin the flow; returns the authorization request's query."""
    params = {'next': next_path} if next_path else {}
    response = await c.get(f'/auth/oidc/{provider_id}/login', params=params)
    assert response.status_code == 302, response.text
    assert response.headers['cache-control'] == 'no-store'
    return query_of(response.headers['location'])


async def flow(
    services: Services, idp: FakeIdp, provider_id: str, *, claims: dict[str, Any] | None = None
) -> tuple[httpx.Response, Browser]:
    """A whole sign-in in a fresh browser (own cookies); returns the callback response and the browser."""
    c = asgi_client(services)
    auth = await start(c, provider_id)
    code = idp.issue_code(idp.mint(idp.claims(auth['nonce'], **(claims or {}))))
    callback = await c.get(f'/auth/oidc/{provider_id}/callback', params={'code': code, 'state': auth['state']})
    return callback, Browser(c)


def failed(response: httpx.Response, reason: str = 'failed') -> bool:
    return response.status_code == 303 and response.headers['location'] == f'/ui/sign-in?error=oidc_{reason}'


# ---------------------------------------------------------------------------- admin API
async def test_admin_crud_keeps_the_client_secret_write_only(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, idp: FakeIdp
) -> None:
    created = await create(client, admin, oidc_body())
    assert created['kind'] == 'oidc' and created['ldap'] is None
    oidc = created['oidc']
    assert oidc['client_secret_set'] is True and 'client_secret' not in oidc
    assert oidc['redirect_uri'] == f'https://registry.example.com/auth/oidc/{created["id"]}/callback'
    assert oidc['post_logout_redirect_uri'] == 'https://registry.example.com/ui/sign-in'
    assert oidc['signing_algs'][0] == 'RS256' and oidc['client_auth'] == 'client_secret_basic'
    doc = await services.db[AUTH_PROVIDERS].find_one({'_id': created['id']})
    assert doc is not None and CLIENT_SECRET not in json.dumps(doc, default=str)
    assert doc['oidc']['client_secret_enc'] and doc['ldap'] is None
    # an update without the secret keeps it; a public client drops it
    body = oidc_body(name='Renamed')
    del body['oidc']['client_secret']
    updated = await client.put(f'/api/v1/auth-providers/{created["id"]}', json=body, headers=admin)
    assert updated.status_code == 200 and updated.json()['oidc']['client_secret_set'] is True
    public = oidc_body(oidc={'client_auth': 'none', 'client_secret': None})
    updated = await client.put(f'/api/v1/auth-providers/{created["id"]}', json=public, headers=admin)
    assert updated.json()['oidc']['client_secret_set'] is False
    # the sign-in page lists it with its login URL
    providers = (await client.get('/api/v1/auth/providers')).json()
    assert {
        'id': created['id'],
        'kind': 'oidc',
        'name': 'Example SSO',
        'login_url': f'/auth/oidc/{created["id"]}/login',
    } in providers


@pytest.mark.parametrize(
    ('body', 'field'),
    [
        (oidc_body(oidc={'issuer': 'http://idp.example.com'}), 'oidc.issuer'),
        (oidc_body(oidc={'issuer': 'https://idp.example.com/?x=1'}), 'oidc.issuer'),
        (oidc_body(oidc={'client_secret': None}), 'oidc.client_secret'),
        (oidc_body(oidc={'require_group': 'ops', 'groups_claim': None}), 'oidc.require_group'),
        (oidc_body(oidc={'signing_algs': ['RS256', 'RS256']}), 'oidc.signing_algs'),
        (
            oidc_body(
                ldap={'connection': {'servers': ['ldap.example.com']}, 'users': {'base_dn': 'dc=x'}, 'groups': {}}
            ),
            'ldap',
        ),
        ({'kind': 'oidc', 'name': 'No config'}, 'oidc'),
    ],
)
async def test_admin_validation(
    client: httpx.AsyncClient, admin: dict[str, str], body: dict[str, Any], field: str
) -> None:
    response = await client.post('/api/v1/auth-providers', json=body, headers=admin)
    assert response.status_code == 422, response.text
    assert field in {e['field'] for e in response.json()['errors']}


async def test_admin_refuses_hmac_algorithms(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    for alg in ('HS256', 'none'):
        response = await client.post(
            '/api/v1/auth-providers', json=oidc_body(oidc={'signing_algs': [alg]}), headers=admin
        )
        assert response.status_code == 422


async def test_kind_is_immutable_and_test_user_is_ldap_only(
    client: httpx.AsyncClient, admin: dict[str, str], idp: FakeIdp
) -> None:
    created = await create(client, admin, oidc_body())
    ldap = {
        'kind': 'ldap',
        'name': 'Example SSO',
        'ldap': {
            'connection': {'servers': ['ldap.example.com'], 'security': 'starttls'},
            'users': {'base_dn': 'dc=example,dc=com'},
            'groups': {'mode': 'none'},
        },
    }
    response = await client.put(f'/api/v1/auth-providers/{created["id"]}', json=ldap, headers=admin)
    assert response.status_code == 422
    response = await client.post(
        '/api/v1/auth-providers/test-user', json={'provider': oidc_body(), 'username': 'alice'}, headers=admin
    )
    assert response.status_code == 422 and response.json()['errors'][0]['field'] == 'provider.kind'


async def test_test_discovery(client: httpx.AsyncClient, admin: dict[str, str], idp: FakeIdp) -> None:
    created = await create(client, admin, oidc_body())
    body = oidc_body()
    del body['oidc']['client_secret']  # the stored secret is used
    response = await client.post(
        '/api/v1/auth-providers/test-connection', json={'id': created['id'], 'provider': body}, headers=admin
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['ok'] is True, result
    assert result['discovery']['issuer'] == ISSUER and result['discovery']['keys'] == 1
    assert result['discovery']['redirect_uri'].endswith(f'/auth/oidc/{created["id"]}/callback')
    assert {c['name'] for c in result['servers'][0]['checks']} >= {'discovery', 'jwks', 'pkce', 'client'}
    token_call = next(c for c in idp.calls if c[1].endswith('/token'))
    assert token_call[2]['Authorization'].startswith('Basic ')
    idp.discovery_overrides = {'issuer': 'https://other.example.com'}
    broken = (
        await client.post('/api/v1/auth-providers/test-connection', json={'provider': oidc_body()}, headers=admin)
    ).json()
    assert broken['ok'] is False and broken['discovery'] is None
    assert 'other.example.com' in broken['servers'][0]['checks'][0]['message']


# ---------------------------------------------------------------------------- the browser flow
async def test_sign_in_provisions_maps_groups_and_creates_a_session(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, idp: FakeIdp
) -> None:
    provider = await create(
        client, admin, oidc_body(group_mapping=[{'external_group': 'developers', 'roles': ['viewer']}])
    )
    pid = provider['id']
    async with asgi_client(services) as browser:
        auth = await start(browser, pid)
        assert auth['client_id'] == CLIENT_ID and auth['code_challenge_method'] == 'S256'
        assert auth['redirect_uri'] == f'https://registry.example.com/auth/oidc/{pid}/callback'
        assert auth['scope'] == 'openid profile email'
        binding = browser.cookies.get('ws_oidc', path='/auth/oidc/')
        assert binding
        code = idp.issue_code(idp.mint(idp.claims(auth['nonce'])))
        callback = await browser.get(f'/auth/oidc/{pid}/callback', params={'code': code, 'state': auth['state']})
        assert callback.status_code == 303, callback.text
        assert callback.headers['location'] == '/ui/repositories'
        cookies = callback.headers.get_list('set-cookie')
        assert any(c.startswith('ws_session=') and 'HttpOnly' in c for c in cookies)
        assert any(c.startswith(('ws_oidc=""', 'ws_oidc=;')) for c in cookies)
        # PKCE: the verifier sent to the token endpoint matches the challenge
        form = next(c for c in idp.calls if c[1].endswith('/token'))[3]
        digest = hashlib.sha256(form['code_verifier'].encode()).digest()
        assert base64.urlsafe_b64encode(digest).rstrip(b'=').decode() == auth['code_challenge']
        me = (await browser.get('/api/v1/auth/me')).json()
        assert me['user']['username'] == 'alice' and me['user']['source'] == f'oidc:{pid}'
        assert me['user']['display_name'] == 'Alice Example' and me['user']['email'] == 'alice@example.com'
        assert me['capabilities'] == ['stats.read']  # viewer, mapped from the group claim
    user = await services.db[USERS].find_one({'username': 'alice'})
    assert user is not None and user.get('password_hash') is None
    assert [(g['id'], g['name']) for g in user['external_groups']] == [
        ('/developers', 'developers'),
        ('platform', 'platform'),
    ]
    assert user['identities'][0]['provider'] == f'oidc:{pid}' and user['identities'][0]['subject'] == 'sub-alice'
    session = await services.db[SESSIONS].find_one({'user_id': user['_id']})
    assert session is not None and session['provider_id'] == pid and session['id_token_enc']


async def test_mapped_roles_follow_the_group_claim(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, idp: FakeIdp
) -> None:
    provider = await create(client, admin, oidc_body(group_mapping=[{'external_group': 'admins', 'roles': ['admin']}]))
    callback, browser = await flow(services, idp, provider['id'], claims={'groups': ['admins']})
    async with browser:
        assert callback.status_code == 303
        assert 'admin' in (await browser.get('/api/v1/auth/me')).json()['capabilities']
    callback, browser = await flow(services, idp, provider['id'], claims={'groups': []})
    async with browser:
        assert 'admin' not in (await browser.get('/api/v1/auth/me')).json()['capabilities']


async def test_state_is_bound_to_the_browser_and_used_once(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, idp: FakeIdp
) -> None:
    pid = (await create(client, admin, oidc_body()))['id']
    async with asgi_client(services) as victim, asgi_client(services) as attacker:
        auth = await start(attacker, pid)
        code = idp.issue_code(idp.mint(idp.claims(auth['nonce'])))
        # login CSRF: the attacker's callback URL opened in another browser is refused
        hijack = await victim.get(f'/auth/oidc/{pid}/callback', params={'code': code, 'state': auth['state']})
        assert failed(hijack)
        assert 'ws_session' not in victim.cookies
        # the state was consumed by that attempt: replaying it in the right browser fails too
        replay = await attacker.get(f'/auth/oidc/{pid}/callback', params={'code': code, 'state': auth['state']})
        assert failed(replay, 'expired')
        # unknown state, missing state
        assert failed(
            await attacker.get(f'/auth/oidc/{pid}/callback', params={'code': code, 'state': 'nope'}), 'expired'
        )
        assert failed(await attacker.get(f'/auth/oidc/{pid}/callback', params={'code': code}))


async def test_callback_refusals(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, idp: FakeIdp
) -> None:
    pid = (await create(client, admin, oidc_body()))['id']
    other = (await create(client, admin, oidc_body(name='Other SSO')))['id']
    async with asgi_client(services) as browser:
        # the provider's error (user cancelled)
        auth = await start(browser, pid)
        cancelled = await browser.get(
            f'/auth/oidc/{pid}/callback', params={'error': 'access_denied', 'state': auth['state']}
        )
        assert failed(cancelled, 'cancelled')
        # a response delivered to another provider's redirect URI (mix-up)
        auth = await start(browser, pid)
        code = idp.issue_code(idp.mint(idp.claims(auth['nonce'])))
        assert failed(await browser.get(f'/auth/oidc/{other}/callback', params={'code': code, 'state': auth['state']}))
        # RFC 9207: a mismatching iss parameter
        auth = await start(browser, pid)
        code = idp.issue_code(idp.mint(idp.claims(auth['nonce'])))
        mixed = await browser.get(
            f'/auth/oidc/{pid}/callback',
            params={'code': code, 'state': auth['state'], 'iss': 'https://evil.example.com'},
        )
        assert failed(mixed)
        # an ID token for another client
        auth = await start(browser, pid)
        code = idp.issue_code(idp.mint(idp.claims(auth['nonce'], aud='someone-else')))
        assert failed(await browser.get(f'/auth/oidc/{pid}/callback', params={'code': code, 'state': auth['state']}))
        # a nonce from another flow
        auth = await start(browser, pid)
        code = idp.issue_code(idp.mint(idp.claims('another-nonce')))
        assert failed(await browser.get(f'/auth/oidc/{pid}/callback', params={'code': code, 'state': auth['state']}))
        # matching iss is fine
        auth = await start(browser, pid)
        code = idp.issue_code(idp.mint(idp.claims(auth['nonce'])))
        ok = await browser.get(
            f'/auth/oidc/{pid}/callback', params={'code': code, 'state': auth['state'], 'iss': ISSUER}
        )
        assert ok.status_code == 303 and ok.headers['location'] == '/ui/repositories'
    assert await services.db[SESSIONS].count_documents({'provider_id': pid}) == 1


async def test_login_start_failures(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, idp: FakeIdp
) -> None:
    pid = (await create(client, admin, oidc_body()))['id']
    assert failed(await client.get('/auth/oidc/unknown/login'))
    idp.fail = {'/openid-configuration': 500}
    services.oidc.forget()
    assert failed(await client.get(f'/auth/oidc/{pid}/login'), 'unavailable')
    # disabled providers are not offered and cannot be used
    body = oidc_body(enabled=False)
    await client.put(f'/api/v1/auth-providers/{pid}', json=body, headers=admin)
    assert failed(await client.get(f'/auth/oidc/{pid}/login'))
    assert pid not in {p['id'] for p in (await client.get('/api/v1/auth/providers')).json()}


async def test_next_is_kept_inside_the_spa(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, idp: FakeIdp
) -> None:
    pid = (await create(client, admin, oidc_body()))['id']
    for target, expected in (('//evil.example.com/x', '/ui/'), ('https://evil.example.com/', '/ui/'), (None, '/ui/')):
        async with asgi_client(services) as browser:
            auth = await start(browser, pid, target)
            code = idp.issue_code(idp.mint(idp.claims(auth['nonce'])))
            done = await browser.get(f'/auth/oidc/{pid}/callback', params={'code': code, 'state': auth['state']})
            assert done.headers['location'] == expected


# ---------------------------------------------------------------------------- identity linking (SPEC §4.9)
async def test_auto_provision_off_links_existing_password_less_users_only(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, idp: FakeIdp
) -> None:
    pid = (await create(client, admin, oidc_body(oidc={'auto_provision': False})))['id']
    callback, browser = await flow(services, idp, pid)
    await browser.aclose()
    assert failed(callback)
    assert await services.db[USERS].find_one({'username': 'alice'}) is None
    # a local account without a password (created for linking) is linked and takes the provider as source
    now = datetime.now(UTC)
    await services.db[USERS].insert_one(
        {
            '_id': new_id(),
            'username': 'alice',
            'display_name': 'alice',
            'source': 'local',
            'roles': [],
            'created_at': now,
        }
    )
    callback, browser = await flow(services, idp, pid)
    await browser.aclose()
    assert callback.status_code == 303
    user = await services.db[USERS].find_one({'username': 'alice'})
    assert user is not None and user['source'] == f'oidc:{pid}'


async def test_never_links_to_a_local_account_with_a_password(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, idp: FakeIdp
) -> None:
    pid = (await create(client, admin, oidc_body()))['id']
    # an IdP user called "admin" must never become the local administrator
    callback, browser = await flow(services, idp, pid, claims={'preferred_username': 'Admin', 'sub': 'sub-x'})
    await browser.aclose()
    assert failed(callback)
    local = await services.db[USERS].find_one({'username': 'admin'})
    assert local is not None and local['source'] == 'local' and not local.get('identities')
    now = datetime.now(UTC)
    await services.db[USERS].insert_one(
        {
            '_id': new_id(),
            'username': 'bob',
            'display_name': 'bob',
            'source': 'local',
            'password_hash': await hash_password('Bob-Passw0rd-1'),
            'roles': [],
            'created_at': now,
        }
    )
    callback, browser = await flow(services, idp, pid, claims={'preferred_username': 'bob', 'sub': 'sub-bob'})
    await browser.aclose()
    assert failed(callback)


async def test_disabled_users_and_other_providers(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, idp: FakeIdp
) -> None:
    first = (await create(client, admin, oidc_body()))['id']
    second = (await create(client, admin, oidc_body(name='Second SSO')))['id']
    callback, browser = await flow(services, idp, first)
    await browser.aclose()
    assert callback.status_code == 303
    # the same username through another provider links to the same user (identities lists both)
    callback, browser = await flow(services, idp, second, claims={'sub': 'other-sub'})
    await browser.aclose()
    assert callback.status_code == 303
    user = await services.db[USERS].find_one({'username': 'alice'})
    assert user is not None and user['source'] == f'oidc:{first}'
    assert {i['provider'] for i in user['identities']} == {f'oidc:{first}', f'oidc:{second}'}
    await services.db[USERS].update_one({'_id': user['_id']}, {'$set': {'disabled': True}})
    callback, browser = await flow(services, idp, first)
    await browser.aclose()
    assert failed(callback)


# ---------------------------------------------------------------------------- sessions, logout, tokens
async def test_rp_initiated_logout(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, idp: FakeIdp
) -> None:
    pid = (await create(client, admin, oidc_body()))['id']
    _, browser = await flow(services, idp, pid)
    async with browser:
        csrf = (await browser.get('/api/v1/auth/me')).json()['csrf_token']
        out = await browser.post('/api/v1/auth/logout', headers={'X-CSRF-Token': csrf})
        assert out.status_code == 200
        url = out.json()['end_session_url']
        assert url.startswith(f'{ISSUER}/protocol/openid-connect/logout?')
        query = query_of(url)
        assert query['client_id'] == CLIENT_ID
        assert query['post_logout_redirect_uri'] == 'https://registry.example.com/ui/sign-in'
        assert query['id_token_hint'].count('.') == 2
        assert (await browser.get('/api/v1/auth/me')).status_code == 401
    # logout off: local sign-out only
    await client.put(f'/api/v1/auth-providers/{pid}', json=oidc_body(oidc={'rp_logout': False}), headers=admin)
    _, browser = await flow(services, idp, pid)
    async with browser:
        csrf = (await browser.get('/api/v1/auth/me')).json()['csrf_token']
        out = await browser.post('/api/v1/auth/logout', headers={'X-CSRF-Token': csrf})
        assert out.json() == {'end_session_url': None}


async def test_oidc_users_use_api_tokens_not_passwords(
    client: httpx.AsyncClient, admin: dict[str, str], services: Services, idp: FakeIdp
) -> None:
    pid = (
        await create(client, admin, oidc_body(group_mapping=[{'external_group': 'developers', 'roles': ['reader']}]))
    )['id']
    _, browser = await flow(services, idp, pid)
    async with browser:
        csrf = (await browser.get('/api/v1/auth/me')).json()['csrf_token']
        created = await browser.post(
            '/api/v1/auth/me/tokens', json={'name': 'laptop', 'scopes': ['read']}, headers={'X-CSRF-Token': csrf}
        )
        assert created.status_code == 201, created.text
        secret = created.json()['secret']
    async with asgi_client(services) as anon:
        ok = await anon.get('/api/v1/repositories', headers={'Authorization': f'Bearer {secret}'})
        assert ok.status_code == 200
        raw = base64.b64encode(f'alice:{secret}'.encode()).decode()
        docker = await anon.get(
            '/auth/token', params={'service': 'registry.example.com'}, headers={'Authorization': f'Basic {raw}'}
        )
        assert docker.status_code == 200
        # there is no password for an OIDC user: password sign-in and docker login with a password fail
        login = await anon.post('/api/v1/auth/login', json={'username': 'alice', 'password': 'anything'})
        assert login.status_code == 401
        raw = base64.b64encode(b'alice:anything').decode()
        docker = await anon.get(
            '/auth/token', params={'service': 'registry.example.com'}, headers={'Authorization': f'Basic {raw}'}
        )
        assert docker.status_code == 401
