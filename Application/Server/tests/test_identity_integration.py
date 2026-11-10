"""Identity and RBAC administration (T-S02/T-S03) against real MongoDB/Valkey: users, groups, custom roles, service
accounts, effective permissions, own sessions, password change and the one-time password, the grant cache and its
invalidation across replicas, and the role matrix of the admin API."""

import asyncio
import base64
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from starlette.responses import PlainTextResponse, Response
from ws_testing import asgi_client

from waystation_common.db.collections import GROUPS, REPOSITORIES, USERS
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.base import FormatRequest
from waystation_server.security import passwords
from waystation_server.security.rbac import grants_key
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
API = '/api/v1'


class Echo:
    def __init__(self, services: Services, fmt: str) -> None:
        descriptor = services.formats.descriptor(fmt)
        assert descriptor is not None
        self.descriptor = descriptor

    async def handle(self, req: FormatRequest) -> Response:
        return PlainTextResponse(f'{req.method} {req.repo.name}/{req.path} as {req.principal.display}')


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000)


async def add_user(services: Services, username: str, roles: list[str], **fields: Any) -> str:
    doc = {
        '_id': f'id-{username}',
        'username': username,
        'display_name': username.title(),
        'source': 'local',
        'password_hash': await passwords.hash_password(PASSWORD),
        'roles': roles,
        'created_at': datetime.now(UTC),
        **fields,
    }
    await services.db[USERS].insert_one(doc)
    return str(doc['_id'])


async def add_repo(services: Services, name: str, fmt: str, repo_type: str = 'proxy') -> None:
    now = datetime.now(UTC)
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': f'id-{name}',
            'name': name,
            'format': fmt,
            'type': repo_type,
            'online': True,
            'proxy': {'upstream_url': 'https://upstream.example.com/'} if repo_type == 'proxy' else None,
            'format_options': {},
            'created_at': now,
            'updated_at': now,
        }
    )


async def sign_in(services: Services, username: str, password: str = PASSWORD) -> httpx.AsyncClient:
    client = asgi_client(services)
    response = await client.post(f'{API}/auth/login', json={'username': username, 'password': password})
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return client


@pytest.fixture
async def world(services: Services) -> None:
    await ensure_bootstrap_admin(services.db, services.settings)
    await add_user(services, 'rita', ['reader'])
    await add_user(services, 'uma', [])  # gets user-admin through a custom role in the tests that need it
    await add_repo(services, 'npm-proxy', 'npm')
    await add_repo(services, 'npm-hosted', 'npm', 'hosted')
    await add_repo(services, 'raw-hosted', 'raw', 'hosted')
    services.catalog.invalidate()
    for fmt in ('raw', 'npm'):
        services.formats.set_plugin(Echo(services, fmt))  # type: ignore[arg-type]


@pytest.fixture
async def admin(services: Services, world: None) -> AsyncIterator[httpx.AsyncClient]:
    client = await sign_in(services, 'admin')
    try:
        yield client
    finally:
        await client.aclose()


async def user_admin(services: Services, admin: httpx.AsyncClient) -> httpx.AsyncClient:
    """``uma`` with a custom role holding only ``user-admin`` (not an administrator)."""
    made = await admin.post(
        f'{API}/roles',
        json={'id': 'user-managers', 'name': 'User managers', 'permissions': [{'action': 'user-admin', 'scope': '*'}]},
    )
    assert made.status_code == 201, made.text
    assert (await admin.patch(f'{API}/users/id-uma', json={'roles': ['user-managers']})).status_code == 200
    return await sign_in(services, 'uma')


def basic(username: str, password: str) -> dict[str, str]:
    return {'Authorization': 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()}


