"""Go modules (T-P5-GO) end to end on the real engine, MongoDB and Valkey: a proxy against a scriptable GOPROXY and
checksum database, the checksum database proxy, a group merging two proxies, browse and search."""

import base64
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

from waystation_common.db.collections import ASSETS, GO_MODULES
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.problems import ResponseAborted
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
MODULE = 'example.com/Demo'  # escaped: example.com/!demo
ESC = 'example.com/!demo'
PSEUDO = 'v0.0.0-20240102030405-abcdefabcdef'
MOD = b'module example.com/Demo\n\ngo 1.24\n'


def h1(files: dict[str, bytes]) -> str:
    summary = ''.join(f'{hashlib.sha256(files[n]).hexdigest()}  {n}\n' for n in sorted(files))
    return 'h1:' + base64.b64encode(hashlib.sha256(summary.encode()).digest()).decode()


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


FILES = {
    f'{MODULE}@v1.1.0/go.mod': MOD,
    f'{MODULE}@v1.1.0/demo.go': b'package demo\n\nconst Answer = 42\n' * 20000,  # > one 256 KiB chunk unzipped
}
ZIP = make_zip(FILES)
TAMPERED = make_zip({**FILES, f'{MODULE}@v1.1.0/extra.go': b'package demo\n'})


def info(version: str) -> bytes:
    return json.dumps({'Version': version, 'Time': '2024-01-02T03:04:05Z'}).encode()


