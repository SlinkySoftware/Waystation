"""Helm repositories (T-P5-HELM) end to end on real MongoDB/Valkey with scriptable local upstreams: the proxy index
rewrite and chart validation, hosted uploads (PUT, ChartMuseum multipart), write policies, deletes, the outbox job
and the worker's index, group merges and uploads through the group, browse views."""

import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import asgi_client

from waystation_common.db.collections import ASSETS, AUDIT, HELM_CHARTS, JOB_OUTBOX
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.helm.plugin import HelmPlugin
from waystation_server.problems import ResponseAborted
from waystation_server.services import Services
from waystation_worker import helm as worker_helm

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'


def chart_package(name: str, version: str, *, extra: str = '') -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for path, data in (
            (f'{name}/Chart.yaml', f'apiVersion: v2\nname: {name}\nversion: {version}\nappVersion: "1.0"\n{extra}'),
            (f'{name}/values.yaml', 'replicas: 1\n'),
        ):
            raw = data.encode()
            info = tarfile.TarInfo(path)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return gzip.compress(buf.getvalue(), mtime=0)


DEMO = chart_package('demo', '0.1.0')
DEMO2 = chart_package('demo', '0.2.0', extra='description: second\n')
EXT = b'external chart bytes'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def elsewhere() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream({'/releases/ext-1.0.0.tgz': Respond(200, {}, EXT)}) as fake:
        yield fake


@pytest.fixture
async def upstream(elsewhere: FakeUpstream) -> AsyncIterator[FakeUpstream]:
    index = f"""apiVersion: v1
entries:
  demo:
    - name: demo
      version: 0.1.0
      appVersion: "1.0"
      description: The demo chart
      digest: {sha(DEMO)}
      created: 2026-01-02T03:04:05Z
      urls: [charts/demo-0.1.0.tgz]
  bad:
    - name: bad
      version: 1.0.0
      digest: {'0' * 64}
      urls: [charts/bad-1.0.0.tgz]
  ext:
    - name: ext
      version: 1.0.0
      urls: [{elsewhere.url.rstrip('/')}/releases/ext-1.0.0.tgz]
"""
    routes: dict[str, Any] = {
        '/index.yaml': Respond(200, {'Content-Type': 'text/yaml'}, index.encode()),
        '/charts/demo-0.1.0.tgz': Respond(200, {}, DEMO),
        '/charts/demo-0.1.0.tgz.prov': Respond(200, {}, b'-----BEGIN PGP SIGNED MESSAGE-----\n'),
        '/charts/bad-1.0.0.tgz': Respond(200, {}, b'tampered'),
    }
    async with FakeUpstream(routes) as fake:
        yield fake


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


@pytest.fixture
async def auth(admin: httpx.AsyncClient) -> tuple[str, str]:
    created = await admin.post('/api/v1/auth/me/tokens', json={'name': 'helm', 'scopes': ['read', 'write', 'delete']})
    assert created.status_code == 201, created.text
    return 'admin', str(created.json()['secret'])


async def create(admin: httpx.AsyncClient, body: dict[str, Any]) -> None:
    made = await admin.post('/api/v1/repositories', json={'format': 'helm', 'online': True, **body})
    assert made.status_code == 201, made.text


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True, 'metadata_max_age_s': 3600}
    await create(admin, {'name': 'charts', 'type': 'proxy', 'proxy': proxy})
    await create(admin, {'name': 'mine', 'type': 'hosted'})
    await create(
        admin, {'name': 'all', 'type': 'group', 'group': {'members': ['mine', 'charts'], 'writable_member': 'mine'}}
    )


def plugin(services: Services) -> HelmPlugin:
    found = services.formats.plugin('helm')
    assert isinstance(found, HelmPlugin)
    return found


def served(services: Services, response: httpx.Response) -> bytes:
    """The bytes behind an X-Accel answer (the ETag names the blob)."""
    assert response.status_code == 200, response.text
    if 'x-accel-redirect' not in response.headers:
        return response.content
    digest = response.headers['etag'].strip('"').removeprefix('sha256:')
    return services.blobs.blob_path(digest).read_bytes()


async def repo_id(services: Services, name: str) -> str:
    repo = await services.catalog.get(name)
    assert repo is not None
    return repo.id


async def run_index_job(services: Services, name: str) -> None:
    rid = await repo_id(services, name)
    jobs = [d async for d in services.db[JOB_OUTBOX].find({'job.kind': 'index.helm'})]
    assert any(d['job']['payload'] == {'repo_id': rid} for d in jobs)
    await worker_helm.rebuild(services.db, services.blobs, rid, now=datetime.now(UTC))


