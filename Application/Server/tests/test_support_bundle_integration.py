"""``GET /api/v1/system/support-bundle`` against real MongoDB/Valkey (T-O10): an administrator gets a zip with every
section and no secret in it; others are refused; the download is audited; captured log records are included."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import zipfile
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_testing import asgi_client

from waystation_common.db.collections import AUDIT, JOB_RUNS, REPOSITORIES, USERS
from waystation_common.recent_errors import KEY as ERRORS_KEY
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.repos.model import secret_aad
from waystation_server.security import passwords
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
API = '/api/v1'
SESSION_SECRET = 'Session-S3cret-planted-for-the-bundle'
PROXY_SECRET = 'Proxy-S3cret-planted'
UPSTREAM_SECRET = 'Upstream-S3cret-planted'
BIND_SECRET = 'Bind-Passw0rd-planted'
LOG_SECRET = 'Logged-S3cret-planted'
PEM = (
    '-----BEGIN CERTIFICATE-----\n' + base64.encodebytes(b'certificate bytes').decode() + '-----END CERTIFICATE-----\n'
)
FILES = {
    'README.txt',
    'manifest.json',
    'versions.json',
    'settings.json',
    'repositories.json',
    'auth_providers.json',
    'health.json',
    'jobs.json',
    'errors.json',
}


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        bootstrap_admin_password=SecretStr(PASSWORD),
        login_rate_limit=10_000,
        session_secret=SecretStr(SESSION_SECRET),
        http_proxy=f'http://proxy:{PROXY_SECRET}@proxy.example.com:3128',
        instance_name='Lab mirror',
    )


async def sign_in(services: Services, username: str) -> httpx.AsyncClient:
    client = asgi_client(services)
    response = await client.post(f'{API}/auth/login', json={'username': username, 'password': PASSWORD})
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


async def _seed(services: Services, admin: httpx.AsyncClient) -> str:
    now = datetime.now(UTC)
    assert services.keyring is not None
    upstream_enc = services.keyring.encrypt_str(UPSTREAM_SECRET, aad=secret_aad('id-npm-proxy'))
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': 'id-npm-proxy',
            'name': 'npm-proxy',
            'format': 'npm',
            'type': 'proxy',
            'online': True,
            'proxy': {
                'upstream_url': f'https://ci:{UPSTREAM_SECRET}@registry.example.com/',
                'auth': {'kind': 'basic', 'username': 'ci', 'secret_enc': upstream_enc},
                'ca_pem': PEM,
            },
            'format_options': {},
            'created_at': now,
            'updated_at': now,
        }
    )
    services.catalog.invalidate()
    provider = {
        'kind': 'ldap',
        'name': 'Directory',
        'enabled': False,
        'order': 10,
        'ldap': {
            'connection': {
                'servers': ['ldap://127.0.0.1:1'],
                'security': 'none',
                'bind_dn': 'uid=svc,dc=example,dc=com',
                'bind_password': BIND_SECRET,
                'connect_timeout_s': 1,
                'timeout_s': 1,
            },
            'users': {'base_dn': 'dc=example,dc=com'},
            'groups': {'mode': 'none'},
        },
    }
    made = await admin.post(f'{API}/auth-providers', json=provider)
    assert made.status_code == 201, made.text
    await services.db[JOB_RUNS].insert_one(
        {
            '_id': 'run-failed-1',
            'job_id': 'job-1',
            'kind': 'blob.gc',
            'queue': 'maintenance',
            'attempt': 3,
            'status': 'dead',
            'started_at': now,
            'finished_at': now,
            'log': [f'line {i}' for i in range(200)],
            'error': 'it broke',
        }
    )
    logging.getLogger('waystation.test.bundle').error('upstream refused', extra={'password': LOG_SECRET})
    await services.recent_errors.write_pending()
    return str(made.json()['id'])


def _open(response: httpx.Response) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


async def test_an_administrator_downloads_a_redacted_bundle(services: Services, admin: httpx.AsyncClient) -> None:
    await _seed(services, admin)
    response = await admin.get(f'{API}/system/support-bundle')
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == 'application/zip'
    assert response.headers['cache-control'] == 'no-store'
    disposition = response.headers['content-disposition']
    assert disposition.startswith('attachment; filename="waystation-support-lab-mirror-')
    assert disposition.endswith('Z.zip"')

    files = _open(response)
    assert set(files) == FILES
    everything = response.content + b''.join(files.values())
    stored_enc = (await services.db[REPOSITORIES].find_one({'_id': 'id-npm-proxy'}))['proxy']['auth']['secret_enc']
    secrets = [SESSION_SECRET, PROXY_SECRET, UPSTREAM_SECRET, BIND_SECRET, LOG_SECRET, PASSWORD, stored_enc]
    secrets += [k.get_secret_value() for k in services.settings.secret_keys]
    for secret in secrets:
        assert secret.encode() not in everything, f'a secret reached the bundle: {secret[:6]}…'

    data = {name: json.loads(body) for name, body in files.items() if name.endswith('.json')}
    assert data['manifest.json']['instance_name'] == 'Lab mirror'
    assert data['manifest.json']['generated_by'] == 'admin'
    assert {f['name'] for f in data['manifest.json']['files']} == FILES - {'README.txt', 'manifest.json'}
    assert 'REVIEW THESE FILES' in files['README.txt'].decode()

    versions = data['versions.json']
    assert versions['mongodb']['version'] and versions['mongodb']['feature_compatibility_version']
    assert versions['valkey']['version']
    assert versions['migrations']['pending'] == [] and versions['migrations']['applied']

    variables = data['settings.json']['environment']['variables']
    assert variables['WAYSTATION_SESSION_SECRET'] == '<redacted>'
    assert variables['WAYSTATION_HTTP_PROXY'] == 'http://***@proxy.example.com:3128'
    assert variables['WAYSTATION_MONGO_DB'] == services.settings.mongo_db

    (repo,) = data['repositories.json']['items']
    assert repo['proxy']['auth'] == {'kind': 'basic', 'username': 'ci', 'secret_set': True}
    assert repo['proxy']['ca_pem']['certificates'] == 1
    assert repo['proxy']['upstream_url'] == 'https://***@registry.example.com/'
    (provider,) = data['auth_providers.json']['items']
    assert provider['ldap']['connection']['bind_password_set'] is True

    health = data['health.json']
    assert {c['name']: c['status'] for c in health['components']} == {'mongodb': 'ok', 'valkey': 'ok'}
    assert health['storage']['total_bytes'] > 0
    assert 'dead_letters' in health['workers']
    assert [u['repository'] for u in health['upstreams']] == ['npm-proxy']
    assert health['upstreams'][0]['health']['block']['failures'] == 0

    (run,) = data['jobs.json']['problem_runs']
    assert run['id'] == 'run-failed-1' and run['status'] == 'dead'
    assert run['log_tail'] == [f'line {i}' for i in range(180, 200)]
    assert data['jobs.json']['dead_letters']['total'] >= 0

    captured = [e for e in data['errors.json']['items'] if e.get('logger') == 'waystation.test.bundle']
    assert captured and captured[0]['msg'] == 'upstream refused'
    assert captured[0]['password'] == '<redacted>'
    assert captured[0]['component'] == 'server'

    for _ in range(100):  # the audit writer is asynchronous
        record = await services.db[AUDIT].find_one({'action': 'system.support_bundle'})
        if record:
            break
        await asyncio.sleep(0.05)
    assert record is not None
    assert record['principal']['name'] == 'admin'
    assert record['target'] == {'kind': 'system', 'id': 'support-bundle', 'name': 'Support bundle'}
    assert record['details']['files'] == len(FILES)


async def test_others_are_refused(services: Services, admin: httpx.AsyncClient) -> None:
    await services.db[USERS].insert_one(
        {
            '_id': 'id-rita',
            'username': 'rita',
            'display_name': 'Rita',
            'source': 'local',
            'password_hash': await passwords.hash_password(PASSWORD),
            'roles': ['reader'],
            'created_at': datetime.now(UTC),
        }
    )
    rita = await sign_in(services, 'rita')
    try:
        refused = await rita.get(f'{API}/system/support-bundle')
        assert refused.status_code == 403
        assert refused.json()['code'] == 'permission_required'
    finally:
        await rita.aclose()
    async with asgi_client(services) as anonymous:
        assert (await anonymous.get(f'{API}/system/support-bundle')).status_code == 401


async def test_a_bundle_is_produced_while_valkey_is_unusable(services: Services, admin: httpx.AsyncClient) -> None:
    await services.valkey.delete(ERRORS_KEY)
    await services.valkey.set(ERRORS_KEY, 'not a stream')  # every stream read fails: the section says so
    try:
        response = await admin.get(f'{API}/system/support-bundle')
    finally:
        await services.valkey.delete(ERRORS_KEY)
    assert response.status_code == 200
    errors = json.loads(_open(response)['errors.json'])
    assert errors == {'error': 'ResponseError'}
