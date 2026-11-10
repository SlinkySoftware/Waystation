"""Hosted yum (T-P5-YUMH) against real MongoDB/Valkey: RPM uploads (PUT, POST), write policies and refusals, deletes,
the outbox job and the worker's signed, atomically published repodata, browse."""

import gzip
import hashlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_rpm import build_rpm
from ws_testing import asgi_client

from waystation_common import openpgp
from waystation_common.crypto import Keyring
from waystation_common.db.collections import ASSETS, AUDIT, JOB_OUTBOX, YUM_PACKAGES
from waystation_common.settings import Settings
from waystation_common.signing import KeyStore
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.yum import repodata
from waystation_server.services import Services
from waystation_worker import yum as worker_yum

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple 42'
HELLO = build_rpm('hello', '1.0', '1.el10', 'noarch')
HELLO2 = build_rpm('hello', '1.10', '1.el10', 'noarch')
WORLD = build_rpm('world', '2.0', '1.el10', 'x86_64', epoch=1, files=('/usr/bin/world',), dirs=())


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
    created = await admin.post('/api/v1/auth/me/tokens', json={'name': 'yum', 'scopes': ['read', 'write', 'delete']})
    assert created.status_code == 201, created.text
    return 'admin', str(created.json()['secret'])


async def create(admin: httpx.AsyncClient, name: str, policy: str = 'allow_redeploy') -> None:
    body: dict[str, Any] = {
        'name': name,
        'format': 'yum',
        'type': 'hosted',
        'online': True,
        'hosted': {'write_policy': policy},
    }
    made = await admin.post('/api/v1/repositories', json=body)
    assert made.status_code == 201, made.text


@pytest.fixture
async def repos(admin: httpx.AsyncClient) -> None:
    await create(admin, 'mine')
    await create(admin, 'strict', 'disable_redeploy')
    await create(admin, 'locked', 'read_only')


def served(services: Services, response: httpx.Response) -> bytes:
    assert response.status_code == 200, response.text
    digest = response.headers['etag'].strip('"').removeprefix('sha256:')
    return services.blobs.blob_path(digest).read_bytes()


async def build(services: Services, name: str) -> worker_yum.IndexResult:
    repo = await services.catalog.get(name)
    assert repo is not None
    jobs = [d async for d in services.db[JOB_OUTBOX].find({'job.kind': 'index.yum'})]
    assert any(d['job']['payload'] == {'repo_id': repo.id} for d in jobs)
    keys = KeyStore.in_data_dir(services.settings.data_dir, Keyring.from_settings(services.settings.secret_keys))
    result = await worker_yum.rebuild(services.db, services.blobs, keys, repo.id, now=datetime.now(UTC))
    assert result is not None
    return result


async def published(services: Services, client: httpx.AsyncClient, auth: tuple[str, str]) -> list[repodata.Package]:
    """The packages the signed repodata of ``mine`` lists (signature and every checksum verified)."""
    base = '/repository/mine'
    repomd = served(services, await client.get(f'{base}/repodata/repomd.xml', auth=auth))
    asc = served(services, await client.get(f'{base}/repodata/repomd.xml.asc', auth=auth))
    key = served(services, await client.get(f'{base}/RPM-GPG-KEY', auth=auth))
    assert served(services, await client.get(f'{base}/repodata/repomd.xml.key', auth=auth)) == key
    openpgp.verify_detached(repomd, asc, openpgp.read_public_keys(key))
    parsed = repodata.parse_repomd(repomd)
    assert sorted(e.type for e in parsed.entries.values()) == ['filelists', 'other', 'primary']
    for entry in parsed.entries.values():
        data = served(services, await client.get(f'{base}/{entry.href}', auth=auth))
        assert entry.checksum is not None
        assert sha(data) == entry.checksum.digest and len(data) == entry.size
        assert gzip.decompress(data).startswith(b'<?xml')
    primary = parsed.of_type('primary')
    assert primary is not None
    data = served(services, await client.get(f'{base}/{primary.href}', auth=auth))
    return repodata.parse_primary(data, '.gz')


