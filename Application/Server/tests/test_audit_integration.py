"""Audit service (T-S10) against real MongoDB/Valkey.

Acceptance: ``test_every_spec_action_is_recorded`` enumerates the catalog (``waystation_common.audit``): every
SPEC §4.11 phrase maps to actions, every action is either emitted here (performed through the API or the code path
that owns it) or listed in ``PENDING`` with the task that adds its feature, and no record holds a secret.
The other tests cover ``/api/v1/audit`` (filters, cursor paging, CSV/NDJSON export, RBAC), retention (TTL) and that
a failing audit store never fails a request.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import dataclasses
import hashlib
import io
import json
import secrets
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_testing import asgi_client

from waystation_common.audit import PENDING, SPEC_ACTIONS, AuditAction, AuditActor, AuditEvent, AuditTarget, Outcome
from waystation_common.db.collections import AUDIT, JOB_DEFINITIONS, USERS
from waystation_common.jobs import BlobGc, JobEnvelope
from waystation_common.jobs import keys as job_keys
from waystation_common.settings import Settings
from waystation_server import audit
from waystation_server.api import audit as audit_api
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.security import passwords
from waystation_server.security.external import ExternalGroup, ExternalIdentity, link_identity
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
UPSTREAM_SECRET = 'upstream-hunter2'
A = AuditAction


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        bootstrap_admin_password=SecretStr(PASSWORD),
        login_rate_limit=10_000,
        login_lockout_failures=3,
        token_failure_limit=10_000,
        audit_retention_days=30,
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


async def sign_in(services: Services, username: str, password: str = PASSWORD) -> httpx.AsyncClient:
    client = asgi_client(services)
    response = await client.post(
        '/api/v1/auth/login',
        json={'username': username, 'password': password},
        headers={'X-Real-IP': '198.51.100.7', 'X-Request-Id': 'req-audit-0001'},
    )
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return client


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    client = await sign_in(services, 'admin')
    try:
        yield client
    finally:
        await client.aclose()


def bearer(secret: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {secret}'}


def basic(username: str, password: str) -> dict[str, str]:
    return {'Authorization': 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()}


class Recorder:
    """Finds the newest record of an action (after flushing the writer) and remembers which actions were seen."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self.seen: set[AuditAction] = set()

    async def all(self, **match: Any) -> list[dict[str, Any]]:
        await self.services.audit.flush()
        return [doc async for doc in self.services.db[AUDIT].find(match, sort=[('ts', -1), ('_id', -1)])]

    async def expect(self, action: AuditAction, **match: Any) -> dict[str, Any]:
        docs = await self.all(action=action.value, **match)
        assert docs, f'no {action.value} record matching {match}'
        self.seen.add(action)
        return docs[0]


