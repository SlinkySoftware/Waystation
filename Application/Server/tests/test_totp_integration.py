"""Two-factor sign-in (T-O08) against real MongoDB/Valkey: enrolment, the two-step sign-in, replay protection (Valkey
and the MongoDB fallback), recovery codes, lockout, disable, the administrator's reset, docker login refusal and the
"require 2FA for local administrators" policy."""

import base64
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_testing import asgi_client

from waystation_common.db.collections import AUDIT, LOGIN_CHALLENGES, USERS
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.security import passwords, second_factor, totp
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
API = '/api/v1'
T0 = 1_800_000_000.0  # a fixed clock: steps are T0 // 30 (+n)


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000, login_lockout_failures=4
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [T0]
    monkeypatch.setattr(second_factor, 'clock', lambda: now[0])
    return now


async def add_user(services: Services, username: str, roles: list[str]) -> str:
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
    return f'id-{username}'


@pytest.fixture
async def world(services: Services) -> None:
    await ensure_bootstrap_admin(services.db, services.settings)
    await add_user(services, 'rita', ['reader'])


def code(secret: str, at: float, steps: int = 0) -> str:
    return totp.hotp(totp.secret_bytes(secret), totp.step_at(at) + steps)


async def password_step(client: httpx.AsyncClient, username: str, password: str = PASSWORD) -> httpx.Response:
    return await client.post(f'{API}/auth/login', json={'username': username, 'password': password})


async def sign_in(services: Services, username: str) -> httpx.AsyncClient:
    client = asgi_client(services)
    response = await password_step(client, username)
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return client


async def sign_in_2fa(services: Services, username: str, second: str) -> httpx.AsyncClient:
    client = asgi_client(services)
    first = await password_step(client, username)
    assert first.status_code == 202, first.text
    done = await client.post(f'{API}/auth/login/totp', json={'code': second})
    assert done.status_code == 200, done.text
    client.headers['X-CSRF-Token'] = done.json()['csrf_token']
    return client


async def enrol(client: httpx.AsyncClient, at: float) -> tuple[str, list[str]]:
    started = await client.post(f'{API}/auth/me/totp/setup', json={'password': PASSWORD})
    assert started.status_code == 200, started.text
    secret = started.json()['secret']
    confirmed = await client.post(f'{API}/auth/me/totp/confirm', json={'code': code(secret, at)})
    assert confirmed.status_code == 200, confirmed.text
    return secret, confirmed.json()['recovery_codes']


@pytest.fixture
async def rita(services: Services, world: None) -> AsyncIterator[httpx.AsyncClient]:
    client = await sign_in(services, 'rita')
    try:
        yield client
    finally:
        await client.aclose()


async def audit_records(services: Services, **match: Any) -> list[dict[str, Any]]:
    await services.audit.flush()
    return [doc async for doc in services.db[AUDIT].find(match, sort=[('ts', -1), ('_id', -1)])]