# ---------------------------------------------------------------------------- users
async def test_user_lifecycle(services: Services, admin: httpx.AsyncClient) -> None:
    made = await admin.post(
        f'{API}/users',
        json={
            'username': 'Zoe',
            'display_name': 'Zoe Z',
            'email': 'zoe@example.com',
            'password': 'zoe-first-password',
            'roles': ['deployer'],
        },
    )
    assert made.status_code == 201, made.text
    zoe = made.json()
    assert zoe['username'] == 'zoe' and zoe['source'] == 'local' and zoe['has_password'] is True
    assert zoe['must_change_password'] is True and zoe['effective_roles'] == ['deployer']
    assert made.headers['location'] == f'/api/v1/users/{zoe["id"]}'
    doc = await services.db[USERS].find_one({'_id': zoe['id']})
    assert doc is not None and doc['password_hash'].startswith('$argon2id$')
    assert 'password' not in made.text.replace('has_password', '').replace('must_change_password', '')

    dup = await admin.post(f'{API}/users', json={'username': 'ZOE'})
    assert dup.status_code == 409 and dup.json()['errors'][0]['field'] == 'username'
    bad = await admin.post(f'{API}/users', json={'username': 'bad name', 'password': 'short', 'roles': ['root']})
    assert bad.status_code == 422
    weak = await admin.post(f'{API}/users', json={'username': 'weak', 'password': 'short'})
    assert weak.status_code == 422 and weak.json()['errors'][0]['field'] == 'password'
    unknown_role = await admin.post(f'{API}/users', json={'username': 'x1', 'roles': ['root']})
    assert unknown_role.status_code == 422 and unknown_role.json()['errors'][0]['field'] == 'roles.0'

    # list, filter and page
    for name in ('amy', 'bob', 'cal'):
        assert (await admin.post(f'{API}/users', json={'username': name})).status_code == 201
    first = (await admin.get(f'{API}/users', params={'limit': 2})).json()
    assert [u['username'] for u in first['items']] == ['admin', 'amy'] and first['next_cursor']
    second = (await admin.get(f'{API}/users', params={'limit': 2, 'cursor': first['next_cursor']})).json()
    assert [u['username'] for u in second['items']] == ['bob', 'cal']
    assert [u['username'] for u in (await admin.get(f'{API}/users', params={'q': 'R'})).json()['items']] == ['rita']
    deployers = (await admin.get(f'{API}/users', params={'role': 'deployer'})).json()['items']
    assert [u['username'] for u in deployers] == ['zoe']
    assert (await admin.get(f'{API}/users', params={'cursor': '!!!'})).status_code == 400

    # update, disable (sessions end), enable
    zoe_client = await sign_in(services, 'zoe', 'zoe-first-password')
    patched = await admin.patch(f'{API}/users/{zoe["id"]}', json={'email': None, 'disabled': True})
    assert patched.status_code == 200 and patched.json()['email'] is None and patched.json()['disabled'] is True
    assert (await zoe_client.get(f'{API}/auth/me')).status_code == 401
    await zoe_client.aclose()
    async with asgi_client(services) as anon:
        refused = await anon.post(f'{API}/auth/login', json={'username': 'zoe', 'password': 'zoe-first-password'})
        assert refused.status_code == 401
    assert (await admin.patch(f'{API}/users/{zoe["id"]}', json={'disabled': False})).status_code == 200

    # reset the password (sessions end, must change), unlock, delete (tokens revoked)
    await services.db[USERS].update_one(
        {'_id': zoe['id']}, {'$set': {'locked_until': datetime.now(UTC) + timedelta(hours=1), 'failed_logins': 2}}
    )
    detail = (await admin.get(f'{API}/users/{zoe["id"]}')).json()
    assert detail['locked'] is True and detail['locked_until'] is not None
    unlocked = await admin.post(f'{API}/users/{zoe["id"]}/unlock')
    assert unlocked.status_code == 200 and unlocked.json()['locked'] is False
    reset = await admin.post(f'{API}/users/{zoe["id"]}/password', json={'password': 'zoe-second-password'})
    assert reset.status_code == 204
    zoe_client = await sign_in(services, 'zoe', 'zoe-second-password')
    assert (await zoe_client.get(f'{API}/auth/me')).json()['must_change_password'] is True
    await zoe_client.aclose()
    assert (await admin.delete(f'{API}/users/{zoe["id"]}')).status_code == 204
    assert (await admin.get(f'{API}/users/{zoe["id"]}')).status_code == 404
    assert (await admin.delete(f'{API}/users/{zoe["id"]}')).status_code == 404


async def test_external_users_and_password_less_accounts(services: Services, admin: httpx.AsyncClient) -> None:
    pre = await admin.post(f'{API}/users', json={'username': 'ldap-user', 'roles': ['reader']})
    assert pre.status_code == 201 and pre.json()['has_password'] is False
    assert pre.json()['must_change_password'] is False
    await add_user(
        services,
        'eve',
        ['reader'],
        source='ldap:p1',
        password_hash=None,
        external_groups=[{'provider': 'ldap:p1', 'id': 'cn=ops,dc=example,dc=com', 'name': 'ops'}],
        identities=[{'provider': 'ldap:p1', 'subject': 'uid=eve,dc=example,dc=com', 'linked_at': datetime.now(UTC)}],
    )
    eve = (await admin.get(f'{API}/users/id-eve')).json()
    assert eve['source'] == 'ldap:p1' and eve['identities'][0]['subject'] == 'uid=eve,dc=example,dc=com'
    assert eve['external_groups'] == [{'provider': 'ldap:p1', 'id': 'cn=ops,dc=example,dc=com', 'name': 'ops'}]
    assert (await admin.post(f'{API}/users/id-eve/password', json={'password': 'x' * 16})).status_code == 409
    by_source = (await admin.get(f'{API}/users', params={'source': 'ldap'})).json()['items']
    assert [u['username'] for u in by_source] == ['eve']


async def test_admin_guards(services: Services, admin: httpx.AsyncClient) -> None:
    me = (await admin.get(f'{API}/auth/me')).json()['user']['id']
    assert (await admin.patch(f'{API}/users/{me}', json={'disabled': True})).status_code == 409
    assert (await admin.delete(f'{API}/users/{me}')).status_code == 409
    demote = await admin.patch(f'{API}/users/{me}', json={'roles': ['reader']})
    assert demote.status_code == 409 and 'administrator' in demote.json()['detail']
    # with a second administrator (through a local group) the first can be demoted
    group = (await admin.post(f'{API}/groups', json={'name': 'Admins', 'roles': ['admin']})).json()
    assert (await admin.post(f'{API}/groups/{group["id"]}/members', json={'user_ids': ['id-rita']})).status_code == 204
    assert (await admin.patch(f'{API}/users/{me}', json={'roles': ['reader']})).status_code == 200
    # ... but now rita's group is the last way to an administrator
    assert (await admin.delete(f'{API}/groups/{group["id"]}')).status_code == 403  # admin is no longer admin
    rita = await sign_in(services, 'rita')
    try:
        assert (await rita.delete(f'{API}/groups/{group["id"]}/members/id-rita')).status_code == 409
        assert (await rita.delete(f'{API}/groups/{group["id"]}')).status_code == 409
    finally:
        await rita.aclose()