# ---------------------------------------------------------------------------- acceptance
async def test_every_spec_action_is_recorded(services: Services, admin: httpx.AsyncClient) -> None:
    covered = {action for actions in SPEC_ACTIONS.values() for action in actions}
    assert covered == set(AuditAction), 'every action belongs to a SPEC §4.11 phrase'
    assert all(SPEC_ACTIONS.values()), 'every SPEC §4.11 phrase has at least one action'
    assert all(PENDING.values()), 'every pending action names the task that adds it'
    rec = Recorder(services)
    api = '/api/v1'

    # --- bootstrap admin (init) and sign-in through the trusted web tier (X-Real-IP, X-Request-Id)
    created = await rec.expect(A.USER_CREATE, **{'principal.kind': 'system', 'details.bootstrap': True})
    assert created['target']['name'] == 'admin'
    login = await rec.expect(A.AUTH_LOGIN, outcome='success', **{'principal.name': 'admin'})
    assert login['ip'] == '198.51.100.7'
    assert login['request_id'] == 'req-audit-0001'
    assert login['principal']['auth'] == 'session'
    assert login['principal']['provider'] == 'local'
    assert login['details'] == {'username': 'admin', 'via': 'ui', 'provider': 'local'}
    assert login['expires_at'] - login['ts'] == timedelta(days=30)

    # --- sign-in failures and lockout
    await add_user(services, 'carol', ['reader'])
    async with asgi_client(services) as anon:
        for _ in range(3):
            bad = await anon.post(f'{api}/auth/login', json={'username': 'carol', 'password': 'wrong-password'})
            assert bad.status_code == 401
        locked = await anon.post(f'{api}/auth/login', json={'username': 'carol', 'password': PASSWORD})
        assert locked.status_code == 401
        unknown = await anon.post(f'{api}/auth/login', json={'username': 'nobody', 'password': 'x'})
        assert unknown.status_code == 401
    failure = await rec.expect(A.AUTH_LOGIN, outcome='failure', reason='bad_credentials')
    assert failure['principal']['kind'] == 'anonymous'
    assert failure['target'] == {'kind': 'user', 'id': 'id-carol', 'name': 'carol'}
    assert failure['details']['username'] == 'carol'
    lockout = await rec.expect(A.AUTH_LOCKOUT, **{'target.name': 'carol'})
    assert lockout['details']['failures'] == 3
    await rec.expect(A.AUTH_LOGIN, outcome='failure', reason='locked')
    await rec.expect(A.AUTH_LOGIN, outcome='failure', reason='unknown_user', **{'details.username': 'nobody'})

    # --- API tokens
    made = await admin.post(f'{api}/auth/me/tokens', json={'name': 'ci', 'scopes': ['admin']})
    assert made.status_code == 201, made.text
    token_id, token_secret = made.json()['token']['id'], made.json()['secret']
    created_token = await rec.expect(A.TOKEN_CREATE, **{'target.id': token_id})
    assert created_token['details']['scopes'] == ['admin']
    async with asgi_client(services) as anon:
        refused = await anon.get(f'{api}/auth/me/tokens', headers=bearer('wst_' + 'A' * 43))
        assert refused.status_code == 401
    use_refused = await rec.expect(A.TOKEN_USE_REFUSED, outcome='failure')
    assert use_refused['details'] == {'prefix': 'wst_AAAAAAAA'}

    # --- repositories (as the API token: the actor carries the token id)
    async with asgi_client(services) as script:
        script.headers.update(bearer(token_secret))
        body: dict[str, Any] = {
            'name': 'npm-private',
            'format': 'npm',
            'type': 'proxy',
            'online': True,
            'proxy': {
                'upstream_url': 'https://npm.example.com/',
                'auth': {'kind': 'basic', 'username': 'ci', 'secret': UPSTREAM_SECRET},
            },
        }
        assert (await script.post(f'{api}/repositories', json=body)).status_code == 201
        body['description'] = 'Private npm'
        body['proxy']['auth']['secret'] = UPSTREAM_SECRET + '-rotated'
        assert (await script.put(f'{api}/repositories/npm-private', json=body)).status_code == 200
        probe = await script.post(
            f'{api}/repositories/test-upstream',
            json={'format': 'raw', 'proxy': {'upstream_url': 'http://127.0.0.1:9/'}},
        )
        assert probe.status_code == 200
        assert (await script.delete(f'{api}/repositories/npm-private')).status_code == 204
    repo_created = await rec.expect(A.REPOSITORY_CREATE, **{'target.name': 'npm-private'})
    assert repo_created['principal']['auth'] == 'api_token'
    assert repo_created['principal']['token_id'] == token_id
    fields = {c['field']: c for c in repo_created['changes']}
    assert fields['proxy.upstream_url']['after'] == 'https://npm.example.com/'
    assert fields['proxy.auth.secret_enc'] == {
        'field': 'proxy.auth.secret_enc',
        'before': None,
        'after': '***',
        'redacted': True,
    }
    updated = await rec.expect(A.REPOSITORY_UPDATE, **{'target.name': 'npm-private'})
    changed = {c['field']: c for c in updated['changes']}
    assert changed['description'] == {'field': 'description', 'before': '', 'after': 'Private npm', 'redacted': False}
    assert changed['proxy.auth.secret_enc']['redacted'] is True
    assert 'config_version' not in changed and 'updated_at' not in changed
    tested = await rec.expect(A.REPOSITORY_TEST_UPSTREAM)
    assert tested['details'] == {'format': 'raw', 'upstream_url': 'http://127.0.0.1:9/', 'reachable': False}
    await rec.expect(A.REPOSITORY_DELETE, **{'target.name': 'npm-private'})

    # --- service accounts
    account = await admin.post(f'{api}/service-accounts', json={'name': 'ci-bot', 'roles': ['reader']})
    assert account.status_code == 201, account.text
    account_id = account.json()['id']
    sa_token = await admin.post(
        f'{api}/service-accounts/{account_id}/tokens', json={'name': 'pull', 'scopes': ['read']}
    )
    assert sa_token.status_code == 201
    patched = await admin.patch(f'{api}/service-accounts/{account_id}', json={'roles': ['deployer'], 'disabled': True})
    assert patched.status_code == 200
    assert (await admin.delete(f'{api}/service-accounts/{account_id}')).status_code == 204
    await rec.expect(A.SERVICE_ACCOUNT_CREATE, **{'target.id': account_id})
    sa_update = await rec.expect(A.SERVICE_ACCOUNT_UPDATE, **{'target.id': account_id})
    assert {c['field']: (c['before'], c['after']) for c in sa_update['changes']} == {
        'disabled': (False, True),
        'roles': (['reader'], ['deployer']),
    }
    sa_delete = await rec.expect(A.SERVICE_ACCOUNT_DELETE, **{'target.id': account_id})
    assert sa_delete['details'] == {'tokens_revoked': 1}

    # --- revoke the admin's own token
    assert (await admin.delete(f'{api}/auth/me/tokens/{token_id}')).status_code == 204
    revoked = await rec.expect(A.TOKEN_REVOKE, **{'target.id': token_id})
    assert revoked['principal']['name'] == 'admin'

    # --- custom roles, groups and users (identity administration)
    role = await admin.post(
        f'{api}/roles',
        json={
            'id': 'npm-publisher',
            'name': 'npm publisher',
            'permissions': [{'action': 'write', 'scope': 'format:npm'}],
        },
    )
    assert role.status_code == 201, role.text
    role_put = await admin.put(
        f'{api}/roles/npm-publisher',
        json={
            'name': 'npm publisher',
            'description': 'Publishes npm packages',
            'permissions': [{'action': 'write', 'scope': 'npm-*'}],
        },
    )
    assert role_put.status_code == 200, role_put.text
    group = await admin.post(f'{api}/groups', json={'name': 'Publishers', 'roles': ['npm-publisher']})
    assert group.status_code == 201, group.text
    group_id = group.json()['id']
    user = await admin.post(
        f'{api}/users',
        json={'username': 'erin', 'display_name': 'Erin', 'password': 'erin-first-Passw0rd', 'roles': ['reader']},
    )
    assert user.status_code == 201, user.text
    erin_id = user.json()['id']
    assert user.json()['must_change_password'] is True
    assert (await admin.post(f'{api}/groups/{group_id}/members', json={'user_ids': [erin_id]})).status_code == 204
    assert (await admin.patch(f'{api}/users/{erin_id}', json={'display_name': 'Erin E.'})).status_code == 200
    assert (await admin.post(f'{api}/users/id-carol/unlock')).status_code == 200
    reset = await admin.post(f'{api}/users/{erin_id}/password', json={'password': 'erin-reset-Passw0rd'})
    assert reset.status_code == 204, reset.text
    erin = await sign_in(services, 'erin', 'erin-reset-Passw0rd')
    try:
        assert (await erin.get(f'{api}/auth/me/tokens')).status_code == 403  # one-time password pending
        changed_pw = await erin.post(
            f'{api}/auth/me/password',
            json={'current_password': 'erin-reset-Passw0rd', 'new_password': 'erin-own-Passw0rd-123'},
        )
        assert changed_pw.status_code == 204, changed_pw.text
        sessions = (await erin.get(f'{api}/auth/sessions')).json()
        assert len(sessions) == 1 and sessions[0]['current'] is True
        session_id = sessions[0]['id']
        assert (await erin.delete(f'{api}/auth/sessions/{session_id}')).status_code == 204
    finally:
        await erin.aclose()
    assert (await admin.delete(f'{api}/groups/{group_id}/members/{erin_id}')).status_code == 204
    assert (await admin.delete(f'{api}/users/{erin_id}')).status_code == 204
    assert (await admin.delete(f'{api}/groups/{group_id}')).status_code == 204
    assert (await admin.delete(f'{api}/roles/npm-publisher')).status_code == 204
    role_created = await rec.expect(A.ROLE_CREATE, **{'target.id': 'npm-publisher'})
    assert {c['field'] for c in role_created['changes']} >= {'name', 'permissions'}
    role_updated = await rec.expect(A.ROLE_UPDATE, **{'target.id': 'npm-publisher'})
    assert {c['field'] for c in role_updated['changes']} == {'description', 'permissions'}
    await rec.expect(A.ROLE_DELETE, **{'target.id': 'npm-publisher'})
    await rec.expect(A.GROUP_CREATE, **{'target.id': group_id})
    added = await rec.expect(A.GROUP_UPDATE, **{'target.id': group_id, 'details.members_added': [erin_id]})
    assert added['principal']['name'] == 'admin'
    await rec.expect(A.GROUP_UPDATE, **{'target.id': group_id, 'details.members_removed': [erin_id]})
    await rec.expect(A.GROUP_DELETE, **{'target.id': group_id})
    erin_created = await rec.expect(A.USER_CREATE, **{'target.id': erin_id})
    assert erin_created['principal']['name'] == 'admin'
    assert erin_created['details'] == {'password_set': True, 'change_at_sign_in': True}
    erin_updated = await rec.expect(A.USER_UPDATE, **{'target.id': erin_id})
    assert erin_updated['changes'] == [
        {'field': 'display_name', 'before': 'Erin', 'after': 'Erin E.', 'redacted': False}
    ]
    await rec.expect(A.USER_UPDATE, **{'target.id': 'id-carol', 'details.unlocked': True})
    by_admin = await rec.expect(A.AUTH_PASSWORD_CHANGE, **{'target.id': erin_id, 'details.by_admin': True})
    assert by_admin['principal']['name'] == 'admin'
    own = await rec.expect(A.AUTH_PASSWORD_CHANGE, **{'target.id': erin_id, 'details.by_admin': False})
    assert own['principal']['name'] == 'erin' and own['details']['was_one_time'] is True
    revoked_session = await rec.expect(A.SESSION_REVOKE, **{'principal.name': 'erin'})
    assert revoked_session['details'] == {'current': True}
    erin_deleted = await rec.expect(A.USER_DELETE, **{'target.id': erin_id})
    assert erin_deleted['details'] == {'sessions_ended': 0, 'tokens_revoked': 0}

    # --- sign-in providers (configuration, tests) with a write-only bind password
    provider_body: dict[str, Any] = {
        'kind': 'ldap',
        'name': 'Directory',
        'enabled': False,
        'order': 10,
        'ldap': {
            'connection': {
                'servers': ['ldap://127.0.0.1:1'],
                'security': 'none',
                'bind_dn': 'uid=svc,dc=example,dc=com',
                'bind_password': 'bind-Passw0rd-secret',
                'connect_timeout_s': 1,
                'timeout_s': 1,
            },
            'users': {'base_dn': 'dc=example,dc=com'},
            'groups': {'mode': 'none'},
        },
        'group_mapping': [{'external_group': 'cn=ops,dc=example,dc=com', 'roles': ['admin']}],
    }
    made_provider = await admin.post(f'{api}/auth-providers', json=provider_body)
    assert made_provider.status_code == 201, made_provider.text
    provider_id = made_provider.json()['id']
    provider_body['name'] = 'Corporate directory'
    provider_body['ldap']['connection']['bind_password'] = 'bind-Passw0rd-rotated'
    assert (await admin.put(f'{api}/auth-providers/{provider_id}', json=provider_body)).status_code == 200
    test = await admin.post(
        f'{api}/auth-providers/test-connection', json={'id': provider_id, 'provider': provider_body}
    )
    assert test.status_code == 200, test.text
    assert (await admin.delete(f'{api}/auth-providers/{provider_id}')).status_code == 204
    provider_created = await rec.expect(A.AUTH_PROVIDER_CREATE, **{'target.id': provider_id})
    created_fields = {c['field']: c for c in provider_created['changes']}
    assert created_fields['ldap.connection.bind_password']['redacted'] is True
    assert created_fields['group_mapping']['after'] == [
        {'external_group': 'cn=ops,dc=example,dc=com', 'roles': ['admin']}
    ]
    provider_updated = await rec.expect(A.AUTH_PROVIDER_UPDATE, **{'target.id': provider_id})
    updated_fields = {c['field']: c for c in provider_updated['changes']}
    assert updated_fields['name']['after'] == 'Corporate directory'
    assert updated_fields['ldap.connection.bind_password'] == {
        'field': 'ldap.connection.bind_password',
        'before': '***',
        'after': '***',
        'redacted': True,
    }
    provider_test = await rec.expect(A.AUTH_PROVIDER_TEST, **{'target.id': provider_id})
    assert provider_test['details']['test'] == 'connection'
    await rec.expect(A.AUTH_PROVIDER_DELETE, **{'target.id': provider_id})

    # --- docker login with a password: audited once per user and address and hour (docker re-sends it every pull)
    async with asgi_client(services) as docker:
        for _ in range(2):
            issued = await docker.get('/auth/token', headers=basic('admin', PASSWORD))
            assert issued.status_code == 200, issued.text
        refused_docker = await docker.get('/auth/token', headers=basic('admin', 'wrong'))
        assert refused_docker.status_code == 401
    docker_logins = await rec.all(action='auth.login', outcome='success', **{'details.via': 'docker'})
    assert len(docker_logins) == 1
    assert docker_logins[0]['principal']['auth'] == 'basic'
    await rec.expect(A.AUTH_LOGIN, outcome='failure', **{'details.via': 'docker'})

    # --- uploads and deletes on a hosted repository (docker push of a manifest, delete of its tag)
    hosted = {'name': 'images', 'format': 'docker', 'type': 'hosted', 'online': True}
    assert (await admin.post(f'{api}/repositories', json=hosted)).status_code == 201
    config = b'{"os":"linux","architecture":"amd64"}'
    config_digest = 'sha256:' + hashlib.sha256(config).hexdigest()
    image_manifest = json.dumps(
        {
            'schemaVersion': 2,
            'mediaType': 'application/vnd.oci.image.manifest.v1+json',
            'config': {
                'mediaType': 'application/vnd.oci.image.config.v1+json',
                'digest': config_digest,
                'size': len(config),
            },
            'layers': [],
        }
    ).encode()
    async with asgi_client(services) as docker:
        issued = await docker.get(
            '/auth/token',
            params={'service': 'registry.example.com', 'scope': 'repository:images/app:pull,push,delete'},
            headers=basic('admin', PASSWORD),
        )
        auth = {'Authorization': f'Bearer {issued.json()["token"]}'}
        blob = await docker.post(
            '/v2/images/app/blobs/uploads/', params={'digest': config_digest}, content=config, headers=auth
        )
        assert blob.status_code == 201, blob.text
        pushed = await docker.put(
            '/v2/images/app/manifests/1.0',
            content=image_manifest,
            headers={'Content-Type': 'application/vnd.oci.image.manifest.v1+json', **auth},
        )
        assert pushed.status_code == 201, pushed.text
        assert (await docker.delete('/v2/images/app/manifests/1.0', headers=auth)).status_code == 202
    upload = await rec.expect(A.CONTENT_UPLOAD, **{'target.name': 'images/app:1.0'})
    assert upload['principal']['auth'] == 'registry_token'
    assert upload['details']['digest'] == pushed.headers['docker-content-digest']
    deleted = await rec.expect(A.CONTENT_DELETE, **{'target.name': 'images/app:1.0'})
    assert deleted['target']['kind'] == 'docker_tag'

    # --- external identities: provisioning, linking, group sync (the code path every provider uses)
    ctx = audit.bind({'client_ip': '203.0.113.9'}, SimpleNamespace(state=SimpleNamespace(services=services)))
    try:
        identity = ExternalIdentity(
            provider='ldap:corp',
            subject='uid=dave,dc=example,dc=com',
            username='dave',
            display_name='Dave',
            email='dave@example.com',
            groups=(ExternalGroup('cn=ops,dc=example,dc=com', 'ops'),),
        )
        await link_identity(services.db, identity, provision=True)
        await link_identity(services.db, dataclasses.replace(identity, groups=()), provision=True)  # left the group
    finally:
        audit.reset(ctx)
    provisioned = await rec.expect(A.USER_CREATE, **{'target.name': 'dave'})
    assert provisioned['principal']['kind'] == 'system'
    assert provisioned['ip'] == '203.0.113.9'
    assert len(await rec.all(action='user.link', **{'target.name': 'dave'})) == 1
    await rec.expect(A.USER_LINK, **{'target.name': 'dave'})
    syncs = await rec.all(action='user.groups_sync', **{'target.name': 'dave'})
    assert sorted(s['changes'][0]['after'] for s in syncs) == [[], ['ops']]
    rec.seen.add(A.USER_GROUPS_SYNC)

    # --- instance settings
    saved = await admin.put(f'{api}/settings', json={'general': {'instance_name': 'Package mirror'}})
    assert saved.status_code == 200, saved.text
    settings_changed = await rec.expect(A.SETTINGS_UPDATE, **{'target.id': 'general'})
    assert settings_changed['changes'] == [
        {'field': 'instance_name', 'before': 'Waystation', 'after': 'Package mirror', 'redacted': False}
    ]

    # --- tasks: schedule edit, run now, dead-letter retry and discard
    await services.db[JOB_DEFINITIONS].insert_one(
        {'_id': 'blob.gc', 'kind': BlobGc.kind, 'params': {}, 'schedule': '0 */6 * * *', 'enabled': True}
    )
    assert (await admin.patch(f'{api}/tasks/scheduled/blob.gc', json={'schedule': '@daily'})).status_code == 200
    assert (await admin.post(f'{api}/tasks/scheduled/blob.gc/run')).status_code == 202
    dead = [
        str(
            await services.valkey.xadd(
                job_keys.DEAD, {'job': JobEnvelope.for_payload(BlobGc()).to_json(), 'error': 'x'}
            )
        )
        for _ in range(2)
    ]
    assert (await admin.post(f'{api}/tasks/dead-letters/{dead[0]}/retry')).status_code == 202
    assert (await admin.delete(f'{api}/tasks/dead-letters/{dead[1]}')).status_code == 204
    task_update = await rec.expect(A.TASK_UPDATE, **{'target.id': 'blob.gc'})
    assert task_update['changes'] == [
        {'field': 'schedule', 'before': '0 */6 * * *', 'after': '@daily', 'redacted': False}
    ]
    await rec.expect(A.TASK_RUN, **{'target.id': 'blob.gc'})
    await rec.expect(A.TASK_RETRY, **{'details.dead_letter': dead[0]})
    await rec.expect(A.TASK_DISCARD, **{'details.dead_letter': dead[1]})

    # --- sign-out
    signed_out = await admin.post(f'{api}/auth/logout')
    assert signed_out.status_code == 200
    assert signed_out.json() == {'end_session_url': None}  # local account: no provider sign-out page
    logout = await rec.expect(A.AUTH_LOGOUT, **{'principal.name': 'admin'})
    assert logout['target']['id'] == login['target']['id']

    # --- every emitted action was exercised; pending ones are owned by a later task
    assert rec.seen == set(AuditAction) - set(PENDING)

    # --- no record holds a secret value
    stored = json.dumps(await rec.all(), default=str)
    for secret in (
        PASSWORD,
        'wrong-password',
        token_secret,
        sa_token.json()['secret'],
        UPSTREAM_SECRET,
        'bind-Passw0rd-secret',
        'bind-Passw0rd-rotated',
        'erin-first-Passw0rd',
        'erin-reset-Passw0rd',
        'erin-own-Passw0rd-123',
    ):
        assert secret not in stored
    assert 'secret_enc' not in stored.replace('"proxy.auth.secret_enc"', '')