async def test_proxy_index_and_charts(
    services: Services, client: httpx.AsyncClient, repos: None, upstream: FakeUpstream, elsewhere: FakeUpstream
) -> None:
    got = await client.get('/repository/charts/index.yaml')
    index = json.loads(served(services, got))
    assert got.headers['content-type'] == 'application/x-yaml'
    host = elsewhere.url.split('://', 1)[1].rstrip('/')
    assert index['entries']['demo'][0]['urls'] == ['charts/demo-0.1.0.tgz']
    assert index['entries']['ext'][0]['urls'] == [f'_ext/http/{host.replace(":", "%3A")}/releases/ext-1.0.0.tgz']
    await plugin(services).drain()
    assert await services.db[HELM_CHARTS].count_documents({}) == 3

    first = await client.get('/repository/charts/charts/demo-0.1.0.tgz')
    assert first.status_code == 200 and first.content == DEMO
    second = await client.get('/repository/charts/charts/demo-0.1.0.tgz')
    assert served(services, second) == DEMO and second.headers['x-ws-cache'] == 'HIT'
    prov = await client.get('/repository/charts/charts/demo-0.1.0.tgz.prov')
    assert prov.status_code == 200 and prov.content.startswith(b'-----BEGIN PGP')

    ext = await client.get(f'/repository/charts/_ext/http/{host}/releases/ext-1.0.0.tgz')
    assert ext.status_code == 200 and ext.content == EXT

    with pytest.raises(ResponseAborted):  # the digest does not match: the transfer is cut, nothing stored
        await client.get('/repository/charts/charts/bad-1.0.0.tgz')
    assert await services.db[ASSETS].count_documents({'path': 'charts/bad-1.0.0.tgz'}) == 0

    index_fetches = sum(1 for s in upstream.seen if s.path == '/index.yaml')
    for path in ('charts/unlisted-1.0.0.tgz', '_ext/http/198.51.100.7/x.tgz'):
        assert (await client.get(f'/repository/charts/{path}')).status_code == 404
        assert (await client.head(f'/repository/charts/{path}')).status_code == 404
    assert not any(s.path.startswith('/charts/unlisted') for s in upstream.seen)
    assert sum(1 for s in upstream.seen if s.path == '/index.yaml') > index_fetches  # refreshed once per miss
    assert (await client.get('/repository/charts/other.txt')).status_code == 404
    assert (await client.put('/repository/charts/x.tgz', content=b'x')).status_code in (401, 405)

    page = (await client.get('/api/v1/repositories/charts/helm/charts')).json()
    assert [c['name'] for c in page['items']] == ['bad', 'demo', 'ext']
    demo = (await client.get('/api/v1/repositories/charts/helm/versions', params={'chart': 'demo'})).json()
    assert demo['chart']['description'] == 'The demo chart'
    (version,) = demo['items']
    assert (version['version'], version['cached'], version['digest']) == ('0.1.0', True, sha(DEMO))
    assert version['download_url'].endswith('/repository/charts/charts/demo-0.1.0.tgz')
    missing = await client.get('/api/v1/repositories/charts/helm/versions', params={'chart': 'nope'})
    assert missing.status_code == 404


