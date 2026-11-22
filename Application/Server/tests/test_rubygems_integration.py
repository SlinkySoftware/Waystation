"""RubyGems repositories (T-P6-GEMS) end to end on real MongoDB/Valkey with a scriptable compact index upstream:
versions/info/names cached and served with Repr-Digest, gems verified against the info checksum (a tampered gem is
refused), gemspecs and legacy specs passed through, the dependency API answering 404; hosted push (a bare API token
in Authorization), write policies, generated compact index and Marshal gemspec, yank; group merges; browse views and
the global search."""

import base64
import gzip
import hashlib
import zlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_gems import make_gem
from ws_testing import asgi_client

from waystation_common.db.collections import ASSETS, AUDIT, RUBYGEMS_GEMS
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.rubygems.plugin import RubygemsPlugin
from waystation_server.problems import ResponseAborted
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
TEXT = {'Content-Type': 'text/plain; charset=utf-8'}
DEMO_1 = make_gem('demo', '1.0.0')
DEMO_2 = make_gem('demo', '2.0.0', deps=[('rack', '>=', '2.0', 'runtime')])
TAMPERED = make_gem('demo', '2.0.0', summary='Not what the index says')
NATIVE = make_gem('demo', '1.0.0', platform='x86_64-linux')
SPEC_RZ = zlib.compress(b'\x04\x08u:\x17Gem::Specification\x00')


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


INFO = (
    f'---\n1.0.0 |checksum:{sha(DEMO_1)}\n'
    f'1.0.0-x86_64-linux |checksum:{sha(NATIVE)}\n'
    f'2.0.0 rack:>= 2.0|checksum:{sha(DEMO_2)},ruby:>= 3.0\n'
    '3.0.0 |ruby:>= 3.0\n'
).encode()
VERSIONS = (
    b'created_at: 2026-11-01T00:00:00Z\n---\n'
    b'rack 3.0.0 '
    + b'1' * 32
    + b'\ndemo 1.0.0,1.0.0-x86_64-linux,2.0.0,3.0.0 '
    + hashlib.md5(INFO).hexdigest().encode()
    + b'\n'
)


def routes() -> dict[str, Any]:
    return {
        '/versions': Respond(200, {**TEXT, 'ETag': '"v1"'}, VERSIONS),
        '/names': Respond(200, TEXT, b'---\ndemo\nrack\n'),
        '/info/demo': Respond(200, TEXT, INFO),
        '/gems/demo-1.0.0.gem': Respond(200, {'Content-Type': 'application/octet-stream'}, DEMO_1),
        '/gems/demo-2.0.0.gem': Respond(200, {}, TAMPERED),
        '/gems/demo-1.0.0-x86_64-linux.gem': Respond(200, {}, NATIVE),
        '/gems/demo-3.0.0.gem': Respond(200, {}, DEMO_1),
        '/quick/Marshal.4.8/demo-1.0.0.gemspec.rz': Respond(200, {}, SPEC_RZ),
        '/quick/Marshal.4.8/demo-2.0.0.gemspec.rz': Respond(200, {}, b'not zlib'),
        '/specs.4.8.gz': Respond(200, {}, gzip.compress(b'\x04\x08[\x00')),
    }


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        fake.routes.update(routes())
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
    created = await admin.post('/api/v1/auth/me/tokens', json={'name': 'ruby', 'scopes': ['read', 'write', 'delete']})
    assert created.status_code == 201, created.text
    return str(created.json()['secret'])


async def create(admin: httpx.AsyncClient, body: dict[str, Any]) -> None:
    made = await admin.post('/api/v1/repositories', json={'format': 'rubygems', 'online': True, **body})
    assert made.status_code == 201, made.text


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    proxy = {'upstream_url': upstream.url.rstrip('/'), 'allow_private_upstream': True, 'metadata_max_age_s': 3600}
    await create(admin, {'name': 'gems-proxy', 'type': 'proxy', 'proxy': proxy})
    await create(admin, {'name': 'gems-hosted', 'type': 'hosted'})
    await create(admin, {'name': 'gems-group', 'type': 'group', 'group': {'members': ['gems-hosted', 'gems-proxy']}})


def plugin(services: Services) -> RubygemsPlugin:
    found = services.formats.plugin('rubygems')
    assert isinstance(found, RubygemsPlugin)
    return found


