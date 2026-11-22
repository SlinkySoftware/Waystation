"""Conda (T-P6-CONDA) end to end on the real engine, MongoDB and Valkey: a channel proxy against a scriptable upstream
(repodata streamed, revalidated and indexed in the background, packages verified against the listing, unverified
fetches checked later, JLAP refused), a hosted channel (uploads, write policy, the worker's repodata), browse and
global search."""

import json
from collections.abc import AsyncIterator, Callable
from compression import zstd
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_conda import conda_package, entry, index_json, repodata, sha256, tar_bz2_package
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import asgi_client

from waystation_common.db.collections import ASSETS, CONDA_PACKAGES
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.problems import ResponseAborted
from waystation_server.services import Services
from waystation_worker import conda as worker_conda

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
TZ = index_json('tzdata', '2024b', 'h0_0')
TZ_PKG = conda_package(TZ)
OLD = index_json('tzdata', '2024a', 'h0_0')
OLD_PKG = tar_bz2_package(OLD)
LATE = index_json('late', '1.0', 'py_0')
LATE_PKG = conda_package(LATE)
BAD = index_json('bad', '1.0', 'py_0')
NOARCH = repodata(
    'noarch',
    {
        'tzdata-2024b-h0_0.conda': entry(TZ, TZ_PKG),
        'tzdata-2024a-h0_0.tar.bz2': entry(OLD, OLD_PKG),
        'bad-1.0-py_0.conda': entry(BAD, conda_package(BAD)),
    },
)


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        fake.routes.update(
            {
                '/channeldata.json': Respond(200, {'Content-Type': 'application/json'}, b'{"packages": {}}'),
                '/noarch/repodata.json': Respond(200, {'ETag': '"r1"'}, NOARCH, chunk=1000),
                '/noarch/repodata.json.zst': Respond(200, {}, zstd.compress(NOARCH)),
                '/noarch/tzdata-2024b-h0_0.conda': Respond(200, {}, TZ_PKG),
                '/noarch/tzdata-2024a-h0_0.tar.bz2': Respond(200, {}, OLD_PKG),
                '/noarch/bad-1.0-py_0.conda': Respond(200, {}, b'tampered'),
                '/noarch/late-1.0-py_0.conda': Respond(200, {}, LATE_PKG),
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
    return await admin.post('/api/v1/repositories', json={'format': 'conda', 'online': True, **body})


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True}
    assert (await create(admin, {'name': 'conda-proxy', 'type': 'proxy', 'proxy': proxy})).status_code == 201
    hosted = {'write_policy': 'disable_redeploy'}
    assert (await create(admin, {'name': 'conda-hosted', 'type': 'hosted', 'hosted': hosted})).status_code == 201


async def stored(services: Services, repo: str, path: str) -> dict[str, Any] | None:
    repo_doc = await services.catalog.get(repo)
    assert repo_doc is not None
    doc: dict[str, Any] | None = await services.db[ASSETS].find_one({'repo_id': repo_doc.id, 'path': path})
    return doc


async def published(services: Services, repo: str, path: str) -> bytes:
    """The bytes of a stored file (served by X-Accel, so the test client gets no body)."""
    doc = await stored(services, repo, path)
    assert doc is not None, path
    return services.blobs.blob_path(doc['blob_sha256']).read_bytes()


async def drain(services: Services) -> None:
    plugin = services.formats.plugin('conda')
    await plugin.drain()  # type: ignore[union-attr]


async def test_groups_are_refused(admin: httpx.AsyncClient) -> None:
    made = await create(admin, {'name': 'conda-all', 'type': 'group', 'group': {'members': []}})
    assert made.status_code in (400, 422), made.text


@pytest.mark.usefixtures('repos')
async def test_proxy_repodata_packages_and_browse(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    # a package requested before any repodata: served, recorded unverified
    late = await client.get('/repository/conda-proxy/noarch/late-1.0-py_0.conda')
    assert late.status_code == 200 and late.content == LATE_PKG
    repo = await services.catalog.get('conda-proxy')
    assert repo is not None
    record = await services.db[CONDA_PACKAGES].find_one({'repo_id': repo.id, 'filename': 'late-1.0-py_0.conda'})
    assert record is not None and record['verified'] is False and record['sha256'] == sha256(LATE_PKG)

    listing = await client.get('/repository/conda-proxy/noarch/repodata.json')
    assert listing.status_code == 200 and listing.content == NOARCH
    asset = await stored(services, 'conda-proxy', 'noarch/repodata.json')
    assert asset is not None and asset['kind'] == 'content'
    assert (await client.get('/repository/conda-proxy/noarch/repodata.json')).headers['x-ws-cache'] == 'HIT'
    assert upstream.hits('/noarch/repodata.json') == 1
    zst = await client.get('/repository/conda-proxy/noarch/repodata.json.zst')
    assert zst.status_code == 200 and zstd.decompress(zst.content) == NOARCH
    assert (await client.get('/repository/conda-proxy/noarch/repodata.jlap')).status_code == 404
    assert upstream.hits('/noarch/repodata.jlap') == 0
    channel = await client.get('/repository/conda-proxy/channeldata.json')
    assert (
        channel.status_code == 200 and (await stored(services, 'conda-proxy', 'channeldata.json'))['kind'] == 'metadata'
    )  # type: ignore[index]
    await drain(services)

    tz = await client.get('/repository/conda-proxy/noarch/tzdata-2024b-h0_0.conda')
    assert tz.status_code == 200 and tz.content == TZ_PKG
    old = await client.get('/repository/conda-proxy/noarch/tzdata-2024a-h0_0.tar.bz2')
    assert old.status_code == 200 and old.content == OLD_PKG
    try:  # the bytes do not match the listed sha256 / size (refused up front by length, or aborted)
        bad = await client.get('/repository/conda-proxy/noarch/bad-1.0-py_0.conda')
        assert bad.status_code == 502
    except ResponseAborted:
        pass
    assert await stored(services, 'conda-proxy', 'noarch/bad-1.0-py_0.conda') is None
    assert (await client.put('/repository/conda-proxy/noarch/x-1-0.conda', content=b'x')).status_code in (401, 405)

    packages = (await admin.get('/api/v1/repositories/conda-proxy/conda/packages')).json()
    names = {p['name']: p for p in packages['items']}
    assert names['tzdata']['latest'] == '2024b' and names['tzdata']['subdirs'] == ['noarch']
    assert names['tzdata']['file_count'] == 2
    files = (await admin.get('/api/v1/repositories/conda-proxy/conda/files', params={'package': 'tzdata'})).json()
    assert [f['version'] for f in files['items']] == ['2024b', '2024a']
    assert files['items'][0]['cached'] is True and files['items'][0]['verified'] is True
    assert files['items'][0]['sha256'] == sha256(TZ_PKG)
    prefix = (await admin.get('/api/v1/repositories/conda-proxy/conda/packages', params={'q': 'tz'})).json()
    assert [p['name'] for p in prefix['items']] == ['tzdata']
    missing = await admin.get('/api/v1/repositories/conda-proxy/conda/files', params={'package': 'nope'})
    assert missing.status_code == 404
    hits = (await admin.get('/api/v1/search', params={'q': 'tzdata'})).json()
    assert any(h['repository'] == 'conda-proxy' and h['name'] == 'tzdata' for h in hits['items'])


@pytest.mark.usefixtures('repos')
async def test_unverified_package_confirmed_or_dropped_by_listing(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    upstream.routes['/noarch/bad-1.0-py_0.conda'] = Respond(200, {}, b'served before any listing')
    first = await client.get('/repository/conda-proxy/noarch/bad-1.0-py_0.conda')
    assert first.status_code == 200
    assert await stored(services, 'conda-proxy', 'noarch/bad-1.0-py_0.conda') is not None
    assert (await client.get('/repository/conda-proxy/noarch/repodata.json')).status_code == 200
    await drain(services)
    assert await stored(services, 'conda-proxy', 'noarch/bad-1.0-py_0.conda') is None  # contradicted: dropped
    repo = await services.catalog.get('conda-proxy')
    assert repo is not None
    record = await services.db[CONDA_PACKAGES].find_one({'repo_id': repo.id, 'filename': 'bad-1.0-py_0.conda'})
    assert (
        record is not None and record['verified'] is True and record['sha256'] != sha256(b'served before any listing')
    )


@pytest.mark.usefixtures('repos')
async def test_hosted_upload_index_and_delete(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services
) -> None:
    made = await admin.post('/api/v1/auth/me/tokens', json={'name': 'conda', 'scopes': ['read', 'write', 'delete']})
    auth = ('admin', made.json()['secret'])
    empty = await client.get('/repository/conda-hosted/noarch/repodata.json', auth=auth)
    assert empty.status_code == 200 and empty.json()['packages.conda'] == {}
    url = '/repository/conda-hosted/noarch/tzdata-2024b-h0_0.conda'
    assert (await client.put(url, content=TZ_PKG)).status_code == 401
    put = await client.put(url, content=TZ_PKG, auth=auth)
    assert put.status_code == 201, put.text
    assert (await client.put(url, content=TZ_PKG, auth=auth)).status_code == 200  # the same bytes
    other = conda_package({**TZ, 'license': 'other'})
    assert (await client.put(url, content=other, auth=auth)).status_code == 409  # disable_redeploy
    wrong = await client.put('/repository/conda-hosted/linux-64/tzdata-2024b-h0_0.conda', content=TZ_PKG, auth=auth)
    assert wrong.status_code == 400 and 'noarch' in wrong.text
    renamed = await client.put('/repository/conda-hosted/noarch/tzdata-1-h0_0.conda', content=TZ_PKG, auth=auth)
    assert renamed.status_code == 400
    garbage = await client.put('/repository/conda-hosted/noarch/x-1-0.tar.bz2', content=b'junk', auth=auth)
    assert garbage.status_code == 400
    old = await client.put('/repository/conda-hosted/noarch/tzdata-2024a-h0_0.tar.bz2', content=OLD_PKG, auth=auth)
    assert old.status_code == 201

    repo = await services.catalog.get('conda-hosted')
    assert repo is not None
    result = await worker_conda.rebuild(services.db, services.blobs, repo.id, now=datetime.now(UTC))
    assert result is not None and result.packages == 2 and result.subdirs == ['noarch']
    listing = json.loads(await published(services, 'conda-hosted', 'noarch/repodata.json'))
    tz = listing['packages.conda']['tzdata-2024b-h0_0.conda']
    assert tz['sha256'] == sha256(TZ_PKG) and tz['size'] == len(TZ_PKG) and tz['depends'] == ['python >=3.9']
    assert list(listing['packages']) == ['tzdata-2024a-h0_0.tar.bz2']
    zst = await published(services, 'conda-hosted', 'noarch/repodata.json.zst')
    assert json.loads(zstd.decompress(zst)) == listing
    channel = json.loads(await published(services, 'conda-hosted', 'channeldata.json'))
    assert channel['packages']['tzdata']['version'] == '2024b'
    download = await client.get(url, auth=auth)
    assert download.status_code == 200 and 'x-accel-redirect' in download.headers
    files = (await admin.get('/api/v1/repositories/conda-hosted/conda/files', params={'package': 'tzdata'})).json()
    assert files['items'][0]['uploaded_by'] == 'admin' and files['items'][0]['depends'] == ['python >=3.9']

    assert (await client.delete(url, auth=auth)).status_code == 204
    assert (await client.delete(url, auth=auth)).status_code == 404
    await worker_conda.rebuild(services.db, services.blobs, repo.id, now=datetime.now(UTC))
    listing = json.loads(await published(services, 'conda-hosted', 'noarch/repodata.json'))
    assert listing['packages.conda'] == {} and list(listing['packages']) == ['tzdata-2024a-h0_0.tar.bz2']
