"""Cargo (T-P6-CARGO) end to end on the real engine, MongoDB and Valkey: a sparse proxy against a scriptable upstream
index (config.json, index files checked and revalidated, ``.crate`` files verified against ``cksum``), publishes to a
hosted repository (Cargo's bare token header, the length-prefixed payload, write policy, name rules, yank, owners),
a group merging both, browse and ``cargo search``."""

import gzip
import hashlib
import io
import json
import struct
import tarfile
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import asgi_client

from waystation_common.db.collections import ASSETS, AUDIT, CARGO_CRATES
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.problems import ResponseAborted
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def crate_file(name: str, vers: str, *, manifest_name: str | None = None, extra: bytes = b'') -> bytes:
    top = f'{name}-{vers}'
    files = {
        f'{top}/Cargo.toml': f'[package]\nname = "{manifest_name or name}"\nversion = "{vers}"\n'.encode(),
        f'{top}/src/lib.rs': b'pub fn answer() -> u32 { 42 }\n' * 2000 + extra,
    }
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode='wb', mtime=0) as packed, tarfile.open(fileobj=packed, mode='w') as archive:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()  # the same bytes on every call (no gzip time stamp)


CRATE = crate_file('demo', '1.0.0')


def line(name: str, vers: str, cksum: str, **extra: Any) -> bytes:
    doc = {'name': name, 'vers': vers, 'deps': [], 'cksum': cksum, 'features': {}, 'yanked': False, **extra}
    return json.dumps(doc).encode() + b'\n'


def payload(name: str, vers: str, crate: bytes, **meta: Any) -> bytes:
    doc = {
        'name': name,
        'vers': vers,
        'deps': [
            {
                'name': 'demo',
                'version_req': '^1',
                'features': [],
                'optional': False,
                'default_features': True,
                'target': None,
                'kind': 'normal',
                'registry': 'https://github.com/rust-lang/crates.io-index',
            }
        ],
        'features': {'default': []},
        'description': f'{name} for tests',
        **meta,
    }
    body = json.dumps(doc).encode()
    return struct.pack('<I', len(body)) + body + struct.pack('<I', len(crate)) + crate


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        index = line('demo', '1.0.0', sha(CRATE)) + line('demo', '1.1.0', sha(b'other'), yanked=True)
        fake.routes.update(
            {
                '/config.json': Respond(
                    200,
                    {'Content-Type': 'application/json'},
                    json.dumps({'dl': f'{fake.url}dl', 'api': fake.url.rstrip('/')}).encode(),
                ),
                '/de/mo/demo': Respond(200, {'Content-Type': 'text/plain', 'ETag': '"i1"'}, index),
                '/dl/demo/1.0.0/download': Respond(200, {}, CRATE, chunk=4096),
                '/dl/demo/1.1.0/download': Respond(200, {}, b'tampered'),  # does not match the index cksum
                '/3/e/evi': Respond(200, {}, line('other', '1.0.0', sha(b'x'))),  # another crate's entry
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


async def create(admin: httpx.AsyncClient, body: dict[str, Any]) -> None:
    made = await admin.post('/api/v1/repositories', json={'format': 'cargo', 'online': True, **body})
    assert made.status_code == 201, made.text


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True}
    await create(admin, {'name': 'crates-proxy', 'type': 'proxy', 'proxy': proxy})
    await create(admin, {'name': 'crates-hosted', 'type': 'hosted'})
    group = {'members': ['crates-hosted', 'crates-proxy'], 'writable_member': 'crates-hosted'}
    await create(admin, {'name': 'crates-all', 'type': 'group', 'group': group})


async def token(admin: httpx.AsyncClient, scopes: list[str]) -> str:
    made = await admin.post('/api/v1/auth/me/tokens', json={'name': f'cargo {scopes}', 'scopes': scopes})
    assert made.status_code == 201, made.text
    return str(made.json()['secret'])


async def stored(services: Services, repo: str, path: str) -> dict[str, Any] | None:
    repo_doc = await services.catalog.get(repo)
    assert repo_doc is not None
    doc: dict[str, Any] | None = await services.db[ASSETS].find_one({'repo_id': repo_doc.id, 'path': path})
    return doc


async def drain(services: Services) -> None:
    plugin = services.formats.plugin('cargo')
    await plugin.drain()  # type: ignore[union-attr]


def entries(body: bytes) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in body.splitlines() if raw.strip()]


