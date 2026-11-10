"""npm hosted and group repositories (T-P5-NPMH) end to end on the real engine, MongoDB and Valkey: publish, generated
packuments, write policy, dist-tags, deprecate, unpublish, ``npm login``/``logout``, a group merging a hosted
repository with a proxy of a scriptable upstream, browse and audit."""

import base64
import hashlib
import io
import json
import tarfile
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import asgi_client

from waystation_common.db.collections import ASSETS, AUDIT, NPM_PACKAGES, NPM_VERSIONS
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
ABBREVIATED = 'application/vnd.npm.install-v1+json'
BASE = 'https://registry.example.com/repository/'


def tarball(name: str, version: str, *, filler: bytes = b'') -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for path, data in (
            ('package/package.json', json.dumps({'name': name, 'version': version}).encode()),
            ('package/index.js', b'module.exports = 42\n' + filler),
        ):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def sri(data: bytes) -> str:
    return 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()


def publish_body(name: str, version: str, tgz: bytes, *, tag: str = 'latest', **meta: Any) -> bytes:
    filename = f'{name}-{version}.tgz'
    manifest = {
        'name': name,
        'version': version,
        'description': f'{name} {version}',
        'dist': {'integrity': sri(tgz), 'shasum': hashlib.sha1(tgz, usedforsecurity=False).hexdigest()},
        **meta,
    }
    return json.dumps(
        {
            '_id': name,
            'name': name,
            'dist-tags': {tag: version},
            'versions': {version: manifest},
            '_attachments': {filename: {'data': base64.b64encode(tgz).decode(), 'length': len(tgz)}},
        }
    ).encode()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


