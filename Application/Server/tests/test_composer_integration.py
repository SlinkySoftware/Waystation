"""Composer repositories (T-P6-COMPOSER) end to end on real MongoDB/Valkey with a scriptable Composer 2 upstream:
packages.json, minified p2 documents rewritten to the repository, archives fetched, checked, verified against their
shasum and pinned (a changed archive after cleanup is refused); hosted uploads (bounded zip, composer.json checked),
write policies and deletes; group merges and archives through the group; browse views and the global search."""

import hashlib
import io
import json
import zipfile
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import asgi_client

from waystation_common import composer
from waystation_common.db.collections import ASSETS, AUDIT, COMPOSER_PACKAGES
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.composer.plugin import ComposerPlugin
from waystation_server.problems import ResponseAborted
from waystation_server.services import Services

N_1_0_0_0 = '1.0.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_1_1_0_0 = '1.1.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_2_0_0_0 = '2.0.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_2_1_0_0 = '2.1.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
JSON = {'Content-Type': 'application/json'}
REF1 = '1' * 40
REF2 = '2' * 40


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():  # a fixed timestamp: the same files make the same zip
            zf.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def package_zip(name: str, *, top: str = '', extra: dict[str, bytes] | None = None, **fields: Any) -> bytes:
    doc = {'name': name, 'type': 'library', 'description': f'{name} package', **fields}
    return make_zip({f'{top}composer.json': json.dumps(doc).encode(), f'{top}src/A.php': b'<?php', **(extra or {})})


ARCHIVE1 = package_zip('demo/lib', top='demo-lib-111/')
ARCHIVE2 = package_zip('demo/lib', top='demo-lib-222/')
TAMPERED = package_zip('demo/lib', top='other/')
UNSAFE = make_zip({'../evil.php': b'<?php'})


def entry(url: str, version: str, ref: str, archive: bytes, **dist: Any) -> dict[str, Any]:
    return {
        'name': 'demo/lib',
        'description': 'Demo library from upstream',
        'version': version,
        'version_normalized': composer.normalise(version),
        'type': 'library',
        'require': {'php': '>=8.1'},
        'source': {'type': 'git', 'url': 'https://example.com/demo/lib.git', 'reference': ref},
        'dist': {'type': 'zip', 'url': f'{url}archives/{ref}.zip', 'reference': ref, 'shasum': '', **dist},
    }


def p2(name: str, *entries: dict[str, Any]) -> Respond:
    doc = {'packages': {name: composer.minify(list(entries))}, 'minified': composer.MINIFIED}
    return Respond(200, {**JSON, 'Last-Modified': 'Sun, 22 Nov 2026 10:00:00 GMT'}, json.dumps(doc).encode())


def routes(url: str) -> dict[str, Any]:
    root = {'packages': [], 'metadata-url': '/p2/%package%.json', 'providers-url': '/p/%package%$%hash%.json'}
    bad_sha = {**entry(url, '1.0.0', 'b' * 40, ARCHIVE1, shasum='0' * 40), 'name': 'bad/lib'}
    unsafe = {**entry(url, '1.0.0', 'c' * 40, UNSAFE), 'name': 'unsafe/lib'}
    elsewhere = {**entry(url, '1.0.0', 'd' * 40, ARCHIVE1), 'name': 'far/lib'}
    elsewhere['dist'] = {**elsewhere['dist'], 'url': 'https://elsewhere.example.net/far.zip'}
    return {
        '/packages.json': Respond(200, JSON, json.dumps(root).encode()),
        '/p2/demo/lib.json': p2(
            'demo/lib',
            entry(url, 'v2.0.0', REF2, ARCHIVE2, shasum=hashlib.sha1(ARCHIVE2).hexdigest()),
            entry(url, 'v1.0.0', REF1, ARCHIVE1),
            entry(url, '2.1.0-beta1', 'e' * 40, ARCHIVE2),
        ),
        '/p2/demo/lib~dev.json': p2('demo/lib', entry(url, 'dev-main', 'f' * 40, ARCHIVE1)),
        '/p2/bad/lib.json': p2('bad/lib', bad_sha),
        '/p2/unsafe/lib.json': p2('unsafe/lib', unsafe),
        '/p2/far/lib.json': p2('far/lib', elsewhere),
        f'/archives/{REF1}.zip': Respond(200, {'Content-Type': 'application/zip'}, ARCHIVE1),
        f'/archives/{REF2}.zip': Respond(200, {'Content-Type': 'application/zip'}, ARCHIVE2),
        f'/archives/{"b" * 40}.zip': Respond(200, {}, ARCHIVE1),
        f'/archives/{"c" * 40}.zip': Respond(200, {}, UNSAFE),
        f'/archives/{"f" * 40}.zip': Respond(200, {}, ARCHIVE1),
    }


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        fake.routes.update(routes(fake.url))
        yield fake


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