async def test_user_admin_cannot_escalate(services: Services, admin: httpx.AsyncClient) -> None:
    uma = await user_admin(services, admin)
    try:
        assert (await uma.get(f'{API}/users')).status_code == 200
        # may hand out repository roles, not admin or global powers it lacks
        assert (await uma.patch(f'{API}/users/id-rita', json={'roles': ['deployer']})).status_code == 200
        refused = await uma.patch(f'{API}/users/id-rita', json={'roles': ['admin']})
        assert refused.status_code == 422 and refused.json()['errors'][0]['code'] == 'role'
        assert (await uma.post(f'{API}/users', json={'username': 'boss', 'roles': ['admin']})).status_code == 422
        role = await uma.post(
            f'{API}/roles', json={'id': 'sneaky', 'name': 'Sneaky', 'permissions': [{'action': 'admin', 'scope': '*'}]}
        )
        assert role.status_code == 422
        audit_role = await uma.post(
            f'{API}/roles',
            json={'id': 'auditors', 'name': 'Auditors', 'permissions': [{'action': 'audit-read', 'scope': '*'}]},
        )
        assert audit_role.status_code == 422  # audit-read is not held by uma
        group = await uma.post(f'{API}/groups', json={'name': 'Boss', 'roles': ['admin']})
        assert group.status_code == 422
        # an administrator cannot be changed by a user-admin
        admin_id = (await admin.get(f'{API}/auth/me')).json()['user']['id']
        assert (await uma.patch(f'{API}/users/{admin_id}', json={'display_name': 'x'})).status_code == 403
        assert (await uma.post(f'{API}/users/{admin_id}/password', json={'password': 'y' * 16})).status_code == 403
        assert (await uma.delete(f'{API}/users/{admin_id}')).status_code == 403
        # nor an admin group, nor a service account holding admin
        admins = (await admin.post(f'{API}/groups', json={'name': 'Admins', 'roles': ['admin']})).json()
        assert (
            await uma.post(f'{API}/groups/{admins["id"]}/members', json={'user_ids': ['id-uma']})
        ).status_code == 403
        assert (await uma.patch(f'{API}/groups/{admins["id"]}', json={'roles': []})).status_code == 403
        bot = (await admin.post(f'{API}/service-accounts', json={'name': 'root-bot', 'roles': ['admin']})).json()
        minted = await uma.post(f'{API}/service-accounts/{bot["id"]}/tokens', json={'name': 't', 'scopes': ['read']})
        assert minted.status_code == 403
        assert (await uma.patch(f'{API}/service-accounts/{bot["id"]}', json={'disabled': True})).status_code == 403
        assert (await uma.post(f'{API}/service-accounts', json={'name': 'b2', 'roles': ['admin']})).status_code == 422
        # nor a role holding powers it lacks
        ops = {'id': 'ops', 'name': 'Ops', 'permissions': [{'action': 'settings-admin', 'scope': '*'}]}
        assert (await admin.post(f'{API}/roles', json=ops)).status_code == 201
        edit = {'name': 'Ops', 'description': 'x', 'permissions': ops['permissions']}
        assert (await uma.put(f'{API}/roles/ops', json=edit)).status_code == 403
        assert (await uma.delete(f'{API}/roles/ops')).status_code == 403
        # roles it holds itself are fine
        assert (
            await uma.post(f'{API}/users', json={'username': 'helper', 'roles': ['user-managers']})
        ).status_code == 201
    finally:
        await uma.aclose()


# ---------------------------------------------------------------------------- groups
async def test_local_groups_and_members(services: Services, admin: httpx.AsyncClient) -> None:
    made = await admin.post(
        f'{API}/groups', json={'name': 'Release team', 'description': 'Ships', 'roles': ['deployer']}
    )
    assert made.status_code == 201, made.text
    group = made.json()
    assert group['source'] == 'local' and group['member_count'] == 0 and group['provider'] is None
    assert (await admin.post(f'{API}/groups', json={'name': 'Release team'})).status_code == 409
    assert (await admin.post(f'{API}/groups', json={'name': 'ldap:x:y'})).status_code == 422

    rita = await sign_in(services, 'rita')
    try:
        put = await rita.request('PUT', '/repository/raw-hosted/a.txt', content=b'x')
        assert put.status_code == 403
        added = await admin.post(f'{API}/groups/{group["id"]}/members', json={'user_ids': ['id-rita']})
        assert added.status_code == 204
        assert (await rita.request('PUT', '/repository/raw-hosted/a.txt', content=b'x')).status_code == 200
        members = (await admin.get(f'{API}/groups/{group["id"]}/members')).json()
        assert [m['username'] for m in members['items']] == ['rita']
        assert (await admin.get(f'{API}/groups/{group["id"]}')).json()['member_count'] == 1
        missing = await admin.post(f'{API}/groups/{group["id"]}/members', json={'user_ids': ['nobody']})
        assert missing.status_code == 422
        renamed = await admin.patch(f'{API}/groups/{group["id"]}', json={'name': 'Releasers', 'roles': ['reader']})
        assert renamed.status_code == 200 and renamed.json()['name'] == 'Releasers'
        assert (await rita.request('PUT', '/repository/raw-hosted/a.txt', content=b'x')).status_code == 403
        assert (await admin.delete(f'{API}/groups/{group["id"]}/members/id-rita')).status_code == 204
        assert (await admin.delete(f'{API}/groups/{group["id"]}/members/id-rita')).status_code == 404
        assert (await admin.delete(f'{API}/groups/{group["id"]}')).status_code == 204
        assert (await admin.get(f'{API}/groups/{group["id"]}')).status_code == 404
    finally:
        await rita.aclose()


