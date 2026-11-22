"""NuGet repositories (T-P6-NUGET) end to end on real MongoDB/Valkey with a scriptable NuGet v3 upstream: the service
index, version lists, gzip-encoded paged registrations rewritten to the repository, packages verified against the
catalog hash and their nuspec, search pass-through; hosted push with ``X-NuGet-ApiKey``, write policies, unlist,
relist and delete; group merges and pushes through the group; browse views and the global search."""

import base64
import gzip
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

from waystation_common.db.collections import ASSETS, AUDIT, NUGET_PACKAGES
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.nuget.plugin import NugetPlugin
from waystation_server.problems import ResponseAborted
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
JSON = {'Content-Type': 'application/json'}
FOUR = '1.0.0.0'  # forbidden-strings: allow (a four-part NuGet version, not an address)


def nuspec(package_id: str, version: str, description: str = 'A demo library') -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">\n'
        f'  <metadata>\n    <id>{package_id}</id>\n    <version>{version}</version>\n'
        f'    <authors>Ann</authors>\n    <description>{description}</description>\n'
        '  </metadata>\n</package>\n'
    ).encode()


def nupkg(package_id: str, version: str, *, extra: dict[str, bytes] | None = None, **kwargs: Any) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f'{package_id}.nuspec', nuspec(package_id, version, **kwargs))
        archive.writestr('lib/net8.0/Demo.dll', b'MZ' + package_id.encode())
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buf.getvalue()


DEMO1 = nupkg('Demo.Lib', '1.0.0')
DEMO2 = nupkg('Demo.Lib', '2.0.0-beta.1')
BAD = nupkg('Bad.Lib', '1.0.0')
WRONG = nupkg('Other.Lib', '1.0.0')  # served for Wrong.Lib: its nuspec names another package


def sha512_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha512(data).digest()).decode()


