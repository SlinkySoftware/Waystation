"""Declarative provisioning (T-O09) against real MongoDB/Valkey: plan and apply through the admin API and as
``init`` does, idempotence (applying twice changes nothing), updates with field diffs and redacted secrets,
adoption of existing objects, release vs guarded prune, invalid files applying nothing, secrets from files and the
environment (never in responses, logs or audit records), audit with ``details.source = provisioning``."""

import json
import os
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import SecretStr
from ws_testing import asgi_client

from waystation_common.db.collections import (
    AUDIT,
    AUTH_PROVIDERS,
    CLEANUP_POLICIES,
    GROUPS,
    REPOSITORIES,
    ROLES,
    SERVICE_ACCOUNTS,
    USERS,
    WEBHOOKS,
)
from waystation_common.settings import Settings
from waystation_server import init
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.provisioning import runner
from waystation_server.repos.model import secret_aad
from waystation_server.security import passwords
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
API = '/api/v1'
HUB_SECRET = 'hub-token-0123456789'
BIND_PASSWORD = 'bind-password-abcdef'
OIDC_SECRET = 'oidc-client-secret-xyz'
HOOK_SECRET = 'webhook-signing-key-0123456789'
HOOK_HEADER = 'Bearer header-credential-42'
SECRETS = (HUB_SECRET, BIND_PASSWORD, OIDC_SECRET, HOOK_SECRET, HOOK_HEADER, 'rotated-hub-token-9876')


@pytest.fixture
def secrets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / 'secrets'
    folder.mkdir()
    (folder / 'hub').write_text(HUB_SECRET + '\n')
    (folder / 'bind').write_text(BIND_PASSWORD)
    (folder / 'hook').write_text(HOOK_SECRET + '\n')
    monkeypatch.setenv('TEST_OIDC_SECRET', OIDC_SECRET)
    monkeypatch.setenv('TEST_HOOK_HEADER', HOOK_HEADER)
    return folder


@pytest.fixture
def provision_file(tmp_path: Path) -> Path:
    return tmp_path / 'provisioning.yaml'


@pytest.fixture
def settings(settings_factory: Callable[..., Settings], provision_file: Path) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(PASSWORD), provision_file=provision_file)


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post(f'{API}/auth/login', json={'username': 'admin', 'password': PASSWORD})
        assert response.status_code == 200, response.text
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


def document(secrets: Path) -> dict[str, Any]:
    return {
        'version': 1,
        'roles': [
            {
                'id': 'docker-pull',
                'name': 'Docker pull',
                'permissions': [
                    {'action': 'browse', 'scope': 'format:docker'},
                    {'action': 'read', 'scope': 'format:docker'},
                ],
            }
        ],
        'cleanup_policies': [
            {'name': 'stale', 'format': '*', 'criteria': {'last_downloaded_older_than_days': 90}, 'schedule': '@daily'}
        ],
        'repositories': [
            {
                'name': 'docker-hub',
                'format': 'docker',
                'type': 'proxy',
                'description': 'Docker Hub',
                'proxy': {
                    'upstream_url': 'https://registry-1.docker.io',
                    'auth': {'kind': 'basic', 'username': 'puller', 'secret': {'file': str(secrets / 'hub')}},
                },
                'format_options': {'docker_hub': True, 'aliases': ['docker.io']},
                'cleanup_policies': ['stale'],
            },
            {'name': 'docker-internal', 'format': 'docker', 'type': 'hosted'},
            {
                'name': 'docker',
                'format': 'docker',
                'type': 'group',
                'group': {'members': ['docker-internal', 'docker-hub'], 'writable_member': 'docker-internal'},
            },
            {
                'name': 'npmjs',
                'format': 'npm',
                'type': 'proxy',
                'proxy': {'upstream_url': 'https://registry.npmjs.org'},
            },
        ],
        'groups': [{'name': 'Pullers', 'roles': ['docker-pull']}],
        'service_accounts': [{'name': 'ci-builder', 'roles': ['deployer']}],
        'auth_providers': [
            {
                'kind': 'ldap',
                'name': 'Directory',
                'ldap': {
                    'connection': {
                        'servers': ['ldaps://ldap.example.com'],
                        'security': 'ldaps',
                        'bind_dn': 'cn=waystation,dc=example,dc=com',
                        'bind_password': {'file': str(secrets / 'bind')},
                    },
                    'users': {'base_dn': 'ou=people,dc=example,dc=com'},
                    'groups': {'mode': 'member_of'},
                },
                'group_mapping': [{'external_group': 'cn=devs,dc=example,dc=com', 'roles': ['deployer']}],
            },
            {
                'kind': 'oidc',
                'name': 'SSO',
                'oidc': {
                    'issuer': 'https://sso.example.com/realms/example',
                    'client_id': 'waystation',
                    'client_secret': {'env': 'TEST_OIDC_SECRET'},
                },
            },
        ],
        'webhooks': [
            {
                'name': 'notify',
                'url': 'https://hooks.example.com/in',
                'events': ['repository.*'],
                'secret': {'file': str(secrets / 'hook')},
                'headers': {'Authorization': {'env': 'TEST_HOOK_HEADER'}},
            }
        ],
        'settings': {
            'instance_name': 'Example registry',
            'anonymous_access': ['anonymous', 'docker-pull'],
            'default_docker_repository': 'docker-hub',
            'require_totp_for_admins': False,
            'storage_guard': {'min_free_bytes': 0, 'min_free_percent': 0},
        },
    }