# ---------------------------------------------------------------------------- API
def _event(i: int, ts: datetime, **overrides: Any) -> AuditEvent:
    values: dict[str, Any] = {
        'action': 'repository.update' if i % 3 else 'token.create',
        'ts': ts,
        'outcome': Outcome.FAILURE if i % 5 == 0 else Outcome.SUCCESS,
        'actor': AuditActor(kind='user', id=f'id-user{i % 2}', name=f'user{i % 2}', auth='session'),
        'target': AuditTarget(kind='repository', id=f'repo-{i % 4}', name=f'repo{i % 4}'),
        'ip': '192.0.2.10',
        'details': {'n': i},
    }
    values.update(overrides)
    return AuditEvent(**values)


async def _clear(services: Services) -> None:
    await services.audit.flush()
    await services.db[AUDIT].delete_many({})


async def _seed(services: Services, count: int) -> datetime:
    base = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    for i in range(count):
        services.audit.record(_event(i, base + timedelta(minutes=i)))
    # two records in the same millisecond: the cursor must not skip or repeat either
    services.audit.record(_event(1000, base + timedelta(minutes=count)))
    services.audit.record(_event(1001, base + timedelta(minutes=count)))
    await services.audit.flush()
    return base


async def test_list_filters_and_cursor_paging(services: Services, admin: httpx.AsyncClient) -> None:
    await _clear(services)
    base = await _seed(services, 30)
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {'limit': 7, 'actor': 'user0'}
        if cursor:
            params['cursor'] = cursor
        page = await admin.get('/api/v1/audit', params=params)
        assert page.status_code == 200, page.text
        body = page.json()
        seen += [item['id'] for item in body['items']]
        cursor = body['next_cursor']
        if cursor is None:
            break
    user0 = await services.db[AUDIT].count_documents({'principal.name': 'user0'})
    assert len(seen) == len(set(seen)) == user0 == 16
    first = (await admin.get('/api/v1/audit', params={'limit': 1})).json()['items'][0]
    assert set(first) == {
        'id', 'ts', 'action', 'outcome', 'actor', 'target', 'client_ip', 'request_id', 'reason', 'details', 'changes'
    }  # fmt: skip

    async def total(**params: Any) -> int:
        response = await admin.get('/api/v1/audit', params={'limit': 200, **params})
        assert response.status_code == 200, response.text
        return len(response.json()['items'])

    assert await total(action='token.create') == 10  # i % 3 == 0 for i in 0..29
    assert await total(action='token.*') == await total(action='token.create')
    assert await total(action=['token.create', 'repository.update']) == 32
    assert await total(outcome='failure') == 6 + 1  # 0, 5, ..., 25 and 1000
    assert await total(target='repo1') == await total(target='repo-1') == 9  # i % 4 == 1: 8 of 0..29, and 1001
    window = {'from': (base + timedelta(minutes=10)).isoformat(), 'to': (base + timedelta(minutes=20)).isoformat()}
    assert await total(**window) == 10
    assert (await admin.get('/api/v1/audit', params={'action': 'bad action'})).status_code == 422
    assert (await admin.get('/api/v1/audit', params={'cursor': '!!!'})).status_code == 422
    assert (await admin.get('/api/v1/audit', params={'from': window['to'], 'to': window['from']})).status_code == 422