PROXY_TGZ_1 = tarball('demo', '1.0.0', filler=b'// upstream build\n')
PROXY_TGZ_3 = tarball('demo', '3.0.0')


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        versions = {
            v: {
                'name': 'demo',
                'version': v,
                'dist': {'tarball': f'{fake.url}demo/-/demo-{v}.tgz', 'integrity': sri(t)},
            }
            for v, t in (('1.0.0', PROXY_TGZ_1), ('3.0.0', PROXY_TGZ_3))
        }
        body = json.dumps({'name': 'demo', 'dist-tags': {'latest': '3.0.0', 'next': '3.0.0'}, 'versions': versions})
        fake.routes.update(
            {
                '/demo': Respond(200, {'Content-Type': 'application/json'}, body.encode()),
                '/demo/-/demo-1.0.0.tgz': Respond(200, {}, PROXY_TGZ_1),
                '/demo/-/demo-3.0.0.tgz': Respond(200, {}, PROXY_TGZ_3),
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
    made = await admin.post('/api/v1/repositories', json={'format': 'npm', 'online': True, **body})
    assert made.status_code == 201, made.text


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True}
    await create(admin, {'name': 'npm-proxy', 'type': 'proxy', 'proxy': proxy})
    await create(admin, {'name': 'npm-hosted', 'type': 'hosted', 'hosted': {'write_policy': 'disable_redeploy'}})
    group = {'members': ['npm-hosted', 'npm-proxy'], 'writable_member': 'npm-hosted'}
    await create(admin, {'name': 'npm-all', 'type': 'group', 'group': group})


async def token(admin: httpx.AsyncClient, scopes: list[str]) -> dict[str, str]:
    made = await admin.post('/api/v1/auth/me/tokens', json={'name': f'npm {scopes}', 'scopes': scopes})
    assert made.status_code == 201, made.text
    return {'Authorization': f'Bearer {made.json()["secret"]}'}


async def user(admin: httpx.AsyncClient, username: str, roles: list[str]) -> None:
    body = {'username': username, 'password': PASSWORD, 'must_change_password': False, 'roles': roles}
    made = await admin.post('/api/v1/users', json=body)
    assert made.status_code == 201, made.text


async def login(client: httpx.AsyncClient, repo: str, name: str, password: str) -> httpx.Response:
    return await client.put(
        f'/repository/{repo}/-/user/org.couchdb.user:{name}',
        json={'_id': f'org.couchdb.user:{name}', 'name': name, 'password': password, 'type': 'user', 'roles': []},
    )


# ---------------------------------------------------------------------------- hosted
@pytest.mark.usefixtures('repos')
async def test_publish_packuments_tags_deprecate_unpublish(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services
) -> None:
    full_access = await token(admin, ['read', 'write', 'delete'])
    url = '/repository/npm-hosted/'
    tgz = tarball('demo', '1.0.0')
    published = await client.put(f'{url}demo', content=publish_body('demo', '1.0.0', tgz), headers=full_access)
    assert published.status_code == 201, published.text

    full = await client.get(f'{url}demo', headers=full_access)
    assert full.status_code == 200 and 'x-accel-redirect' in full.headers
    doc = json.loads(services.blobs.blob_path(full.headers['x-ws-digest'].removeprefix('sha256:')).read_bytes())
    dist = doc['versions']['1.0.0']['dist']
    assert dist['tarball'] == f'{BASE}npm-hosted/demo/-/demo-1.0.0.tgz'
    assert dist['integrity'] == sri(tgz) and dist['fileCount'] == 2
    assert (
        doc['dist-tags'] == {'latest': '1.0.0'} and doc['description'] == 'demo 1.0.0' and doc['_rev'].startswith('1-')
    )
    etag = full.headers['etag']
    assert (await client.get(f'{url}demo', headers={**full_access, 'If-None-Match': etag})).status_code == 304
    corgi = await client.get(f'{url}demo', headers={**full_access, 'Accept': ABBREVIATED})
    assert corgi.headers['content-type'].startswith(ABBREVIATED)
    tar = await client.get(f'{url}demo/-/demo-1.0.0.tgz', headers=full_access)
    assert tar.status_code == 200 and 'x-accel-redirect' in tar.headers
    assert (await client.get(f'{url}demo/-/demo-9.9.9.tgz', headers=full_access)).status_code == 404

    again = await client.put(f'{url}demo', content=publish_body('demo', '1.0.0', tgz), headers=full_access)
    assert again.status_code == 403 and 'cannot publish over' in again.json()['error']
    bad = publish_body('demo', '1.1.0', tarball('demo', '1.0.0'))
    refused = await client.put(f'{url}demo', content=bad, headers=full_access)
    assert refused.status_code == 400 and 'package.json in the tarball' in refused.json()['error']
    assert (await client.put(f'{url}demo', content=bad)).status_code == 401  # anonymous: hidden, challenged (ADR 0005)

    beta = tarball('demo', '2.0.0-rc.1')
    body = publish_body('demo', '2.0.0-rc.1', beta, tag='beta')
    assert (await client.put(f'{url}demo', content=body, headers=full_access)).status_code == 201
    scoped = tarball('@acme/util', '0.1.0')
    made = await client.put(
        f'{url}@acme%2futil', content=publish_body('@acme/util', '0.1.0', scoped), headers=full_access
    )
    assert made.status_code == 201, made.text
    assert (await client.get(f'{url}@acme/util/-/util-0.1.0.tgz', headers=full_access)).status_code == 200

    # dist-tags: a deployer (no delete permission) may add and remove tags
    await user(admin, 'dee', ['deployer'])
    deployer = await client.put(
        '/repository/npm-hosted/-/user/org.couchdb.user:dee',
        json={'name': 'dee', 'password': PASSWORD},
    )
    assert deployer.status_code == 201, deployer.text
    dee = {'Authorization': f'Bearer {deployer.json()["token"]}'}
    tags = await client.get(f'{url}-/package/demo/dist-tags', headers=dee)
    assert tags.json() == {'beta': '2.0.0-rc.1', 'latest': '1.0.0'}
    assert (await client.put(f'{url}-/package/demo/dist-tags/next', json='1.0.0', headers=dee)).status_code == 200
    assert (await client.put(f'{url}-/package/demo/dist-tags/next', json='5.0.0', headers=dee)).status_code == 404
    assert (await client.delete(f'{url}-/package/demo/dist-tags/beta', headers=dee)).status_code == 200
    assert (await client.delete(f'{url}-/package/demo/dist-tags/latest', headers=dee)).status_code == 400
    tags = await client.get(f'{url}-/package/demo/dist-tags', headers=dee)
    assert tags.json() == {'latest': '1.0.0', 'next': '1.0.0'}

    # npm deprecate: GET ?write=true, PUT the packument back with the message
    current = (await client.get(f'{url}demo?write=true', headers=dee, follow_redirects=True)).headers['x-ws-digest']
    packument = json.loads(services.blobs.blob_path(current.removeprefix('sha256:')).read_bytes())
    packument['versions']['1.0.0']['deprecated'] = 'use 2.x'
    assert (await client.put(f'{url}demo', json=packument, headers=dee)).status_code == 200
    corgi = await client.get(f'{url}demo', headers={**dee, 'Accept': ABBREVIATED})
    corgi_doc = json.loads(services.blobs.blob_path(corgi.headers['x-ws-digest'].removeprefix('sha256:')).read_bytes())
    assert corgi_doc['versions']['1.0.0']['deprecated'] == 'use 2.x'

    # npm unpublish demo@2.0.0-rc.1: the packument without the version (needs delete), then the tarball
    del packument['versions']['2.0.0-rc.1']
    rev = packument['_rev']
    assert (await client.put(f'{url}demo/-rev/{rev}', json=packument, headers=dee)).status_code == 403
    assert (await client.put(f'{url}demo/-rev/{rev}', json=packument, headers=full_access)).status_code == 200
    gone = await client.delete(f'{url}repository/npm-hosted/demo/-/demo-2.0.0-rc.1.tgz/-rev/{rev}', headers=full_access)
    assert gone.status_code == 200
    assert (await client.get(f'{url}demo/-/demo-2.0.0-rc.1.tgz', headers=full_access)).status_code == 404
    assert await services.db[NPM_VERSIONS].count_documents({'name': 'demo'}) == 1

    # browse: versions with dist-tags and deprecation
    versions = await admin.get('/api/v1/repositories/npm-hosted/npm/versions', params={'package': 'demo'})
    assert versions.status_code == 200, versions.text
    row = versions.json()['items'][0]
    assert row['version'] == '1.0.0' and row['deprecated'] == 'use 2.x' and row['cached']
    assert row['dist_tags'] == ['latest', 'next']
    packages = await admin.get('/api/v1/repositories/npm-hosted/npm/packages')
    assert {p['name'] for p in packages.json()['items']} == {'demo', '@acme/util'}
    search = await client.get(f'{url}-/v1/search', params={'text': 'acme'}, headers=dee)
    assert [o['package']['name'] for o in search.json()['objects']] == ['@acme/util']

    # unpublish everything
    assert (await client.delete(f'{url}demo/-rev/{rev}', headers=dee)).status_code == 403  # router: delete needed
    assert (await client.delete(f'{url}demo/-rev/{rev}', headers=full_access)).status_code == 200
    assert (await client.get(f'{url}demo', headers=full_access)).status_code == 404
    assert await services.db[NPM_PACKAGES].count_documents({'name': 'demo'}) == 0

    actions = {d['action'] async for d in services.db[AUDIT].find({'target.kind': 'npm_package'})}
    assert actions == {'content.upload', 'content.update', 'content.delete'}


@pytest.mark.usefixtures('repos')
async def test_read_only_policy(client: httpx.AsyncClient, admin: httpx.AsyncClient) -> None:
    await create(admin, {'name': 'npm-frozen', 'type': 'hosted', 'hosted': {'write_policy': 'read_only'}})
    headers = await token(admin, ['read', 'write'])
    body = publish_body('demo', '1.0.0', tarball('demo', '1.0.0'))
    refused = await client.put('/repository/npm-frozen/demo', content=body, headers=headers)
    assert refused.status_code == 403 and refused.json() == {'error': 'this repository is read-only'}
    proxy = await client.put('/repository/npm-proxy/demo', content=body, headers=headers)
    assert proxy.status_code == 405


# ---------------------------------------------------------------------------- login / logout
@pytest.mark.usefixtures('repos')
async def test_login_issues_a_token_and_logout_revokes_it(client: httpx.AsyncClient, admin: httpx.AsyncClient) -> None:
    signed = await login(client, 'npm-hosted', 'admin', PASSWORD)
    assert signed.status_code == 201, signed.text
    secret = signed.json()['token']
    assert secret.startswith('wst_') and signed.headers['cache-control'] == 'no-store'
    bearer = {'Authorization': f'Bearer {secret}'}
    assert (await client.get('/repository/npm-hosted/-/whoami', headers=bearer)).json() == {'username': 'admin'}
    listed = (await admin.get('/api/v1/auth/me/tokens')).json()['items']
    made = next(t for t in listed if t['name'].startswith('npm login (npm-hosted)'))
    assert sorted(made['scopes']) == ['delete', 'read', 'write'] and made['expires_at'] is None

    # wrong password: a hidden repository answers like a missing one (no credentials sent: the 401 challenge,
    # ADR 0005, with the same body), an anonymous-readable proxy says why (401 npm error)
    hidden = await login(client, 'npm-hosted', 'admin', 'wrong')
    missing = await login(client, 'npm-nope', 'admin', 'wrong')
    assert hidden.status_code == missing.status_code == 401 and hidden.content == missing.content
    assert hidden.headers['www-authenticate'] == missing.headers['www-authenticate']
    assert (await login(client, 'npm-proxy', 'admin', 'wrong')).status_code == 401
    await user(admin, 'norole', [])
    assert (await login(client, 'npm-hosted', 'norole', PASSWORD)).content == missing.content
    # no role, but every signed-in user also receives the anonymous grants (read on proxies): a read token
    proxied = await login(client, 'npm-proxy', 'norole', PASSWORD)
    assert proxied.status_code == 201, proxied.text
    norole = {'Authorization': f'Bearer {proxied.json()["token"]}'}
    norole_tokens = (await client.get('/api/v1/auth/me/tokens', headers=norole)).json()['items']
    assert [t['scopes'] for t in norole_tokens] == [['read']]
    mismatch = await client.put(
        '/repository/npm-hosted/-/user/org.couchdb.user:admin', json={'name': 'other', 'password': PASSWORD}
    )
    assert mismatch.status_code == 401 and mismatch.content == missing.content  # hidden: even a bad request
    mismatch = await client.put(
        '/repository/npm-proxy/-/user/org.couchdb.user:admin', json={'name': 'other', 'password': PASSWORD}
    )
    assert mismatch.status_code == 400

    other = await login(client, 'npm-all', 'admin', PASSWORD)
    assert other.status_code == 201
    other_secret = other.json()['token']
    assert (
        await client.delete(f'/repository/npm-hosted/-/user/token/{other_secret}', headers=bearer)
    ).status_code == 404
    out = await client.delete(f'/repository/npm-hosted/-/user/token/{secret}', headers=bearer)
    assert out.status_code == 200
    assert (await client.get('/repository/npm-hosted/-/whoami', headers=bearer)).status_code == 401


# ---------------------------------------------------------------------------- group
@pytest.mark.usefixtures('repos')
async def test_group_merges_publishes_and_serves_in_member_order(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    headers = await token(admin, ['read', 'write', 'delete'])
    mine = tarball('demo', '1.0.0')  # the same version as the upstream's, other bytes: the hosted member wins it
    via_group = await client.put(
        '/repository/npm-all/demo', content=publish_body('demo', '1.0.0', mine), headers=headers
    )
    assert via_group.status_code == 201 and via_group.headers['x-ws-group-member'] == 'npm-hosted'

    merged = await client.get('/repository/npm-all/demo', headers=headers)
    assert merged.status_code == 200 and 'x-accel-redirect' in merged.headers
    doc = json.loads(services.blobs.blob_path(merged.headers['x-ws-digest'].removeprefix('sha256:')).read_bytes())
    assert set(doc['versions']) == {'1.0.0', '3.0.0'}
    assert doc['versions']['1.0.0']['dist']['integrity'] == sri(mine)
    assert doc['versions']['3.0.0']['dist']['tarball'] == f'{BASE}npm-all/demo/-/demo-3.0.0.tgz'
    assert doc['dist-tags'] == {'latest': '1.0.0', 'next': '3.0.0'}
    again = await client.get('/repository/npm-all/demo', headers={**headers, 'If-None-Match': merged.headers['etag']})
    assert again.status_code == 304
    corgi = await client.get('/repository/npm-all/demo', headers={**headers, 'Accept': ABBREVIATED})
    assert corgi.headers['content-type'].startswith(ABBREVIATED)

    hosted_tgz = await client.get('/repository/npm-all/demo/-/demo-1.0.0.tgz', headers=headers)
    assert hosted_tgz.headers['x-ws-group-member'] == 'npm-hosted'
    proxied = await client.get('/repository/npm-all/demo/-/demo-3.0.0.tgz', headers=headers)
    assert proxied.status_code == 200 and proxied.content == PROXY_TGZ_3
    assert proxied.headers['x-ws-group-member'] == 'npm-proxy'
    assert (await client.get('/repository/npm-all/demo/-/demo-7.0.0.tgz', headers=headers)).status_code == 404

    tags = await client.get('/repository/npm-all/-/package/demo/dist-tags', headers=headers)
    assert tags.json() == {'latest': '1.0.0', 'next': '3.0.0'}
    assert (
        await client.put('/repository/npm-all/-/package/demo/dist-tags/stable', json='1.0.0', headers=headers)
    ).status_code == 200
    writable = await client.get('/repository/npm-all/demo?write=true', headers=headers)
    own = json.loads(services.blobs.blob_path(writable.headers['x-ws-digest'].removeprefix('sha256:')).read_bytes())
    assert set(own['versions']) == {'1.0.0'} and own['dist-tags'] == {'latest': '1.0.0', 'stable': '1.0.0'}
    merged_again = await client.get('/repository/npm-all/demo', headers=headers)
    assert merged_again.headers['etag'] != merged.headers['etag']  # a member changed: merged again

    proxy_tags = await client.get('/repository/npm-proxy/-/package/demo/dist-tags')
    assert proxy_tags.json() == {'latest': '3.0.0', 'next': '3.0.0'}

    await create(admin, {'name': 'npm-ro', 'type': 'group', 'group': {'members': ['npm-proxy']}})
    body = publish_body('demo', '4.0.0', tarball('demo', '4.0.0'))
    assert (await client.put('/repository/npm-ro/demo', content=body, headers=headers)).status_code == 405
    read_only = await token(admin, ['read'])
    assert (await client.put('/repository/npm-all/demo', content=body, headers=read_only)).status_code == 403


@pytest.mark.usefixtures('repos')
async def test_version_whose_tarball_was_cleaned_up_is_dropped(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services
) -> None:
    headers = await token(admin, ['read', 'write', 'delete'])
    for version in ('1.0.0', '1.1.0'):
        body = publish_body('demo', version, tarball('demo', version))
        assert (await client.put('/repository/npm-hosted/demo', content=body, headers=headers)).status_code == 201
    repo = await services.catalog.get('npm-hosted')
    assert repo is not None
    await services.db[ASSETS].delete_one({'repo_id': repo.id, 'path': 'demo/-/demo-1.0.0.tgz'})  # as cleanup does
    missing = await client.get('/repository/npm-hosted/demo/-/demo-1.0.0.tgz', headers=headers)
    assert missing.status_code == 404  # just published: left alone (a publish may be between its two writes)
    assert await services.db[NPM_VERSIONS].count_documents({'repo_id': repo.id}) == 2
    old = datetime.now(UTC) - timedelta(hours=1)
    await services.db[NPM_VERSIONS].update_many({'repo_id': repo.id}, {'$set': {'published_at': old}})
    assert (await client.get('/repository/npm-hosted/demo/-/demo-1.0.0.tgz', headers=headers)).status_code == 404
    full = await client.get('/repository/npm-hosted/demo', headers=headers)
    doc = json.loads(services.blobs.blob_path(full.headers['x-ws-digest'].removeprefix('sha256:')).read_bytes())
    assert list(doc['versions']) == ['1.1.0'] and doc['dist-tags'] == {'latest': '1.1.0'}
