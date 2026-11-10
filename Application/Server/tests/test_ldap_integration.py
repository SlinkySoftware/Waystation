"""LDAP sign-in against real directories (T-A01): an OpenLDAP server with a FreeIPA-style tree (``ldap1.example.com``)
and a Samba Active Directory domain controller (``dc1.example.com``), both from ``make test-integration``
(``Build/Containers/test/``), with a throwaway CA (``WAYSTATION_TEST_LDAP_CA``).

Covers: StartTLS and LDAPS with the CA (and a wrong CA / wrong host name), failover, wrong password, locked and
disabled accounts, the require-group gate, nested groups (memberOf walk and group search), group sync and group →
role mapping, the same-username rules, ``docker login`` (registry token service) with an LDAP password and the bind
cache, the admin API (CRUD, write-only encrypted bind password, validation, RBAC) and the test endpoints.
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import SecretStr
from ws_testing import asgi_client

from waystation_common.db.collections import AUDIT, AUTH_PROVIDERS, GROUPS, USERS
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.security import ldap as ldap_mod
from waystation_server.services import Services

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get('WAYSTATION_TEST_LDAP_CA'), reason='LDAP test directories not running'),
]

ADMIN_PASSWORD = 'local-admin-password-1'
OPENLDAP = os.environ.get('WAYSTATION_TEST_OPENLDAP_HOST', 'ldap1.example.com')
SAMBA = os.environ.get('WAYSTATION_TEST_SAMBA_HOST', 'dc1.example.com')
SVC_PASSWORD = 'Svc-Passw0rd1'
PASSWORDS = {
    'alice': 'Alice-Passw0rd1',
    'bob': 'Bob-Passw0rd1',
    'carol': 'Carol-Passw0rd1',
    'dave': 'Dave-Passw0rd1',
    'erin': 'Erin-Passw0rd1',
    'admin': 'Admin-Passw0rd1',
}
IPA_USERS = 'cn=users,cn=accounts,dc=example,dc=com'
IPA_GROUPS = 'cn=groups,cn=accounts,dc=example,dc=com'
AD_USERS = 'CN=Users,DC=example,DC=com'


def ca_pem() -> str:
    return Path(os.environ['WAYSTATION_TEST_LDAP_CA']).read_text(encoding='utf-8')


def other_ca_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Unrelated test CA')])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def openldap(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        'kind': 'ldap',
        'name': 'Directory',
        'enabled': True,
        'order': 10,
        'ldap': {
            'connection': {
                'servers': [OPENLDAP],
                'security': 'starttls',
                'ca_pem': ca_pem(),
                'bind_dn': 'uid=waystation-svc,cn=sysaccounts,cn=etc,dc=example,dc=com',
                'bind_password': SVC_PASSWORD,
                'connect_timeout_s': 3,
                'timeout_s': 5,
            },
            'users': {
                'base_dn': IPA_USERS,
                'filter': '(objectClass=inetOrgPerson)',
                'username_attribute': 'uid',
                'display_name_attribute': 'displayName',
                'email_attribute': 'mail',
            },
            'groups': {'mode': 'member_of', 'member_of_attribute': 'memberOf', 'nested': False},
            'cache_ttl_s': 60,
        },
        'group_mapping': [],
    }
    return _merge(body, overrides)


def samba(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        'kind': 'ldap',
        'name': 'Active Directory',
        'enabled': True,
        'order': 20,
        'ldap': {
            'connection': {
                'servers': [f'ldaps://{SAMBA}'],
                'security': 'ldaps',
                'ca_pem': ca_pem(),
                'bind_dn': 'CN=waystation-svc,CN=Users,DC=example,DC=com',
                'bind_password': SVC_PASSWORD,
                'connect_timeout_s': 3,
                'timeout_s': 5,
            },
            'users': {
                'base_dn': AD_USERS,
                'filter': '(objectClass=user)',
                'username_attribute': 'sAMAccountName',
                'display_name_attribute': 'displayName',
                'email_attribute': 'mail',
            },
            'groups': {'mode': 'member_of', 'member_of_attribute': 'memberOf', 'nested': False},
            'cache_ttl_s': 60,
        },
        'group_mapping': [],
    }
    return _merge(body, overrides)


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = value
    return base


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(ADMIN_PASSWORD), login_rate_limit=10_000)


@pytest.fixture(autouse=True)
def _fresh_failover_state() -> None:
    ldap_mod.DOWN._until.clear()


@pytest.fixture
async def admin(services: Services, client: httpx.AsyncClient) -> dict[str, str]:
    """Signs the bootstrap admin in on ``client``; returns the CSRF header for unsafe requests."""
    await ensure_bootstrap_admin(services.db, services.settings)
    response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    return {'X-CSRF-Token': response.json()['csrf_token']}


async def create(client: httpx.AsyncClient, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post('/api/v1/auth-providers', json=body, headers=headers)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


async def sign_in(
    services: Services, username: str, password: str | None = None, *, provider: str | None = None
) -> httpx.Response:
    """A fresh client (own cookies) signing in."""

    body: dict[str, Any] = {'username': username, 'password': password or PASSWORDS[username.lower()]}
    if provider:
        body['provider'] = provider
    async with asgi_client(services) as c:
        return await c.post('/api/v1/auth/login', json=body)


def basic(username: str, password: str) -> dict[str, str]:
    return {'Authorization': 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()}


async def audited(services: Services, action: str, **match: Any) -> list[dict[str, Any]]:
    """The audit records of ``action`` matching ``match`` (T-S10), newest first, after flushing the writer."""
    await services.audit.flush()
    cursor = services.db[AUDIT].find({'action': action, **match}, sort=[('ts', -1), ('_id', -1)])
    return [doc async for doc in cursor]


# ---------------------------------------------------------------------------- connection tests
async def test_connection_starttls_and_ldaps(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    for body in (openldap(), openldap(ldap={'connection': {'servers': [f'ldaps://{OPENLDAP}'], 'security': 'ldaps'}})):
        response = await client.post('/api/v1/auth-providers/test-connection', json={'provider': body}, headers=admin)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['ok'] is True, result
        server = result['servers'][0]
        assert server['tls'].startswith('TLSv1.') and f'CN={OPENLDAP}' in server['tls']
        assert {c['name'] for c in server['checks']} == {'connect', 'tls', 'bind', 'user_base'}


async def test_connection_reports_each_server(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    servers = ['waystation-mongodb:389', '192.0.2.1:389', OPENLDAP]  # refused, unroutable (RFC 5737), working
    body = openldap(ldap={'connection': {'servers': servers, 'connect_timeout_s': 1}})
    started = time.monotonic()
    response = await client.post('/api/v1/auth-providers/test-connection', json={'provider': body}, headers=admin)
    assert time.monotonic() - started < 15
    result = response.json()
    assert result['ok'] is False
    assert [s['ok'] for s in result['servers']] == [False, False, True]
    assert result['servers'][0]['checks'][0]['name'] == 'connect'


async def test_connection_refuses_wrong_ca_and_wrong_host_name(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    wrong_ca = openldap(ldap={'connection': {'ca_pem': other_ca_pem()}})
    response = await client.post('/api/v1/auth-providers/test-connection', json={'provider': wrong_ca}, headers=admin)
    assert response.json()['ok'] is False
    assert 'certificate' in response.json()['servers'][0]['checks'][0]['message'].lower()
    # the same server under a name its certificate does not carry (the compose service name)
    wrong_name = openldap(ldap={'connection': {'servers': ['openldap']}})
    response = await client.post('/api/v1/auth-providers/test-connection', json={'provider': wrong_name}, headers=admin)
    assert response.json()['ok'] is False


async def test_connection_service_bind_failures(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    bad_password = openldap(ldap={'connection': {'bind_password': 'not-the-password'}})
    response = await client.post(
        '/api/v1/auth-providers/test-connection', json={'provider': bad_password}, headers=admin
    )
    checks = response.json()['servers'][0]['checks']
    assert response.json()['ok'] is False and any(c['name'] == 'bind' and not c['ok'] for c in checks)
    # Samba refuses simple binds without TLS (strong auth required)
    clear = samba(ldap={'connection': {'servers': [SAMBA], 'security': 'none', 'ca_pem': None}})
    response = await client.post('/api/v1/auth-providers/test-connection', json={'provider': clear}, headers=admin)
    assert response.json()['ok'] is False


async def test_connection_samba_starttls_and_ldaps(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    for body in (samba(), samba(ldap={'connection': {'servers': [SAMBA], 'security': 'starttls'}})):
        response = await client.post('/api/v1/auth-providers/test-connection', json={'provider': body}, headers=admin)
        assert response.json()['ok'] is True, response.json()


# ---------------------------------------------------------------------------- test-user
async def test_user_lookup_shows_groups_and_mapped_roles(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    body = openldap(
        ldap={'groups': {'require_group': 'waystation-users', 'nested': True}},
        group_mapping=[
            {'external_group': f'cn=developers,{IPA_GROUPS}', 'roles': ['deployer']},
            {'external_group': 'platform', 'roles': ['viewer']},
        ],
    )
    request = {'provider': body, 'username': 'alice', 'password': PASSWORDS['alice']}
    result = (await client.post('/api/v1/auth-providers/test-user', json=request, headers=admin)).json()
    assert result['ok'] is True and result['found'] is True and result['password'] == 'ok'
    assert result['user'] == {
        'dn': f'uid=alice,{IPA_USERS}',
        'username': 'alice',
        'display_name': 'Alice Example',
        'email': 'alice@example.com',
    }
    assert {g['name'] for g in result['groups']} == {'developers', 'waystation-users'}
    assert result['require_group_ok'] is True and result['roles'] == ['deployer']

    request = {'provider': body, 'username': 'bob'}
    result = (await client.post('/api/v1/auth-providers/test-user', json=request, headers=admin)).json()
    assert result['password'] == 'not_tested'
    assert {g['name'] for g in result['groups']} == {'ops', 'platform', 'waystation-users'}  # nested
    assert result['roles'] == ['viewer']

    request = {'provider': body, 'username': 'alice', 'password': 'wrong'}
    result = (await client.post('/api/v1/auth-providers/test-user', json=request, headers=admin)).json()
    assert result['ok'] is False and result['password'] == 'failed'

    request = {'provider': body, 'username': 'dave'}
    result = (await client.post('/api/v1/auth-providers/test-user', json=request, headers=admin)).json()
    assert result['found'] is True and result['require_group_ok'] is False and result['ok'] is False

    request = {'provider': body, 'username': 'nobody'}
    result = (await client.post('/api/v1/auth-providers/test-user', json=request, headers=admin)).json()
    assert result['found'] is False and result['user'] is None and result['roles'] == []

    missing = await client.post('/api/v1/auth-providers/test-user', json={'provider': body}, headers=admin)
    assert missing.status_code == 422 and missing.json()['errors'][0]['field'] == 'username'


async def test_user_lookup_reuses_the_stored_bind_password(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    created = await create(client, admin, openldap())
    body = openldap()
    del body['ldap']['connection']['bind_password']
    request = {'id': created['id'], 'provider': body, 'username': 'alice'}
    result = (await client.post('/api/v1/auth-providers/test-user', json=request, headers=admin)).json()
    assert result['found'] is True
    without_id = await client.post('/api/v1/auth-providers/test-user', json={**request, 'id': None}, headers=admin)
    assert without_id.status_code == 422


# ---------------------------------------------------------------------------- sign-in
async def test_sign_in_with_ldap_password(services: Services, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    provider = await create(client, admin, openldap())
    providers = (await client.get('/api/v1/auth/providers')).json()
    assert providers == [
        {'id': 'local', 'kind': 'local', 'name': 'Local account', 'login_url': None},
        {'id': provider['id'], 'kind': 'ldap', 'name': 'Directory', 'login_url': None},
    ]
    response = await sign_in(services, 'Alice')
    assert response.status_code == 200, response.text
    user = response.json()['user']
    assert user['username'] == 'alice' and user['display_name'] == 'Alice Example'
    assert user['email'] == 'alice@example.com' and user['source'] == f'ldap:{provider["id"]}'
    doc = await services.db[USERS].find_one({'username': 'alice'})
    assert doc is not None and doc.get('password_hash') is None
    assert doc['identities'][0]['subject'] == f'uid=alice,{IPA_USERS}'
    assert {g['name'] for g in doc['external_groups']} == {'developers', 'waystation-users'}

    for username, password in (('alice', 'wrong'), ('carol', None), ('nobody', 'whatever')):
        response = await sign_in(services, username, password)
        assert response.status_code == 401
        assert response.json()['detail'] == 'invalid username or password'
    assert await services.db[USERS].find_one({'username': 'carol'}) is None  # locked: never provisioned

    source = f'ldap:{provider["id"]}'
    (login,) = await audited(services, 'auth.login', outcome='success', **{'target.name': 'alice'})
    assert login['principal']['provider'] == source and login['details']['via'] == 'ui'
    assert len(await audited(services, 'user.create', **{'target.name': 'alice', 'details.provider': source})) == 1
    assert len(await audited(services, 'user.link', **{'target.name': 'alice'})) == 1
    (sync,) = await audited(services, 'user.groups_sync', **{'target.name': 'alice'})
    assert sync['changes'][0]['after'] == ['developers', 'waystation-users']
    failed = await audited(services, 'auth.login', outcome='failure')
    assert {d['details']['username']: d['reason'] for d in failed} == {
        'alice': 'bad_credentials',
        'carol': 'bad_credentials',
        'nobody': 'unknown_user',
    }
    assert all(d['details']['provider'] in (source, None) for d in failed)


async def test_require_group_gate(services: Services, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    await create(client, admin, openldap(ldap={'groups': {'require_group': f'CN=waystation-users, {IPA_GROUPS}'}}))
    assert (await sign_in(services, 'alice')).status_code == 200
    assert (await sign_in(services, 'dave')).status_code == 401
    assert await services.db[USERS].find_one({'username': 'dave'}) is None
    (refused,) = await audited(services, 'auth.login', outcome='failure', **{'details.username': 'dave'})
    assert refused['reason'] == 'not_in_group'


async def test_group_sync_maps_roles(services: Services, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    mapping = [
        {'external_group': 'developers', 'roles': ['admin']},
        {'external_group': f'cn=platform,{IPA_GROUPS}', 'roles': ['viewer']},
    ]
    provider = await create(client, admin, openldap(group_mapping=mapping))
    assert provider['group_mapping'] == mapping
    alice = await sign_in(services, 'alice')
    assert 'admin' in alice.json()['capabilities']
    bob = await sign_in(services, 'bob')
    # platform is only reached through ops (nested off): no role, only the anonymous grants every user receives
    assert bob.json()['capabilities'] == ['repositories.browse']

    body = openldap(group_mapping=mapping, ldap={'groups': {'nested': True}})
    del body['ldap']['connection']['bind_password']  # keep the stored one
    response = await client.put(f'/api/v1/auth-providers/{provider["id"]}', json=body, headers=admin)
    assert response.status_code == 200, response.text
    bob = await sign_in(services, 'bob')
    assert 'stats.read' in bob.json()['capabilities']

    # a mapping change applies to existing sessions on the next request (no new sign-in needed)

    async with asgi_client(services) as c:
        login = await c.post('/api/v1/auth/login', json={'username': 'bob', 'password': PASSWORDS['bob']})
        assert 'stats.read' in login.json()['capabilities']
        body['group_mapping'] = []
        response = await client.put(f'/api/v1/auth-providers/{provider["id"]}', json=body, headers=admin)
        assert response.status_code == 200
        services.groups.invalidate()
        assert (await c.get('/api/v1/auth/me')).json()['capabilities'] == ['repositories.browse']  # anonymous only
    assert await services.db[GROUPS].count_documents({'provider_id': provider['id']}) == 0


async def test_group_search_mode_with_nesting(
    services: Services, client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    groups = {
        'mode': 'search',
        'base_dn': IPA_GROUPS,
        'filter': '(objectClass=groupOfNames)',
        'member_attribute': 'member',
        'member_value': 'dn',
        'name_attribute': 'cn',
        'nested': True,
    }
    await create(client, admin, openldap(ldap={'groups': groups}))
    assert (await sign_in(services, 'bob')).status_code == 200
    doc = await services.db[USERS].find_one({'username': 'bob'})
    assert doc is not None
    assert {g['name'] for g in doc['external_groups']} == {'ops', 'platform', 'waystation-users'}
    assert {g['id'] for g in doc['external_groups']} >= {f'cn=platform,{IPA_GROUPS}'}


async def test_failover_to_the_next_server(
    services: Services, client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    body = openldap(ldap={'connection': {'servers': ['waystation-mongodb:389', OPENLDAP], 'connect_timeout_s': 2}})
    await create(client, admin, body)
    assert (await sign_in(services, 'alice')).status_code == 200
    started = time.monotonic()
    assert (await sign_in(services, 'bob')).status_code == 200  # the dead server is skipped now
    assert time.monotonic() - started < 2


async def test_unavailable_directory_fails_closed(
    services: Services, client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    await create(client, admin, openldap(ldap={'connection': {'servers': ['waystation-mongodb:389']}}))
    assert (await sign_in(services, 'alice')).status_code == 401
    (failed,) = await audited(services, 'auth.login', outcome='failure', **{'details.username': 'alice'})
    assert failed['reason'] == 'directory_unavailable'


async def test_samba_sign_in_disabled_and_nested(
    services: Services, client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    mapping = [{'external_group': 'platform', 'roles': ['viewer']}]
    provider = await create(client, admin, samba(ldap={'groups': {'nested': True}}, group_mapping=mapping))
    alice = await sign_in(services, 'alice')
    assert alice.status_code == 200, alice.text
    assert alice.json()['user']['source'] == f'ldap:{provider["id"]}'
    assert alice.json()['user']['display_name'] == 'Alice Example'
    assert (await sign_in(services, 'carol')).status_code == 401  # disabled in AD
    assert (await sign_in(services, 'alice', 'Wrong-Passw0rd1')).status_code == 401
    bob = await sign_in(services, 'bob')
    assert 'stats.read' in bob.json()['capabilities']
    doc = await services.db[USERS].find_one({'username': 'bob'})
    assert doc is not None
    assert {g['name'] for g in doc['external_groups']} == {'ops', 'platform', 'waystation-users'}


# ---------------------------------------------------------------------------- same-username rules
async def test_local_password_is_never_bypassed(
    services: Services, client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    provider = await create(client, admin, openldap())
    # the directory also has an "admin" account: its password never signs the local admin in
    assert (await sign_in(services, 'admin', PASSWORDS['admin'])).status_code == 401
    assert (await sign_in(services, 'admin', PASSWORDS['admin'], provider=provider['id'])).status_code == 401
    assert (await sign_in(services, 'admin', ADMIN_PASSWORD, provider=provider['id'])).status_code == 200
    local = await sign_in(services, 'admin', ADMIN_PASSWORD)
    assert local.status_code == 200 and local.json()['user']['source'] == 'local'
    # and an LDAP user cannot use the local form
    assert (await sign_in(services, 'alice', provider='local')).status_code == 401


async def test_links_password_less_local_account_and_other_providers(
    services: Services, client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    now = datetime.now(UTC)
    await services.db[USERS].insert_one(
        {'_id': 'u-erin', 'username': 'erin', 'display_name': 'erin', 'source': 'local', 'roles': ['reader'],
         'created_at': now}
    )  # fmt: skip
    ipa = await create(client, admin, openldap())
    ad = await create(client, admin, samba())
    erin = await sign_in(services, 'erin')
    assert erin.status_code == 200
    assert erin.json()['user']['id'] == 'u-erin' and erin.json()['user']['source'] == f'ldap:{ipa["id"]}'
    doc = await services.db[USERS].find_one({'_id': 'u-erin'})
    assert doc is not None and doc['roles'] == ['reader'] and doc['display_name'] == 'Erin Example'

    # alice signs in through the directory, then through AD: one Waystation user with both identities
    first = await sign_in(services, 'alice')
    second = await sign_in(services, 'alice', provider=ad['id'])
    assert second.status_code == 200 and second.json()['user']['id'] == first.json()['user']['id']
    doc = await services.db[USERS].find_one({'username': 'alice'})
    assert doc is not None and doc['source'] == f'ldap:{ipa["id"]}'
    assert {i['provider'] for i in doc['identities']} == {f'ldap:{ipa["id"]}', f'ldap:{ad["id"]}'}
    providers = {g['provider'] for g in doc['external_groups']}
    assert providers == {f'ldap:{ipa["id"]}', f'ldap:{ad["id"]}'}

    # a disabled Waystation user stays out whatever the directory says
    assert (await sign_in(services, 'bob')).status_code == 200
    await services.db[USERS].update_one({'username': 'bob'}, {'$set': {'disabled': True}})
    assert (await sign_in(services, 'bob')).status_code == 401


# ---------------------------------------------------------------------------- docker login
async def test_docker_login_with_ldap_password_uses_the_bind_cache(
    services: Services, client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    await create(client, admin, openldap())
    response = await client.get(
        '/auth/token', params={'service': 'registry.example.com'}, headers=basic('alice', PASSWORDS['alice'])
    )
    assert response.status_code == 200, response.text
    assert response.json()['token']
    assert len(services.ldap.cache._entries) == 1
    # cached: the directory is not asked again (even when it became unreachable)
    ldap_mod.DOWN.mark(ldap_mod.parse_server(OPENLDAP, ldap_mod.LdapSecurity.STARTTLS))
    response = await client.get(
        '/auth/token', params={'service': 'registry.example.com'}, headers=basic('alice', PASSWORDS['alice'])
    )
    assert response.status_code == 200
    wrong = await client.get('/auth/token', params={'service': 'registry.example.com'}, headers=basic('alice', 'nope'))
    assert wrong.status_code == 401


async def test_ldap_user_api_token_carries_mapped_roles(
    services: Services, client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    """Format clients use API tokens (T-S04): an LDAP user's token narrows its effective roles, groups included."""
    mapping = [{'external_group': 'developers', 'roles': ['viewer']}]
    provider = await create(client, admin, openldap(group_mapping=mapping))
    async with asgi_client(services) as alice:
        login = await alice.post('/api/v1/auth/login', json={'username': 'alice', 'password': PASSWORDS['alice']})
        csrf = {'X-CSRF-Token': login.json()['csrf_token']}
        body = {'name': 'ci', 'scopes': ['stats-read']}
        created = await alice.post('/api/v1/auth/me/tokens', json=body, headers=csrf)
        assert created.status_code == 201, created.text
        secret = created.json()['secret']
    async with asgi_client(services) as script:
        bearer = {'Authorization': f'Bearer {secret}'}
        assert (await script.get('/api/v1/stats/summary', headers=bearer)).status_code == 200
        # LDAP passwords are not accepted as basic credentials by format clients and the API
        assert (
            await script.get('/api/v1/stats/summary', headers=basic('alice', PASSWORDS['alice']))
        ).status_code == 401
    body = openldap(group_mapping=[])
    del body['ldap']['connection']['bind_password']
    assert (await client.put(f'/api/v1/auth-providers/{provider["id"]}', json=body, headers=admin)).status_code == 200
    await services.tokens.announce('*')
    async with asgi_client(services) as script:
        assert (await script.get('/api/v1/stats/summary', headers=bearer)).status_code == 403