async def test_export_csv_and_ndjson(
    services: Services, admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _clear(services)
    services.audit.record(
        _event(1, datetime(2026, 11, 2, tzinfo=UTC), details={'note': '=HYPERLINK("http://example.com")'})
    )
    await _seed(services, 4)
    response = await admin.get('/api/v1/audit/export', params={'format': 'csv', 'actor': 'user1'})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/csv')
    assert response.headers['content-disposition'].startswith('attachment; filename="waystation-audit-')
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0][:3] == ['ts', 'action', 'outcome']
    assert len(rows) == 1 + 4  # user1: i = 1, 3, 1001 and the formula record
    details = [r[rows[0].index('details')] for r in rows[1:]]
    assert all(not d.startswith('=') for d in details)
    ndjson = await admin.get('/api/v1/audit/export', params={'format': 'ndjson'})
    assert ndjson.headers['content-type'].startswith('application/x-ndjson')
    lines = [json.loads(line) for line in ndjson.text.splitlines()]
    assert len(lines) == 7
    assert lines[0]['actor']['name'] in ('user0', 'user1')

    monkeypatch.setattr(audit_api, 'EXPORT_LIMIT', 3)
    cut = (await admin.get('/api/v1/audit/export', params={'format': 'ndjson'})).text.splitlines()
    assert len(cut) == 4 and json.loads(cut[-1]) == {'truncated': True}
    cut_csv = list(csv.reader(io.StringIO((await admin.get('/api/v1/audit/export')).text)))
    assert cut_csv[-1][0].startswith('# truncated')