async def test_hosted_upload_index_delete(
    services: Services, client: httpx.AsyncClient, auth: tuple[str, str], admin: httpx.AsyncClient, repos: None
) -> None:
    empty = await client.get('/repository/mine/index.yaml', auth=auth)
    assert empty.status_code == 200 and json.loads(empty.content)['entries'] == {}
    assert (await client.get('/repository/mine/index.yaml')).status_code == 404  # hidden from anonymous

    put = await client.put('/repository/mine/anything.tgz', content=DEMO, auth=auth)
    assert put.status_code == 201, put.text
    assert put.headers['location'] == 'demo-0.1.0.tgz'
    boundary = 'helmboundary'
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="chart"; filename="demo-0.2.0.tgz"\r\n'
        'Content-Type: application/octet-stream\r\n\r\n'
    ).encode() + DEMO2
    body += (
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="prov"\r\n\r\nsigned\r\n--{boundary}--\r\n'.encode()
    )
    posted = await client.post(
        '/repository/mine/api/charts',
        content=body,
        auth=auth,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    assert posted.status_code == 201, posted.text
    assert posted.json() == {'saved': True}
    assert (await client.get('/repository/mine/demo-0.2.0.tgz.prov', auth=auth)).status_code == 200

    bad = await client.put('/repository/mine/broken.tgz', content=b'not a chart', auth=auth)
    assert bad.status_code == 400
    orphan_prov = await client.put('/repository/mine/none-1.0.0.tgz.prov', content=b'x', auth=auth)
    assert orphan_prov.status_code == 404

    await run_index_job(services, 'mine')
    index = json.loads(served(services, await client.get('/repository/mine/index.yaml', auth=auth)))
    versions = index['entries']['demo']
    assert [v['version'] for v in versions] == ['0.2.0', '0.1.0']
    assert versions[1]['urls'] == ['demo-0.1.0.tgz'] and versions[1]['digest'] == sha(DEMO)
    assert versions[0]['description'] == 'second' and versions[0]['apiVersion'] == 'v2'
    assert served(services, await client.get('/repository/mine/demo-0.1.0.tgz', auth=auth)) == DEMO

    page = (await client.get('/api/v1/repositories/mine/helm/versions', params={'chart': 'demo'}, auth=auth)).json()
    assert [(v['version'], v['cached']) for v in page['items']] == [('0.2.0', True), ('0.1.0', True)]

    gone = await client.delete('/repository/mine/demo-0.1.0.tgz', auth=auth)
    assert gone.status_code == 204
    api_delete = await client.delete('/repository/mine/api/charts/demo/0.2.0', auth=auth)
    assert api_delete.status_code == 200 and api_delete.json() == {'deleted': True}
    assert (await client.get('/repository/mine/demo-0.2.0.tgz.prov', auth=auth)).status_code == 404
    assert (await client.delete('/repository/mine/demo-0.1.0.tgz', auth=auth)).status_code == 404
    await run_index_job(services, 'mine')
    index = json.loads(served(services, await client.get('/repository/mine/index.yaml', auth=auth)))
    assert index['entries'] == {}
    assert await services.db[HELM_CHARTS].count_documents({}) == 0

    await services.audit.flush()
    actions = [d['action'] async for d in services.db[AUDIT].find({'target.kind': 'helm_chart'})]
    assert actions.count('content.upload') == 2 and actions.count('content.delete') == 2

    await create(admin, {'name': 'once', 'type': 'hosted', 'hosted': {'write_policy': 'disable_redeploy'}})
    assert (await client.put('/repository/once/demo.tgz', content=DEMO, auth=auth)).status_code == 201
    assert (await client.put('/repository/once/demo.tgz', content=DEMO, auth=auth)).status_code == 409
    await create(admin, {'name': 'frozen', 'type': 'hosted', 'hosted': {'write_policy': 'read_only'}})
    assert (await client.put('/repository/frozen/demo.tgz', content=DEMO, auth=auth)).status_code == 403


async def test_group_merges_and_uploads(
    services: Services, client: httpx.AsyncClient, auth: tuple[str, str], repos: None
) -> None:
    hosted_demo = chart_package('demo', '0.1.0', extra='description: from the hosted member\n')
    assert (await client.put('/repository/all/demo.tgz', content=hosted_demo, auth=auth)).status_code == 201
    await run_index_job(services, 'mine')

    got = await client.get('/repository/all/index.yaml', auth=auth)
    merged = json.loads(served(services, got))
    assert merged['entries']['demo'][0]['description'] == 'from the hosted member'  # first member wins
    assert set(merged['entries']) == {'demo', 'bad', 'ext'}
    again = await client.get('/repository/all/index.yaml', auth=auth)
    assert again.headers['etag'] == got.headers['etag']  # unchanged members: the same merged blob
    assert (
        await client.get('/repository/all/index.yaml', auth=auth, headers={'If-None-Match': got.headers['etag']})
    ).status_code == 304

    chart = await client.get('/repository/all/demo-0.1.0.tgz', auth=auth)
    assert served(services, chart) == hosted_demo and chart.headers['x-ws-group-member'] == 'mine'
    proxied = await client.get('/repository/all/charts/demo-0.1.0.tgz', auth=auth)
    assert proxied.content == DEMO and proxied.headers['x-ws-group-member'] == 'charts'
    assert (await client.get('/repository/all/charts/nothing-1.0.0.tgz', auth=auth)).status_code == 404

    page = (await client.get('/api/v1/repositories/all/helm/charts', auth=auth)).json()
    assert [c['name'] for c in page['items']] == ['bad', 'demo', 'ext']
    versions = (await client.get('/api/v1/repositories/all/helm/versions', params={'chart': 'demo'}, auth=auth)).json()
    assert [v['description'] for v in versions['items']] == ['from the hosted member']

    await client.put('/repository/mine/demo2.tgz', content=DEMO2, auth=auth)
    await run_index_job(services, 'mine')
    changed = await client.get('/repository/all/index.yaml', auth=auth)
    assert changed.headers['etag'] != got.headers['etag']
    assert [v['version'] for v in json.loads(served(services, changed))['entries']['demo']] == ['0.2.0', '0.1.0']