@pytest.fixture
async def token(admin: httpx.AsyncClient) -> str:
    created = await admin.post('/api/v1/auth/me/tokens', json={'name': 'php', 'scopes': ['read', 'write', 'delete']})
    assert created.status_code == 201, created.text
    return str(created.json()['secret'])


async def create(admin: httpx.AsyncClient, body: dict[str, Any]) -> None:
    made = await admin.post('/api/v1/repositories', json={'format': 'composer', 'online': True, **body})
    assert made.status_code == 201, made.text


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    proxy = {'upstream_url': upstream.url.rstrip('/'), 'allow_private_upstream': True, 'metadata_max_age_s': 3600}
    await create(admin, {'name': 'php-proxy', 'type': 'proxy', 'proxy': proxy})
    await create(admin, {'name': 'php-hosted', 'type': 'hosted'})
    await create(admin, {'name': 'php-group', 'type': 'group', 'group': {'members': ['php-hosted', 'php-proxy']}})


def plugin(services: Services) -> ComposerPlugin:
    found = services.formats.plugin('composer')
    assert isinstance(found, ComposerPlugin)
    return found


def served(services: Services, response: httpx.Response) -> bytes:
    """The bytes behind an X-Accel answer (the ETag names the blob)."""
    assert response.status_code == 200, response.text
    if 'x-accel-redirect' not in response.headers:
        return response.content
    digest = response.headers['etag'].strip('"').removeprefix('sha256:')
    return services.blobs.blob_path(digest).read_bytes()


async def refused(client: httpx.AsyncClient, url: str, auth: tuple[str, str]) -> None:
    """The upstream bytes are refused: 502 before anything is sent, or the transfer is cut."""
    try:
        response = await client.get(url, auth=auth)
    except ResponseAborted:
        return
    assert response.status_code == 502, response.status_code


async def upload(
    client: httpx.AsyncClient, repo: str, name: str, version: str, data: bytes, token: str
) -> httpx.Response:
    return await client.put(f'/repository/{repo}/packages/upload/{name}/{version}', content=data, auth=('admin', token))