async def test_audit_needs_audit_read(services: Services, admin: httpx.AsyncClient) -> None:
    await add_user(services, 'vera', ['viewer'])
    viewer = await sign_in(services, 'vera')
    async with asgi_client(services) as anon:
        assert (await viewer.get('/api/v1/audit')).status_code == 403
        assert (await viewer.get('/api/v1/audit/export')).status_code == 403
        assert (await anon.get('/api/v1/audit')).status_code == 401
        stats_token = await viewer.post('/api/v1/auth/me/tokens', json={'name': 'dash', 'scopes': ['stats-read']})
        assert (await anon.get('/api/v1/audit', headers=bearer(stats_token.json()['secret']))).status_code == 403
    await viewer.aclose()
    narrow = await admin.post('/api/v1/auth/me/tokens', json={'name': 'stats', 'scopes': ['stats-read']})
    wide = await admin.post('/api/v1/auth/me/tokens', json={'name': 'all', 'scopes': ['admin']})
    async with asgi_client(services) as script:
        assert (await script.get('/api/v1/audit', headers=bearer(narrow.json()['secret']))).status_code == 403
        assert (await script.get('/api/v1/audit', headers=bearer(wide.json()['secret']))).status_code == 200
    me = await admin.get('/api/v1/auth/me')
    assert 'audit.read' in me.json()['capabilities']