async def test_mapped_groups_are_read_only_except_roles(services: Services, admin: httpx.AsyncClient) -> None:
    now = datetime.now(UTC)
    await services.db[GROUPS].insert_one(
        {
            '_id': 'g-mapped',
            'name': 'ldap:Corp:cn=ops,dc=example,dc=com',
            'source': 'ldap',
            'provider_id': 'p1',
            'external_id': 'cn=ops,dc=example,dc=com',
            'external_group': 'cn=ops,dc=example,dc=com',
            'position': 0,
            'roles': ['reader'],
            'created_at': now,
            'updated_at': now,
        }
    )
    listed = (await admin.get(f'{API}/groups', params={'source': 'ldap'})).json()
    assert [(g['id'], g['member_count'], g['provider']) for g in listed] == [
        ('g-mapped', None, {'id': 'p1', 'name': None})
    ]
    assert (await admin.patch(f'{API}/groups/g-mapped', json={'name': 'x'})).status_code == 409
    assert (await admin.delete(f'{API}/groups/g-mapped')).status_code == 409
    assert (await admin.post(f'{API}/groups/g-mapped/members', json={'user_ids': ['id-rita']})).status_code == 409
    assert (await admin.get(f'{API}/groups/g-mapped/members')).json() == {'items': [], 'next_cursor': None}
    roles = await admin.patch(f'{API}/groups/g-mapped', json={'roles': ['deployer']})
    assert roles.status_code == 200 and roles.json()['roles'] == ['deployer']


# ---------------------------------------------------------------------------- roles
async def test_custom_roles(services: Services, admin: httpx.AsyncClient) -> None:
    builtin = (await admin.get(f'{API}/roles')).json()
    assert [r['id'] for r in builtin] == ['admin', 'reader', 'deployer', 'viewer']
    assert builtin[1]['permissions'] == [{'action': 'browse', 'scope': '*'}, {'action': 'read', 'scope': '*'}]
    body = {'id': 'npm-publisher', 'name': 'npm publisher', 'permissions': [{'action': 'write', 'scope': 'format:npm'}]}
    made = await admin.post(f'{API}/roles', json=body)
    assert made.status_code == 201 and made.json()['builtin'] is False
    assert (await admin.post(f'{API}/roles', json=body)).status_code == 409
    assert (await admin.post(f'{API}/roles', json={**body, 'id': 'reader'})).status_code == 409
    assert (await admin.post(f'{API}/roles', json={**body, 'id': 'Bad Id'})).status_code == 422
    bad_scope = await admin.post(
        f'{API}/roles', json={**body, 'id': 'r2', 'permissions': [{'action': 'user-admin', 'scope': 'npm-*'}]}
    )
    assert bad_scope.status_code == 422 and bad_scope.json()['errors'][0]['field'] == 'permissions.0.scope'
    assert (
        await admin.put(f'{API}/roles/reader', json={'name': 'x', 'permissions': body['permissions']})
    ).status_code == 409
    assert (await admin.delete(f'{API}/roles/admin')).status_code == 409
    assert [r['id'] for r in (await admin.get(f'{API}/roles')).json()][-1] == 'npm-publisher'

    # the role takes effect for a signed-in user at once, and changes apply on the next request
    assert (await admin.patch(f'{API}/users/id-rita', json={'roles': ['npm-publisher']})).status_code == 200
    rita = await sign_in(services, 'rita')
    try:
        assert (await rita.request('PUT', '/repository/npm-hosted/pkg', content=b'x')).status_code == 200
        assert (await rita.request('PUT', '/repository/raw-hosted/a', content=b'x')).status_code == 404  # hidden
        changed = await admin.put(
            f'{API}/roles/npm-publisher',
            json={'name': 'npm publisher', 'permissions': [{'action': 'write', 'scope': 'raw-*'}]},
        )
        assert changed.status_code == 200
        assert (await rita.request('PUT', '/repository/raw-hosted/a', content=b'x')).status_code == 200
        assert (await rita.request('PUT', '/repository/npm-hosted/pkg', content=b'x')).status_code == 404
        perms = (await rita.get(f'{API}/auth/me/permissions')).json()
        assert perms['roles'] == [{'id': 'npm-publisher', 'name': 'npm publisher', 'via': ['direct']}]
        assert perms['repositories'] == [
            {'name': 'raw-hosted', 'format': 'raw', 'type': 'hosted', 'actions': ['browse', 'read', 'write']}
        ]
        assert perms['global_actions'] == [] and perms['narrowed_by_token'] is False
    finally:
        await rita.aclose()
    detail = (await admin.get(f'{API}/roles/npm-publisher')).json()
    assert detail['usage'] == {'users': 1, 'groups': 0, 'service_accounts': 0}
    in_use = await admin.delete(f'{API}/roles/npm-publisher')
    assert in_use.status_code == 409 and '1 users' in in_use.json()['detail']
    assert (await admin.patch(f'{API}/users/id-rita', json={'roles': []})).status_code == 200
    assert (await admin.delete(f'{API}/roles/npm-publisher')).status_code == 204
    assert (await admin.get(f'{API}/roles/npm-publisher')).status_code == 404