# ---------------------------------------------------------------------------- admin API
async def test_admin_api_crud_and_secrets(services: Services, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    created = await create(client, admin, openldap(group_mapping=[{'external_group': 'ops', 'roles': ['reader']}]))
    connection = created['ldap']['connection']
    assert connection['bind_password_set'] is True and 'bind_password' not in connection
    doc = await services.db[AUTH_PROVIDERS].find_one({'_id': created['id']})
    assert doc is not None
    stored = doc['ldap']['connection']
    assert 'bind_password' not in stored and SVC_PASSWORD not in str(doc) and stored['bind_password_enc']
    group = await services.db[GROUPS].find_one({'provider_id': created['id']})
    assert group is not None and group['roles'] == ['reader'] and group['source'] == 'ldap'

    listing = (await client.get('/api/v1/auth-providers', headers=admin)).json()
    assert [p['id'] for p in listing] == [created['id']]
    assert (await client.get(f'/api/v1/auth-providers/{created["id"]}')).json()['name'] == 'Directory'

    duplicate = await client.post('/api/v1/auth-providers', json=openldap(name='directory'), headers=admin)
    assert duplicate.status_code == 409

    bad = openldap(
        ldap={
            'connection': {'servers': ['ldaps://ldap1.example.com', 'ftp://x'], 'security': 'starttls'},
            'users': {'filter': 'objectClass=person', 'username_attribute': 'uid)(x'},
            'groups': {'mode': 'search', 'base_dn': None},
        },
        group_mapping=[{'external_group': 'a', 'roles': ['root']}, {'external_group': 'A', 'roles': []}],
    )
    response = await client.post('/api/v1/auth-providers', json=bad, headers=admin)
    assert response.status_code == 422
    fields = {e['field'] for e in response.json()['errors']}
    assert fields == {
        'ldap.connection.servers.0',
        'ldap.connection.servers.1',
        'ldap.users.filter',
        'ldap.users.username_attribute',
        'ldap.groups.base_dn',
        'group_mapping.0.roles',
        'group_mapping.1.external_group',
    }

    body = openldap(name='Renamed', enabled=False)
    del body['ldap']['connection']['bind_password']
    updated = (await client.put(f'/api/v1/auth-providers/{created["id"]}', json=body, headers=admin)).json()
    assert updated['name'] == 'Renamed' and updated['ldap']['connection']['bind_password_set'] is True
    after = await services.db[AUTH_PROVIDERS].find_one({'_id': created['id']})
    assert after is not None and after['ldap']['connection']['bind_password_enc'] == stored['bind_password_enc']
    assert after['config_version'] == 2
    assert await services.db[GROUPS].count_documents({'provider_id': created['id']}) == 0
    services.auth_providers.invalidate()
    assert [p['id'] for p in (await client.get('/api/v1/auth/providers')).json()] == ['local']  # disabled

    body['ldap']['connection']['bind_dn'] = None
    cleared = (await client.put(f'/api/v1/auth-providers/{created["id"]}', json=body, headers=admin)).json()
    assert cleared['ldap']['connection']['bind_password_set'] is False

    assert (await client.delete(f'/api/v1/auth-providers/{created["id"]}', headers=admin)).status_code == 204
    assert (await client.get(f'/api/v1/auth-providers/{created["id"]}')).status_code == 404


async def test_admin_api_needs_settings_admin(
    services: Services, client: httpx.AsyncClient, admin: dict[str, str]
) -> None:

    await create(client, admin, openldap(group_mapping=[{'external_group': 'developers', 'roles': ['deployer']}]))
    async with asgi_client(services) as anonymous:
        assert (await anonymous.get('/api/v1/auth-providers')).status_code == 401
        assert (await anonymous.get('/api/v1/roles')).status_code == 401
        login = await anonymous.post('/api/v1/auth/login', json={'username': 'alice', 'password': PASSWORDS['alice']})
        csrf = {'X-CSRF-Token': login.json()['csrf_token']}
        assert (await anonymous.get('/api/v1/auth-providers')).status_code == 403
        assert (await anonymous.post('/api/v1/auth-providers', json=openldap(), headers=csrf)).status_code == 403
        roles = (await anonymous.get('/api/v1/roles')).json()
        assert {r['id'] for r in roles} >= {'admin', 'reader', 'deployer', 'viewer'}