# ---------------------------------------------------------------------------- proxy
@pytest.mark.usefixtures('repos')
async def test_proxy_config_index_and_verified_downloads(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    config = await client.get('/repository/crates-proxy/index/config.json')
    assert config.status_code == 200 and config.headers['etag']
    doc = config.json()
    assert doc['dl'].endswith('/repository/crates-proxy/crates/{crate}/{crate}-{version}.crate')
    assert doc['api'].endswith('/repository/crates-proxy') and doc['auth-required'] is False

    index = await client.get('/repository/crates-proxy/index/de/mo/demo')
    assert index.status_code == 200 and 'x-accel-redirect' in index.headers
    cached = await stored(services, 'crates-proxy', 'index/de/mo/demo')
    assert cached is not None and cached['kind'] == 'metadata'
    assert (await client.get('/repository/crates-proxy/index/de/mo/demo')).headers['x-ws-cache'] == 'HIT'
    assert upstream.hits('/de/mo/demo') == 1
    assert (await client.get('/repository/crates-proxy/index/de/mo/Demo')).status_code == 404  # not Cargo's path
    assert (await client.get('/repository/crates-proxy/index/3/e/evi')).status_code == 502  # another crate's entry
    assert await stored(services, 'crates-proxy', 'index/3/e/evi') is None

    download = await client.get('/repository/crates-proxy/crates/demo/demo-1.0.0.crate')
    assert download.status_code == 200 and download.content == CRATE
    crate = await stored(services, 'crates-proxy', 'crates/demo/demo-1.0.0.crate')
    assert crate is not None and crate['blob_sha256'] == sha(CRATE) and crate['kind'] == 'content'
    again = await client.get('/repository/crates-proxy/crates/Demo/Demo-1.0.0.crate')  # cargo uses the index name
    assert again.headers['x-ws-cache'] == 'HIT'
    assert upstream.hits('/config.json') == 1

    with pytest.raises(ResponseAborted):  # the bytes do not match the index cksum
        await client.get('/repository/crates-proxy/crates/demo/demo-1.1.0.crate')
    assert await stored(services, 'crates-proxy', 'crates/demo/demo-1.1.0.crate') is None
    assert (await client.get('/repository/crates-proxy/crates/demo/demo-9.9.9.crate')).status_code == 404
    assert upstream.hits('/de/mo/demo') == 2  # unlisted version: the index was refreshed once

    writer = {'Authorization': await token(admin, ['read', 'write'])}
    publish = await client.put('/repository/crates-proxy/api/v1/crates/new', content=b'x', headers=writer)
    assert publish.status_code == 405 and 'read-only proxy' in publish.json()['errors'][0]['detail']

    await drain(services)
    crates = (await admin.get('/api/v1/repositories/crates-proxy/cargo/crates')).json()
    assert crates['items'] == [
        {
            'name': 'demo',
            'description': None,
            'latest': '1.0.0',
            'version_count': 2,
            'cached_versions': 1,
            'size_bytes': len(CRATE),
        }
    ]
    versions = (await admin.get('/api/v1/repositories/crates-proxy/cargo/versions', params={'crate': 'Demo'})).json()
    assert [(v['version'], v['yanked'], v['cached']) for v in versions['items']] == [
        ('1.1.0', True, False),
        ('1.0.0', False, True),
    ]
    assert versions['items'][1]['cksum'] == sha(CRATE)
    missing = await admin.get('/api/v1/repositories/crates-proxy/cargo/versions', params={'crate': 'nothing'})
    assert missing.status_code == 404
    assert (await admin.get('/api/v1/repositories/crates-all/cargo/crates')).status_code == 404  # via members
    search = await client.get('/repository/crates-proxy/api/v1/crates', params={'q': 'dem', 'per_page': '5'})
    assert search.json() == {
        'crates': [{'name': 'demo', 'max_version': '1.0.0', 'description': None}],
        'meta': {'total': 1},
    }
    hits = (await admin.get('/api/v1/search', params={'q': 'demo'})).json()['items']
    assert any(h['format'] == 'cargo' and h['browse']['query'] == {'crate': 'demo'} for h in hits)


# ---------------------------------------------------------------------------- hosted
@pytest.mark.usefixtures('repos')
async def test_hosted_publish_index_policy_yank_and_owners(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services
) -> None:
    secret = await token(admin, ['read', 'write'])
    reader = await token(admin, ['read'])
    auth = {'Authorization': secret}  # Cargo sends the bare token
    url = '/repository/crates-hosted/api/v1/crates/new'
    assert (await client.get('/repository/crates-hosted/index/config.json')).status_code == 401  # then cargo retries

    body = payload('ws-demo', '0.1.0', crate_file('ws-demo', '0.1.0'))
    assert (await client.put(url, content=body, headers={'Authorization': reader})).status_code == 403
    ok = await client.put(url, content=body, headers=auth)
    assert ok.status_code == 200, ok.text
    assert ok.json() == {'warnings': {'invalid_categories': [], 'invalid_badges': [], 'other': []}}

    config = await client.get('/repository/crates-hosted/index/config.json', headers={'Authorization': reader})
    assert config.json()['auth-required'] is True
    index = await client.get('/repository/crates-hosted/index/ws/-d/ws-demo', headers=auth)
    assert index.status_code == 200 and index.headers['etag']
    [entry] = entries(index.content)
    assert entry['name'] == 'ws-demo' and entry['vers'] == '0.1.0' and entry['yanked'] is False
    assert entry['cksum'] == sha(crate_file('ws-demo', '0.1.0'))
    assert entry['deps'][0]['registry'] == 'https://github.com/rust-lang/crates.io-index'
    assert entry['deps'][0]['req'] == '^1'
    unchanged = await client.get(
        '/repository/crates-hosted/index/ws/-d/ws-demo', headers={**auth, 'If-None-Match': index.headers['etag']}
    )
    assert unchanged.status_code == 304
    download = await client.get('/repository/crates-hosted/crates/ws-demo/ws-demo-0.1.0.crate', headers=auth)
    assert download.status_code == 200 and 'x-accel-redirect' in download.headers
    held = await stored(services, 'crates-hosted', 'crates/ws-demo/ws-demo-0.1.0.crate')
    assert held is not None and held['blob_sha256'] == entry['cksum']

    refused = [
        (payload('ws_demo', '0.2.0', crate_file('ws_demo', '0.2.0')), 'conflicts'),  # '-' and '_' are one crate
        (payload('WS-demo', '0.2.0', crate_file('WS-demo', '0.2.0')), 'exists as'),
        (payload('ws-demo', '0.1.0+build', crate_file('ws-demo', '0.1.0+build')), 'already uploaded'),
        (payload('ws-demo', '0.3.0', crate_file('ws-demo', '0.3.0', manifest_name='other')), 'Cargo.toml'),
        (payload('ws-demo', '0.3.0', b'not a crate'), 'gzipped tar'),
        (payload('ws-demo', '0.3', b'x'), 'SemVer'),
        (body[:-10], 'does not match its parts'),
    ]
    for data, message in refused:
        answer = await client.put(url, content=data, headers=auth)
        assert answer.status_code == 400 and message in answer.json()['errors'][0]['detail'], (message, answer.text)

    repo = await services.catalog.get('crates-hosted')
    assert repo is not None
    await services.db['repositories'].update_one(
        {'_id': repo.id}, {'$set': {'hosted.write_policy': 'disable_redeploy'}}
    )
    services.catalog.invalidate()
    assert (await client.put(url, content=body, headers=auth)).status_code == 200  # the same bytes again
    other = payload('ws-demo', '0.1.0', crate_file('ws-demo', '0.1.0', extra=b'// changed\n'))
    again = await client.put(url, content=other, headers=auth)
    assert again.status_code == 400 and 'already uploaded' in again.text

    yank = await client.delete('/repository/crates-hosted/api/v1/crates/ws-demo/0.1.0/yank', headers=auth)
    assert yank.status_code == 200 and yank.json() == {'ok': True}
    yanked = await client.get('/repository/crates-hosted/index/ws/-d/ws-demo', headers=auth)
    assert entries(yanked.content)[0]['yanked'] is True
    assert (
        await client.delete(
            '/repository/crates-hosted/api/v1/crates/ws-demo/0.1.0/yank', headers={'Authorization': reader}
        )
    ).status_code == 403
    unyank = await client.put('/repository/crates-hosted/api/v1/crates/ws-demo/0.1.0/unyank', headers=auth)
    assert unyank.status_code == 200
    gone = await client.delete('/repository/crates-hosted/api/v1/crates/ws-demo/9.0.0/yank', headers=auth)
    assert gone.status_code == 404

    owners = await client.get('/repository/crates-hosted/api/v1/crates/ws-demo/owners', headers=auth)
    assert owners.json() == {'users': [{'id': 1, 'login': 'admin', 'name': None}]}
    change = await client.put('/repository/crates-hosted/api/v1/crates/ws-demo/owners', headers=auth, json={})
    assert change.status_code == 400 and 'roles' in change.json()['errors'][0]['detail']

    summary = await services.db[CARGO_CRATES].find_one({'repo_id': repo.id, 'name': 'ws-demo'})
    assert summary is not None and summary['latest'] == '0.1.0' and summary['description'] == 'ws-demo for tests'
    assert await services.db[AUDIT].find_one({'action': 'content.upload', 'details.crate': 'ws-demo'})
    assert await services.db[AUDIT].find_one({'action': 'content.update', 'details.yanked': True})
    versions = (
        await admin.get('/api/v1/repositories/crates-hosted/cargo/versions', params={'crate': 'ws-demo'})
    ).json()
    assert versions['items'][0]['published_by'] == 'admin' and versions['items'][0]['cached'] is True


# ---------------------------------------------------------------------------- group
@pytest.mark.usefixtures('repos')
async def test_group_merges_indexes_and_publishes_to_the_writable_member(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    secret = await token(admin, ['read', 'write'])
    auth = {'Authorization': f'Bearer {secret}'}
    published = await client.put(
        '/repository/crates-all/api/v1/crates/new',
        content=payload('demo', '2.0.0', crate_file('demo', '2.0.0')),
        headers=auth,
    )
    assert published.status_code == 200, published.text
    assert await stored(services, 'crates-hosted', 'crates/demo/demo-2.0.0.crate') is not None

    config = (await client.get('/repository/crates-all/index/config.json', headers=auth)).json()
    assert config['dl'].endswith('/repository/crates-all/crates/{crate}/{crate}-{version}.crate')
    index = await client.get('/repository/crates-all/index/de/mo/demo', headers=auth)
    assert index.status_code == 200
    assert [e['vers'] for e in entries(index.content)] == ['2.0.0', '1.0.0', '1.1.0']  # hosted first, then the proxy

    proxied = await client.get('/repository/crates-all/crates/demo/demo-1.0.0.crate', headers=auth)
    assert proxied.status_code == 200 and proxied.content == CRATE
    assert proxied.headers['x-ws-group-member'] == 'crates-proxy'
    hosted = await client.get('/repository/crates-all/crates/demo/demo-2.0.0.crate', headers=auth)
    assert hosted.headers['x-ws-group-member'] == 'crates-hosted'
    assert (await client.get('/repository/crates-all/index/no/th/nothing', headers=auth)).status_code == 404

    search = (await client.get('/repository/crates-all/api/v1/crates', params={'q': 'demo'}, headers=auth)).json()
    assert [c['name'] for c in search['crates']] == ['demo']
    upstream.routes['/de/mo/demo'] = Respond(503, {}, b'down')
    still = await client.get('/repository/crates-all/index/de/mo/demo', headers=auth)
    assert still.status_code == 200  # the proxy's cached copy (or the hosted entries) still answer
