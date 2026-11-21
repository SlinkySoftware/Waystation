"""Nexus import (T-M01) against a real Nexus Repository 3 CE container: ``make test-nexus`` (opt-in, about 3 GB of
memory; never in ``make check`` or CI by default).

The test seeds Nexus through its REST API with the repositories of the hand-written fixtures
(``tests/fixtures/nexus3``: every detail document is posted back as the create request), then checks that the importer
reads the real Nexus exactly like it reads the fixtures (same plan, item by item), and applies the plan. So a run
proves both the importer against a real Nexus and that the fixtures the unit and integration tests use have the
real shapes. Nexus never contacts an upstream: proxies only store their URL.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream
from ws_nexus import DETAIL_PATH, detail, listing, nexus_routes
from ws_testing import asgi_client

from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.services import Services

NEXUS = os.environ.get('WAYSTATION_TEST_NEXUS_URL', '')
pytestmark = [pytest.mark.nexus, pytest.mark.skipif(not NEXUS, reason='no Nexus container (make test-nexus)')]

ADMIN_PASSWORD = 'correct horse battery staple'
NEXUS_USER = 'admin'
NEXUS_INITIAL = 'admin123'  # NEXUS_SECURITY_RANDOMPASSWORD=false (Build/Containers/compose.nexus.yaml)
NEXUS_PASSWORD = 'nexus-live-test-password'  # throwaway, set by the seed
UPSTREAM_SECRET = 'upstream-test-secret'  # the ghcr-proxy upstream password (Nexus never returns it)
API = '/service/rest/v1'
PREVIEW = '/api/v1/import/nexus/preview'
APPLY = '/api/v1/import/nexus/apply'


def _ok(response: httpx.Response, *allowed: int) -> httpx.Response:
    assert response.status_code in (allowed or (200, 201, 204)), f'{response.request.url}: {response.text[:500]}'
    return response


def _create_body(entry: dict[str, Any], with_cleanup: bool) -> dict[str, Any]:
    """The create request for a fixture repository: its detail document without the read-only fields."""
    body = copy.deepcopy(detail(entry['name']) or {'name': entry['name'], 'online': True})
    for key in ('url', 'format', 'type'):
        body.pop(key, None)
    body.setdefault('storage', {'blobStoreName': 'default', 'strictContentTypeValidation': True})
    if entry['type'] == 'proxy':
        body.setdefault(
            'proxy',
            {'remoteUrl': entry['attributes']['proxy']['remoteUrl'], 'contentMaxAge': 1440, 'metadataMaxAge': 1440},
        )
        body.setdefault('negativeCache', {'enabled': True, 'timeToLive': 1440})
        body.setdefault('httpClient', {'blocked': False, 'autoBlock': True})
        auth = (body.get('httpClient') or {}).get('authentication')
        if auth and not auth.get('password'):
            auth['password'] = UPSTREAM_SECRET
    if not with_cleanup and body.get('cleanup'):
        body['cleanup'] = None
    return body


def seed(nexus: str) -> dict[str, Any]:
    """Prepare a new Nexus container: admin password, EULA, the extra blob store and cleanup policy the fixtures
    name, then exactly the fixture repositories. Returns what could not be reproduced: ``cleanup`` is False when this
    Nexus has no cleanup policy API, ``pro_only`` lists the groups created without their writable member (a Pro
    feature that Community Edition refuses)."""
    with httpx.Client(base_url=nexus, timeout=60) as http:
        auth = (NEXUS_USER, NEXUS_PASSWORD)
        _ok(
            http.put(
                f'{API}/security/users/admin/change-password',
                auth=(NEXUS_USER, NEXUS_INITIAL),
                content=NEXUS_PASSWORD,
                headers={'Content-Type': 'text/plain'},
            )
        )
        eula = http.get(f'{API}/system/eula', auth=auth)
        if eula.status_code == 200 and not eula.json().get('accepted'):
            _ok(http.post(f'{API}/system/eula', auth=auth, json={**eula.json(), 'accepted': True}))
        stores = {s['name'] for s in _ok(http.get(f'{API}/blobstores', auth=auth)).json()}
        if 'docker' not in stores:
            _ok(http.post(f'{API}/blobstores/file', auth=auth, json={'name': 'docker', 'path': 'docker'}))
        policies = http.get(f'{API}/cleanup-policies', auth=auth)
        cleanup = policies.status_code == 200
        if cleanup and 'docker-unused-90d' not in {p['name'] for p in policies.json()}:
            created = http.post(
                f'{API}/cleanup-policies',
                auth=auth,
                json={'name': 'docker-unused-90d', 'format': 'docker', 'criteriaLastDownloaded': 90},
            )
            cleanup = created.status_code in (200, 201, 204)
        pro_only: list[str] = []
        # Start from an empty Nexus: a new one comes with sample repositories (maven-central, maven-public, nuget-*).
        for repo in sorted(_ok(http.get(f'{API}/repositories', auth=auth)).json(), key=lambda r: r['type'] != 'group'):
            _ok(http.delete(f'{API}/repositories/{repo["name"]}', auth=auth))
        for entry in listing():  # members come before their groups in the fixture list
            fmt = DETAIL_PATH.get(entry['format'], entry['format'])
            url = f'{API}/repositories/{fmt}/{entry["type"]}'
            body = _create_body(entry, cleanup)
            created = http.post(url, auth=auth, json=body)
            if (
                created.status_code == 400
                and 'PRO-licensed' in created.text
                and 'writableMember' in body.get('group', {})
            ):
                del body['group']['writableMember']  # Community Edition refuses a writable group member
                pro_only.append(entry['name'])
                created = http.post(url, auth=auth, json=body)
            _ok(created)
        return {'cleanup': cleanup, 'pro_only': pro_only}


@pytest.fixture(scope='module')
def seeded() -> dict[str, Any]:
    return seed(NEXUS)


@pytest.fixture
def settings(settings_factory: Any) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(ADMIN_PASSWORD))  # type: ignore[no-any-return]


@pytest.fixture
async def admin(services: Services) -> httpx.AsyncClient:
    await ensure_bootstrap_admin(services.db, services.settings)
    client = asgi_client(services)
    response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': ADMIN_PASSWORD})
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return client


@pytest.fixture
async def fake() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream(nexus_routes()) as upstream:
        yield upstream


def _comparable(item: dict[str, Any], cleanup: bool) -> dict[str, Any]:
    differences = sorted(
        (d['field'], d['nexus'], d['waystation'], d['note'])
        for d in item['differences']
        if cleanup or d['field'] != 'cleanup.policyNames'
    )
    return {**item, 'differences': differences}


def _flat(value: Any, path: str = '') -> dict[str, Any]:
    """Leaf values by dotted path, so a mismatch names the exact setting."""
    if isinstance(value, dict):
        return {k: v for key, item in value.items() for k, v in _flat(item, f'{path}.{key}' if path else key).items()}
    if isinstance(value, list | tuple) and any(isinstance(item, dict | list | tuple) for item in value):
        return {k: v for i, item in enumerate(value) for k, v in _flat(item, f'{path}[{i}]').items()}
    return {path: value}


async def test_real_nexus_reads_like_the_fixtures_and_imports(
    seeded: dict[str, Any], admin: httpx.AsyncClient, fake: FakeUpstream
) -> None:
    real_body = {'url': NEXUS, 'username': NEXUS_USER, 'password': NEXUS_PASSWORD, 'allow_private': True}
    fake_body = {**real_body, 'url': fake.url, 'password': 'nexus-test-password'}

    real = await admin.post(PREVIEW, json=real_body)
    assert real.status_code == 200, real.text
    report = real.json()
    assert (report['nexus_version'] or '').startswith('Nexus/3.'), report['nexus_version']
    assert NEXUS_PASSWORD not in real.text and UPSTREAM_SECRET not in real.text
    from_fixtures = (await admin.post(PREVIEW, json=fake_body)).json()

    real_items = {i['name']: _comparable(i, seeded['cleanup']) for i in report['items']}
    fixture_items = {i['name']: _comparable(i, seeded['cleanup']) for i in from_fixtures['items']}
    assert sorted(real_items) == sorted(fixture_items)
    # npm has no detail fixture on purpose (the list fallback of older Nexus releases); a real Nexus has the detail
    # endpoint, and the seed made up its ages. Compare only what the list fallback maps.
    for name in [n for n in fixture_items if detail(n) is None]:
        real_item, fixture_item = real_items.pop(name), fixture_items.pop(name)
        assert real_item['repository']['proxy']['upstream_url'] == fixture_item['repository']['proxy']['upstream_url']
        assert not real_item['notes'], real_item['notes']  # read from the detail endpoint, no fallback
    for name in seeded['pro_only']:  # Community Edition: created without the writable member
        fixture_items[name]['repository']['group']['writable_member'] = None
    real_flat, fixture_flat = _flat(real_items), _flat(fixture_items)
    mismatched = {
        path: {'real': real_flat.get(path), 'fixtures': fixture_flat.get(path)}
        for path in sorted(real_flat.keys() | fixture_flat.keys())
        if real_flat.get(path) != fixture_flat.get(path)
    }
    if mismatched:
        pytest.fail('the real Nexus reads differently from the fixtures:\n' + json.dumps(mismatched, indent=1))
    assert report['summary'] == from_fixtures['summary']
    assert report['default_docker_repo'] == from_fixtures['default_docker_repo']

    applied = await admin.post(APPLY, json=real_body)
    assert applied.status_code == 200, applied.text
    result = applied.json()
    creates = {i['name'] for i in report['items'] if i['action'] == 'create'}
    assert {i['name'] for i in result['items'] if i['outcome'] == 'created'} == creates
    assert {
        'dockerhub-proxy',
        'docker-group',
        'debian-security',
        'npm',
        'files',
        'raw-group',
        'maven-central',
    } <= creates
    assert {i['name']: i['action'] for i in report['items']}['r-hosted'] == 'unsupported'

    hub = (await admin.get('/api/v1/repositories/dockerhub-proxy')).json()
    assert hub['format_options'] == {'aliases': ['docker.io'], 'docker_hub': True, 'default_repo': True}
    assert hub['proxy']['upstream_url'] == 'https://registry-1.docker.io'
    assert (await admin.get('/api/v1/config')).json()['docker']['default_repo'] == 'dockerhub-proxy'
    group = (await admin.get('/api/v1/repositories/docker-group')).json()
    writable = None if 'docker-group' in seeded['pro_only'] else 'docker-hosted'
    assert group['group'] == {
        'members': ['docker-hosted', 'dockerhub-proxy', 'ghcr-proxy'],
        'writable_member': writable,
    }
    ghcr = (await admin.get('/api/v1/repositories/ghcr-proxy')).json()
    assert ghcr['format_options']['aliases'] == ['ghcr.io']
    debian = (await admin.get('/api/v1/repositories/debian-security')).json()
    assert debian['format_options'] == {
        'distribution': 'trixie-security',
        'flat': False,
        'components': ['main'],
        'architectures': ['amd64', 'arm64'],
    }
    assert (await admin.get('/api/v1/repositories/oracle')).json()['proxy']['offline'] is True
    files = (await admin.get('/api/v1/repositories/files')).json()
    assert (files['type'], files['hosted']['write_policy']) == ('hosted', 'disable_redeploy')
    raw_group = (await admin.get('/api/v1/repositories/raw-group')).json()
    assert raw_group['group']['members'] == ['files', 'github-releases']
    maven = (await admin.get('/api/v1/repositories/maven-central')).json()
    assert maven['format_options'] == {'version_policy': 'release', 'layout_policy': 'strict'}

    again = (await admin.post(APPLY, json=real_body)).json()
    assert again['summary']['created'] == 0
    assert (await admin.get('/api/v1/repositories')).json()['total'] == len(creates)