async def test_upload_build_sign_and_delete(
    services: Services, client: httpx.AsyncClient, admin: httpx.AsyncClient, auth: tuple[str, str], repos: None
) -> None:
    assert (await client.get('/repository/mine/repodata/repomd.xml', auth=auth)).status_code == 404  # not built
    assert (await client.get('/repository/mine/repodata/repomd.xml')).status_code in (401, 404)  # anonymous

    put = await client.put('/repository/mine/Packages/h/hello-1.0-1.el10.noarch.rpm', content=HELLO, auth=auth)
    assert put.status_code == 201, put.text
    assert put.json() == {
        'saved': True,
        'path': 'Packages/h/hello-1.0-1.el10.noarch.rpm',
        'name': 'hello',
        'evr': '1.0-1.el10.noarch',
    }
    posted = await client.post('/repository/mine/', content=WORLD, auth=auth)
    assert posted.status_code == 201, posted.text
    assert posted.headers['location'] == 'world-2.0-1.el10.x86_64.rpm'

    result = await build(services, 'mine')
    assert result.packages == 2
    packages = {p.name: p for p in await published(services, client, auth)}
    assert packages['hello'].href == 'Packages/h/hello-1.0-1.el10.noarch.rpm'
    assert packages['hello'].checksum.digest == sha(HELLO) and packages['hello'].size == len(HELLO)
    assert (packages['world'].epoch, packages['world'].arch) == ('1', 'x86_64')
    got = await client.get('/repository/mine/Packages/h/hello-1.0-1.el10.noarch.rpm', auth=auth)
    assert served(services, got) == HELLO

    # Browse: packages and their files.
    page = (await client.get('/api/v1/repositories/mine/yum/packages', auth=auth)).json()
    assert [(p['name'], p['latest_version']) for p in page['items']] == [
        ('hello', '1.0-1.el10'),
        ('world', '1:2.0-1.el10'),
    ]
    files = (await client.get('/api/v1/repositories/mine/yum/versions', params={'package': 'world'}, auth=auth)).json()
    (item,) = files['items']
    assert (item['cached'], item['checksum'], item['path']) == (
        True,
        f'sha256:{sha(WORLD)}',
        'world-2.0-1.el10.x86_64.rpm',
    )
    assert item['uploaded_at'] is not None

    # Refusals: not an RPM, under repodata/, the same package at another path, policies, a missing file.
    assert (await client.put('/repository/mine/bad.rpm', content=b'nope' * 40, auth=auth)).status_code == 400
    assert (await client.put('/repository/mine/repodata/x.rpm', content=HELLO, auth=auth)).status_code == 400
    twin = await client.put('/repository/mine/elsewhere/hello.rpm', content=HELLO, auth=auth)
    assert twin.status_code == 409
    assert (await client.put('/repository/mine/x.txt', content=b'x', auth=auth)).status_code == 405
    assert (await client.put('/repository/locked/a.rpm', content=HELLO, auth=auth)).status_code == 403
    assert (await client.put('/repository/strict/a.rpm', content=HELLO, auth=auth)).status_code == 201
    assert (await client.put('/repository/strict/a.rpm', content=HELLO2, auth=auth)).status_code == 409
    assert (await client.delete('/repository/mine/none.rpm', auth=auth)).status_code == 404
    assert (await client.put('/repository/mine/hello.rpm', content=HELLO2)).status_code in (401, 404)

    # Redeploy at the same path replaces it; a newer version is listed first.
    again = await client.put('/repository/mine/Packages/h/hello-1.0-1.el10.noarch.rpm', content=HELLO, auth=auth)
    assert again.status_code == 201
    assert (
        await client.put('/repository/mine/Packages/h/hello-1.10-1.el10.noarch.rpm', content=HELLO2, auth=auth)
    ).status_code == 201
    second = await build(services, 'mine')
    assert second.fingerprint == result.fingerprint  # one key per repository, created once
    versions = (
        await client.get('/api/v1/repositories/mine/yum/versions', params={'package': 'hello'}, auth=auth)
    ).json()
    assert [v['evr'] for v in versions['items']] == ['1.10-1.el10', '1.0-1.el10']

    deleted = await client.delete('/repository/mine/Packages/h/hello-1.0-1.el10.noarch.rpm', auth=auth)
    assert deleted.status_code == 204
    assert await services.db[ASSETS].find_one({'path': 'Packages/h/hello-1.0-1.el10.noarch.rpm'}) is None
    await build(services, 'mine')
    names = sorted((p.name, p.version) for p in await published(services, client, auth))
    assert names == [('hello', '1.10'), ('world', '2.0')]
    repo = await services.catalog.get('mine')
    assert repo is not None
    primaries = await services.db[ASSETS].count_documents({'repo_id': repo.id, 'path': {'$regex': '-primary.xml.gz$'}})
    assert primaries == 2  # the current generation and the previous one
    actions = [d['action'] async for d in services.db[AUDIT].find({'target.kind': 'yum_package'})]
    assert actions.count('content.upload') >= 4 and 'content.delete' in actions


async def test_records_of_removed_files_are_dropped_and_empty_repositories_build(
    services: Services, client: httpx.AsyncClient, auth: tuple[str, str], repos: None
) -> None:
    assert (await client.put('/repository/mine/hello.rpm', content=HELLO, auth=auth)).status_code == 201
    await services.db[ASSETS].delete_one({'path': 'hello.rpm'})  # as a cleanup policy would
    result = await build(services, 'mine')
    assert result.packages == 0
    assert await services.db[YUM_PACKAGES].count_documents({'path': 'hello.rpm'}) == 0
    assert await published(services, client, auth) == []
    keys = KeyStore.in_data_dir(services.settings.data_dir, Keyring.from_settings(services.settings.secret_keys))
    assert await worker_yum.rebuild(services.db, services.blobs, keys, 'no-such-repo', now=datetime.now(UTC)) is None
