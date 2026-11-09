"""Raw hosted and group repositories (T-P2-RAW) against real MongoDB/Valkey and a fake upstream: uploads with checksum
verification, size limit, write policies, deletes, directory listings (HTML and JSON), group reads in member order,
uploads through the writable member, RBAC, audit and the activity feed."""

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import asgi_client

from waystation_common.db.collections import ASSETS, AUDIT, USERS
from waystation_common.events import keys
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.security import passwords
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
DATA = bytes(range(256)) * 1200  # 307200 bytes: more than one 256 KiB chunk
SHA256 = hashlib.sha256(DATA).hexdigest()
SHA1 = hashlib.sha1(DATA, usedforsecurity=False).hexdigest()
MD5 = hashlib.md5(DATA, usedforsecurity=False).hexdigest()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    routes: dict[str, Any] = {
        '/both.txt': Respond(200, {'Content-Type': 'text/plain'}, b'from the upstream\n'),
        '/only-upstream/tool.bin': Respond(200, {}, b'upstream tool'),
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


async def token(client: httpx.AsyncClient, *scopes: str) -> str:
    created = await client.post('/api/v1/auth/me/tokens', json={'name': 'raw', 'scopes': list(scopes)})
    assert created.status_code == 201, created.text
    return str(created.json()['secret'])


@pytest.fixture
async def auth(admin: httpx.AsyncClient) -> tuple[str, str]:
    """Basic auth for format clients: the user name and an API token (passwords are refused there)."""
    return 'admin', await token(admin, 'read', 'write', 'delete')


async def create(admin: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await admin.post('/api/v1/repositories', json={'format': 'raw', 'online': True, **body})


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    made = await create(admin, {'name': 'files', 'type': 'hosted'})
    assert made.status_code == 201, made.text
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True}
    assert (await create(admin, {'name': 'mirror', 'type': 'proxy', 'proxy': proxy})).status_code == 201
    group = {'members': ['files', 'mirror'], 'writable_member': 'files'}
    made = await create(admin, {'name': 'all-files', 'type': 'group', 'group': group})
    assert made.status_code == 201, made.text


async def test_validation(admin: httpx.AsyncClient, repos: None) -> None:
    bad = await create(admin, {'name': 'bad', 'type': 'hosted', 'format_options': {'metadata_path_regex': '^/x'}})
    assert bad.status_code == 422
    assert {e['field'] for e in bad.json()['errors']} == {'format_options.metadata_path_regex'}
    bad = await create(
        admin, {'name': 'bad', 'type': 'group', 'group': {'members': ['files', 'mirror'], 'writable_member': 'mirror'}}
    )
    assert bad.status_code == 422
    setup = (await admin.get('/api/v1/repositories/files/setup')).json()
    contents = [s['content'] for c in setup['clients'] for s in c['snippets']]
    assert any('--upload-file' in c for c in contents)
    assert any('X-Checksum-Sha256' in c for c in contents)


async def test_upload_download_list_delete(
    auth: tuple[str, str], services: Services, client: httpx.AsyncClient, repos: None
) -> None:
    url = '/repository/files/releases/1.0/app-1.0.tar.gz'
    headers = {'X-Checksum-Sha256': SHA256, 'X-Checksum-Sha1': SHA1, 'X-Checksum-Md5': MD5.upper()}
    put = await client.put(url, content=DATA, auth=auth, headers=headers)
    assert put.status_code == 201, put.text
    assert put.headers['etag'] == f'"sha256:{SHA256}"'

    got = await client.get(url)
    assert got.status_code == 404  # hosted repositories are hidden from anonymous (not 403)
    got = await client.get(url, auth=auth)
    assert got.status_code == 200
    assert got.headers['x-accel-redirect'].endswith(SHA256)
    assert got.headers['content-type'] == 'application/gzip'
    assert got.headers['content-disposition'] == 'attachment; filename="app-1.0.tar.gz"'
    assert got.headers['x-content-type-options'] == 'nosniff'
    assert (await client.get(url, auth=auth, headers={'If-None-Match': got.headers['etag']})).status_code == 304
    head = await client.head(url, auth=auth)
    assert head.headers['content-length'] == str(len(DATA))

    # directory listings: JSON (browse API shape) and HTML
    root = await client.get('/repository/files/', auth=auth, headers={'Accept': 'application/json'})
    assert root.status_code == 200
    assert root.json()['entries'] == [{**root.json()['entries'][0], 'name': 'releases', 'kind': 'folder'}]
    folder = await client.get('/repository/files/releases/1.0/', auth=auth, headers={'Accept': 'application/json'})
    entry = folder.json()['entries'][0]
    assert (entry['path'], entry['size'], entry['sha256']) == ('releases/1.0/app-1.0.tar.gz', len(DATA), SHA256)
    page = await client.get('/repository/files/releases/1.0/', auth=auth)
    assert page.headers['content-type'].startswith('text/html')
    assert "default-src 'none'" in page.headers['content-security-policy']
    assert 'href="./app-1.0.tar.gz"' in page.text
    assert (await client.get('/repository/files', auth=auth)).status_code == 200
    assert (await client.get('/repository/files/nothing/here/', auth=auth)).status_code == 404

    # a name that could be markup is escaped in the listing
    odd = '/repository/files/odd/<b>x.txt'
    assert (await client.put(odd, content=b'x', auth=auth)).status_code == 201
    listing = await client.get('/repository/files/odd/', auth=auth)
    assert '<b>' not in listing.text and '&lt;b&gt;x.txt' in listing.text

    # redeploy is allowed by default: the file is replaced
    again = await client.put(url, content=b'version two', auth=auth)
    assert again.status_code == 201
    asset = await services.db[ASSETS].find_one({'path': 'releases/1.0/app-1.0.tar.gz'})
    assert asset is not None and asset['size'] == len(b'version two')
    assert asset['content_type'] == 'application/gzip'

    deleted = await client.delete(url, auth=auth)
    assert deleted.status_code == 204
    assert (await client.get(url, auth=auth)).status_code == 404
    assert (await client.delete(url, auth=auth)).status_code == 404
    assert (await client.put('/repository/files/releases/', content=b'x', auth=auth)).status_code == 400

    await services.audit.flush()
    uploads = [d async for d in services.db[AUDIT].find({'action': 'content.upload'})]
    app = [d['details'] for d in uploads if d['details']['path'] == 'releases/1.0/app-1.0.tar.gz']
    first, second = sorted(app, key=lambda d: d['replaced'])
    assert (first['sha256'], first['sha1'], first['md5'], first['size']) == (SHA256, SHA1, MD5, len(DATA))
    assert second['replaced'] is True
    assert [d['details']['path'] async for d in services.db[AUDIT].find({'action': 'content.delete'})] == [
        'releases/1.0/app-1.0.tar.gz'
    ]
    await services.emitter.flush()
    entries = await services.valkey.xrange(keys.ACTIVITY)
    types = [fields['type'] for _, fields in entries]
    assert 'asset.created' in types and 'asset.deleted' in types
    finished = [json.loads(f['data']) for _, f in entries if f['type'] == 'transfer.finished']
    assert any(e['detail'].get('direction') == 'upload' and e['bytes'] == len(DATA) for e in finished)


async def test_checksums_and_size_limit(
    auth: tuple[str, str], services: Services, client: httpx.AsyncClient, repos: None
) -> None:
    url = '/repository/files/checked.bin'
    for name, value in (('X-Checksum-Sha256', '0' * 64), ('X-Checksum-Sha1', '0' * 40), ('X-Checksum-Md5', '0' * 32)):
        refused = await client.put(url, content=DATA, auth=auth, headers={name: value})
        assert refused.status_code == 400, name
        assert name.lower() in refused.text
    malformed = await client.put(url, content=DATA, auth=auth, headers={'X-Checksum-Sha1': 'xyz'})
    assert malformed.status_code == 400
    assert await services.db[ASSETS].count_documents({'path': 'checked.bin'}) == 0
    assert not list(services.blobs.uploads_dir.iterdir())  # nothing left behind

    services.blobs.max_size = 1000
    too_big = await client.put(url, content=DATA, auth=auth)
    assert too_big.status_code == 413
    assert await services.db[ASSETS].count_documents({'path': 'checked.bin'}) == 0


async def test_write_policies(
    auth: tuple[str, str], admin: httpx.AsyncClient, client: httpx.AsyncClient, repos: None
) -> None:
    made = await create(admin, {'name': 'once', 'type': 'hosted', 'hosted': {'write_policy': 'disable_redeploy'}})
    assert made.status_code == 201, made.text
    assert (await client.put('/repository/once/a.txt', content=b'1', auth=auth)).status_code == 201
    conflict = await client.put('/repository/once/a.txt', content=b'2', auth=auth)
    assert conflict.status_code == 409
    assert (await client.get('/repository/once/a.txt', auth=auth)).status_code == 200
    assert (await client.delete('/repository/once/a.txt', auth=auth)).status_code == 204  # deleting stays allowed

    made = await create(admin, {'name': 'frozen', 'type': 'hosted', 'hosted': {'write_policy': 'read_only'}})
    assert made.status_code == 201
    assert (await client.put('/repository/frozen/a.txt', content=b'1', auth=auth)).status_code == 403
    assert (await client.delete('/repository/frozen/a.txt', auth=auth)).status_code == 403


async def test_group_reads_in_order_and_writes_to_the_member(
    auth: tuple[str, str],
    services: Services,
    admin: httpx.AsyncClient,
    client: httpx.AsyncClient,
    upstream: FakeUpstream,
    repos: None,
) -> None:
    assert (await client.put('/repository/files/both.txt', content=b'hosted copy\n', auth=auth)).status_code == 201
    first = await client.get('/repository/all-files/both.txt', auth=auth)
    assert first.status_code == 200
    assert first.headers['x-ws-group-member'] == 'files'  # the hosted member is first
    assert upstream.hits('/both.txt') == 0
    fetched = await client.get('/repository/all-files/only-upstream/tool.bin', auth=auth)
    assert fetched.status_code == 200
    assert fetched.content == b'upstream tool'
    assert fetched.headers['x-ws-group-member'] == 'mirror'
    assert (await client.get('/repository/all-files/missing.txt', auth=auth)).status_code == 404

    # uploads through the group land in the writable member
    put = await client.put('/repository/all-files/via-group/x.txt', content=b'through the group', auth=auth)
    assert put.status_code == 201
    stored = await client.get('/repository/files/via-group/x.txt', auth=auth)
    assert stored.status_code == 200

    # merged listing: folders from both members, files once
    root = await client.get('/repository/all-files/', auth=auth, headers={'Accept': 'application/json'})
    names = [(e['kind'], e['name']) for e in root.json()['entries']]
    assert names == [('folder', 'only-upstream'), ('folder', 'via-group'), ('file', 'both.txt')]

    # a group without a writable member is read-only
    made = await create(admin, {'name': 'ro-files', 'type': 'group', 'group': {'members': ['files', 'mirror']}})
    assert made.status_code == 201
    assert (await client.put('/repository/ro-files/y.txt', content=b'y', auth=auth)).status_code == 405

    # write on the group alone is not enough: the content goes to the writable member
    role = {'id': 'group-writers', 'name': 'Group writers', 'permissions': [{'action': 'write', 'scope': 'all-files'}]}
    assert (await admin.post('/api/v1/roles', json=role)).status_code == 201
    await services.db[USERS].insert_one(
        {
            '_id': 'id-gail',
            'username': 'gail',
            'display_name': 'Gail',
            'source': 'local',
            'password_hash': await passwords.hash_password(PASSWORD),
            'roles': ['group-writers'],
            'created_at': datetime.now(UTC),
        }
    )
    services.catalog.invalidate()
    async with asgi_client(services) as session:
        login = await session.post('/api/v1/auth/login', json={'username': 'gail', 'password': PASSWORD})
        session.headers['X-CSRF-Token'] = login.json()['csrf_token']
        gail = ('gail', await token(session, 'read', 'write'))
    assert (await client.put('/repository/all-files/z.txt', content=b'z', auth=gail)).status_code == 403
    read = await client.get('/repository/all-files/both.txt', auth=gail)
    assert read.status_code == 200  # reading through the group needs read on the group only