async def test_retention_index(services: Services) -> None:
    indexes = {ix['name']: ix async for ix in await services.db[AUDIT].list_indexes()}
    assert indexes['expires_at_ttl']['expireAfterSeconds'] == 0
    services.audit.record(_event(1, datetime.now(UTC)))
    await services.audit.flush()
    doc = await services.db[AUDIT].find_one({})
    assert doc is not None
    assert doc['expires_at'] - doc['ts'] == timedelta(days=30)


async def test_a_failing_audit_store_never_fails_the_request(services: Services, admin: httpx.AsyncClient) -> None:
    async def broken(batch: list[dict[str, Any]]) -> None:
        raise RuntimeError('mongo down')

    services.audit._insert = broken
    services.audit._retries = ()
    made = await admin.post('/api/v1/auth/me/tokens', json={'name': 'still works', 'scopes': ['read']})
    assert made.status_code == 201
    await services.audit.flush()
    assert services.audit.dropped >= 1


async def test_queue_overflow_drops_and_counts(services: Services, admin: httpx.AsyncClient) -> None:
    await services.audit.aclose()  # stop the drain task: the queue fills up
    services.audit._queue = asyncio.Queue(maxsize=2)
    for _ in range(5):
        made = await admin.post('/api/v1/auth/me/tokens', json={'name': secrets.token_hex(4), 'scopes': ['read']})
        assert made.status_code == 201
    assert services.audit.dropped == 3
    services.audit.start()
    await services.audit.flush()
    assert await services.db[AUDIT].count_documents({'action': 'token.create'}) == 2
