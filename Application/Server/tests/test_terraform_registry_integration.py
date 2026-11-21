"""Terraform module registry proxy and the hosted Terraform repository (T-P5-TFM) end to end on the real engine,
MongoDB and Valkey: a proxy against a scriptable module registry (204 + ``X-Terraform-Get`` and JSON ``location``
answers, archives cached with their digest pinned, sources passed through), the hosted repository (module and
provider uploads with their checks, immutable versions, deletes, the registry protocols, signed download URLs),
``/.well-known/terraform.json``, one hosted repository per instance, browse and search."""

import hashlib
import io
import json
import tarfile
import zipfile
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urljoin

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_openpgp import PgpKey, Sub, armor, cert, detached
from ws_testing import asgi_client

from waystation_common.db.collections import ASSETS, TERRAFORM_MODULES
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
MODULE = 'acme/net/aws'


def tarball(files: dict[str, bytes], top: str = '') -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(f'{top}/{name}' if top else name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def zipped(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


NET_1 = tarball({'modules/x/main.tf': b'output "x" { value = 1 }\n', 'main.tf': b''})
NET_11 = zipped({'main.tf': b'output "y" { value = 2 }\n'})
HOSTED_MODULE = tarball({'main.tf': b'variable "cidr" {}\n', 'outputs.tf': b'output "o" { value = 1 }\n'})
PROVIDER_FILES = {'terraform-provider-thing_v1.0.0': b'\x7fELF' + b'provider ' * 5000}
PROVIDER_ZIP = zipped(PROVIDER_FILES)


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        base = fake.url
        versions = {'modules': [{'versions': [{'version': v} for v in ('1.0.0', '1.1.0', '2.0.0', '3.0.0')]}]}
        fake.routes.update(
            {
                '/.well-known/terraform.json': Respond(200, {}, b'{"modules.v1": "/v1/modules/"}'),
                '/v1/modules/acme/net/aws/versions': Respond(200, {}, json.dumps(versions).encode()),
                # the Terraform registry's answer: 204 + X-Terraform-Get (an archive with a subdirectory)
                '/v1/modules/acme/net/aws/1.0.0/download': Respond(
                    204, {'X-Terraform-Get': f'{base}files/net-1.0.0.tar.gz//modules/x'}, b''
                ),
                '/files/net-1.0.0.tar.gz': Respond(200, {}, NET_1),
                # the OpenTofu registry's answer: 200 + JSON location (relative)
                '/v1/modules/acme/net/aws/1.1.0/download': Respond(200, {}, b'{"location": "./net-1.1.0.zip"}'),
                '/v1/modules/acme/net/aws/1.1.0/net-1.1.0.zip': Respond(200, {}, NET_11),
                # a git source on another host: passed through
                '/v1/modules/acme/net/aws/2.0.0/download': Respond(
                    204, {'X-Terraform-Get': 'git::https://git.example.com/acme/net?ref=v2.0.0'}, b''
                ),
                # not an archive
                '/v1/modules/acme/net/aws/3.0.0/download': Respond(
                    204, {'X-Terraform-Get': f'{base}files/net-3.0.0.tar.gz'}, b''
                ),
                '/files/net-3.0.0.tar.gz': Respond(200, {}, b'this is not a gzip file'),
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


@pytest.fixture
async def token(admin: httpx.AsyncClient) -> str:
    created = await admin.post('/api/v1/auth/me/tokens', json={'name': 'tf', 'scopes': ['read', 'write', 'delete']})
    assert created.status_code == 201, created.text
    return str(created.json()['secret'])


def bearer(secret: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {secret}'}


async def create(admin: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await admin.post('/api/v1/repositories', json={'format': 'terraform', 'online': True, **body})


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True}
    made = await create(admin, {'name': 'tf', 'type': 'proxy', 'proxy': proxy})
    assert made.status_code == 201, made.text
    made = await create(admin, {'name': 'tf-hosted', 'type': 'hosted'})
    assert made.status_code == 201, made.text


async def asset(services: Services, repo: str, path: str) -> dict[str, Any] | None:
    found = await services.catalog.get(repo)
    assert found is not None
    doc: dict[str, Any] | None = await services.db[ASSETS].find_one({'repo_id': found.id, 'path': path})
    return doc


def served(response: httpx.Response) -> str:
    """The sha256 of what a response delivers: the X-Accel target's blob, or the streamed body."""
    if 'x-accel-redirect' in response.headers:
        return response.headers['x-accel-redirect'].rsplit('/', 1)[-1]
    return hashlib.sha256(response.content).hexdigest()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def drain(services: Services) -> None:
    await services.formats.plugin('terraform').drain()  # type: ignore[union-attr]


def resolve(download_path: str, location: str) -> tuple[str, str]:
    """A client's view of ``X-Terraform-Get``: the archive URL path (with its query) and the subdirectory."""
    url, _, query = location.partition('?')
    archive, _, subdir = url.partition('//')
    absolute = urljoin(f'http://testserver{download_path}', archive)
    return absolute.removeprefix('http://testserver') + (f'?{query}' if query else ''), subdir


async def test_module_proxy(client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repos: None) -> None:
    base = f'/repository/tf/v1/modules/{MODULE}'
    versions = await client.get(f'{base}/versions')
    assert versions.status_code == 200, versions.text
    assert versions.json() == {
        'modules': [{'versions': [{'version': v} for v in ('3.0.0', '2.0.0', '1.1.0', '1.0.0')]}]
    }
    again = await client.get(f'{base}/versions', headers={'If-None-Match': versions.headers['etag']})
    assert again.status_code == 304
    assert upstream.hits('/.well-known/terraform.json') == 1

    # 204 + X-Terraform-Get: the archive is cached before the answer, which points at it (anonymous: no grant)
    download = await client.get(f'{base}/1.0.0/download')
    assert download.status_code == 204, download.text
    location = download.headers['x-terraform-get']
    assert location == './archive.tar.gz//modules/x'
    archive_path, subdir = resolve(f'{base}/1.0.0/download', location)
    assert archive_path == f'{base}/1.0.0/archive.tar.gz' and subdir == 'modules/x'
    assert upstream.hits('/files/net-1.0.0.tar.gz') == 1
    got = await client.get(archive_path)
    assert got.status_code == 200 and served(got) == sha(NET_1) and got.headers['x-ws-cache'] == 'HIT'
    assert got.headers['content-type'] == 'application/gzip'
    assert (await client.get(f'{base}/1.0.0/download')).status_code == 204
    assert upstream.hits('/v1/modules/acme/net/aws/1.0.0/download') == 1  # kept: a release does not change

    # 200 + JSON location, relative to the download URL; a zip
    zip_answer = await client.get(f'{base}/1.1.0/download')
    assert zip_answer.headers['x-terraform-get'] == './archive.zip'
    assert served(await client.get(f'{base}/1.1.0/archive.zip')) == sha(NET_11)
    assert (await client.get(f'{base}/1.1.0/archive.tar.gz')).status_code == 404

    # sources Waystation does not cache are passed through
    passthrough = await client.get(f'{base}/2.0.0/download')
    assert passthrough.status_code == 204
    assert passthrough.headers['x-terraform-get'] == 'git::https://git.example.com/acme/net?ref=v2.0.0'
    assert (await client.get(f'{base}/2.0.0/archive.tar.gz')).status_code == 404

    broken = await client.get(f'{base}/3.0.0/download')
    assert broken.status_code == 502 and 'archive' in broken.text
    assert await asset(services, 'tf', f'v1/modules/{MODULE}/3.0.0/archive.tar.gz') is None

    assert (await client.get(f'{base}/9.0.0/download')).status_code == 404
    assert (await client.put(f'{base}/1.0.0', content=b'x')).status_code in (401, 405)
    assert (await client.get('/repository/tf/v1/providers/acme/thing/versions')).status_code == 404

    # the digest is pinned at the first fetch: after cleanup, different bytes are refused
    await drain(services)
    record = await services.db[TERRAFORM_MODULES].find_one({'name': MODULE})
    assert record is not None and record['latest'] == '3.0.0' and record['version_count'] == 4
    pinned = next(v for v in record['versions'] if v['version'] == '1.0.0')
    assert pinned['sha256'] == hashlib.sha256(NET_1).hexdigest()
    assert pinned['source'].endswith('files/net-1.0.0.tar.gz//modules/x')
    repo = await services.catalog.get('tf')
    assert repo is not None
    await services.db[ASSETS].delete_one({'repo_id': repo.id, 'path': f'v1/modules/{MODULE}/1.0.0/archive.tar.gz'})
    upstream.routes['/files/net-1.0.0.tar.gz'] = Respond(200, {}, tarball({'main.tf': b'# changed\n'}, top='x'))
    changed = await client.get(f'{base}/1.0.0/download')
    assert changed.status_code == 502


async def test_hosted_modules(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, token: str, repos: None
) -> None:
    base = f'/repository/tf-hosted/v1/modules/{MODULE}'
    assert (await client.put(f'{base}/1.0.0', content=HOSTED_MODULE)).status_code == 401  # anonymous
    put = await client.put(f'{base}/1.0.0', content=HOSTED_MODULE, headers=bearer(token))
    assert put.status_code == 201, put.text
    assert put.json()['sha256'] == hashlib.sha256(HOSTED_MODULE).hexdigest()
    again = await client.put(f'{base}/1.0.0', content=HOSTED_MODULE, headers=bearer(token))
    assert again.status_code == 409  # immutable
    assert (await client.put(f'{base}/1.2.0', content=HOSTED_MODULE, headers=bearer(token))).status_code == 201
    nested = tarball({'main.tf': b''}, top='net')
    refused = await client.put(f'{base}/1.3.0', content=nested, headers=bearer(token))
    assert refused.status_code == 400 and '.tf file at its root' in refused.text
    assert (await client.put(f'{base}/1.3.0', content=b'junk', headers=bearer(token))).status_code == 400

    versions = await client.get(f'{base}/versions', headers=bearer(token))
    assert versions.json() == {'modules': [{'versions': [{'version': '1.2.0'}, {'version': '1.0.0'}]}]}
    assert (await client.get(f'{base}/versions')).status_code == 401  # hidden from anonymous

    # the download answer carries a signed grant: the archive is fetched without credentials (adr/0009)
    download = await client.get(f'{base}/1.0.0/download', headers=bearer(token))
    assert download.status_code == 204
    location = download.headers['x-terraform-get']
    assert location.startswith('./archive.tar.gz?ws_grant=')
    archive_path, subdir = resolve(f'{base}/1.0.0/download', location)
    assert subdir == ''
    fetched = await client.get(archive_path)
    assert fetched.status_code == 200 and served(fetched) == sha(HOSTED_MODULE)
    assert (await client.head(archive_path)).status_code == 200
    plain = archive_path.split('?', 1)[0]
    assert (await client.get(plain)).status_code == 401
    other = archive_path.replace('/1.0.0/', '/1.2.0/')
    assert (await client.get(other)).status_code == 401  # a grant names one file
    forged = archive_path[:-3] + ('abc' if not archive_path.endswith('abc') else 'xyz')
    assert (await client.get(forged)).status_code == 401
    assert (await client.delete(archive_path)).status_code == 401  # a grant only reads
    with_token = await client.get(plain, headers=bearer(token))
    assert with_token.status_code == 200
    # names are case-insensitive: the grant follows the path as the client wrote it
    upper = '/repository/tf-hosted/v1/modules/ACME/Net/aws/1.0.0/download'
    mixed = await client.get(upper, headers=bearer(token))
    mixed_path, _ = resolve(upper, mixed.headers['x-terraform-get'])
    assert '/ACME/Net/' in mixed_path and served(await client.get(mixed_path)) == sha(HOSTED_MODULE)

    await drain(services)
    page = await admin.get('/api/v1/repositories/tf-hosted/terraform/modules')
    assert page.status_code == 200, page.text
    assert page.json()['items'] == [
        {
            'module': MODULE,
            'latest': '1.2.0',
            'version_count': 2,
            'cached_versions': 2,
            'size_bytes': 2 * len(HOSTED_MODULE),
        }
    ]
    detail = await admin.get('/api/v1/repositories/tf-hosted/terraform/module-versions', params={'module': MODULE})
    items = detail.json()['items']
    assert [i['version'] for i in items] == ['1.2.0', '1.0.0'] and items[1]['archive']['uploaded_by'] == 'admin'
    assert items[1]['archive']['path'] == f'v1/modules/{MODULE}/1.0.0/archive.tar.gz'
    search = await admin.get('/api/v1/search', params={'q': 'acme/net', 'format': 'terraform'})
    hits = [h for h in search.json()['items'] if h['kind'] == 'package']
    assert any(h['name'] == MODULE and h['browse']['query'] == {'module': MODULE} for h in hits)

    deleted = await client.delete(f'{base}/1.0.0', headers=bearer(token))
    assert deleted.status_code == 204
    assert (await client.delete(f'{base}/1.0.0', headers=bearer(token))).status_code == 404
    assert (await client.get(f'{base}/1.0.0/download', headers=bearer(token))).status_code == 404
    assert await asset(services, 'tf-hosted', f'v1/modules/{MODULE}/1.0.0/archive.tar.gz') is None
    record = await services.db[TERRAFORM_MODULES].find_one({'name': MODULE})
    assert record is not None and record['latest'] == '1.2.0' and record['version_count'] == 1


def release_form(
    key: PgpKey,
    armored: str,
    *,
    version: str = '1.0.0',
    archive: bytes = PROVIDER_ZIP,
    listed: bytes | None = None,
    sign: bytes | None = None,
) -> dict[str, Any]:
    filename = f'terraform-provider-thing_{version}_linux_amd64.zip'
    sums = listed if listed is not None else f'{hashlib.sha256(archive).hexdigest()}  {filename}\n'.encode()
    return {
        'files': [
            ('archive', (filename, archive, 'application/zip')),
            ('shasums', (f'terraform-provider-thing_{version}_SHA256SUMS', sums, 'text/plain')),
            ('signature', (f'terraform-provider-thing_{version}_SHA256SUMS.sig', detached(key, sign or sums), 'x')),
            ('signing_key', ('key.asc', armored.encode(), 'text/plain')),
        ],
        'data': {'protocols': '5.0,6.0'},
    }


async def test_hosted_providers(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, token: str, repos: None
) -> None:
    primary, signing = PgpKey('ed25519'), PgpKey('rsa')
    armored = armor(cert(primary, subkeys=(Sub(signing),))).decode()
    base = '/repository/tf-hosted/v1/providers/acme/thing'
    upload = await client.post(f'{base}/1.0.0', headers=bearer(token), **release_form(signing, armored))
    assert upload.status_code == 201, upload.text
    assert upload.json()['platforms'] == ['linux_amd64']
    again = await client.post(f'{base}/1.0.0', headers=bearer(token), **release_form(signing, armored))
    assert again.status_code == 409
    bad_sig = await client.post(
        f'{base}/1.1.0', headers=bearer(token), **release_form(signing, armored, version='1.1.0', sign=b'x')
    )
    assert bad_sig.status_code == 400 and 'signature' in bad_sig.text
    unlisted = release_form(signing, armored, version='1.1.0', listed=b'%s  other.zip\n' % (b'0' * 64))
    assert (await client.post(f'{base}/1.1.0', headers=bearer(token), **unlisted)).status_code == 400
    not_zip = release_form(signing, armored, version='1.1.0', archive=b'not a zip')
    assert (await client.post(f'{base}/1.1.0', headers=bearer(token), **not_zip)).status_code == 400
    assert (await client.post(f'{base}/1.1.0', headers=bearer(token), content=b'x')).status_code == 400

    versions = await client.get(f'{base}/versions', headers=bearer(token))
    assert versions.json() == {
        'versions': [{'version': '1.0.0', 'protocols': ['5.0', '6.0'], 'platforms': [{'os': 'linux', 'arch': 'amd64'}]}]
    }
    doc_path = f'{base}/1.0.0/download/linux/amd64'
    doc = (await client.get(doc_path, headers=bearer(token))).json()
    assert doc['filename'] == 'terraform-provider-thing_1.0.0_linux_amd64.zip'
    assert doc['shasum'] == hashlib.sha256(PROVIDER_ZIP).hexdigest()
    assert doc['signing_keys']['gpg_public_keys'][0]['ascii_armor'] == armored
    assert doc['signing_keys']['gpg_public_keys'][0]['key_id'] == primary_key_id(armored)
    for field, want in (('download_url', PROVIDER_ZIP), ('shasums_signature_url', None), ('shasums_url', None)):
        url = urljoin(f'http://testserver{doc_path}', doc[field]).removeprefix('http://testserver')
        assert '?ws_grant=' in url
        got = await client.get(url)  # no credentials: the grant
        assert got.status_code == 200, (field, got.text)
        if want is not None:
            assert served(got) == sha(want)
    sig = await client.get(
        urljoin(f'http://testserver{doc_path}', doc['shasums_signature_url']).removeprefix('http://testserver')
    )
    stored_sig = services.blobs.blob_path(served(sig)).read_bytes()
    assert stored_sig[:1] != b'-'  # stored binary, as clients expect
    assert (await client.get(f'{base}/1.0.0/download/darwin/arm64', headers=bearer(token))).status_code == 404

    browse = await admin.get('/api/v1/repositories/tf-hosted/terraform/providers')
    assert browse.json()['items'][0]['provider'] == 'acme/thing'
    detail = await admin.get('/api/v1/repositories/tf-hosted/terraform/versions', params={'provider': 'acme/thing'})
    release = detail.json()['items'][0]
    assert release['key_id'] == primary_key_id(armored) and release['archives'][0]['h1'].startswith('h1:')

    assert (await client.delete(f'{base}/1.0.0', headers=bearer(token))).status_code == 204
    assert (await client.get(f'{base}/versions', headers=bearer(token))).status_code == 404
    assert (
        await asset(services, 'tf-hosted', 'v1/providers/acme/thing/1.0.0/terraform-provider-thing_1.0.0_SHA256SUMS')
        is None
    )


def primary_key_id(armored: str) -> str:
    from waystation_common import openpgp

    return openpgp.read_certs(armored.encode())[0].fingerprint[-16:]


async def test_discovery_and_single_hosted(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, token: str, repos: None
) -> None:
    second = await create(admin, {'name': 'tf-hosted-2', 'type': 'hosted'})
    assert second.status_code in (409, 422) and 'one hosted Terraform repository' in second.text

    discovery = await client.get('/.well-known/terraform.json', headers=bearer(token))
    assert discovery.status_code == 200
    assert discovery.json() == {
        'modules.v1': '/repository/tf-hosted/v1/modules/',
        'providers.v1': '/repository/tf-hosted/v1/providers/',
    }
    assert (await client.get('/.well-known/terraform.json')).json() == {}  # hidden from anonymous
    assert (await client.get('/.well-known/terraform.json', headers=bearer('wst_nope'))).json() == {}
    assert (await client.head('/.well-known/terraform.json')).status_code == 200
