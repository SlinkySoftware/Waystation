"""PyPI (T-P5-PYPI) end to end on the real engine, MongoDB and Valkey: a proxy against a scriptable upstream index,
uploads to a hosted repository (the twine/uv multipart API, write policy, yank), a group merging both, browse and
search."""

import hashlib
import io
import json
import tarfile
import zipfile
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import asgi_client

from waystation_common.db.collections import ASSETS, AUDIT, PYPI_PACKAGES
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.problems import ResponseAborted
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
JSON = 'application/vnd.pypi.simple.v1+json'
PIP_ACCEPT = f'{JSON}, application/vnd.pypi.simple.v1+html; q=0.1, text/html; q=0.01'
SDIST = b'sdist bytes ' * 30000  # > one 256 KiB chunk
WHEEL_METADATA = b'Metadata-Version: 2.1\nName: demo\nVersion: 2.0\nRequires-Python: >=3.9\n'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_wheel(name: str, version: str, *, summary: str = 'Demo') -> tuple[str, bytes, bytes]:
    dist = name.replace('-', '_')
    metadata = f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nSummary: {summary}\n'.encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(f'{dist}/__init__.py', b'VALUE = 1\n')
        archive.writestr(f'{dist}-{version}.dist-info/METADATA', metadata)
        archive.writestr(f'{dist}-{version}.dist-info/WHEEL', b'Wheel-Version: 1.0\n')
    return f'{dist}-{version}-py3-none-any.whl', buffer.getvalue(), metadata


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


def upstream_page(base: str) -> dict[str, Any]:
    return {
        'meta': {'api-version': '1.1'},
        'name': 'demo',
        'versions': ['1.0', '2.0'],
        'files': [
            {
                'filename': 'demo-1.0.tar.gz',
                'url': f'{base}files/demo-1.0.tar.gz',
                'hashes': {'sha256': sha(SDIST)},
                'requires-python': '>=3.8',
                'yanked': False,
                'size': len(SDIST),
            },
            {
                'filename': 'demo-2.0-py3-none-any.whl',
                'url': f'{base}files/demo-2.0-py3-none-any.whl',
                'hashes': {'sha256': sha(b'wheel')},
                'core-metadata': {'sha256': sha(WHEEL_METADATA)},
                'yanked': 'broken',
                'size': 5,
            },
            {
                'filename': 'demo-3.0.tar.gz',
                'url': f'{base}files/demo-3.0.tar.gz',
                'hashes': {'sha256': sha(b'other bytes')},
                'size': 11,
            },
        ],
    }


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        page = json.dumps(upstream_page(fake.url)).encode()
        fake.routes.update(
            {
                '/simple/demo/': Respond(200, {'Content-Type': JSON, 'ETag': '"p1"'}, page),
                '/files/demo-1.0.tar.gz': Respond(200, {}, SDIST, chunk=65536),
                '/files/demo-2.0-py3-none-any.whl': Respond(200, {}, b'wheel'),
                '/files/demo-2.0-py3-none-any.whl.metadata': Respond(200, {}, WHEEL_METADATA),
                '/files/demo-3.0.tar.gz': Respond(200, {}, b'tampered!!!'),  # does not match the page's sha256
                '/simple/': Respond(
                    200, {'Content-Type': 'text/html'}, b'<html><body><a href="/simple/demo/">demo</a></body></html>'
                ),
                '/pypi/demo/json': Respond(
                    200,
                    {'Content-Type': 'application/json'},
                    json.dumps(
                        {
                            'info': {'name': 'demo', 'version': '2.0'},
                            'urls': [{'filename': 'demo-1.0.tar.gz', 'url': f'{fake.url}files/demo-1.0.tar.gz'}],
                        }
                    ).encode(),
                ),
            }
        )
        yield fake


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


async def create(admin: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await admin.post('/api/v1/repositories', json={'format': 'pypi', 'online': True, **body})


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True}
    assert (await create(admin, {'name': 'pypi-proxy', 'type': 'proxy', 'proxy': proxy})).status_code == 201
    assert (await create(admin, {'name': 'pypi-hosted', 'type': 'hosted'})).status_code == 201
    group = {'members': ['pypi-hosted', 'pypi-proxy'], 'writable_member': 'pypi-hosted'}
    made = await create(admin, {'name': 'pypi-all', 'type': 'group', 'group': group})
    assert made.status_code == 201, made.text


async def token(admin: httpx.AsyncClient, scopes: list[str]) -> str:
    made = await admin.post('/api/v1/auth/me/tokens', json={'name': f'pypi {scopes}', 'scopes': scopes})
    assert made.status_code == 201, made.text
    return str(made.json()['secret'])