async def test_grant_cache_is_keyed_by_grant_version(services: Services, admin: httpx.AsyncClient) -> None:
    version = services.rbac.version
    assert (await admin.patch(f'{API}/users/id-rita', json={'roles': ['deployer']})).status_code == 200
    rita = await sign_in(services, 'rita')
    try:
        assert (await rita.get(f'{API}/auth/me')).status_code == 200
    finally:
        await rita.aclose()
    assert services.rbac.version > version
    cached = await services.valkey.get(grants_key('id-rita', services.rbac.version))
    assert cached is not None and 'deployer' in cached


async def test_rbac_changes_reach_other_replicas(
    services: Services, admin: httpx.AsyncClient, settings: Settings
) -> None:
    """A role change on one replica applies on another within moments (MongoDB change stream on ``rbac_state``;
    polling is switched off on the other replica so only the stream can tell it)."""
    made = await admin.post(
        f'{API}/roles',
        json={'id': 'raw-writer', 'name': 'raw writer', 'permissions': [{'action': 'read', 'scope': 'raw-*'}]},
    )
    assert made.status_code == 201
    assert (await admin.patch(f'{API}/users/id-rita', json={'roles': ['raw-writer']})).status_code == 200
    replica = Services.create(settings)
    replica.formats.set_plugin(Echo(replica, 'raw'))  # type: ignore[arg-type]
    replica.rbac._poll_s = 3600.0
    await replica.start()
    try:
        await asyncio.wait_for(replica.rbac.watching.wait(), timeout=10)
        other = asgi_client(replica)
        login = await other.post(f'{API}/auth/login', json={'username': 'rita', 'password': PASSWORD})
        assert login.status_code == 200
        other.headers['X-CSRF-Token'] = login.json()['csrf_token']
        assert (await other.request('PUT', '/repository/raw-hosted/a', content=b'x')).status_code == 403
        start = time.monotonic()
        changed = await admin.put(
            f'{API}/roles/raw-writer',
            json={'name': 'raw writer', 'permissions': [{'action': 'write', 'scope': 'raw-*'}]},
        )
        assert changed.status_code == 200
        while (await other.request('PUT', '/repository/raw-hosted/a', content=b'x')).status_code != 200:
            assert time.monotonic() - start < 3.0, 'role change not applied by the other replica'
            await asyncio.sleep(0.02)
        await other.aclose()
    finally:
        await replica.stop()


# ---------------------------------------------------------------------------- service accounts and permissions
async def test_service_account_with_custom_role(services: Services, admin: httpx.AsyncClient) -> None:
    role = {'id': 'npm-reader', 'name': 'npm reader', 'permissions': [{'action': 'read', 'scope': 'format:npm'}]}
    assert (await admin.post(f'{API}/roles', json=role)).status_code == 201
    made = await admin.post(f'{API}/service-accounts', json={'name': 'ci', 'roles': ['npm-reader']})
    assert made.status_code == 201, made.text
    account = made.json()
    assert (await admin.get(f'{API}/service-accounts/{account["id"]}')).json()['roles'] == ['npm-reader']
    token = await admin.post(f'{API}/service-accounts/{account["id"]}/tokens', json={'name': 't', 'scopes': ['read']})
    assert token.status_code == 201, token.text
    secret = token.json()['secret']
    async with asgi_client(services) as ci:
        bearer = {'Authorization': f'Bearer {secret}'}
        assert (await ci.get('/repository/npm-hosted/pkg', headers=bearer)).status_code == 200
        assert (await ci.get('/repository/raw-hosted/a', headers=bearer)).status_code == 404
        perms = (await ci.get(f'{API}/auth/me/permissions', headers=bearer)).json()
        assert perms['principal']['kind'] == 'service_account' and perms['narrowed_by_token'] is True
    perms = (await admin.get(f'{API}/service-accounts/{account["id"]}/permissions')).json()
    assert sorted(r['name'] for r in perms['repositories']) == ['npm-hosted', 'npm-proxy']
    assert (await admin.get(f'{API}/service-accounts/nope/permissions')).status_code == 404