def routes(url: str) -> dict[str, Any]:
    reg = f'{url}v3/registration5-gz-semver2/'
    flat = f'{url}v3-flatcontainer/'

    def leaf(package_id: str, version: str, data: bytes) -> dict[str, Any]:
        lower = package_id.lower()
        return {
            '@id': f'{reg}{lower}/{version}.json',
            'catalogEntry': {
                '@id': f'{url}v3/catalog0/data/{lower}.{version}.json',
                'id': package_id,
                'version': version,
                'description': f'{package_id} from upstream',
                'authors': 'Upstream Author',
                'listed': True,
                'packageContent': f'{flat}{lower}/{version}/{lower}.{version}.nupkg',
            },
            'packageContent': f'{flat}{lower}/{version}/{lower}.{version}.nupkg',
            'registration': f'{reg}{lower}/index.json',
        }

    def catalog(package_id: str, version: str, data: bytes) -> Respond:
        doc = {
            'id': package_id,
            'version': version,
            'packageHash': sha512_b64(data),
            'packageHashAlgorithm': 'SHA512',
            'packageSize': len(data),
        }
        return Respond(200, JSON, json.dumps(doc).encode())

    page_url = f'{reg}demo.lib/page/1.0.0/2.0.0-beta.1.json'
    demo_index = {
        '@id': f'{reg}demo.lib/index.json',
        'count': 1,
        'items': [{'@id': page_url, 'count': 2, 'lower': '1.0.0', 'upper': '2.0.0-beta.1'}],  # not inlined
    }
    demo_page = {
        '@id': page_url,
        'items': [leaf('Demo.Lib', '1.0.0', DEMO1), leaf('Demo.Lib', '2.0.0-beta.1', DEMO2)],
        'lower': '1.0.0',
        'upper': '2.0.0-beta.1',
    }
    bad_index = {'@id': f'{reg}bad.lib/index.json', 'items': [{'@id': 'p', 'items': [leaf('Bad.Lib', '1.0.0', BAD)]}]}

    def gz(doc: Any) -> Respond:
        return Respond(200, {**JSON, 'Content-Encoding': 'gzip'}, gzip.compress(json.dumps(doc).encode()))

    service = {
        'version': '3.0.0',
        'resources': [
            {'@id': flat, '@type': 'PackageBaseAddress/3.0.0'},
            {'@id': reg, '@type': 'RegistrationsBaseUrl/3.6.0'},
            {'@id': f'{url}query', '@type': 'SearchQueryService/3.5.0'},
        ],
    }
    search = {
        'totalHits': 1,
        'data': [{'@id': f'{reg}demo.lib/index.json', 'id': 'Demo.Lib', 'version': '1.0.0', 'versions': []}],
    }
    return {
        '/v3/index.json': Respond(200, JSON, json.dumps(service).encode()),
        '/v3-flatcontainer/demo.lib/index.json': Respond(200, JSON, b'{"versions": ["1.0.0", "2.0.0-beta.1"]}'),
        '/v3-flatcontainer/demo.lib/1.0.0/demo.lib.1.0.0.nupkg': Respond(200, {}, DEMO1),
        '/v3-flatcontainer/demo.lib/2.0.0-beta.1/demo.lib.2.0.0-beta.1.nupkg': Respond(200, {}, DEMO2),
        '/v3-flatcontainer/demo.lib/1.0.0/demo.lib.nuspec': Respond(200, {}, nuspec('Demo.Lib', '1.0.0')),
        '/v3-flatcontainer/bad.lib/1.0.0/bad.lib.1.0.0.nupkg': Respond(200, {}, BAD + b'tampered'),
        '/v3-flatcontainer/wrong.lib/1.0.0/wrong.lib.1.0.0.nupkg': Respond(200, {}, WRONG),
        '/v3/registration5-gz-semver2/demo.lib/index.json': gz(demo_index),
        '/v3/registration5-gz-semver2/demo.lib/page/1.0.0/2.0.0-beta.1.json': gz(demo_page),
        '/v3/registration5-gz-semver2/bad.lib/index.json': gz(bad_index),
        '/v3/catalog0/data/demo.lib.1.0.0.json': catalog('Demo.Lib', '1.0.0', DEMO1),
        '/v3/catalog0/data/demo.lib.2.0.0-beta.1.json': catalog('Demo.Lib', '2.0.0-beta.1', DEMO2),
        '/v3/catalog0/data/bad.lib.1.0.0.json': catalog('Bad.Lib', '1.0.0', BAD),
        '/query?q=demo&skip=0&take=20&prerelease=false': Respond(200, JSON, json.dumps(search).encode()),
        '/query?q=&skip=0&take=20&prerelease=false': Respond(200, JSON, json.dumps(search).encode()),
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
    created = await admin.post('/api/v1/auth/me/tokens', json={'name': 'nuget', 'scopes': ['read', 'write', 'delete']})
    assert created.status_code == 201, created.text
    return str(created.json()['secret'])


async def create(admin: httpx.AsyncClient, body: dict[str, Any]) -> None:
    made = await admin.post('/api/v1/repositories', json={'format': 'nuget', 'online': True, **body})
    assert made.status_code == 201, made.text


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    proxy = {
        'upstream_url': f'{upstream.url}v3/index.json',
        'allow_private_upstream': True,
        'metadata_max_age_s': 3600,
    }
    await create(admin, {'name': 'nuget-proxy', 'type': 'proxy', 'proxy': proxy})
    await create(admin, {'name': 'nuget-hosted', 'type': 'hosted'})
    await create(
        admin,
        {
            'name': 'nuget-group',
            'type': 'group',
            'group': {'members': ['nuget-hosted', 'nuget-proxy'], 'writable_member': 'nuget-hosted'},
        },
    )


def plugin(services: Services) -> NugetPlugin:
    found = services.formats.plugin('nuget')
    assert isinstance(found, NugetPlugin)
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


def basic(token: str) -> tuple[str, str]:
    return ('admin', token)


async def push(client: httpx.AsyncClient, repo: str, data: bytes, token: str) -> httpx.Response:
    boundary = 'nuget-boundary-1'
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="package"; filename="package.nupkg"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n'
        ).encode()
        + data
        + f'\r\n--{boundary}--\r\n'.encode()
    )
    return await client.put(
        f'/repository/{repo}/api/v2/package',
        content=body,
        headers={'Content-Type': f'multipart/form-data; boundary="{boundary}"', 'X-NuGet-ApiKey': token},
    )