async def test_proxy(
    services: Services, client: httpx.AsyncClient, repos: None, upstream: FakeUpstream, token: str
) -> None:
    auth = ('admin', token)
    root = (await client.get('/repository/php-proxy/packages.json', auth=auth)).json()
    assert root['metadata-url'].endswith('/repository/php-proxy/p2/%package%.json')
    assert 'providers-url' not in root

    doc = json.loads(served(services, await client.get('/repository/php-proxy/p2/Demo/Lib.json', auth=auth)))
    assert doc['minified'] == composer.MINIFIED
    entries = composer.expand(doc['packages']['demo/lib'])
    assert [e['version'] for e in entries] == ['v2.0.0', 'v1.0.0', '2.1.0-beta1']
    dist = entries[1]['dist']
    assert dist['url'].endswith(f'/repository/php-proxy/dists/demo/lib/{N_1_0_0_0}/{REF1}.zip')
    assert 'source' in entries[1]
    path = dist['url'].split('/repository/php-proxy/', 1)[1]

    got = await client.get(f'/repository/php-proxy/{path}', auth=auth)
    assert got.status_code == 200 and got.content == ARCHIVE1
    second = await client.get(f'/repository/php-proxy/dists/demo/lib/{N_2_0_0_0}/{REF2}.zip', auth=auth)
    assert second.status_code == 200 and second.content == ARCHIVE2  # verified against its shasum
    again = await client.get(f'/repository/php-proxy/{path}', auth=auth)
    assert served(services, again) == ARCHIVE1 and again.headers['x-ws-cache'] == 'HIT'
    assert upstream.hits(f'/archives/{REF1}.zip') == 1
    repo = await services.catalog.get('php-proxy')
    assert repo is not None
    pins = await services.db[COMPOSER_PACKAGES].find_one({'repo_id': repo.id, 'name': 'demo/lib'})
    assert pins is not None
    assert {p['path']: p['sha256'] for p in pins['pins']}[path] == hashlib.sha256(ARCHIVE1).hexdigest()

    # pinned: after cleanup a changed archive is refused
    await services.db[ASSETS].delete_one({'repo_id': repo.id, 'path': path})
    upstream.routes[f'/archives/{REF1}.zip'] = Respond(200, {}, TAMPERED)
    await refused(client, f'/repository/php-proxy/{path}', auth)
    upstream.routes[f'/archives/{REF1}.zip'] = Respond(200, {}, ARCHIVE1)
    assert (await client.get(f'/repository/php-proxy/{path}', auth=auth)).content == ARCHIVE1

    dev = json.loads(served(services, await client.get('/repository/php-proxy/p2/demo/lib~dev.json', auth=auth)))
    dev_dist = composer.expand(dev['packages']['demo/lib'])[0]['dist']['url']
    assert dev_dist.endswith(f'/dists/demo/lib/dev-main/{"f" * 40}.zip')
    assert (
        await client.get(f'/repository/php-proxy/dists/demo/lib/dev-main/{"f" * 40}.zip', auth=auth)
    ).status_code == 200

    # shasum mismatch, an archive unsafe to extract, an unknown archive, a host that is not allowed
    assert (await client.get('/repository/php-proxy/p2/bad/lib.json', auth=auth)).status_code == 200
    await refused(client, f'/repository/php-proxy/dists/bad/lib/{N_1_0_0_0}/{"b" * 40}.zip', auth)
    assert (await client.get('/repository/php-proxy/p2/unsafe/lib.json', auth=auth)).status_code == 200
    await refused(client, f'/repository/php-proxy/dists/unsafe/lib/{N_1_0_0_0}/{"c" * 40}.zip', auth)
    assert await services.db[ASSETS].count_documents({'path': {'$regex': '^dists/(bad|unsafe)/'}}) == 0
    missing = await client.get(f'/repository/php-proxy/dists/demo/lib/{N_1_0_0_0}/{"9" * 40}.zip', auth=auth)
    assert missing.status_code == 404
    far = json.loads(served(services, await client.get('/repository/php-proxy/p2/far/lib.json', auth=auth)))
    assert composer.expand(far['packages']['far/lib'])[0]['dist']['url'] == 'https://elsewhere.example.net/far.zip'
    assert (await client.get('/repository/php-proxy/p2/none/such.json', auth=auth)).status_code == 404
    assert (await client.get('/repository/php-proxy/_upstream/packages.json', auth=auth)).status_code == 404
    assert (await upload(client, 'php-proxy', 'demo/lib', '1.0.0', ARCHIVE1, token)).status_code == 405

    await plugin(services).drain()
    packages = (await client.get('/api/v1/repositories/php-proxy/composer/packages', auth=auth)).json()
    names = {p['name']: p for p in packages['items']}
    assert names['demo/lib']['latest_version'] == 'v2.0.0' and names['demo/lib']['version_count'] == 3
    assert names['demo/lib']['description'] == 'Demo library from upstream'
    versions = (
        await client.get('/api/v1/repositories/php-proxy/composer/versions', params={'package': 'Demo/Lib'}, auth=auth)
    ).json()
    assert [(v['version'], v['cached'], v['stability']) for v in versions['items']] == [
        ('2.1.0-beta1', False, 'beta'),
        ('v2.0.0', True, 'stable'),
        ('v1.0.0', True, 'stable'),
    ]
    assert versions['items'][2]['reference'] == REF1
    found = (await client.get('/api/v1/search', params={'q': 'demo/lib'}, auth=auth)).json()
    assert any(h['format'] == 'composer' and h['name'] == 'demo/lib' for h in found['items'])