async def test_user_permissions_view(services: Services, admin: httpx.AsyncClient) -> None:
    group = (await admin.post(f'{API}/groups', json={'name': 'Viewers', 'roles': ['viewer']})).json()
    assert (await admin.post(f'{API}/groups/{group["id"]}/members', json={'user_ids': ['id-rita']})).status_code == 204
    perms = (await admin.get(f'{API}/users/id-rita/permissions')).json()
    assert perms['principal'] == {'kind': 'user', 'id': 'id-rita', 'name': 'rita'}
    assert {r['id']: r['via'] for r in perms['roles']} == {'reader': ['direct'], 'viewer': ['Viewers']}
    assert perms['global_actions'] == ['stats-read']
    assert len(perms['repositories']) == 3
    detail = (await admin.get(f'{API}/users/id-rita')).json()
    assert detail['effective_roles'] == ['reader', 'viewer']
    assert detail['group_refs'] == [{'id': group['id'], 'name': 'Viewers', 'source': 'local'}]


# ---------------------------------------------------------------------------- own account
async def test_own_sessions(services: Services, admin: httpx.AsyncClient) -> None:
    other = await sign_in(services, 'admin')
    rita = await sign_in(services, 'rita')
    try:
        listed = (await admin.get(f'{API}/auth/sessions')).json()
        assert len(listed) == 2 and sum(s['current'] for s in listed) == 1
        assert all(len(s['id']) == 32 for s in listed)
        token = admin.cookies['ws_session']
        assert all(token not in s['id'] for s in listed)
        # another user's session is not found
        rita_session = (await rita.get(f'{API}/auth/sessions')).json()[0]['id']
        assert (await admin.delete(f'{API}/auth/sessions/{rita_session}')).status_code == 404
        # sign out everywhere else
        revoked = await admin.delete(f'{API}/auth/sessions')
        assert revoked.json() == {'revoked': 1}
        assert (await other.get(f'{API}/auth/me')).status_code == 401
        assert (await admin.get(f'{API}/auth/me')).status_code == 200
        # ending the current session clears its cookies
        mine = (await admin.get(f'{API}/auth/sessions')).json()[0]['id']
        ended = await admin.delete(f'{API}/auth/sessions/{mine}')
        assert ended.status_code == 204 and 'ws_session=""' in ended.headers.get('set-cookie', '')
    finally:
        await other.aclose()
        await rita.aclose()


async def test_change_own_password_and_profile(services: Services, admin: httpx.AsyncClient) -> None:
    rita = await sign_in(services, 'rita')
    elsewhere = await sign_in(services, 'rita')
    try:
        wrong = await rita.post(
            f'{API}/auth/me/password', json={'current_password': 'nope', 'new_password': 'a brand new secret'}
        )
        assert wrong.status_code == 422 and wrong.json()['errors'][0]['field'] == 'current_password'
        same = await rita.post(f'{API}/auth/me/password', json={'current_password': PASSWORD, 'new_password': PASSWORD})
        assert same.status_code == 422 and same.json()['errors'][0]['field'] == 'new_password'
        ok = await rita.post(
            f'{API}/auth/me/password', json={'current_password': PASSWORD, 'new_password': 'a brand new secret'}
        )
        assert ok.status_code == 204
        assert (await rita.get(f'{API}/auth/me')).status_code == 200
        assert (await elsewhere.get(f'{API}/auth/me')).status_code == 401  # other sessions ended
        profile = await rita.patch(f'{API}/auth/me', json={'display_name': 'Rita R', 'email': 'rita@example.com'})
        assert profile.status_code == 200 and profile.json()['display_name'] == 'Rita R'
        assert (await rita.patch(f'{API}/auth/me', json={'email': 'not-an-email'})).status_code == 422
    finally:
        await rita.aclose()
        await elsewhere.aclose()
    # an API token cannot change the password
    secret = (await admin.post(f'{API}/auth/me/tokens', json={'name': 't', 'scopes': ['admin']})).json()['secret']
    async with asgi_client(services) as script:
        changed = await script.post(
            f'{API}/auth/me/password',
            json={'current_password': PASSWORD, 'new_password': 'another new secret'},
            headers={'Authorization': f'Bearer {secret}'},
        )
        assert changed.status_code == 403