def lookup(module: str, version: str, zip_h1: str, mod_h1: str) -> bytes:
    return f'42\n{module} {version} {zip_h1}\n{module} {version}/go.mod {mod_h1}\n\n— sum.golang.org note\n'.encode()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    mod_h1 = h1({'go.mod': MOD})
    async with FakeUpstream() as fake:
        fake.routes.update(
            {
                # proxy A
                f'/a/{ESC}/@v/list': Respond(200, {'Content-Type': 'text/plain'}, b'v1.0.0\nv1.1.0\nv1.2.0\n'),
                f'/a/{ESC}/@latest': Respond(200, {'Content-Type': 'application/json'}, info('v1.2.0')),
                f'/a/{ESC}/@v/v1.1.0.info': Respond(200, {}, info('v1.1.0')),
                f'/a/{ESC}/@v/v1.1.0.mod': Respond(200, {}, MOD),
                f'/a/{ESC}/@v/v1.1.0.zip': Respond(200, {}, ZIP, chunk=65536),
                f'/a/{ESC}/@v/v1.2.0.zip': Respond(200, {}, TAMPERED),  # does not match the checksum database
                f'/a/{ESC}/@v/v1.2.0.mod': Respond(200, {}, MOD),
                f'/a/{ESC}/@v/master.info': Respond(200, {}, info(PSEUDO)),
                f'/a/{ESC}/@v/{PSEUDO}.info': Respond(200, {}, info('v9.9.9')),  # names another version
                '/a/example.com/gone/@v/list': Respond(410, {}, b'not found: gone'),
                '/a/example.com/private/x/@v/v1.0.0.mod': Respond(200, {}, b'module example.com/private/x\n'),
                '/a/example.com/unknown/@v/v1.0.0.mod': Respond(200, {}, b'module example.com/unknown\n'),
                # proxy B
                f'/b/{ESC}/@v/list': Respond(200, {}, b'v1.1.0\nv2.0.0+incompatible\n'),
                f'/b/{ESC}/@v/v2.0.0+incompatible.info': Respond(200, {}, info('v2.0.0+incompatible')),
                # checksum database
                f'/sum/lookup/{ESC}@v1.1.0': Respond(200, {}, lookup(MODULE, 'v1.1.0', h1(FILES), mod_h1)),
                f'/sum/lookup/{ESC}@v1.2.0': Respond(200, {}, lookup(MODULE, 'v1.2.0', h1(FILES), mod_h1)),
                '/sum/lookup/example.com/unknown@v1.0.0': Respond(404, {}, b'not found'),
                '/sum/latest': Respond(200, {}, b'go.sum database tree\n42\nhash\n\n— note\n'),
                '/sum/tile/8/0/000.p/3': Respond(200, {}, b'h' * 96),
                '/sum/tile/8/0/001': Respond(200, {}, b'short'),  # a full tile is 8192 bytes
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
    return await admin.post('/api/v1/repositories', json={'format': 'go', 'online': True, **body})


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    options = {'sumdb_url': f'{upstream.url}sum', 'nosumdb': ['example.com/private/*']}
    proxy = {'upstream_url': f'{upstream.url}a/', 'allow_private_upstream': True}
    made = await create(admin, {'name': 'go-a', 'type': 'proxy', 'proxy': proxy, 'format_options': options})
    assert made.status_code == 201, made.text
    proxy_b = {'upstream_url': f'{upstream.url}b', 'allow_private_upstream': True}
    off = {'sumdb_proxy': False, 'verify_checksums': False}
    made = await create(admin, {'name': 'go-b', 'type': 'proxy', 'proxy': proxy_b, 'format_options': off})
    assert made.status_code == 201, made.text
    made = await create(admin, {'name': 'go-all', 'type': 'group', 'group': {'members': ['go-b', 'go-a']}})
    assert made.status_code == 201, made.text
    hosted = await create(admin, {'name': 'go-hosted', 'type': 'hosted'})
    assert hosted.status_code == 422  # Go has no hosted repositories


async def stored(services: Services, repo: str, path: str) -> dict[str, Any] | None:
    repo_doc = await services.catalog.get(repo)
    assert repo_doc is not None
    doc: dict[str, Any] | None = await services.db[ASSETS].find_one({'repo_id': repo_doc.id, 'path': path})
    return doc


# ---------------------------------------------------------------------------- proxy
@pytest.mark.usefixtures('repos')
async def test_proxy_lists_info_and_verified_files(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    listed = await client.get(f'/repository/go-a/{ESC}/@v/list')
    assert listed.status_code == 200 and 'x-accel-redirect' in listed.headers
    assert listed.headers['content-type'].startswith('text/plain')
    upper = await client.get(f'/repository/go-a/{MODULE}/@v/list')  # normalised to the escaped form
    assert upper.status_code == 200 and upper.headers['x-ws-cache'] == 'HIT'
    assert upstream.hits(f'/a/{ESC}/@v/list') == 1
    assert (await client.get(f'/repository/go-a/{ESC}/@latest')).status_code == 200
    assert (await client.get('/repository/go-a/example.com/!Demo/@v/list')).status_code == 404  # bad escape
    assert (await client.get('/repository/go-a/example.com/gone/@v/list')).status_code == 404  # upstream 410
    assert (await client.get(f'/repository/go-a/{ESC}/@v/master.zip')).status_code == 404  # not canonical
    put = await client.put(f'/repository/go-a/{ESC}/@v/list', content=b'x')
    assert put.status_code in (401, 405)

    zipped = await client.get(f'/repository/go-a/{ESC}/@v/v1.1.0.zip')
    assert zipped.status_code == 200 and zipped.content == ZIP and zipped.headers['x-ws-cache'] == 'FETCHING'
    cached = await stored(services, 'go-a', f'{ESC}/@v/v1.1.0.zip')
    assert cached is not None and cached['kind'] == 'content' and cached['content_type'] == 'application/zip'
    mod = await client.get(f'/repository/go-a/{ESC}/@v/v1.1.0.mod')
    assert mod.status_code == 200 and mod.content == MOD
    assert upstream.hits(f'/sum/lookup/{ESC}@v1.1.0') == 1  # one cached lookup verifies both files
    assert (await client.get(f'/repository/go-a/{ESC}/@v/v1.1.0.zip')).headers['x-ws-cache'] == 'HIT'

    with pytest.raises(ResponseAborted):  # cut before the last bytes: the zip does not match go.sum
        await client.get(f'/repository/go-a/{ESC}/@v/v1.2.0.zip')
    assert await stored(services, 'go-a', f'{ESC}/@v/v1.2.0.zip') is None
    assert (await client.get(f'/repository/go-a/{ESC}/@v/v1.2.0.mod')).status_code == 200

    info_ok = await client.get(f'/repository/go-a/{ESC}/@v/v1.1.0.info')
    assert info_ok.status_code == 200 and info_ok.json()['Version'] == 'v1.1.0'
    query = await client.get(f'/repository/go-a/{ESC}/@v/master.info')
    assert query.status_code == 200 and query.json()['Version'] == PSEUDO
    assert (await stored(services, 'go-a', f'{ESC}/@v/master.info') or {}).get('kind') == 'metadata'
    with pytest.raises(ResponseAborted):  # an .info naming another version is refused
        await client.get(f'/repository/go-a/{ESC}/@v/{PSEUDO}.info')

    private = await client.get('/repository/go-a/example.com/private/x/@v/v1.0.0.mod')
    assert private.status_code == 200  # nosumdb: not looked up
    assert not any(s.path.startswith('/sum/lookup/example.com/private') for s in upstream.seen)
    unknown = await client.get('/repository/go-a/example.com/unknown/@v/v1.0.0.mod')
    assert unknown.status_code == 200  # not in the checksum database: cached unverified
    assert upstream.hits('/sum/lookup/example.com/unknown@v1.0.0') == 1


@pytest.mark.usefixtures('repos')
async def test_checksum_database_proxy(client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    base = '/repository/go-a/sumdb/sum.golang.org'
    supported = await client.get(f'{base}/supported')
    assert supported.status_code == 200 and supported.content == b''
    assert (await client.get('/repository/go-a/sumdb/sum.example.com/supported')).status_code == 404
    assert (await client.get('/repository/go-b/sumdb/sum.golang.org/supported')).status_code == 404  # option off
    latest = await client.get(f'{base}/latest')
    assert latest.status_code == 200 and latest.content.startswith(b'go.sum database tree')
    found = await client.get(f'{base}/lookup/{MODULE}@v1.1.0')
    assert found.status_code == 200 and found.content.startswith(b'42\n')
    again = await client.get(f'{base}/lookup/{ESC}@v1.1.0')
    assert again.headers['x-ws-cache'] == 'HIT' and upstream.hits(f'/sum/lookup/{ESC}@v1.1.0') == 1
    assert (await client.get(f'{base}/lookup/example.com/unknown@v1.0.0')).status_code == 404
    tile = await client.get(f'{base}/tile/8/0/000.p/3')
    assert tile.status_code == 200 and len(tile.content) == 96
    assert (await client.get(f'{base}/tile/8/0/001')).status_code == 502  # wrong size
    assert (await client.get(f'{base}/tile/8/0/0001')).status_code == 404
    assert (await client.get(f'{base}/other')).status_code == 404


# ---------------------------------------------------------------------------- group, browse, search
async def token(admin: httpx.AsyncClient) -> str:
    made = await admin.post('/api/v1/auth/me/tokens', json={'name': 'go', 'scopes': ['read']})
    assert made.status_code == 201, made.text
    return str(made.json()['secret'])


@pytest.mark.usefixtures('repos')
async def test_group_merges_lists_and_serves_members(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    assert (await client.get(f'/repository/go-all/{ESC}/@v/list')).status_code == 404  # hidden from anonymous
    auth = ('go', await token(admin))
    listed = await client.get(f'/repository/go-all/{ESC}/@v/list', auth=auth)
    assert listed.status_code == 200, listed.text
    assert listed.text == 'v2.0.0+incompatible\nv1.2.0\nv1.1.0\nv1.0.0\n'
    etag = listed.headers['etag']
    assert (await client.get(f'/repository/go-all/{ESC}/@v/list', auth=auth, headers={'If-None-Match': etag})).status_code == 304

    zipped = await client.get(f'/repository/go-all/{ESC}/@v/v1.1.0.zip', auth=auth)
    assert zipped.status_code == 200 and zipped.content == ZIP
    assert zipped.headers['x-ws-group-member'] == 'go-a'  # go-b does not have it
    for ext in ('mod', 'info'):
        member = await client.get(f'/repository/go-all/{ESC}/@v/v1.1.0.{ext}', auth=auth)
        assert member.status_code == 200 and member.headers['x-ws-group-member'] == 'go-a'
    again = await client.get(f'/repository/go-all/{ESC}/@v/v1.1.0.zip', auth=auth)
    assert again.headers['x-ws-group-member'] == 'go-a' and 'x-accel-redirect' in again.headers
    incompatible = await client.get(f'/repository/go-all/{ESC}/@v/v2.0.0+incompatible.info', auth=auth)
    assert incompatible.headers['x-ws-group-member'] == 'go-b'
    assert (await client.get('/repository/go-all/example.com/none/@v/v1.0.0.zip', auth=auth)).status_code == 404
    supported = await client.get('/repository/go-all/sumdb/sum.golang.org/supported', auth=auth)
    assert supported.status_code == 200 and supported.headers['x-ws-group-member'] == 'go-a'

    await services.formats.plugin('go').drain()  # type: ignore[union-attr]
    summaries = {d['name']: d async for d in services.db[GO_MODULES].find({})}
    assert summaries[MODULE]['name_lc'] == MODULE.lower()

    modules = await admin.get('/api/v1/repositories/go-a/go/modules')
    assert modules.status_code == 200, modules.text
    item = next(i for i in modules.json()['items'] if i['module'] == MODULE)
    assert (item['latest'], item['version_count'], item['cached_versions']) == ('v1.2.0', 3, 1)
    versions = await admin.get('/api/v1/repositories/go-a/go/versions', params={'module': MODULE})
    assert versions.status_code == 200, versions.text
    page = versions.json()
    assert [v['version'] for v in page['items']] == ['v1.2.0', 'v1.1.0', 'v1.0.0']
    held = next(v for v in page['items'] if v['version'] == 'v1.1.0')
    assert held['cached'] and {f['kind'] for f in held['files']} == {'info', 'mod', 'zip'}
    assert held['time'].startswith('2024-01-02T03:04:05')
    assert (await admin.get('/api/v1/repositories/go-all/go/modules')).status_code == 404  # browsed via members
    assert (await admin.get('/api/v1/repositories/go-a/go/versions', params={'module': 'nope'})).status_code == 404

    search = await admin.get('/api/v1/search', params={'q': 'example.com/demo', 'format': 'go'})
    assert search.status_code == 200
    hits = [h for h in search.json()['items'] if h['kind'] == 'package']
    assert {h['repository'] for h in hits} >= {'go-a'} and hits[0]['browse']['query'] == {'module': MODULE}