def upload_form(filename: str, content: bytes, **fields: str) -> tuple[dict[str, str], dict[str, Any]]:
    data = {':action': 'file_upload', 'protocol_version': '1', 'sha256_digest': sha(content), **fields}
    return data, {'content': (filename, content, 'application/octet-stream')}


async def upload(
    client: httpx.AsyncClient, repo: str, secret: str, filename: str, content: bytes, **fields: str
) -> httpx.Response:
    data, files = upload_form(filename, content, **fields)
    return await client.post(f'/repository/{repo}/', data=data, files=files, auth=('__token__', secret))


async def stored(services: Services, repo: str, path: str) -> dict[str, Any] | None:
    repo_doc = await services.catalog.get(repo)
    assert repo_doc is not None
    doc: dict[str, Any] | None = await services.db[ASSETS].find_one({'repo_id': repo_doc.id, 'path': path})
    return doc


def blob(services: Services, doc: dict[str, Any]) -> bytes:
    return services.blobs.blob_path(doc['blob_sha256']).read_bytes()


# ---------------------------------------------------------------------------- proxy
@pytest.mark.usefixtures('repos')
async def test_proxy_pages_negotiated_rewritten_and_cached(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    redirect = await client.get('/repository/pypi-proxy/simple/Demo')
    assert redirect.status_code == 301 and redirect.headers['location'] == '/repository/pypi-proxy/simple/demo/'
    assert (await client.get('/repository/pypi-proxy/simple')).headers['location'] == '/repository/pypi-proxy/simple/'

    page = await client.get('/repository/pypi-proxy/simple/demo/', headers={'Accept': PIP_ACCEPT})
    assert page.status_code == 200 and page.headers['content-type'] == JSON
    assert page.headers['vary'] == 'Accept' and 'x-accel-redirect' in page.headers
    doc = json.loads(blob(services, await stored(services, 'pypi-proxy', 'simple/demo/index.json') or {}))
    first = doc['files'][0]
    assert first['url'] == '../../packages/demo/demo-1.0.tar.gz' and first['requires-python'] == '>=3.8'
    assert first['hashes'] == {'sha256': sha(SDIST)} and doc['files'][1]['yanked'] == 'broken'
    assert doc['files'][1]['core-metadata'] == {'sha256': sha(WHEEL_METADATA)}

    html = await client.get('/repository/pypi-proxy/simple/demo/')
    assert html.headers['content-type'] == 'text/html; charset=utf-8'
    body = blob(services, await stored(services, 'pypi-proxy', 'simple/demo/index.html') or {}).decode()
    assert f'href="../../packages/demo/demo-1.0.tar.gz#sha256={sha(SDIST)}"' in body and 'data-yanked="broken"' in body
    assert next(s.headers['accept'] for s in upstream.seen if s.path == '/simple/demo/').startswith(JSON)
    again = await client.get('/repository/pypi-proxy/simple/demo/', headers={'Accept': PIP_ACCEPT})
    assert again.headers['x-ws-cache'] == 'HIT' and upstream.hits('/simple/demo/') == 2  # once per representation
    assert (
        await client.get('/repository/pypi-proxy/simple/demo/', headers={'Accept': 'application/json'})
    ).status_code == 406

    index = await client.get('/repository/pypi-proxy/simple/', headers={'Accept': JSON})
    assert index.status_code == 200
    assert json.loads(blob(services, await stored(services, 'pypi-proxy', 'simple/index.json') or {}))['projects'] == [
        {'name': 'demo'}
    ]


@pytest.mark.usefixtures('repos')
async def test_proxy_files_verified_metadata_and_legacy_json(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    response = await client.get('/repository/pypi-proxy/packages/demo/demo-1.0.tar.gz')
    assert response.status_code == 200 and response.content == SDIST
    assert response.headers['x-ws-cache'] == 'FETCHING'  # the page was fetched first, from the upstream
    cached = await stored(services, 'pypi-proxy', 'packages/demo/demo-1.0.tar.gz')
    assert cached is not None and cached['blob_sha256'] == sha(SDIST) and cached['kind'] == 'content'
    assert (await client.get('/repository/pypi-proxy/packages/demo/demo-1.0.tar.gz')).headers['x-ws-cache'] == 'HIT'

    meta = await client.get('/repository/pypi-proxy/packages/demo/demo-2.0-py3-none-any.whl.metadata')
    assert meta.status_code == 200 and meta.content == WHEEL_METADATA
    assert (await client.get('/repository/pypi-proxy/packages/demo/demo-1.0.tar.gz.metadata')).status_code == 404

    with pytest.raises(ResponseAborted):  # the transfer is cut: the bytes do not match the page
        await client.get('/repository/pypi-proxy/packages/demo/demo-3.0.tar.gz')
    assert await stored(services, 'pypi-proxy', 'packages/demo/demo-3.0.tar.gz') is None

    missing = await client.get('/repository/pypi-proxy/packages/demo/demo-9.9.tar.gz')
    assert missing.status_code == 404
    assert upstream.hits('/simple/demo/') == 2  # unlisted file: the page was refreshed once, nothing else asked

    legacy = await client.get('/repository/pypi-proxy/pypi/demo/json')
    assert legacy.status_code == 200
    doc = json.loads(blob(services, await stored(services, 'pypi-proxy', 'pypi/demo/json') or {}))
    assert doc['urls'][0]['url'].endswith('/repository/pypi-proxy/packages/demo/demo-1.0.tar.gz')


# ---------------------------------------------------------------------------- hosted
@pytest.mark.usefixtures('repos')
async def test_hosted_upload_pages_metadata_policy_and_yank(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services
) -> None:
    secret = await token(admin, ['read', 'write'])
    filename, content, metadata = make_wheel('ws-demo', '0.1.0')
    assert (await client.get('/repository/pypi-hosted/simple/ws-demo/')).status_code == 401  # hidden: challenge

    refused = await upload(client, 'pypi-hosted', secret, filename, content, name='other', version='0.1.0')
    assert refused.status_code == 400 and 'other' in refused.text
    wrong = await upload(client, 'pypi-hosted', secret, filename, content, sha256_digest='0' * 64)
    assert wrong.status_code == 400 and 'sha256' in wrong.text
    assert (await upload(client, 'pypi-proxy', secret, filename, content)).status_code == 405

    ok = await upload(client, 'pypi-hosted', secret, filename, content, name='ws-demo', version='0.1.0')
    assert ok.status_code == 200, ok.text
    sidecar = await stored(services, 'pypi-hosted', f'packages/ws-demo/{filename}.metadata')
    assert sidecar is not None and blob(services, sidecar) == metadata

    auth = ('__token__', secret)
    page = await client.get('/repository/pypi-hosted/simple/ws_demo/', auth=auth, follow_redirects=True)
    assert page.status_code == 200 and f'../../packages/ws-demo/{filename}#sha256={sha(content)}' in page.text
    as_json = await client.get('/repository/pypi-hosted/simple/ws-demo/', auth=auth, headers={'Accept': PIP_ACCEPT})
    doc = as_json.json()
    assert as_json.headers['content-type'] == JSON and doc['meta'] == {'api-version': '1.1'}
    assert doc['name'] == 'ws-demo' and doc['versions'] == ['0.1.0']
    assert doc['files'][0]['core-metadata'] == {'sha256': sha(metadata)} and doc['files'][0]['size'] == len(content)
    etag = as_json.headers['etag']
    conditional = await client.get(
        '/repository/pypi-hosted/simple/ws-demo/', auth=auth, headers={'Accept': PIP_ACCEPT, 'If-None-Match': etag}
    )
    assert conditional.status_code == 304
    file = await client.get(f'/repository/pypi-hosted/packages/ws-demo/{filename}', auth=auth)
    assert file.status_code == 200 and 'x-accel-redirect' in file.headers
    index = await client.get('/repository/pypi-hosted/simple/', auth=auth)
    assert '<a href="ws-demo/">ws-demo</a>' in index.text

    # write policy: allow_redeploy (default) replaces; disable_redeploy accepts the same bytes only
    other_content = make_wheel('ws-demo', '0.1.0', summary='Changed')[1]
    assert (await upload(client, 'pypi-hosted', secret, filename, other_content)).status_code == 200
    repo = (await admin.get('/api/v1/repositories/pypi-hosted')).json()
    repo['hosted'] = {'write_policy': 'disable_redeploy', 'immutable_tag_patterns': []}
    update = {k: repo[k] for k in ('name', 'format', 'type', 'online', 'hosted', 'format_options')}
    assert (await admin.put('/api/v1/repositories/pypi-hosted', json=update)).status_code == 200
    services.catalog.invalidate()
    exists = await upload(client, 'pypi-hosted', secret, filename, content)
    assert exists.status_code == 400 and 'File already exists' in exists.text
    assert (await upload(client, 'pypi-hosted', secret, filename, other_content)).status_code == 200  # same bytes
    reader = await token(admin, ['read'])
    assert (await upload(client, 'pypi-hosted', reader, filename, other_content)).status_code == 403

    audit_docs = [d async for d in services.db[AUDIT].find({'action': 'content.upload'})]
    assert any(d['details']['filename'] == filename for d in audit_docs)

    # yank (REST, needs write) and browse
    yank = await admin.post(
        '/api/v1/repositories/pypi-hosted/pypi/yank',
        json={'project': 'WS_Demo', 'version': '0.1.0', 'yanked': True, 'reason': 'bad build'},
    )
    assert yank.status_code == 200 and yank.json() == {
        'project': 'ws-demo',
        'version': '0.1.0',
        'yanked': True,
        'files': 1,
    }
    yanked = await client.get('/repository/pypi-hosted/simple/ws-demo/', auth=auth, headers={'Accept': JSON})
    assert yanked.json()['files'][0]['yanked'] == 'bad build'
    versions = await admin.get('/api/v1/repositories/pypi-hosted/pypi/versions', params={'project': 'ws-demo'})
    assert versions.status_code == 200
    item = versions.json()['items'][0]
    assert item['version'] == '0.1.0' and item['yanked'] and item['files'][0]['uploaded_by'] == 'admin'
    assert item['files'][0]['core_metadata'] and item['files'][0]['kind'] == 'wheel'
    projects = (await admin.get('/api/v1/repositories/pypi-hosted/pypi/projects')).json()['items']
    assert projects[0]['name'] == 'ws-demo' and projects[0]['latest'] == '0.1.0' and projects[0]['files'] == 1
    proxy_yank = await admin.post(
        '/api/v1/repositories/pypi-proxy/pypi/yank', json={'project': 'demo', 'version': '1.0', 'yanked': True}
    )
    assert proxy_yank.status_code == 404


# ---------------------------------------------------------------------------- group
@pytest.mark.usefixtures('repos')
async def test_group_merges_members_serves_files_and_uploads_to_the_writable_member(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    secret = await token(admin, ['read', 'write'])
    auth = ('__token__', secret)
    filename, content, _ = make_wheel('demo', '1.0')  # same version as the proxy's sdist, a different file
    shadow = 'demo-1.0.tar.gz'
    sdist_bytes = make_sdist('demo', '1.0')
    via_group = await upload(client, 'pypi-all', secret, filename, content)
    assert via_group.status_code == 200 and via_group.headers['x-ws-group-member'] == 'pypi-hosted'
    assert (await upload(client, 'pypi-all', secret, shadow, sdist_bytes)).status_code == 200

    page = await client.get('/repository/pypi-all/simple/demo/', auth=auth, headers={'Accept': JSON})
    assert page.status_code == 200, page.text
    files = {f['filename']: f for f in page.json()['files']}
    assert set(files) == {filename, shadow, 'demo-2.0-py3-none-any.whl', 'demo-3.0.tar.gz'}
    assert files[shadow]['hashes']['sha256'] == sha(sdist_bytes)  # the hosted member comes first
    assert page.json()['versions'] == ['1.0', '2.0', '3.0'] and '_ws_url' not in files['demo-3.0.tar.gz']

    hosted_file = await client.get(f'/repository/pypi-all/packages/demo/{shadow}', auth=auth)
    assert hosted_file.headers['x-ws-group-member'] == 'pypi-hosted' and 'x-accel-redirect' in hosted_file.headers
    proxied = await client.get('/repository/pypi-all/packages/demo/demo-2.0-py3-none-any.whl', auth=auth)
    assert proxied.status_code == 200 and proxied.content == b'wheel'
    assert proxied.headers['x-ws-group-member'] == 'pypi-proxy'
    assert (await client.get('/repository/pypi-all/packages/demo/nope-1.0.tar.gz', auth=auth)).status_code == 404

    index = await client.get('/repository/pypi-all/simple/', auth=auth, headers={'Accept': JSON})
    assert index.json()['projects'] == [{'name': 'demo'}]

    readonly = await create(admin, {'name': 'pypi-ro', 'type': 'group', 'group': {'members': ['pypi-proxy']}})
    assert readonly.status_code == 201
    assert (await upload(client, 'pypi-ro', secret, filename, content)).status_code == 405

    await services.formats.plugin('pypi').drain()  # type: ignore[union-attr]
    summaries = {d['name']: d async for d in services.db[PYPI_PACKAGES].find({})}
    assert summaries['demo']['latest'] in ('1.0', '3.0')
    search = await admin.get('/api/v1/search', params={'q': 'demo', 'format': 'pypi'})
    assert search.status_code == 200
    assert {h['repository'] for h in search.json()['items'] if h['kind'] == 'package'} >= {'pypi-hosted'}


def make_sdist(name: str, version: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        data = f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n'.encode()
        info = tarfile.TarInfo(f'{name}-{version}/PKG-INFO')
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