async def test_one_time_password_must_be_changed_first(services: Services, world: None) -> None:
    await services.db[USERS].delete_many({'username': 'admin'})
    await add_user(services, 'newbie', ['admin'], must_change_password=True)
    client = await sign_in(services, 'newbie')
    try:
        me = await client.get(f'{API}/auth/me')
        assert me.status_code == 200 and me.json()['must_change_password'] is True
        for path in ('/repositories', '/users', '/auth/me/tokens', '/auth/sessions'):
            refused = await client.get(f'{API}{path}')
            assert refused.status_code == 403, path
            assert 'one-time password' in refused.json()['detail']
        assert (await client.get('/repository/npm-proxy/pkg')).status_code == 403
        async with asgi_client(services) as docker:
            assert (await docker.get('/auth/token', headers=basic('newbie', PASSWORD))).status_code == 401
        changed = await client.post(
            f'{API}/auth/me/password', json={'current_password': PASSWORD, 'new_password': 'my very own password'}
        )
        assert changed.status_code == 204, changed.text
        assert (await client.get(f'{API}/auth/me')).json()['must_change_password'] is False
        assert (await client.get(f'{API}/users')).status_code == 200
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------- anonymous access
async def test_anonymous_access_is_configurable(services: Services, admin: httpx.AsyncClient) -> None:
    async with asgi_client(services) as anon:
        assert (await anon.get('/repository/npm-proxy/pkg')).status_code == 200  # default: read proxy repositories
        assert (await anon.get('/repository/raw-hosted/a')).status_code == 404
        assert (await admin.get(f'{API}/anonymous-access')).json() == {'roles': ['anonymous']}
        role = {'id': 'raw-public', 'name': 'raw public', 'permissions': [{'action': 'read', 'scope': 'raw-*'}]}
        assert (await admin.post(f'{API}/roles', json=role)).status_code == 201
        saved = await admin.put(f'{API}/anonymous-access', json={'roles': ['raw-public']})
        assert saved.status_code == 200 and saved.json() == {'roles': ['raw-public']}
        assert (await anon.get('/repository/raw-hosted/a')).status_code == 200
        assert (await anon.get('/repository/npm-proxy/pkg')).status_code == 404
        refused = await admin.put(f'{API}/anonymous-access', json={'roles': ['viewer', 'admin', 'nope']})
        assert [(e['field'], e['code']) for e in refused.json()['errors']] == [
            ('roles.0', 'global'),
            ('roles.1', 'global'),
            ('roles.2', 'role'),
        ]
        assert (await admin.put(f'{API}/anonymous-access', json={'roles': []})).status_code == 200
        assert (await anon.get('/repository/raw-hosted/a')).status_code == 404
        assert (await anon.get('/repository/npm-proxy/pkg')).status_code == 404
        # docker's anonymous registry tokens follow the same roles
        token = await anon.get('/auth/token', params={'scope': 'repository:raw-hosted/x:pull'})
        assert token.status_code == 200
    changed = await admin.get(f'{API}/audit', params={'action': 'role.update', 'target': 'anonymous'})
    assert changed.status_code == 200 and len(changed.json()['items']) == 2


# ---------------------------------------------------------------------------- matrix
async def test_admin_passes_every_access_rule(services: Services, admin: httpx.AsyncClient) -> None:
    """Route x principal matrix, the administrator column: no REST route answers 401/403 to an administrator's
    session (the anonymous and reader columns are unit tests: ``test_access``)."""
    from ws_testing import iter_routes

    from waystation_server.app import create_app
    from waystation_server.security import access

    checked = 0
    for path, methods, endpoint in iter_routes(create_app()):
        rule = access.route_rule(endpoint)
        if not path.startswith(API) or rule is None or rule.kind not in ('global', 'authenticated'):
            continue
        if path in (f'{API}/auth/sessions', f'{API}/auth/sessions/{{session_id}}', f'{API}/events/stream'):
            continue  # would end the admin's own sessions / stream forever
        url = path.replace('{', '').replace('}', '-missing')
        for method in sorted(methods - {'HEAD'}):
            response = await admin.request(method, url, json={} if method in ('POST', 'PUT', 'PATCH') else None)
            assert response.status_code not in (401, 403), f'{method} {path}: {response.status_code} {response.text}'
            checked += 1
    assert checked > 50


SKIP_FOR_MINIMAL = {f'{API}/auth/sessions', f'{API}/auth/sessions/{{session_id}}', f'{API}/events/stream'}


