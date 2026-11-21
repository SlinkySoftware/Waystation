"""Hosted apt (T-P5-APTH) against real MongoDB/Valkey: .deb uploads (PUT, POST raw and multipart), components,
write policies and refusals, deletes, the outbox job and the worker's signed, atomically published dists/, by-hash
files, browse and the component index."""

import hashlib
import lzma
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_deb import build_deb
from ws_testing import asgi_client

from waystation_common import openpgp
from waystation_common.crypto import Keyring
from waystation_common.db import components as components_db
from waystation_common.db.collections import APT_HOSTED, APT_PACKAGES, ASSETS, AUDIT, COMPONENTS, JOB_OUTBOX
from waystation_common.settings import Settings
from waystation_common.signing import KeyStore
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.apt import deb822
from waystation_server.services import Services
from waystation_worker import apt_index as worker_apt

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple 42'
HELLO = build_deb('ws-hello', '1.0-1', 'all')
HELLO2 = build_deb('ws-hello', '1.10-1', 'all', compression='xz')
TOOL = build_deb('ws-tool', '1:2.0', 'amd64', compression='zst')
HELLO_PATH = 'pool/main/w/ws-hello/ws-hello_1.0-1_all.deb'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000)


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


@pytest.fixture
async def auth(admin: httpx.AsyncClient) -> tuple[str, str]:
    created = await admin.post('/api/v1/auth/me/tokens', json={'name': 'apt', 'scopes': ['read', 'write', 'delete']})
    assert created.status_code == 201, created.text
    return 'admin', str(created.json()['secret'])


async def create(admin: httpx.AsyncClient, name: str, policy: str = 'allow_redeploy', **options: Any) -> None:
    body: dict[str, Any] = {
        'name': name,
        'format': 'apt',
        'type': 'hosted',
        'online': True,
        'hosted': {'write_policy': policy},
        'format_options': {'distribution': 'stable', 'components': ['main', 'contrib'], **options},
    }
    made = await admin.post('/api/v1/repositories', json=body)
    assert made.status_code == 201, made.text


@pytest.fixture
async def repos(admin: httpx.AsyncClient) -> None:
    await create(admin, 'debs', architectures=['amd64', 'arm64'])
    await create(admin, 'strict', 'disable_redeploy')
    await create(admin, 'locked', 'read_only')


def served(services: Services, response: httpx.Response) -> bytes:
    assert response.status_code == 200, response.text
    digest = response.headers['etag'].strip('"').removeprefix('sha256:')
    return services.blobs.blob_path(digest).read_bytes()


async def build(services: Services, name: str) -> worker_apt.IndexResult:
    repo = await services.catalog.get(name)
    assert repo is not None
    jobs = [d async for d in services.db[JOB_OUTBOX].find({'job.kind': 'index.apt'})]
    assert any(d['job']['payload'] == {'repo_id': repo.id} for d in jobs)
    keys = KeyStore.in_data_dir(services.settings.data_dir, Keyring.from_settings(services.settings.secret_keys))
    result = await worker_apt.rebuild(services.db, services.blobs, keys, repo.id, now=datetime.now(UTC))
    assert result is not None
    return result


def _signed_text(inrelease: bytes) -> bytes:
    """The text an InRelease signs, as gpgv sees it (canonical text, without the last line end)."""
    body = deb822.strip_clearsign(inrelease).decode()
    return '\n'.join(line.rstrip(' \t') for line in body.removesuffix('\n').split('\n')).encode()