async def test_strip_source(
    services: Services, client: httpx.AsyncClient, admin: httpx.AsyncClient, upstream: FakeUpstream, token: str
) -> None:
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True}
    await create(
        admin, {'name': 'stripped', 'type': 'proxy', 'proxy': proxy, 'format_options': {'source_mode': 'strip'}}
    )
    doc = json.loads(served(services, await client.get('/repository/stripped/p2/demo/lib.json', auth=('admin', token))))
    assert all('source' not in e for e in composer.expand(doc['packages']['demo/lib']))
    refused_options = await admin.post(
        '/api/v1/repositories',
        json={'name': 'h', 'format': 'composer', 'type': 'hosted', 'format_options': {'source_mode': 'strip'}},
    )
    assert refused_options.status_code == 422


async def test_hosted(
    services: Services, client: httpx.AsyncClient, admin: httpx.AsyncClient, repos: None, token: str
) -> None:
    auth = ('admin', token)
    assert (await client.put('/repository/php-hosted/packages/upload/acme/demo/1.0.0', content=b'x')).status_code == 401
    first = await upload(client, 'php-hosted', 'acme/demo', '1.0.0', package_zip('acme/demo'), token)
    assert first.status_code == 201, first.text
    beta = await upload(client, 'php-hosted', 'Acme/Demo', 'v1.1.0-beta1', package_zip('acme/demo', top='x/'), token)
    assert beta.status_code == 201, beta.text
    branch = await upload(client, 'php-hosted', 'acme/demo', 'dev-main', package_zip('acme/demo'), token)
    assert branch.status_code == 201, branch.text
    for name, version, data, message in (
        ('acme/demo', '2.0.0', package_zip('other/pkg'), 'names the package'),
        ('acme/demo', '2.0.0', package_zip('acme/demo', version='3.0.0'), 'declares the version'),
        ('acme/demo', '2.0.0', UNSAFE, 'unsafe'),
        ('acme/demo', '2.0.0', b'not a zip', 'not a Composer package'),
        ('acme/demo', '2.0.0', b'', 'empty'),
        ('acme/demo', 'not-a-version', package_zip('acme/demo'), 'not a Composer version'),
    ):
        bad = await upload(client, 'php-hosted', name, version, data, token)
        assert bad.status_code == 400 and message in bad.json()['message'], (message, bad.text)

    root = (await client.get('/repository/php-hosted/packages.json', auth=auth)).json()
    assert root['available-packages'] == ['acme/demo']
    p2 = await client.get('/repository/php-hosted/p2/acme/demo.json', auth=auth)
    entries = composer.expand(p2.json()['packages']['acme/demo'])
    assert [(e['version'], e['version_normalized']) for e in entries] == [
        ('v1.1.0-beta1', f'{N_1_1_0_0}-beta1'),
        ('1.0.0', N_1_0_0_0),
    ]
    sha1 = hashlib.sha1(package_zip('acme/demo')).hexdigest()
    assert entries[1]['dist']['shasum'] == sha1 and entries[1]['type'] == 'library'
    assert entries[1]['dist']['url'].endswith(f'/repository/php-hosted/dists/acme/demo/{N_1_0_0_0}/demo-1.0.0.0.zip')
    cached = await client.get(
        '/repository/php-hosted/p2/acme/demo.json', auth=auth, headers={'If-None-Match': p2.headers['etag']}
    )
    assert cached.status_code == 304
    dev = (await client.get('/repository/php-hosted/p2/acme/demo~dev.json', auth=auth)).json()
    assert [e['version'] for e in composer.expand(dev['packages']['acme/demo'])] == ['dev-main']
    got = await client.get(f'/repository/php-hosted/dists/acme/demo/{N_1_0_0_0}/demo-1.0.0.0.zip', auth=auth)
    assert served(services, got) == package_zip('acme/demo')
    assert (await client.get('/repository/php-hosted/p2/none/such.json', auth=auth)).status_code == 404

    browse = (
        await admin.get('/api/v1/repositories/php-hosted/composer/versions', params={'package': 'acme/demo'})
    ).json()
    assert [(v['version'], v['uploaded_by'], v['cached']) for v in browse['items']] == [
        ('v1.1.0-beta1', 'admin', True),
        ('1.0.0', 'admin', True),
        ('dev-main', 'admin', True),
    ]
    assert browse['package']['latest_version'] == '1.0.0'

    deleted = await client.delete('/repository/php-hosted/packages/upload/acme/demo/v1.1.0-beta1', auth=auth)
    assert deleted.status_code == 204
    again = await client.delete('/repository/php-hosted/packages/upload/acme/demo/1.1.0-beta1', auth=auth)
    assert again.status_code == 404

    await create(admin, {'name': 'once', 'type': 'hosted', 'hosted': {'write_policy': 'disable_redeploy'}})
    assert (await upload(client, 'once', 'acme/demo', '1.0.0', package_zip('acme/demo'), token)).status_code == 201
    assert (await upload(client, 'once', 'acme/demo', '1.0', package_zip('acme/demo'), token)).status_code == 409
    await create(admin, {'name': 'frozen', 'type': 'hosted', 'hosted': {'write_policy': 'read_only'}})
    assert (await upload(client, 'frozen', 'acme/demo', '1.0.0', package_zip('acme/demo'), token)).status_code == 403
    assert (await client.delete('/repository/once/packages/upload/acme/demo/1.0.0', auth=auth)).status_code == 204
    once = await services.catalog.get('once')
    assert once is not None
    assert await services.db[COMPOSER_PACKAGES].count_documents({'repo_id': once.id}) == 0
    await services.audit.flush()
    actions = {d['action'] async for d in services.db[AUDIT].find({'target.kind': 'composer_package'})}
    assert actions == {'content.upload', 'content.delete'}