@pytest.mark.parametrize(
    ('username', 'roles', 'visible'),
    [
        ('nobody', [], []),  # signed in (e.g. in the provider's required group) with no role at all
        ('guest', ['anonymous'], ['npm-proxy']),  # only what anonymous access grants
        ('rita', ['reader'], ['npm-hosted', 'npm-proxy', 'raw-hosted']),
    ],
)
async def test_minimal_principals_get_consistent_answers(
    services: Services, admin: httpx.AsyncClient, username: str, roles: list[str], visible: list[str]
) -> None:
    """Route x principal matrix, the "signed in, minimal permissions" columns against a real session (SPEC §4.10
    "What a user without roles sees"): never 401 (that would end the UI session); every route needing a global
    action is 403 ``permission_required``; listings are filtered (an empty page, not an error); hidden repositories
    are 404 on every repository route; routes for any signed-in user work."""
    from ws_testing import iter_routes

    from waystation_server.app import create_app
    from waystation_server.security import access

    if username != 'rita':
        await add_user(services, username, roles)
    client = await sign_in(services, username)
    try:
        me = (await client.get(f'{API}/auth/me')).json()
        assert me['capabilities'] == ([] if not visible else ['repositories.browse'])
        listed = await client.get(f'{API}/repositories')
        assert listed.status_code == 200, listed.text
        assert sorted(r['name'] for r in listed.json()['items']) == visible
        found = await client.get(f'{API}/search', params={'q': ''})
        assert found.status_code == 200, found.text
        assert {i['repository'] for i in found.json()['items']} <= set(visible)
        stream = await client.get(f'{API}/events/stream', params={'topics': 'transfers,stats,activity'})
        assert stream.status_code == 403
        assert stream.json()['code'] == 'permission_required'
        assert (await client.get(f'{API}/cleanup-policies')).json()['code'] == 'permission_required'

        checked = 0
        for path, methods, endpoint in iter_routes(create_app()):
            rule = access.route_rule(endpoint)
            if not path.startswith(API) or rule is None or rule.kind == 'public' or path in SKIP_FOR_MINIMAL:
                continue  # public routes include sign-out
            for method in sorted(methods - {'HEAD'}):
                body = {} if method in ('POST', 'PUT', 'PATCH') else None
                if rule.kind == 'repo':
                    for repo in ('npm-proxy', 'npm-hosted', 'raw-hosted'):
                        url = path.replace('{name}', repo).replace('{', '').replace('}', '-missing')
                        response = await client.request(method, url, json=body)
                        assert response.status_code != 401, f'{username} {method} {url}: 401'
                        if repo not in visible:
                            assert response.status_code == 404, f'{username} {method} {url}: {response.status_code}'
                        elif rule.action not in (access.Action.BROWSE, access.Action.READ):
                            assert response.status_code == 403, f'{username} {method} {url}: {response.status_code}'
                        checked += 1
                    continue
                url = path.replace('{', '').replace('}', '-missing')
                response = await client.request(method, url, json=body)
                assert response.status_code != 401, f'{username} {method} {path}: 401 {response.text}'
                if rule.kind == 'global':
                    assert response.status_code == 403, f'{username} {method} {path}: {response.status_code}'
                    assert response.json()['code'] == 'permission_required', f'{method} {path}: {response.text}'
                elif rule.kind in ('authenticated', 'filtered'):
                    assert response.status_code != 403, f'{username} {method} {path}: 403 {response.text}'
                    assert response.status_code < 500, f'{username} {method} {path}: {response.status_code}'
                checked += 1
        assert checked > 100
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------- T-U08: sessions, settings, system
async def test_admin_lists_and_ends_a_users_sessions(services: Services, admin: httpx.AsyncClient) -> None:
    rita = await sign_in(services, 'rita')
    other = await sign_in(services, 'rita')
    try:
        listed = await admin.get(f'{API}/users/id-rita/sessions')
        assert listed.status_code == 200, listed.text
        assert len(listed.json()) == 2
        assert not any(s['current'] for s in listed.json())  # neither is the admin's own session
        admin_id = (await admin.get(f'{API}/auth/me')).json()['user']['id']
        own = (await admin.get(f'{API}/users/{admin_id}/sessions')).json()
        assert [s['current'] for s in own] == [True]

        # a user-admin may not sign out an administrator (holds more), but may sign out a reader
        uma = await user_admin(services, admin)
        try:
            assert (await uma.delete(f'{API}/users/{admin_id}/sessions')).status_code == 403
            ended = await uma.delete(f'{API}/users/id-rita/sessions')
            assert ended.status_code == 200, ended.text
            assert ended.json() == {'revoked': 2}
        finally:
            await uma.aclose()
        assert (await rita.get(f'{API}/auth/me')).status_code == 401
        assert (await other.get(f'{API}/auth/me')).status_code == 401
        assert (await admin.get(f'{API}/users/nobody/sessions')).status_code == 404
    finally:
        await rita.aclose()
        await other.aclose()


async def test_instance_settings(services: Services, admin: httpx.AsyncClient) -> None:
    initial = await admin.get(f'{API}/settings')
    assert initial.status_code == 200, initial.text
    body = initial.json()
    assert body['general'] == {'instance_name': services.settings.instance_name}
    assert body['updated_at'] is None
    assert body['limits']['session_idle_minutes'] == services.settings.session_idle_minutes
    assert body['limits']['stats_minute_retention_days'] == 7

    saved = await admin.put(f'{API}/settings', json={'general': {'instance_name': '  Package mirror  '}})
    assert saved.status_code == 200, saved.text
    assert saved.json()['general'] == {'instance_name': 'Package mirror'}
    assert saved.json()['updated_by'] == 'admin'
    async with asgi_client(services) as anon:
        assert (await anon.get(f'{API}/config')).json()['instance_name'] == 'Package mirror'

    assert (await admin.put(f'{API}/settings', json={'general': {'instance_name': ' '}})).status_code == 422
    assert (await admin.put(f'{API}/settings', json={'general': {'instance_name': 'x' * 65}})).status_code == 422
    rita = await sign_in(services, 'rita')
    try:
        assert (await rita.get(f'{API}/settings')).status_code == 403
        assert (await rita.get(f'{API}/system')).status_code == 403
    finally:
        await rita.aclose()


async def test_system_status(services: Services, admin: httpx.AsyncClient) -> None:
    response = await admin.get(f'{API}/system')
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['version']
    assert body['python'].startswith('3.')
    components = {c['name']: c for c in body['components']}
    assert components['mongodb']['status'] == 'ok'
    assert components['mongodb']['version']
    assert components['valkey']['status'] == 'ok'
    assert components['valkey']['version']
    assert components['mongodb']['latency_ms'] >= 0
    storage = body['storage']
    assert storage['path'] == str(services.settings.data_dir)
    assert storage['total_bytes'] >= storage['free_bytes'] > 0
    assert storage['min_free_bytes'] == services.settings.min_free_bytes