def write(path: Path, doc: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def assert_no_secret(value: Any) -> None:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    for secret in SECRETS:
        assert secret not in text


async def plan(admin: httpx.AsyncClient) -> dict[str, Any]:
    response = await admin.post(f'{API}/provisioning/plan')
    assert response.status_code == 200, response.text
    assert_no_secret(response.text)
    body: dict[str, Any] = response.json()
    return body


async def apply(admin: httpx.AsyncClient) -> dict[str, Any]:
    response = await admin.post(f'{API}/provisioning/apply')
    assert response.status_code == 200, response.text
    assert_no_secret(response.text)
    body: dict[str, Any] = response.json()
    return body


def actions(result: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {(i['kind'], i['name']): i['action'] for i in result['items']}


async def audit_records(services: Services) -> list[dict[str, Any]]:
    await services.audit.flush()
    return [d async for d in services.db[AUDIT].find({'details.source': 'provisioning'})]


async def test_apply_creates_everything_and_a_second_apply_is_a_no_op(
    services: Services, admin: httpx.AsyncClient, provision_file: Path, secrets_dir: Path
) -> None:
    write(provision_file, document(secrets_dir))
    planned = await plan(admin)
    assert planned['valid'] is True, planned['errors']
    # settings sections are updates of the defaults (or unchanged where the file says what they already are)
    assert set(actions(planned).values()) == {'create', 'update', 'unchanged'}
    assert planned['summary']['create'] == 11
    assert await services.db[REPOSITORIES].count_documents({}) == 0  # a plan writes nothing

    applied = await apply(admin)
    assert applied['applied'] is True
    assert applied['summary']['failed'] == 0, [i for i in applied['items'] if i['outcome'] == 'failed']
    assert {i['outcome'] for i in applied['items'] if i['action'] != 'unchanged'} == {'applied'}

    # everything is tagged and works like an API-made object
    for collection in (REPOSITORIES, ROLES, CLEANUP_POLICIES, SERVICE_ACCOUNTS, AUTH_PROVIDERS, WEBHOOKS):
        docs = [d async for d in services.db[collection].find({})]
        assert docs and all(d.get('managed_by') == 'provisioning' for d in docs), collection
    group = await services.db[GROUPS].find_one({'name': 'Pullers'})
    assert group is not None and group['managed_by'] == 'provisioning'
    hub = await services.db[REPOSITORIES].find_one({'name': 'docker-hub'})
    assert hub is not None
    assert services.keyring is not None
    assert services.keyring.decrypt_str(hub['proxy']['auth']['secret_enc'], aad=secret_aad(hub['_id'])) == HUB_SECRET
    assert hub['format_options']['default_repo'] is True
    policy = await services.db[CLEANUP_POLICIES].find_one({'name': 'stale'})
    assert policy is not None and hub['cleanup_policy_ids'] == [policy['_id']]
    docker = await services.db[REPOSITORIES].find_one({'name': 'docker'})
    assert docker is not None and docker['group']['members'] == ['docker-internal', 'docker-hub']
    view = (await admin.get(f'{API}/repositories/docker-hub')).json()
    assert view['managed_by'] == 'provisioning'
    settings_view = (await admin.get(f'{API}/settings')).json()
    assert settings_view['general']['instance_name'] == 'Example registry'
    assert (await admin.get(f'{API}/anonymous-access')).json() == {'roles': ['anonymous', 'docker-pull']}
    providers = (await admin.get(f'{API}/auth-providers')).json()
    ldap = next(p for p in providers if p['kind'] == 'ldap')
    assert ldap['ldap']['connection']['bind_password_set'] is True and ldap['managed_by'] == 'provisioning'
    assert ldap['group_mapping'] == [{'external_group': 'cn=devs,dc=example,dc=com', 'roles': ['deployer']}]

    # audited with the source, as the administrator, without secrets
    records = await audit_records(services)
    assert {r['action'] for r in records} >= {
        'role.create',
        'cleanup_policy.create',
        'repository.create',
        'group.create',
        'service_account.create',
        'auth_provider.create',
        'webhook.create',
        'settings.update',
        'role.update',
    }
    assert all(r['principal']['name'] == 'admin' for r in records)
    assert_no_secret(records)
    count = len(records)

    # the second plan and apply change nothing
    again = await plan(admin)
    assert set(actions(again).values()) == {'unchanged'}, [i for i in again['items'] if i['action'] != 'unchanged']
    second = await apply(admin)
    assert second['summary'] == {'create': 0, 'update': 0, 'unchanged': len(second['items']), 'prune': 0, 'failed': 0}
    assert len(await audit_records(services)) == count
    status = (await admin.get(f'{API}/provisioning')).json()
    assert status['configured'] is True
    assert status['last_apply']['outcome'] == 'applied' and status['last_apply']['by'] == 'admin'
    assert status['last_apply']['sha256'] == second['sha256']


async def test_updates_show_field_diffs_and_redacted_secrets(
    services: Services, admin: httpx.AsyncClient, provision_file: Path, secrets_dir: Path
) -> None:
    doc = document(secrets_dir)
    write(provision_file, doc)
    assert (await apply(admin))['summary']['failed'] == 0

    changed = deepcopy(doc)
    changed['repositories'][0]['description'] = 'Docker Hub mirror'
    changed['service_accounts'][0]['disabled'] = True
    changed['auth_providers'][0]['group_mapping'][0]['roles'] = ['reader']
    write(provision_file, changed)
    (secrets_dir / 'hub').write_text('rotated-hub-token-9876\n')

    planned = await plan(admin)
    hub = next(i for i in planned['items'] if i['name'] == 'docker-hub')
    assert hub['action'] == 'update'
    assert {'field': 'description', 'before': 'Docker Hub', 'after': 'Docker Hub mirror', 'redacted': False} in hub[
        'changes'
    ]
    assert {'field': 'proxy.auth.secret', 'before': '***', 'after': '***', 'redacted': True} in hub['changes']
    account = next(i for i in planned['items'] if i['name'] == 'ci-builder')
    assert account['changes'] == [{'field': 'disabled', 'before': False, 'after': True, 'redacted': False}]
    assert actions(planned)[('auth_provider', 'Directory')] == 'update'
    assert actions(planned)[('webhook', 'notify')] == 'unchanged'

    assert (await apply(admin))['summary']['failed'] == 0
    stored = await services.db[REPOSITORIES].find_one({'name': 'docker-hub'})
    assert stored is not None and services.keyring is not None
    secret = services.keyring.decrypt_str(stored['proxy']['auth']['secret_enc'], aad=secret_aad(stored['_id']))
    assert secret == 'rotated-hub-token-9876'
    update = next(r for r in await audit_records(services) if r['action'] == 'repository.update')
    assert any(c['field'] == 'proxy.auth.secret' and c['redacted'] for c in update['changes'])
    assert_no_secret(update)
    assert set(actions(await plan(admin)).values()) == {'unchanged'}


async def test_existing_objects_are_adopted(
    services: Services, admin: httpx.AsyncClient, provision_file: Path, secrets_dir: Path
) -> None:
    made = await admin.post(
        f'{API}/repositories',
        json={
            'name': 'npmjs',
            'format': 'npm',
            'type': 'proxy',
            'online': True,
            'proxy': {'upstream_url': 'https://registry.npmjs.org'},
        },
    )
    assert made.status_code == 201, made.text
    assert made.json()['managed_by'] is None
    write(provision_file, {'version': 1, 'repositories': [document(secrets_dir)['repositories'][3]]})
    planned = await plan(admin)
    assert planned['items'][0]['action'] == 'update'
    assert planned['items'][0]['changes'] == [
        {'field': 'managed_by', 'before': None, 'after': 'provisioning', 'redacted': False}
    ]
    await apply(admin)
    assert (await admin.get(f'{API}/repositories/npmjs')).json()['managed_by'] == 'provisioning'


async def test_objects_left_out_are_released_unless_pruned(
    services: Services, admin: httpx.AsyncClient, provision_file: Path, secrets_dir: Path
) -> None:
    doc = document(secrets_dir)
    write(provision_file, doc)
    assert (await apply(admin))['summary']['failed'] == 0

    # no prune: left-out objects stay, untagged
    smaller = deepcopy(doc)
    smaller['repositories'] = [r for r in doc['repositories'] if r['name'] != 'npmjs']
    smaller['service_accounts'] = []
    write(provision_file, smaller)
    planned = await plan(admin)
    released = next(i for i in planned['items'] if i['name'] == 'npmjs')
    assert released['action'] == 'update' and 'released' in released['note']
    await apply(admin)
    npm = await services.db[REPOSITORIES].find_one({'name': 'npmjs'})
    assert npm is not None and npm.get('managed_by') is None
    assert await services.db[SERVICE_ACCOUNTS].count_documents({'name': 'ci-builder', 'managed_by': None}) == 1

    # prune deletes what the file manages and no longer declares
    write(provision_file, doc)
    await apply(admin)  # adopt both again
    await services.db[USERS].insert_one(
        {
            '_id': 'id-puller',
            'username': 'puller',
            'source': 'local',
            'roles': ['docker-pull'],
            'created_at': datetime.now(UTC),
        }
    )
    pruning = deepcopy(smaller)
    pruning['prune'] = {'repositories': True, 'service_accounts': True, 'roles': True}
    pruning['roles'] = []  # docker-pull is still held by a user and named by the anonymous access
    pruning['groups'] = [{'name': 'Pullers', 'roles': ['reader']}]
    write(provision_file, pruning)
    planned = await plan(admin)
    assert planned['valid'] is False  # the anonymous access still names the pruned role
    assert any(e['field'] == 'settings.anonymous_access.1' for e in planned['errors'])
    del pruning['settings']['anonymous_access']
    write(provision_file, pruning)
    planned = await plan(admin)
    assert actions(planned)[('repository', 'npmjs')] == 'prune'
    assert actions(planned)[('service_account', 'ci-builder')] == 'prune'
    assert actions(planned)[('role', 'docker-pull')] == 'prune'
    result = await apply(admin)
    outcomes = {(i['kind'], i['name']): (i['outcome'], i['error']) for i in result['items']}
    assert outcomes[('repository', 'npmjs')] == ('applied', None)
    assert outcomes[('service_account', 'ci-builder')] == ('applied', None)
    # still held by a user: refused like in the API, the rest applied
    outcome, error = outcomes[('role', 'docker-pull')]
    assert outcome == 'failed' and error is not None and 'still' in error.lower()
    assert result['summary']['failed'] == 1
    assert await services.db[REPOSITORIES].count_documents({'name': 'npmjs'}) == 0
    assert await services.db[SERVICE_ACCOUNTS].count_documents({'name': 'ci-builder'}) == 0
    status = (await admin.get(f'{API}/provisioning')).json()
    assert status['last_apply']['outcome'] == 'failed'
    deleted = [r for r in await audit_records(services) if r['action'] == 'repository.delete']
    assert deleted and deleted[0]['target']['name'] == 'npmjs'


async def test_an_invalid_file_applies_nothing(
    services: Services, admin: httpx.AsyncClient, provision_file: Path, secrets_dir: Path
) -> None:
    doc = document(secrets_dir)
    doc['repositories'][0]['proxy']['auth']['secret'] = HUB_SECRET  # inline: refused
    write(provision_file, doc)
    result = await apply(admin)
    assert result['valid'] is False and result['applied'] is False
    assert result['errors'][0]['field'] == 'repositories.0.proxy.auth.secret'
    assert await services.db[ROLES].count_documents({}) == 0

    doc = document(secrets_dir)
    doc['webhooks'][0]['headers']['Authorization'] = {'env': 'TEST_UNSET_VARIABLE'}
    doc['repositories'][1]['format_options'] = {'aliases': ['quay.io']}  # aliases are for proxies
    doc['groups'][0]['roles'] = ['no-such-role']
    write(provision_file, doc)
    result = await apply(admin)
    fields = {e['field'] for e in result['errors']}
    assert {
        'webhooks.0.headers.Authorization',
        'repositories.1.format_options.aliases',
        'groups.0.roles.0',
    } <= fields
    assert await services.db[REPOSITORIES].count_documents({}) == 0
    assert (await admin.get(f'{API}/provisioning')).json()['last_apply']['outcome'] == 'invalid'


async def test_the_api_needs_admin_and_a_configured_file(
    services: Services, admin: httpx.AsyncClient, settings_factory: Callable[..., Settings]
) -> None:
    await services.db[USERS].insert_one(
        {
            '_id': 'id-reader',
            'username': 'reader',
            'display_name': 'Reader',
            'source': 'local',
            'password_hash': await passwords.hash_password(PASSWORD),
            'roles': ['reader', 'deployer'],
            'created_at': datetime.now(UTC),
        }
    )
    async with asgi_client(services) as other:
        login = await other.post(f'{API}/auth/login', json={'username': 'reader', 'password': PASSWORD})
        other.headers['X-CSRF-Token'] = login.json()['csrf_token']
        assert (await other.get(f'{API}/provisioning')).status_code == 403
        assert (await other.post(f'{API}/provisioning/apply')).status_code == 403
    missing = await admin.post(f'{API}/provisioning/plan')
    assert missing.status_code == 200  # configured but absent: reported, not an error
    assert missing.json()['valid'] is False and missing.json()['errors'][0]['field'] == 'file'

    unconfigured = Services.create(settings_factory(), mongo_client=None)
    await unconfigured.start()
    try:
        await ensure_bootstrap_admin(unconfigured.db, unconfigured.settings)
        async with asgi_client(unconfigured) as client:
            login = await client.post(f'{API}/auth/login', json={'username': 'admin', 'password': PASSWORD})
            client.headers['X-CSRF-Token'] = login.json()['csrf_token']
            assert (await client.get(f'{API}/provisioning')).json() == {
                'configured': False,
                'file': None,
                'last_apply': None,
            }
            assert (await client.post(f'{API}/provisioning/plan')).status_code == 409
    finally:
        await unconfigured.stop()


async def test_init_applies_the_file_as_system(
    services: Services, settings: Settings, provision_file: Path, secrets_dir: Path, tmp_path: Path
) -> None:
    write(provision_file, document(secrets_dir))
    result = await runner.run(settings, provision_file, apply=True, by='init')
    assert result.applied and result.summary.failed == 0
    records = await audit_records(services)
    assert records and all(r['principal']['kind'] == 'system' for r in records)
    assert_no_secret(runner.render(result))

    # init: an invalid file fails the run (the server then does not start)
    write(provision_file, {'version': 1, 'roles': [{'id': 'admin', 'name': 'Admin', 'permissions': []}]})
    init_settings = settings.model_copy(
        update={'uid': os.geteuid(), 'gid': os.getegid(), 'data_dir': tmp_path / 'init'}
    )
    with pytest.raises(init.ProvisioningError):
        await init.run(init_settings)