async def test_proxy(
    services: Services, client: httpx.AsyncClient, repos: None, upstream: FakeUpstream, token: str
) -> None:
    auth = basic(token)
    index = (await client.get('/repository/nuget-proxy/index.json', auth=auth)).json()
    types = {r['@type']: r['@id'] for r in index['resources']}
    assert types['PackageBaseAddress/3.0.0'].endswith('/repository/nuget-proxy/v3-flatcontainer/')
    assert 'PackagePublish/2.0.0' not in types

    versions = await client.get('/repository/nuget-proxy/v3-flatcontainer/Demo.Lib/index.json', auth=auth)
    assert json.loads(served(services, versions)) == {'versions': ['1.0.0', '2.0.0-beta.1']}

    got = await client.get('/repository/nuget-proxy/v3-flatcontainer/demo.lib/1.0.0/demo.lib.1.0.0.nupkg', auth=auth)
    assert got.status_code == 200 and got.content == DEMO1
    assert upstream.hits('/v3/catalog0/data/demo.lib.1.0.0.json') == 1  # verified against the catalog hash
    assert upstream.hits('/v3/registration5-gz-semver2/demo.lib/page/1.0.0/2.0.0-beta.1.json') == 1
    again = await client.get(
        f'/repository/nuget-proxy/v3-flatcontainer/demo.lib/{FOUR}/demo.lib.{FOUR}.nupkg', auth=auth
    )
    assert served(services, again) == DEMO1 and again.headers['x-ws-cache'] == 'HIT'  # normalised version
    spec = await client.get('/repository/nuget-proxy/v3-flatcontainer/demo.lib/1.0.0/demo.lib.nuspec', auth=auth)
    assert b'<id>Demo.Lib</id>' in served(services, spec)

    reg = await client.get('/repository/nuget-proxy/v3/registration/demo.lib/index.json', auth=auth)
    doc = json.loads(served(services, reg))
    page_id = doc['items'][0]['@id']
    assert page_id.endswith('/repository/nuget-proxy/v3/registration/demo.lib/page/1.0.0/2.0.0-beta.1.json')
    page_path = page_id.split('/repository/nuget-proxy/', 1)[1]
    page = json.loads(served(services, await client.get(f'/repository/nuget-proxy/{page_path}', auth=auth)))
    content = page['items'][0]['packageContent']
    assert content.endswith('/repository/nuget-proxy/v3-flatcontainer/demo.lib/1.0.0/demo.lib.1.0.0.nupkg')

    # the catalog size/hash does not match (refused before or while streaming), the nuspec names another package
    await refused(client, '/repository/nuget-proxy/v3-flatcontainer/bad.lib/1.0.0/bad.lib.1.0.0.nupkg', auth)
    await refused(client, '/repository/nuget-proxy/v3-flatcontainer/wrong.lib/1.0.0/wrong.lib.1.0.0.nupkg', auth)
    assert await services.db[ASSETS].count_documents({'path': {'$regex': '^v3-flatcontainer/(bad|wrong)'}}) == 0
    missing = await client.get('/repository/nuget-proxy/v3-flatcontainer/none.lib/index.json', auth=auth)
    assert missing.status_code == 404
    assert (await client.get('/repository/nuget-proxy/_upstream/index.json', auth=auth)).status_code == 404

    search = (await client.get('/repository/nuget-proxy/query', params={'q': 'demo'}, auth=auth)).json()
    assert search['data'][0]['@id'].endswith('/repository/nuget-proxy/v3/registration/demo.lib/index.json')
    pushed = await push(client, 'nuget-proxy', DEMO1, token)
    assert pushed.status_code == 405

    await plugin(services).drain()
    packages = (await client.get('/api/v1/repositories/nuget-proxy/nuget/packages', auth=auth)).json()
    # bad.lib: its registration was cached (to verify the refused package): it lists the version
    assert [(p['name'], p['id'], p['latest_version']) for p in packages['items']] == [
        ('bad.lib', 'Bad.Lib', '1.0.0'),
        ('demo.lib', 'Demo.Lib', '1.0.0'),
    ]
    assert packages['items'][1]['description'] == 'Demo.Lib from upstream'
    page2 = (
        await client.get('/api/v1/repositories/nuget-proxy/nuget/versions', params={'package': 'DEMO.LIB'}, auth=auth)
    ).json()
    assert [(v['version'], v['cached'], v['prerelease']) for v in page2['items']] == [
        ('2.0.0-beta.1', False, True),
        ('1.0.0', True, False),
    ]
    found = (await client.get('/api/v1/search', params={'q': 'demo.lib'}, auth=auth)).json()
    assert any(h['format'] == 'nuget' and h['name'] == 'Demo.Lib' for h in found['items'])