async def test_group(
    services: Services, client: httpx.AsyncClient, repos: None, upstream: FakeUpstream, token: str
) -> None:
    auth = ('admin', token)
    hosted_v1 = package_zip('demo/lib', description='hosted copy')
    assert (await upload(client, 'php-hosted', 'demo/lib', 'v1.0.0', hosted_v1, token)).status_code == 201
    root = (await client.get('/repository/php-group/packages.json', auth=auth)).json()
    assert root['metadata-url'].endswith('/repository/php-group/p2/%package%.json')
    assert 'available-packages' not in root  # a proxy member: any package may exist

    doc = (await client.get('/repository/php-group/p2/demo/lib.json', auth=auth)).json()
    entries = composer.expand(doc['packages']['demo/lib'])
    assert [e['version_normalized'] for e in entries] == [f'{N_2_1_0_0}-beta1', N_2_0_0_0, N_1_0_0_0]
    by_version = {e['version_normalized']: e for e in entries}
    assert by_version[N_1_0_0_0]['description'] == 'hosted copy'  # the hosted member is first
    assert all('/repository/php-group/dists/' in e['dist']['url'] for e in entries)

    hosted_path = by_version[N_1_0_0_0]['dist']['url'].split('/repository/php-group/', 1)[1]
    hosted = await client.get(f'/repository/php-group/{hosted_path}', auth=auth)
    assert served(services, hosted) == hosted_v1 and hosted.headers['x-ws-group-member'] == 'php-hosted'
    proxied_path = by_version[N_2_0_0_0]['dist']['url'].split('/repository/php-group/', 1)[1]
    proxied = await client.get(f'/repository/php-group/{proxied_path}', auth=auth)
    assert proxied.status_code == 200 and proxied.content == ARCHIVE2
    assert proxied.headers['x-ws-group-member'] == 'php-proxy'
    assert (await client.get('/repository/php-group/p2/none/such.json', auth=auth)).status_code == 404
    assert (await client.get(f'/repository/php-group/dists/none/such/{N_1_0_0_0}/x.zip', auth=auth)).status_code == 404
    assert (await upload(client, 'php-group', 'demo/lib', '9.0.0', hosted_v1, token)).status_code == 405

    await plugin(services).drain()
    packages = (await client.get('/api/v1/repositories/php-group/composer/packages', auth=auth)).json()
    assert [p['name'] for p in packages['items']] == ['demo/lib']
    versions = (
        await client.get('/api/v1/repositories/php-group/composer/versions', params={'package': 'demo/lib'}, auth=auth)
    ).json()
    assert [v['version_normalized'] for v in versions['items']] == [f'{N_2_1_0_0}-beta1', N_2_0_0_0, N_1_0_0_0]
