"""Nexus import (T-M01) through the REST API against real MongoDB/Valkey and a fake Nexus 3 serving the
hand-written fixtures in ``tests/fixtures/nexus3`` (never a real Nexus)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream
from ws_nexus import PASSWORD, USER, nexus_routes
from ws_testing import asgi_client

from waystation_common.db.collections import AUDIT
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.services import Services

pytestmark = pytest.mark.integration

ADMIN_PASSWORD = 'correct horse battery staple'
PREVIEW = '/api/v1/import/nexus/preview'
APPLY = '/api/v1/import/nexus/apply'


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
async def nexus() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream(nexus_routes()) as upstream:
        yield upstream


def body(nexus: FakeUpstream, **extra: Any) -> dict[str, Any]:
    return {'url': nexus.url, 'username': USER, 'password': PASSWORD, 'allow_private': True, **extra}


async def test_preview_then_apply_is_idempotent(
    services: Services, admin: httpx.AsyncClient, nexus: FakeUpstream
) -> None:
    preview = await admin.post(PREVIEW, json=body(nexus))
    assert preview.status_code == 200, preview.text
    report = preview.json()
    assert PASSWORD not in preview.text
    creates = {i['name'] for i in report['items'] if i['action'] == 'create'}
    assert {'dockerhub-proxy', 'ghcr-proxy', 'debian', 'ubuntu-security', 'rocky', 'npm', 'github-releases'} <= creates
    assert (await admin.get('/api/v1/repositories')).json()['total'] == 0  # a preview writes nothing

    applied = await admin.post(APPLY, json=body(nexus))
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result['applied'] is True
    outcomes = {i['name']: i['outcome'] for i in result['items']}
    assert {n for n, o in outcomes.items() if o == 'created'} == creates
    assert result['summary']['created'] == len(creates)
    assert outcomes['maven-central'] == 'created'  # T-P5-MAVEN
    maven = (await admin.get('/api/v1/repositories/maven-central')).json()
    assert maven['format_options'] == {'version_policy': 'release', 'layout_policy': 'strict'}
    assert maven['proxy']['upstream_url'] == 'https://repo.maven.apache.org/maven2/'

    hub = (await admin.get('/api/v1/repositories/dockerhub-proxy')).json()
    assert hub['format_options'] == {'aliases': ['docker.io'], 'docker_hub': True, 'default_repo': True}
    assert hub['proxy']['upstream_url'] == 'https://registry-1.docker.io'
    assert hub['proxy']['metadata_max_age_s'] == 86400
    assert (await admin.get('/api/v1/config')).json()['docker']['default_repo'] == 'dockerhub-proxy'
    debian = (await admin.get('/api/v1/repositories/debian-security')).json()
    assert debian['format_options'] == {'distribution': 'trixie-security', 'flat': False}
    oracle = (await admin.get('/api/v1/repositories/oracle')).json()
    assert oracle['proxy']['offline'] is True
    ghcr = (await admin.get('/api/v1/repositories/ghcr-proxy')).json()
    assert ghcr['proxy']['auth'] == {'kind': 'none', 'username': None, 'secret_set': False}
    assert {'docker-hosted', 'docker-group'} <= creates  # the group is created after its members
    group = (await admin.get('/api/v1/repositories/docker-group')).json()
    assert group['group']['members'] == ['docker-hosted', 'dockerhub-proxy', 'ghcr-proxy']
    assert group['group']['writable_member'] == 'docker-hosted'
    assert (await admin.get('/api/v1/repositories/docker-hosted')).json()['type'] == 'hosted'
    assert {'files', 'raw-group'} <= creates  # raw hosted and group (T-P2-RAW)
    files = (await admin.get('/api/v1/repositories/files')).json()
    assert (files['type'], files['hosted']['write_policy']) == ('hosted', 'disable_redeploy')
    raw_group = (await admin.get('/api/v1/repositories/raw-group')).json()
    assert raw_group['group']['members'] == ['files', 'github-releases']

    # audited once per repository, with the source
    await services.audit.flush()
    records = await services.db[AUDIT].find({'action': 'repository.create'}).to_list()
    assert len(records) == len(creates)
    assert all(r['details']['source'] == 'nexus_import' for r in records)
    assert PASSWORD not in str(records)

    again = (await admin.post(APPLY, json=body(nexus))).json()
    assert again['summary']['created'] == 0
    assert {i['name'] for i in again['items'] if i['outcome'] == 'exists'} >= creates
    assert (await admin.get('/api/v1/repositories')).json()['total'] == len(creates)


async def test_apply_only_selected(admin: httpx.AsyncClient, nexus: FakeUpstream) -> None:
    result = (await admin.post(APPLY, json=body(nexus, repositories=['debian', 'npm']))).json()
    created = sorted(i['name'] for i in result['items'] if i['outcome'] == 'created')
    assert created == ['debian', 'npm']
    assert result['default_docker_repo']['name'] is None  # dockerhub-proxy was not selected
    assert (await admin.get('/api/v1/repositories')).json()['total'] == 2


async def test_errors(admin: httpx.AsyncClient, client: httpx.AsyncClient, nexus: FakeUpstream) -> None:
    assert (await client.post(PREVIEW, json=body(nexus))).status_code == 401
    wrong = await admin.post(PREVIEW, json=body(nexus, password='wrong-password'))
    assert wrong.status_code == 502
    assert wrong.headers['content-type'] == 'application/problem+json'
    assert 'refused the credentials' in wrong.json()['detail']
    private = await admin.post(PREVIEW, json=body(nexus, allow_private=False))
    assert private.status_code == 502 and 'private' in private.json()['detail']
    bad_url = await admin.post(PREVIEW, json=body(nexus, url='https://user:pw@nexus.example.com'))
    assert bad_url.status_code == 422
    assert [e['field'] for e in bad_url.json()['errors']] == ['url']
    bad_ca = await admin.post(PREVIEW, json=body(nexus, ca_pem='not a certificate'))
    assert bad_ca.status_code == 422
    assert [e['field'] for e in bad_ca.json()['errors']] == ['ca_pem']
    assert 'wrong-password' not in wrong.text