async def published(
    services: Services, client: httpx.AsyncClient, auth: tuple[str, str], arch: str = 'amd64', component: str = 'main'
) -> list[deb822.PoolEntry]:
    """The packages ``debs`` lists for ``arch``: InRelease and Release.gpg verified with the served key, every index
    checked against the Release at its plain and by-hash path."""
    base = '/repository/debs'
    inrelease = served(services, await client.get(f'{base}/dists/stable/InRelease', auth=auth))
    release = served(services, await client.get(f'{base}/dists/stable/Release', auth=auth))
    signature = served(services, await client.get(f'{base}/dists/stable/Release.gpg', auth=auth))
    key = served(services, await client.get(f'{base}/public.asc', auth=auth))
    binary = served(services, await client.get(f'{base}/public.gpg', auth=auth))
    certs = openpgp.read_certs(key)
    assert openpgp.read_certs(binary)[0].fingerprint == certs[0].fingerprint
    openpgp.verify_detached(release, signature, certs)
    openpgp.verify_detached(_signed_text(inrelease), inrelease[inrelease.index(b'-----BEGIN PGP SIGNATURE') :], certs)
    assert deb822.strip_clearsign(inrelease) == release
    parsed = deb822.parse_release(inrelease)
    assert parsed.by_hash and parsed.info['suite'] == 'stable' and parsed.info['codename'] == 'stable'
    for rel, entry in parsed.files.items():
        plain = served(services, await client.get(f'{base}/dists/stable/{rel}', auth=auth))
        directory = rel.rsplit('/', 1)[0]
        hashed = served(
            services, await client.get(f'{base}/dists/stable/{directory}/by-hash/SHA256/{entry.sha256}', auth=auth)
        )
        assert sha(plain) == entry.sha256 == sha(hashed) and len(plain) == entry.size
    rel = f'{component}/binary-{arch}/Packages.xz'
    assert rel in parsed.files
    data = served(services, await client.get(f'{base}/dists/stable/{rel}', auth=auth))
    plain = served(services, await client.get(f'{base}/dists/stable/{component}/binary-{arch}/Packages', auth=auth))
    assert lzma.decompress(data) == plain
    return deb822.parse_packages(plain)


