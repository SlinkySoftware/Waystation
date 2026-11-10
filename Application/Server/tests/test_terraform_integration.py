"""Terraform provider network mirror (T-P5-TFP) end to end on the real engine, MongoDB and Valkey: a mirror against a
scriptable registry (service discovery, version list, download documents, SHA256SUMS signed with a key generated
for the test, archives behind a redirect), signature and checksum refusals, browse and search."""

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
from ws_openpgp import PgpKey, Sub, armor, cert, detached
from ws_testing import asgi_client

from waystation_common.db.collections import ASSETS, TERRAFORM_PROVIDERS
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.problems import ResponseAborted
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


FILES = {'terraform-provider-demo_v1.0.0': b'\x7fELF' + b'provider binary ' * 40_000}
ARCHIVE = make_zip(FILES)
TAMPERED = make_zip({**FILES, 'extra': b'x'})
H1 = (
    'h1:'
    + base64.b64encode(
        hashlib.sha256(
            ''.join(f'{hashlib.sha256(FILES[n]).hexdigest()}  {n}\n' for n in sorted(FILES)).encode()
        ).digest()
    ).decode()
)


def name(version: str, platform: str) -> str:
    return f'terraform-provider-demo_{version}_{platform}.zip'


def sums(version: str, archive: bytes) -> bytes:
    return (
        f'{hashlib.sha256(archive).hexdigest()}  {name(version, "linux_amd64")}\n'
        f'{"d" * 64}  {name(version, "darwin_arm64")}\n'
        f'{"e" * 64}  {name(version, "windows_amd64")}\n'
    ).encode()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture(scope='module')
def signer() -> tuple[PgpKey, str]:
    primary, signing = PgpKey('ed25519'), PgpKey('rsa')
    return signing, armor(cert(primary, subkeys=(Sub(signing),))).decode()