def served(services: Services, response: httpx.Response) -> bytes:
    """The bytes behind an X-Accel answer (the ETag names the blob)."""
    assert response.status_code == 200, response.text
    if 'x-accel-redirect' not in response.headers:
        return response.content
    digest = response.headers['etag'].strip('"').removeprefix('sha256:')
    return services.blobs.blob_path(digest).read_bytes()


def repr_digest(data: bytes) -> str:
    return 'sha-256=:' + base64.b64encode(hashlib.sha256(data).digest()).decode() + ':'


async def refused(client: httpx.AsyncClient, url: str, auth: tuple[str, str]) -> None:
    try:
        response = await client.get(url, auth=auth)
    except ResponseAborted:
        return
    assert response.status_code == 502, response.status_code


async def push(client: httpx.AsyncClient, repo: str, data: bytes, token: str | None) -> httpx.Response:
    headers = {'Content-Type': 'application/octet-stream'}
    if token is not None:
        headers['Authorization'] = token  # gem push sends the bare API key
    return await client.post(f'/repository/{repo}/api/v1/gems', content=data, headers=headers)


async def test_proxy(
    services: Services, client: httpx.AsyncClient, repos: None, upstream: FakeUpstream, token: str
) -> None:
    auth = ('admin', token)
    versions = await client.get('/repository/gems-proxy/versions', auth=auth)
    assert served(services, versions) == VERSIONS
    again = await client.get('/repository/gems-proxy/versions', auth=auth)
    assert again.headers['x-ws-cache'] == 'HIT' and again.headers['repr-digest'] == repr_digest(VERSIONS)
    assert again.headers['content-type'].startswith('text/plain')
    head = await client.head('/repository/gems-proxy/versions', auth=auth)
    assert head.status_code == 200 and head.headers['content-length'] == str(len(VERSIONS))
    unchanged = await client.get(
        '/repository/gems-proxy/versions', auth=auth, headers={'If-None-Match': again.headers['etag']}
    )
    assert unchanged.status_code == 304
    assert served(services, await client.get('/repository/gems-proxy/info/demo', auth=auth)) == INFO
    assert served(services, await client.get('/repository/gems-proxy/names', auth=auth)).startswith(b'---\n')

    gem = await client.get('/repository/gems-proxy/gems/demo-1.0.0.gem', auth=auth)
    assert served(services, gem) == DEMO_1
    native = await client.get('/repository/gems-proxy/gems/demo-1.0.0-x86_64-linux.gem', auth=auth)
    assert served(services, native) == NATIVE
    hit = await client.get('/repository/gems-proxy/gems/demo-1.0.0.gem', auth=auth)
    assert hit.headers['x-ws-cache'] == 'HIT' and upstream.hits('/gems/demo-1.0.0.gem') == 1
    await refused(client, '/repository/gems-proxy/gems/demo-2.0.0.gem', auth)  # checksum mismatch
    no_checksum = await client.get('/repository/gems-proxy/gems/demo-3.0.0.gem', auth=auth)
    assert no_checksum.status_code == 400 and 'no checksum' in no_checksum.text
    unknown = await client.get('/repository/gems-proxy/gems/demo-9.9.9.gem', auth=auth)
    assert unknown.status_code == 404
    assert (
        await services.db[ASSETS].count_documents({'path': {'$in': ['gems/demo-2.0.0.gem', 'gems/demo-3.0.0.gem']}})
        == 0
    )

    spec = await client.get('/repository/gems-proxy/quick/Marshal.4.8/demo-1.0.0.gemspec.rz', auth=auth)
    assert served(services, spec) == SPEC_RZ
    bad_spec = await client.get('/repository/gems-proxy/quick/Marshal.4.8/demo-2.0.0.gemspec.rz', auth=auth)
    assert bad_spec.status_code == 502
    assert (await client.get('/repository/gems-proxy/specs.4.8.gz', auth=auth)).status_code == 200
    deps = await client.get('/repository/gems-proxy/api/v1/dependencies?gems=demo', auth=auth)
    assert deps.status_code == 404
    assert (await push(client, 'gems-proxy', DEMO_1, token)).status_code == 405

    await plugin(services).drain()
    gems = (await client.get('/api/v1/repositories/gems-proxy/rubygems/gems', auth=auth)).json()
    assert [(g['name'], g['latest_version'], g['version_count']) for g in gems['items']] == [('demo', '3.0.0', 3)]
    page = (
        await client.get('/api/v1/repositories/gems-proxy/rubygems/versions', params={'gem': 'demo'}, auth=auth)
    ).json()
    rows = [(v['version'], v['platform'], v['cached']) for v in page['items']]
    assert rows == [
        ('3.0.0', 'ruby', False),
        ('2.0.0', 'ruby', False),
        ('1.0.0', 'ruby', True),
        ('1.0.0', 'x86_64-linux', True),
    ]
    assert page['items'][1]['dependencies'] == [{'name': 'rack', 'requirement': '>= 2.0'}]
    assert page['items'][1]['ruby_requirement'] == '>= 3.0'
    found = (await client.get('/api/v1/search', params={'q': 'demo'}, auth=auth)).json()
    assert any(h['format'] == 'rubygems' and h['browse']['query'] == {'gem': 'demo'} for h in found['items'])