# ---------------------------------------------------------------------------- enrolment
async def test_enrolment(services: Services, rita: httpx.AsyncClient, clock: list[float]) -> None:
    state = (await rita.get(f'{API}/auth/me/totp')).json()
    assert state == {
        'available': True,
        'enabled': False,
        'enabled_at': None,
        'required': False,
        'recovery_codes_left': 0,
    }

    wrong = await rita.post(f'{API}/auth/me/totp/setup', json={'password': 'not-it'})
    assert wrong.status_code == 422 and wrong.json()['errors'][0]['field'] == 'password'
    no_setup = await rita.post(f'{API}/auth/me/totp/confirm', json={'code': '123456'})
    assert no_setup.status_code == 409

    started = await rita.post(f'{API}/auth/me/totp/setup', json={'password': PASSWORD})
    assert started.status_code == 200, started.text
    body = started.json()
    secret = body['secret']
    assert len(secret) == 32 and body['account'] == 'rita'
    assert body['otpauth_uri'].startswith('otpauth://totp/') and f'secret={secret}' in body['otpauth_uri']
    doc = await services.db[USERS].find_one({'_id': 'id-rita'})
    assert doc is not None and doc.get('totp_enc') is None
    assert doc['totp_pending_enc'].startswith('ws1.') and secret not in doc['totp_pending_enc']

    bad = await rita.post(f'{API}/auth/me/totp/confirm', json={'code': code(secret, T0, 3)})
    assert bad.status_code == 422 and bad.json()['errors'][0]['field'] == 'code'
    confirmed = await rita.post(f'{API}/auth/me/totp/confirm', json={'code': code(secret, T0)})
    assert confirmed.status_code == 200, confirmed.text
    codes = confirmed.json()['recovery_codes']
    assert len(codes) == 10 == len(set(codes))

    state = (await rita.get(f'{API}/auth/me/totp')).json()
    assert state['enabled'] is True and state['recovery_codes_left'] == 10 and state['enabled_at']
    doc = await services.db[USERS].find_one({'_id': 'id-rita'})
    assert doc is not None and doc['totp_pending_enc'] is None and doc['totp_enc'].startswith('ws1.')
    stored = repr(doc)
    assert secret not in stored and not any(c in stored or c.replace('-', '') in stored for c in codes)
    assert (await rita.post(f'{API}/auth/me/totp/setup', json={'password': PASSWORD})).status_code == 409
    listed = (await rita.get(f'{API}/auth/me')).json()
    assert listed['totp_setup_required'] is False

    records = await audit_records(services, action='auth.totp_enable')
    assert records and records[0]['target']['id'] == 'id-rita' and records[0]['details'] == {'recovery_codes': 10}


async def test_enrolment_needs_a_session_and_a_local_password(services: Services, world: None) -> None:
    await services.db[USERS].insert_one(
        {
            '_id': 'id-dora',
            'username': 'dora',
            'display_name': 'Dora',
            'source': 'ldap:corp',
            'roles': ['reader'],
            'created_at': datetime.now(UTC),
        }
    )
    session = await services.sessions.create('id-dora', ip=None, user_agent=None)
    async with asgi_client(services) as dora:
        dora.cookies.set('ws_session', session[0])
        dora.headers['X-CSRF-Token'] = session[1].csrf
        assert (await dora.get(f'{API}/auth/me/totp')).json()['available'] is False
        refused = await dora.post(f'{API}/auth/me/totp/setup', json={'password': PASSWORD})
        assert refused.status_code == 409


# ---------------------------------------------------------------------------- sign-in
async def test_two_step_sign_in_and_replay(services: Services, rita: httpx.AsyncClient, clock: list[float]) -> None:
    secret, _ = await enrol(rita, T0)
    async with asgi_client(services) as client:
        first = await password_step(client, 'rita')
        assert first.status_code == 202, first.text
        assert first.json()['second_factor'] == 'totp'
        assert 'ws_session' not in first.cookies and first.cookies.get('ws_mfa')
        cookie = first.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'samesite=strict' in cookie and 'path=/api/v1/auth/login' in cookie
        assert (await client.get(f'{API}/auth/me')).status_code == 401  # a pending sign-in is not a session

        # The enrolment code (step T0) was used up by the confirmation: replaying it fails.
        replay = await client.post(f'{API}/auth/login/totp', json={'code': code(secret, T0)})
        assert replay.status_code == 401 and replay.json()['code'] == 'unauthorized'
        done = await client.post(f'{API}/auth/login/totp', json={'code': code(secret, T0, 1)})
        assert done.status_code == 200, done.text
        assert done.json()['user']['username'] == 'rita'
        assert client.cookies.get('ws_session')
        assert (await client.get(f'{API}/auth/me')).status_code == 200
        # The pending sign-in is spent.
        again = await client.post(f'{API}/auth/login/totp', json={'code': code(secret, T0, 1)})
        assert again.status_code == 401 and again.json()['code'] == 'second_factor_expired'

    async with asgi_client(services) as other:
        assert (await password_step(other, 'rita')).status_code == 202
        reused = await other.post(f'{API}/auth/login/totp', json={'code': code(secret, T0, 1)})
        assert reused.status_code == 401  # the same code twice: refused (once per step per user)
        older = await other.post(f'{API}/auth/login/totp', json={'code': code(secret, T0)})
        assert older.status_code == 401  # an earlier step after a later one: refused too
    clock[0] = T0 + 90
    async with asgi_client(services) as later:
        assert (await password_step(later, 'rita')).status_code == 202
        assert (await later.post(f'{API}/auth/login/totp', json={'code': code(secret, T0 + 90)})).status_code == 200

    successes = await audit_records(services, action='auth.login', outcome='success', **{'target.id': 'id-rita'})
    assert successes[0]['details']['second_factor'] == 'totp'
    failures = await audit_records(services, action='auth.login', outcome='failure', reason='bad_second_factor')
    assert failures and failures[0]['details']['step'] == 'second_factor'