@pytest.fixture
async def upstream(signer: tuple[PgpKey, str]) -> AsyncIterator[FakeUpstream]:
    key, armored = signer
    async with FakeUpstream() as fake:
        base = fake.url  # http://127.0.0.1:<port>/
        versions = {
            'versions': [
                {'version': v, 'protocols': ['5.0'], 'platforms': [{'os': o, 'arch': a} for o, a in platforms]}
                for v, platforms in (
                    ('1.0.0', [('linux', 'amd64'), ('darwin', 'arm64')]),
                    ('2.0.0', [('linux', 'amd64')]),
                    ('3.0.0', [('linux', 'amd64')]),
                    ('3.1.0-beta.1', [('linux', 'amd64')]),
                )
            ]
        }

        def download(version: str, archive: bytes) -> bytes:
            return json.dumps(
                {
                    'protocols': ['5.0'],
                    'os': 'linux',
                    'arch': 'amd64',
                    'filename': name(version, 'linux_amd64'),
                    'download_url': f'{base}dl/{version}/{name(version, "linux_amd64")}',
                    'shasums_url': f'{base}files/{version}/SHA256SUMS',
                    'shasums_signature_url': f'{base}files/{version}/SHA256SUMS.sig',
                    'shasum': hashlib.sha256(archive).hexdigest(),
                    'signing_keys': {'gpg_public_keys': [{'key_id': 'X', 'ascii_armor': armored}]},
                }
            ).encode()

        good_sums = sums('1.0.0', ARCHIVE)
        fake.routes.update(
            {
                '/.well-known/terraform.json': Respond(200, {}, b'{"providers.v1": "/v1/providers/"}'),
                '/v1/providers/acme/demo/versions': Respond(200, {}, json.dumps(versions).encode()),
                '/v1/providers/acme/demo/1.0.0/download/linux/amd64': Respond(200, {}, download('1.0.0', ARCHIVE)),
                '/files/1.0.0/SHA256SUMS': Respond(200, {}, good_sums),
                '/files/1.0.0/SHA256SUMS.sig': Respond(200, {}, detached(key, good_sums)),
                # the archive lives behind a redirect (as GitHub release assets do)
                f'/dl/1.0.0/{name("1.0.0", "linux_amd64")}': Respond(302, {'Location': '/assets/a1'}, b''),
                '/assets/a1': Respond(200, {}, ARCHIVE, chunk=65536),
                # 2.0.0: the signature does not match SHA256SUMS
                '/v1/providers/acme/demo/2.0.0/download/linux/amd64': Respond(200, {}, download('2.0.0', ARCHIVE)),
                '/files/2.0.0/SHA256SUMS': Respond(200, {}, sums('2.0.0', ARCHIVE)),
                '/files/2.0.0/SHA256SUMS.sig': Respond(200, {}, detached(key, good_sums)),
                # 3.0.0: signed, but the archive served is not the one listed
                '/v1/providers/acme/demo/3.0.0/download/linux/amd64': Respond(200, {}, download('3.0.0', ARCHIVE)),
                '/files/3.0.0/SHA256SUMS': Respond(200, {}, sums('3.0.0', ARCHIVE)),
                '/files/3.0.0/SHA256SUMS.sig': Respond(200, {}, detached(key, sums('3.0.0', ARCHIVE))),
                f'/dl/3.0.0/{name("3.0.0", "linux_amd64")}': Respond(200, {}, TAMPERED),
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
    return await admin.post('/api/v1/repositories', json={'format': 'terraform', 'online': True, **body})


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> str:
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True}
    made = await create(admin, {'name': 'tf', 'type': 'proxy', 'proxy': proxy})
    assert made.status_code == 201, made.text
    loose = {'verify_signatures': False}
    made = await create(admin, {'name': 'tf-loose', 'type': 'proxy', 'proxy': proxy, 'format_options': loose})
    assert made.status_code == 201, made.text
    hosted = await create(admin, {'name': 'tf-hosted', 'type': 'hosted'})
    assert hosted.status_code == 422  # hosted Terraform repositories are T-P5-TFM
    return f'127.0.0.1:{upstream.port}/acme/demo'


async def stored(services: Services, repo: str, path: str) -> dict[str, Any] | None:
    repo_doc = await services.catalog.get(repo)
    assert repo_doc is not None
    doc: dict[str, Any] | None = await services.db[ASSETS].find_one({'repo_id': repo_doc.id, 'path': path})
    return doc


async def test_mirror_documents_and_archive(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repos: str
) -> None:
    provider = repos
    index = await client.get(f'/repository/tf/{provider}/index.json')
    assert index.status_code == 200, index.text
    assert index.json() == {'versions': {'3.1.0-beta.1': {}, '3.0.0': {}, '2.0.0': {}, '1.0.0': {}}}
    assert index.headers['content-type'] == 'application/json' and index.headers['etag']
    again = await client.get(f'/repository/tf/{provider}/index.json', headers={'If-None-Match': index.headers['etag']})
    assert again.status_code == 304
    head = await client.head(f'/repository/tf/{provider}/index.json')
    assert head.status_code == 200 and int(head.headers['content-length']) == len(index.content)
    assert upstream.hits('/v1/providers/acme/demo/versions') == 1
    assert upstream.hits('/.well-known/terraform.json') == 1

    doc = await client.get(f'/repository/tf/{provider}/1.0.0.json')
    assert doc.status_code == 200, doc.text
    archives = doc.json()['archives']
    assert set(archives) == {'linux_amd64', 'darwin_arm64'}  # windows_amd64 is not in the version list
    assert archives['linux_amd64'] == {
        'url': f'1.0.0/linux/amd64/{name("1.0.0", "linux_amd64")}',
        'hashes': [f'zh:{hashlib.sha256(ARCHIVE).hexdigest()}'],
    }
    assert await stored(services, 'tf', f'{provider}/1.0.0/SHA256SUMS') is not None
    assert upstream.hits('/files/1.0.0/SHA256SUMS.sig') == 1

    url = f'/repository/tf/{provider}/1.0.0/linux/amd64/{name("1.0.0", "linux_amd64")}'
    got = await client.get(url)
    assert got.status_code == 200 and got.content == ARCHIVE and got.headers['x-ws-cache'] == 'FETCHING'
    assert got.headers['content-type'] == 'application/zip'
    assert (await client.get(url)).headers['x-ws-cache'] == 'HIT'
    assert upstream.hits('/assets/a1') == 1
    await services.formats.plugin('terraform').drain()  # type: ignore[union-attr]
    with_h1 = (await client.get(f'/repository/tf/{provider}/1.0.0.json')).json()['archives']['linux_amd64']
    assert with_h1['hashes'] == [H1, f'zh:{hashlib.sha256(ARCHIVE).hexdigest()}']

    wrong_name = await client.get(f'/repository/tf/{provider}/1.0.0/linux/amd64/other.zip')
    assert wrong_name.status_code == 404
    assert (await client.get(f'/repository/tf/{provider}/9.9.9.json')).status_code == 404
    assert (await client.get(f'/repository/tf/{provider}/versions')).status_code == 404  # internal asset
    other_host = await client.get('/repository/tf/registry.example.com/acme/demo/index.json')
    assert other_host.status_code == 404 and 'not an origin registry' in other_host.text
    post = await client.post(f'/repository/tf/{provider}/index.json', content=b'x')
    assert post.status_code in (401, 405)


async def test_signature_and_checksum_refusals(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repos: str
) -> None:
    provider = repos
    bad = await client.get(f'/repository/tf/{provider}/2.0.0.json')
    assert bad.status_code == 502 and 'signature' in bad.text
    assert await stored(services, 'tf', f'{provider}/2.0.0/SHA256SUMS') is None
    archive = await client.get(f'/repository/tf/{provider}/2.0.0/linux/amd64/{name("2.0.0", "linux_amd64")}')
    assert archive.status_code == 502

    loose = await client.get(f'/repository/tf-loose/{provider}/2.0.0.json')  # verify_signatures off
    assert loose.status_code == 200 and 'linux_amd64' in loose.json()['archives']

    assert (await client.get(f'/repository/tf/{provider}/3.0.0.json')).status_code == 200
    with pytest.raises(ResponseAborted):  # cut before the last bytes: the archive does not match SHA256SUMS
        await client.get(f'/repository/tf/{provider}/3.0.0/linux/amd64/{name("3.0.0", "linux_amd64")}')
    assert await stored(services, 'tf', f'{provider}/3.0.0/linux/amd64/{name("3.0.0", "linux_amd64")}') is None

    missing = await client.get(f'/repository/tf/{provider.replace("demo", "absent")}/index.json')
    assert missing.status_code == 404


async def test_browse_and_search(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, repos: str
) -> None:
    provider = repos
    assert (await client.get(f'/repository/tf/{provider}/1.0.0.json')).status_code == 200
    url = f'/repository/tf/{provider}/1.0.0/linux/amd64/{name("1.0.0", "linux_amd64")}'
    assert (await client.get(url)).status_code == 200
    await services.formats.plugin('terraform').drain()  # type: ignore[union-attr]
    summary = await services.db[TERRAFORM_PROVIDERS].find_one({'name': provider})
    assert summary is not None and summary['latest'] == '3.0.0' and summary['version_count'] == 4
    assert summary['archives'][0]['h1'] == H1

    page = await admin.get('/api/v1/repositories/tf/terraform/providers', params={'q': 'demo'})
    assert page.status_code == 200, page.text
    item = page.json()['items'][0]
    assert item['provider'] == provider and item['cached_archives'] == 1 and item['size_bytes'] == len(ARCHIVE)
    versions = await admin.get('/api/v1/repositories/tf/terraform/versions', params={'provider': provider})
    assert versions.status_code == 200, versions.text
    body = versions.json()
    assert [v['version'] for v in body['items']] == ['3.1.0-beta.1', '3.0.0', '2.0.0', '1.0.0']
    first = body['items'][3]
    assert first['platforms'] == ['darwin_arm64', 'linux_amd64'] and first['listed']
    assert first['archives'][0]['h1'] == H1 and first['archives'][0]['os'] == 'linux'
    assert body['items'][0]['prerelease']
    paged = await admin.get('/api/v1/repositories/tf/terraform/versions', params={'provider': provider, 'limit': 2})
    assert paged.json()['next_cursor']
    unknown = await admin.get('/api/v1/repositories/tf/terraform/versions', params={'provider': 'a.example/b/c'})
    assert unknown.status_code == 404
    invalid = await admin.get('/api/v1/repositories/tf/terraform/versions', params={'provider': 'nope'})
    assert invalid.status_code == 404

    search = await admin.get('/api/v1/search', params={'q': 'demo', 'format': 'terraform'})
    assert search.status_code == 200
    hits = [h for h in search.json()['items'] if h['kind'] == 'package']
    assert hits and hits[0]['name'] == provider and hits[0]['browse']['query'] == {'provider': provider}