async def test_upload_build_sign_and_delete(
    services: Services, client: httpx.AsyncClient, admin: httpx.AsyncClient, auth: tuple[str, str], repos: None
) -> None:
    assert (await client.get('/repository/debs/dists/stable/InRelease', auth=auth)).status_code == 404  # not built
    assert (await client.get('/repository/debs/dists/stable/InRelease')).status_code in (401, 404)  # anonymous

    put = await client.put('/repository/debs/ws-hello_1.0-1_all.deb', content=HELLO, auth=auth)
    assert put.status_code == 201, put.text
    assert put.json() == {
        'saved': True,
        'path': HELLO_PATH,
        'package': 'ws-hello',
        'version': '1.0-1',
        'architecture': 'all',
        'distribution': 'stable',
        'component': 'main',
    }
    assert put.headers['location'] == HELLO_PATH
    posted = await client.post('/repository/debs/?component=contrib', content=TOOL, auth=auth)
    assert posted.status_code == 201, posted.text
    assert posted.headers['location'] == 'pool/contrib/w/ws-tool/ws-tool_2.0_amd64.deb'

    result = await build(services, 'debs')
    assert (result.packages, result.distribution) == (2, 'stable')
    amd64 = {p.package: p for p in await published(services, client, auth)}
    assert amd64['ws-hello'].filename == HELLO_PATH
    assert (amd64['ws-hello'].sha256, amd64['ws-hello'].size) == (sha(HELLO), len(HELLO))
    assert [p.package for p in await published(services, client, auth, arch='arm64')] == ['ws-hello']  # all
    tools = await published(services, client, auth, component='contrib')
    assert [(p.package, p.version) for p in tools] == [('ws-tool', '1:2.0')]
    assert served(services, await client.get(f'/repository/debs/{HELLO_PATH}', auth=auth)) == HELLO

    # Browse (the apt browser reads the published Release and indexes) and the component index (dpkg order).
    suites = (await client.get('/api/v1/repositories/debs/apt/suites', auth=auth)).json()
    assert [(s['suite'], s['components'], s['architectures']) for s in suites] == [
        ('stable', ['main', 'contrib'], ['amd64', 'arm64'])
    ]
    page = (
        await client.get(
            '/api/v1/repositories/debs/apt/packages', params={'suite': 'stable', 'arch': 'amd64'}, auth=auth
        )
    ).json()
    assert [(p['name'], p['component'], p['cached'], p['verified']) for p in page['items']] == [
        ('ws-hello', 'main', True, True),
        ('ws-tool', 'contrib', True, True),
    ]

    # Refusals: not a package, unknown component, the same version in another component, policies, a missing file.
    assert (await client.put('/repository/debs/bad.deb', content=b'nope' * 40, auth=auth)).status_code == 400
    assert (await client.put('/repository/debs/x.deb?component=nope', content=HELLO, auth=auth)).status_code == 400
    twin = await client.put('/repository/debs/x.deb?component=contrib', content=HELLO, auth=auth)
    assert twin.status_code == 409
    assert (await client.put('/repository/debs/x.txt', content=b'x', auth=auth)).status_code == 405
    assert (await client.post('/repository/debs/pool/', content=HELLO, auth=auth)).status_code == 405
    assert (await client.put('/repository/locked/a.deb', content=HELLO, auth=auth)).status_code == 403
    assert (await client.put('/repository/strict/a.deb', content=HELLO, auth=auth)).status_code == 201
    assert (await client.put('/repository/strict/a.deb', content=HELLO, auth=auth)).status_code == 409
    assert (await client.delete('/repository/debs/pool/main/n/none/none_1_all.deb', auth=auth)).status_code == 404
    assert (await client.put('/repository/debs/a.deb', content=HELLO2)).status_code in (401, 404)
    assert await services.db[APT_HOSTED].count_documents({}) == 3

    # Redeploy replaces; a multipart form (curl -F) uploads too; a newer version is listed after the older one.
    assert (await client.put('/repository/debs/', content=HELLO, auth=auth)).status_code == 201
    form = await client.post('/repository/debs/', files={'package': ('ws-hello_1.10-1_all.deb', HELLO2)}, auth=auth)
    assert form.status_code == 201, form.text
    assert form.json()['version'] == '1.10-1'
    second = await build(services, 'debs')
    assert second.fingerprint == result.fingerprint  # one key per repository, created once
    versions = [p.version for p in await published(services, client, auth) if p.package == 'ws-hello']
    assert versions == ['1.0-1', '1.10-1']  # dpkg order (1.10 > 1.0)

    deleted = await client.delete(f'/repository/debs/{HELLO_PATH}', auth=auth)
    assert deleted.status_code == 204
    repo = await services.catalog.get('debs')
    assert repo is not None
    assert await services.db[ASSETS].find_one({'repo_id': repo.id, 'path': HELLO_PATH}) is None
    await build(services, 'debs')
    listed_now = [
        *await published(services, client, auth),
        *await published(services, client, auth, component='contrib'),
    ]
    assert sorted((p.package, p.version) for p in listed_now) == [('ws-hello', '1.10-1'), ('ws-tool', '1:2.0')]
    hashed = await services.db[ASSETS].count_documents(
        {'repo_id': repo.id, 'path': {'$regex': '^dists/stable/main/binary-amd64/by-hash/SHA256/'}}
    )
    assert hashed <= 6  # the current generation's three files and the previous one's
    listed = await services.db[APT_PACKAGES].count_documents({'repo_id': repo.id, 'name': 'ws-hello'})
    assert listed == 2  # 1.10-1 in main/binary-amd64 and main/binary-arm64

    await components_db.index_batch(services.db)
    found = {(c['name'], c['version'], c['format']) async for c in services.db[COMPONENTS].find({'repo_id': repo.id})}
    assert found == {('ws-hello', '1.10-1', 'apt'), ('ws-tool', '2.0', 'apt')}
    actions = [d['action'] async for d in services.db[AUDIT].find({'target.kind': 'apt_package'})]
    assert actions.count('content.upload') >= 4 and 'content.delete' in actions


async def test_records_of_removed_files_are_dropped_and_empty_repositories_build(
    services: Services, client: httpx.AsyncClient, auth: tuple[str, str], repos: None
) -> None:
    assert (await client.put('/repository/debs/', content=HELLO, auth=auth)).status_code == 201
    await services.db[ASSETS].delete_one({'path': HELLO_PATH})  # as a cleanup policy would
    result = await build(services, 'debs')
    assert result.packages == 0
    assert await services.db[APT_HOSTED].count_documents({'path': HELLO_PATH}) == 0
    assert await published(services, client, auth) == []
    keys = KeyStore.in_data_dir(services.settings.data_dir, Keyring.from_settings(services.settings.secret_keys))
    assert await worker_apt.rebuild(services.db, services.blobs, keys, 'no-such-repo', now=datetime.now(UTC)) is None