async def test_hosted(
    services: Services, client: httpx.AsyncClient, admin: httpx.AsyncClient, repos: None, token: str
) -> None:
    auth = ('admin', token)
    assert (await push(client, 'gems-hosted', DEMO_1, None)).status_code == 401
    assert (await push(client, 'gems-hosted', DEMO_1, 'not-a-token')).status_code == 401
    first = await push(client, 'gems-hosted', make_gem('mine', '1.0.0'), token)
    assert first.status_code == 200 and first.text == 'Successfully registered gem: mine (1.0.0)'
    pre = await push(
        client, 'gems-hosted', make_gem('mine', '1.1.0.rc1', deps=[('demo', '~>', '1.0', 'runtime')]), token
    )
    assert pre.status_code == 200, pre.text
    native = await push(client, 'gems-hosted', make_gem('mine', '1.0.0', platform='java'), token)
    assert native.text == 'Successfully registered gem: mine (1.0.0-java)'
    for data in (b'', b'not a gem', make_gem('mine', '2.0.0', tamper_checksum=True)):
        assert (await push(client, 'gems-hosted', data, token)).status_code == 400

    versions = await client.get('/repository/gems-hosted/versions', auth=auth)
    assert versions.status_code == 200 and versions.headers['repr-digest'] == repr_digest(versions.content)
    info = await client.get('/repository/gems-hosted/info/mine', auth=auth)
    lines = info.content.decode().splitlines()
    mine_1 = make_gem('mine', '1.0.0')
    assert lines[0] == '---' and lines[1] == f'1.0.0 |checksum:{sha(mine_1)},ruby:>= 2.7.0'
    assert lines[2].startswith('1.1.0.rc1 demo:~> 1.0|checksum:')
    assert lines[3].startswith('1.0.0-java |checksum:')
    md5 = hashlib.md5(info.content, usedforsecurity=False).hexdigest()
    assert versions.content.decode().splitlines()[2] == f'mine 1.0.0,1.1.0.rc1,1.0.0-java {md5}'
    assert (
        await client.get(
            '/repository/gems-hosted/info/mine', auth=auth, headers={'If-None-Match': info.headers['etag']}
        )
    ).status_code == 304
    assert (await client.get('/repository/gems-hosted/info/none', auth=auth)).status_code == 404
    assert (await client.get('/repository/gems-hosted/names', auth=auth)).content == b'---\nmine\n'
    assert served(services, await client.get('/repository/gems-hosted/gems/mine-1.0.0.gem', auth=auth)) == mine_1
    rz = served(
        services, await client.get('/repository/gems-hosted/quick/Marshal.4.8/mine-1.0.0.gemspec.rz', auth=auth)
    )
    assert zlib.decompress(rz).startswith(b'\x04\x08u:\x17Gem::Specification')
    specs = await client.get('/repository/gems-hosted/specs.4.8.gz', auth=auth)
    assert b'mine' in gzip.decompress(specs.content) and specs.headers['content-type'] == 'application/x-gzip'
    prerelease = gzip.decompress(
        (await client.get('/repository/gems-hosted/prerelease_specs.4.8.gz', auth=auth)).content
    )
    assert b'1.1.0.rc1' in prerelease

    page = (
        await client.get('/api/v1/repositories/gems-hosted/rubygems/versions', params={'gem': 'mine'}, auth=auth)
    ).json()
    assert [(v['version'], v['platform'], v['uploaded_by']) for v in page['items']] == [
        ('1.1.0.rc1', 'ruby', 'admin'),
        ('1.0.0', 'ruby', 'admin'),
        ('1.0.0', 'java', 'admin'),
    ]
    assert page['gem']['latest_version'] == '1.0.0' and page['items'][0]['prerelease'] is True

    yank = await client.request(
        'DELETE',
        '/repository/gems-hosted/api/v1/gems/yank',
        content=b'gem_name=mine&version=1.0.0&platform=java',
        headers={'Authorization': token, 'Content-Type': 'application/x-www-form-urlencoded'},
    )
    assert yank.status_code == 200, yank.text
    again = await client.request(
        'DELETE',
        '/repository/gems-hosted/api/v1/gems/yank',
        params={'gem_name': 'mine', 'version': '1.0.0', 'platform': 'java'},
        headers={'Authorization': token},
    )
    assert again.status_code == 404
    assert '1.0.0-java' not in (await client.get('/repository/gems-hosted/info/mine', auth=auth)).text
    assert (await client.get('/repository/gems-hosted/gems/mine-1.0.0-java.gem', auth=auth)).status_code == 404
    actions = {d['action'] async for d in services.db[AUDIT].find({'target.kind': 'rubygems_gem'})}
    assert actions == {'content.upload', 'content.delete'}

    await create(admin, {'name': 'once', 'type': 'hosted', 'hosted': {'write_policy': 'disable_redeploy'}})
    assert (await push(client, 'once', mine_1, token)).status_code == 200
    assert (await push(client, 'once', mine_1, token)).status_code == 409
    await create(admin, {'name': 'frozen', 'type': 'hosted', 'hosted': {'write_policy': 'read_only'}})
    assert (await push(client, 'frozen', mine_1, token)).status_code == 403
    repo = await services.catalog.get('gems-hosted')
    assert repo is not None
    assert await services.db[RUBYGEMS_GEMS].count_documents({'repo_id': repo.id, 'name': 'mine'}) == 1