async def test_hosted(
    services: Services, client: httpx.AsyncClient, admin: httpx.AsyncClient, repos: None, token: str
) -> None:
    auth = basic(token)
    assert (await push(client, 'nuget-hosted', DEMO1, 'not-a-token')).status_code == 401
    anonymous = await client.put('/repository/nuget-hosted/api/v2/package', content=DEMO1)
    assert anonymous.status_code == 401
    first = await push(client, 'nuget-hosted', DEMO1, token)
    assert first.status_code == 201, first.text
    raw = await client.put('/repository/nuget-hosted/api/v2/package', content=DEMO2, headers={'X-NuGet-ApiKey': token})
    assert raw.status_code == 201, raw.text
    unsafe = nupkg('Evil.Lib', '1.0.0', extra={'../../evil.txt': b'x'})
    refused = await push(client, 'nuget-hosted', unsafe, token)
    assert refused.status_code == 400 and 'unsafe' in refused.text
    assert (await push(client, 'nuget-hosted', b'not a zip', token)).status_code == 400

    index = (await client.get('/repository/nuget-hosted/index.json', auth=auth)).json()
    assert any(r['@type'] == 'PackagePublish/2.0.0' for r in index['resources'])
    versions = (await client.get('/repository/nuget-hosted/v3-flatcontainer/demo.lib/index.json', auth=auth)).json()
    assert versions == {'versions': ['1.0.0', '2.0.0-beta.1']}
    got = await client.get('/repository/nuget-hosted/v3-flatcontainer/demo.lib/1.0.0/demo.lib.1.0.0.nupkg', auth=auth)
    assert served(services, got) == DEMO1
    reg = (await client.get('/repository/nuget-hosted/v3/registration/demo.lib/index.json', auth=auth)).json()
    leaves = reg['items'][0]['items']
    assert [leaf['catalogEntry']['version'] for leaf in leaves] == ['1.0.0', '2.0.0-beta.1']
    assert leaves[0]['catalogEntry']['packageHash'] == sha512_b64(DEMO1)
    leaf = await client.get('/repository/nuget-hosted/v3/registration/demo.lib/1.0.0.json', auth=auth)
    assert leaf.status_code == 200 and leaf.json()['listed'] is True
    etag = leaf.headers['etag']
    cached = await client.get(
        '/repository/nuget-hosted/v3/registration/demo.lib/1.0.0.json', auth=auth, headers={'If-None-Match': etag}
    )
    assert cached.status_code == 304

    search = (await client.get('/repository/nuget-hosted/query', params={'q': 'demo'}, auth=auth)).json()
    assert search['totalHits'] == 1 and search['data'][0]['version'] == '1.0.0'
    semver2 = (
        await client.get(
            '/repository/nuget-hosted/query',
            params={'q': 'Demo', 'prerelease': 'true', 'semVerLevel': '2.0.0'},
            auth=auth,
        )
    ).json()
    assert semver2['data'][0]['version'] == '2.0.0-beta.1'

    unlisted = await client.delete(
        '/repository/nuget-hosted/api/v2/package/Demo.Lib/1.0.0', headers={'X-NuGet-ApiKey': token}
    )
    assert unlisted.status_code == 204
    reg = (await client.get('/repository/nuget-hosted/v3/registration/demo.lib/index.json', auth=auth)).json()
    assert reg['items'][0]['items'][0]['catalogEntry']['listed'] is False
    assert (await client.get('/repository/nuget-hosted/query', params={'q': 'demo'}, auth=auth)).json()['data'] == []
    still = await client.get('/repository/nuget-hosted/v3-flatcontainer/demo.lib/1.0.0/demo.lib.1.0.0.nupkg', auth=auth)
    assert still.status_code == 200  # unlisted versions stay restorable
    relisted = await client.post(
        '/repository/nuget-hosted/api/v2/package/demo.lib/1.0.0', headers={'X-NuGet-ApiKey': token}
    )
    assert relisted.status_code == 200
    listing = await admin.post(
        '/api/v1/repositories/nuget-hosted/nuget/listing',
        json={'package': 'Demo.Lib', 'version': '2.0.0-beta.1', 'listed': False},
    )
    assert listing.json() == {'package': 'demo.lib', 'version': '2.0.0-beta.1', 'listed': False}
    browse = (
        await admin.get('/api/v1/repositories/nuget-hosted/nuget/versions', params={'package': 'demo.lib'})
    ).json()
    assert [(v['version'], v['listed'], v['uploaded_by']) for v in browse['items']] == [
        ('2.0.0-beta.1', False, 'admin'),
        ('1.0.0', True, 'admin'),
    ]

    await create(admin, {'name': 'once', 'type': 'hosted', 'hosted': {'write_policy': 'disable_redeploy'}})
    assert (await push(client, 'once', DEMO1, token)).status_code == 201
    assert (await push(client, 'once', DEMO1, token)).status_code == 409
    await create(admin, {'name': 'purge', 'type': 'hosted', 'format_options': {'delete_mode': 'delete'}})
    assert (await push(client, 'purge', DEMO1, token)).status_code == 201
    deleted = await client.delete('/repository/purge/api/v2/package/demo.lib/1.0.0', headers={'X-NuGet-ApiKey': token})
    assert deleted.status_code == 204
    gone = await client.get('/repository/purge/v3-flatcontainer/demo.lib/index.json', auth=auth)
    assert gone.status_code == 404
    purge = await services.catalog.get('purge')
    assert purge is not None
    assert await services.db[NUGET_PACKAGES].count_documents({'repo_id': purge.id}) == 0
    again = await client.delete('/repository/purge/api/v2/package/demo.lib/1.0.0', headers={'X-NuGet-ApiKey': token})
    assert again.status_code == 404
    await services.audit.flush()
    actions = {d['action'] async for d in services.db[AUDIT].find({'target.kind': 'nuget_package'})}
    assert actions == {'content.upload', 'content.update', 'content.delete'}