async def test_replay_is_refused_without_valkey(
    services: Services, rita: httpx.AsyncClient, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    secret, _ = await enrol(rita, T0)

    async def down(*args: object, **kwargs: object) -> None:
        raise ConnectionError('valkey down')

    monkeypatch.setattr(services.valkey, 'set', down)
    await (await sign_in_2fa(services, 'rita', code(secret, T0, 1))).aclose()
    async with asgi_client(services) as client:
        assert (await password_step(client, 'rita')).status_code == 202
        assert (await client.post(f'{API}/auth/login/totp', json={'code': code(secret, T0, 1)})).status_code == 401


async def test_recovery_codes_are_single_use(services: Services, rita: httpx.AsyncClient, clock: list[float]) -> None:
    secret, codes = await enrol(rita, T0)
    client = await sign_in_2fa(services, 'rita', codes[0].upper().replace('-', ' '))
    state = (await client.get(f'{API}/auth/me/totp')).json()
    assert state['recovery_codes_left'] == 9
    async with asgi_client(services) as other:
        assert (await password_step(other, 'rita')).status_code == 202
        assert (await other.post(f'{API}/auth/login/totp', json={'code': codes[0]})).status_code == 401
    records = await audit_records(
        services, action='auth.login', outcome='success', **{'details.second_factor': 'recovery_code'}
    )
    assert records

    # New recovery codes need a code from the app; the old ones stop working.
    refused = await client.post(f'{API}/auth/me/totp/recovery-codes', json={'code': codes[1]})
    assert refused.status_code == 422
    fresh = await client.post(f'{API}/auth/me/totp/recovery-codes', json={'code': code(secret, T0, 1)})
    assert fresh.status_code == 200, fresh.text
    new_codes = fresh.json()['recovery_codes']
    assert len(new_codes) == 10 and not set(new_codes) & set(codes)
    async with asgi_client(services) as other:
        assert (await password_step(other, 'rita')).status_code == 202
        assert (await other.post(f'{API}/auth/login/totp', json={'code': codes[1]})).status_code == 401
        assert (await other.post(f'{API}/auth/login/totp', json={'code': new_codes[0]})).status_code == 200
    assert await audit_records(services, action='auth.totp_recovery_codes', outcome='success')
    await client.aclose()


async def test_wrong_codes_lock_the_account(services: Services, rita: httpx.AsyncClient, clock: list[float]) -> None:
    """Wrong codes count towards the lockout, and a correct password does not reset the count while the second
    factor is pending (a known password does not buy unlimited guesses)."""
    secret, _ = await enrol(rita, T0)
    wrong = code(secret, T0, 5)
    async with asgi_client(services) as attacker:
        for _ in range(2):
            assert (await password_step(attacker, 'rita')).status_code == 202
            failed = await attacker.post(f'{API}/auth/login/totp', json={'code': wrong})
            assert failed.status_code == 401
        doc = await services.db[USERS].find_one({'_id': 'id-rita'})
        assert doc is not None and doc['failed_logins'] == 2
        assert (await password_step(attacker, 'rita')).status_code == 202
        for _ in range(2):
            assert (await attacker.post(f'{API}/auth/login/totp', json={'code': wrong})).status_code == 401
        # Locked now: even the right password and code are refused.
        locked = await password_step(attacker, 'rita')
        assert locked.status_code == 401
    lockouts = await audit_records(services, action='auth.lockout', **{'target.id': 'id-rita'})
    assert lockouts and lockouts[0]['details']['failures'] == 4


async def test_a_pending_sign_in_ends_after_a_few_wrong_codes(
    services: Services, rita: httpx.AsyncClient, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(second_factor, 'MAX_ATTEMPTS', 2)  # below the lockout threshold (4)
    secret, _ = await enrol(rita, T0)
    async with asgi_client(services) as client:
        assert (await password_step(client, 'rita')).status_code == 202
        for _ in range(second_factor.MAX_ATTEMPTS):
            assert (await client.post(f'{API}/auth/login/totp', json={'code': '000000'})).status_code == 401
        ended = await client.post(f'{API}/auth/login/totp', json={'code': code(secret, T0, 1)})
        assert ended.status_code == 401 and ended.json()['code'] == 'second_factor_expired'
    assert await services.db[LOGIN_CHALLENGES].count_documents({}) == 0


async def test_docker_login_with_the_password_is_refused(
    services: Services, rita: httpx.AsyncClient, clock: list[float]
) -> None:
    await enrol(rita, T0)
    basic = 'Basic ' + base64.b64encode(f'rita:{PASSWORD}'.encode()).decode()
    async with asgi_client(services) as docker:
        refused = await docker.get('/auth/token', headers={'Authorization': basic})
        assert refused.status_code == 401
    records = await audit_records(services, action='auth.login', outcome='failure', reason='totp_required')
    assert records and records[0]['details']['via'] == 'docker'
    # API tokens remain the way for clients.
    made = await rita.post(f'{API}/auth/me/tokens', json={'name': 'docker', 'scopes': ['read']})
    assert made.status_code == 201, made.text
    token = made.json()['secret']
    async with asgi_client(services) as docker:
        auth = 'Basic ' + base64.b64encode(f'rita:{token}'.encode()).decode()
        assert (await docker.get('/auth/token', headers={'Authorization': auth})).status_code == 200


# ---------------------------------------------------------------------------- turning it off
async def test_disable(services: Services, rita: httpx.AsyncClient, clock: list[float]) -> None:
    _, codes = await enrol(rita, T0)
    wrong_password = await rita.post(f'{API}/auth/me/totp/disable', json={'password': 'nope', 'code': codes[0]})
    assert wrong_password.status_code == 422 and wrong_password.json()['errors'][0]['field'] == 'password'
    wrong_code = await rita.post(f'{API}/auth/me/totp/disable', json={'password': PASSWORD, 'code': '000000'})
    assert wrong_code.status_code == 422 and wrong_code.json()['errors'][0]['field'] == 'code'
    off = await rita.post(f'{API}/auth/me/totp/disable', json={'password': PASSWORD, 'code': codes[0]})
    assert off.status_code == 204, off.text
    assert (await rita.get(f'{API}/auth/me/totp')).json()['enabled'] is False
    await (await sign_in(services, 'rita')).aclose()  # one step again
    records = await audit_records(services, action='auth.totp_disable', outcome='success')
    assert records[0]['details'] == {'by_admin': False, 'with': 'recovery_code'}


async def test_api_tokens_cannot_manage_two_factor(services: Services, rita: httpx.AsyncClient) -> None:
    made = await rita.post(f'{API}/auth/me/tokens', json={'name': 'script', 'scopes': ['read']})
    token = made.json()['secret']
    async with asgi_client(services) as script:
        script.headers['Authorization'] = f'Bearer {token}'
        assert (await script.get(f'{API}/auth/me/totp')).status_code == 200
        refused = await script.post(f'{API}/auth/me/totp/setup', json={'password': PASSWORD})
        assert refused.status_code == 403


async def test_administrator_reset(services: Services, rita: httpx.AsyncClient, clock: list[float]) -> None:
    await enrol(rita, T0)
    admin = await sign_in(services, 'admin')
    detail = (await admin.get(f'{API}/users/id-rita')).json()
    assert detail['totp_enabled'] is True
    listed = (await admin.get(f'{API}/users', params={'q': 'rita'})).json()['items']
    assert listed[0]['totp_enabled'] is True

    # A user-admin (not an administrator) cannot reset an administrator's.
    await add_user(services, 'uma', [])
    made = await admin.post(
        f'{API}/roles',
        json={'id': 'user-managers', 'name': 'User managers', 'permissions': [{'action': 'user-admin', 'scope': '*'}]},
    )
    assert made.status_code == 201, made.text
    assert (await admin.patch(f'{API}/users/id-uma', json={'roles': ['user-managers']})).status_code == 200
    uma = await sign_in(services, 'uma')
    admin_id = (await admin.get(f'{API}/auth/me')).json()['user']['id']
    assert (await uma.delete(f'{API}/users/{admin_id}/totp')).status_code == 403
    assert (await admin.delete(f'{API}/users/{admin_id}/totp')).status_code == 409  # not one's own

    reset = await uma.delete(f'{API}/users/id-rita/totp')
    assert reset.status_code == 204, reset.text
    assert (await uma.delete(f'{API}/users/id-rita/totp')).status_code == 409  # nothing left to reset
    assert (await uma.delete(f'{API}/users/id-nobody/totp')).status_code == 404
    await (await sign_in(services, 'rita')).aclose()
    records = await audit_records(services, action='auth.totp_disable', **{'details.by_admin': True})
    assert records[0]['principal']['name'] == 'uma' and records[0]['target']['id'] == 'id-rita'
    for client in (admin, uma):
        await client.aclose()


# ---------------------------------------------------------------------------- policy
async def test_required_for_local_administrators(services: Services, world: None, clock: list[float]) -> None:
    admin = await sign_in(services, 'admin')
    current = (await admin.get(f'{API}/settings')).json()
    assert current['security'] == {'require_totp_for_admins': False}
    saved = await admin.put(
        f'{API}/settings',
        json={'general': current['general'], 'security': {'require_totp_for_admins': True}},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()['security'] == {'require_totp_for_admins': True}
    changed = await audit_records(services, action='settings.update', **{'target.id': 'security'})
    assert changed[0]['changes'] == [
        {'field': 'require_totp_for_admins', 'before': False, 'after': True, 'redacted': False}
    ]

    # The administrator's session may now only enrol (or sign out); a reader is not affected.
    me = (await admin.get(f'{API}/auth/me')).json()
    assert me['totp_setup_required'] is True
    blocked = await admin.get(f'{API}/users')
    assert blocked.status_code == 403 and blocked.json()['code'] == 'totp_setup_required'
    assert (await admin.get(f'{API}/auth/me/totp')).json()['required'] is True
    rita = await sign_in(services, 'rita')
    assert (await rita.get(f'{API}/auth/me')).json()['totp_setup_required'] is False
    assert (await rita.get(f'{API}/repositories')).status_code == 200

    basic = 'Basic ' + base64.b64encode(f'admin:{PASSWORD}'.encode()).decode()
    async with asgi_client(services) as docker:
        assert (await docker.get('/auth/token', headers={'Authorization': basic})).status_code == 401
    assert await audit_records(services, action='auth.login', reason='totp_setup_required')

    await enrol(admin, T0)
    assert (await admin.get(f'{API}/users')).status_code == 200
    assert (await admin.get(f'{API}/auth/me')).json()['totp_setup_required'] is False
    # Leaving the policy off again needs no second factor; the PUT without `security` keeps it.
    kept = await admin.put(f'{API}/settings', json={'general': current['general']})
    assert kept.json()['security'] == {'require_totp_for_admins': True}
    for client in (admin, rita):
        await client.aclose()