async def test_group(
    services: Services, client: httpx.AsyncClient, repos: None, upstream: FakeUpstream, token: str
) -> None:
    auth = ('admin', token)
    own = make_gem('demo', '0.5.0')
    assert (await push(client, 'gems-hosted', own, token)).status_code == 200
    assert (await push(client, 'gems-hosted', make_gem('mine', '1.0.0'), token)).status_code == 200
    versions = await client.get('/repository/gems-group/versions', auth=auth)
    body = served(services, versions)
    assert versions.headers['repr-digest'] == repr_digest(body)
    lines = {line.split(' ')[0]: line.split(' ') for line in body.decode().splitlines()[2:]}
    assert set(lines) == {'demo', 'mine', 'rack'}
    assert lines['demo'][1] == '0.5.0,1.0.0,1.0.0-x86_64-linux,2.0.0,3.0.0'
    info = await client.get('/repository/gems-group/info/demo', auth=auth)
    assert info.status_code == 200 and info.text.splitlines()[1].startswith('0.5.0 |checksum:')
    assert lines['demo'][2] == hashlib.md5(info.content, usedforsecurity=False).hexdigest()
    single = await client.get('/repository/gems-group/info/mine', auth=auth)
    assert single.headers['x-ws-group-member'] == 'gems-hosted'
    assert lines['mine'][2] == hashlib.md5(single.content, usedforsecurity=False).hexdigest()
    names = await client.get('/repository/gems-group/names', auth=auth)
    assert served(services, names) == b'---\ndemo\nmine\nrack\n'

    hosted_gem = await client.get('/repository/gems-group/gems/demo-0.5.0.gem', auth=auth)
    assert served(services, hosted_gem) == own and hosted_gem.headers['x-ws-group-member'] == 'gems-hosted'
    proxied = await client.get('/repository/gems-group/gems/demo-1.0.0.gem', auth=auth)
    assert served(services, proxied) == DEMO_1 and proxied.headers['x-ws-group-member'] == 'gems-proxy'
    assert (await client.get('/repository/gems-group/gems/none-1.0.0.gem', auth=auth)).status_code == 404
    assert (await push(client, 'gems-group', own, token)).status_code == 405
    specs = gzip.decompress((await client.get('/repository/gems-group/specs.4.8.gz', auth=auth)).content)
    assert b'mine' in specs

    await plugin(services).drain()
    gems = (await client.get('/api/v1/repositories/gems-group/rubygems/gems', auth=auth)).json()
    assert {g['name'] for g in gems['items']} == {'demo', 'mine'}
    page = (
        await client.get('/api/v1/repositories/gems-group/rubygems/versions', params={'gem': 'demo'}, auth=auth)
    ).json()
    assert [v['version'] for v in page['items']] == ['3.0.0', '2.0.0', '1.0.0', '1.0.0', '0.5.0']
    assert (
        await client.get('/api/v1/repositories/gems-group/rubygems/versions', params={'gem': 'x/y'}, auth=auth)
    ).status_code == 404