async def test_group(
    services: Services, client: httpx.AsyncClient, repos: None, upstream: FakeUpstream, token: str
) -> None:
    auth = basic(token)
    pushed = await push(client, 'nuget-group', nupkg('Demo.Lib', '3.0.0'), token)
    assert pushed.status_code == 201 and pushed.headers['x-ws-group-member'] == 'nuget-hosted'
    index = (await client.get('/repository/nuget-group/index.json', auth=auth)).json()
    assert any(r['@type'] == 'PackagePublish/2.0.0' for r in index['resources'])

    versions = (await client.get('/repository/nuget-group/v3-flatcontainer/demo.lib/index.json', auth=auth)).json()
    assert versions == {'versions': ['1.0.0', '2.0.0-beta.1', '3.0.0']}
    reg = (await client.get('/repository/nuget-group/v3/registration/demo.lib/index.json', auth=auth)).json()
    leaves = reg['items'][0]['items']
    assert [leaf['catalogEntry']['version'] for leaf in leaves] == ['1.0.0', '2.0.0-beta.1', '3.0.0']
    assert all('/repository/nuget-group/' in leaf['packageContent'] for leaf in leaves)

    proxied = await client.get(
        '/repository/nuget-group/v3-flatcontainer/demo.lib/1.0.0/demo.lib.1.0.0.nupkg', auth=auth
    )
    assert proxied.status_code == 200 and proxied.headers['x-ws-group-member'] == 'nuget-proxy'
    hosted = await client.get('/repository/nuget-group/v3-flatcontainer/demo.lib/3.0.0/demo.lib.3.0.0.nupkg', auth=auth)
    assert hosted.headers['x-ws-group-member'] == 'nuget-hosted'
    leaf = await client.get('/repository/nuget-group/v3/registration/demo.lib/3.0.0.json', auth=auth)
    assert leaf.status_code == 200 and '/repository/nuget-group/' in leaf.json()['packageContent']

    search = (await client.get('/repository/nuget-group/query', params={'q': 'demo'}, auth=auth)).json()
    assert [h['id'] for h in search['data']] == ['Demo.Lib']
    assert search['data'][0]['version'] == '3.0.0'  # the hosted member comes first
    deleted = await client.delete(
        '/repository/nuget-group/api/v2/package/demo.lib/3.0.0', headers={'X-NuGet-ApiKey': token}
    )
    assert deleted.status_code == 204

    await plugin(services).drain()
    packages = (await client.get('/api/v1/repositories/nuget-group/nuget/packages', auth=auth)).json()
    assert [p['name'] for p in packages['items']] == ['demo.lib']
    page = (
        await client.get('/api/v1/repositories/nuget-group/nuget/versions', params={'package': 'demo.lib'}, auth=auth)
    ).json()
    assert [v['version'] for v in page['items']][:1] == ['3.0.0']
