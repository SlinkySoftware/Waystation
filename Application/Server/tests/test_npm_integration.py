"""npm proxy (T-P2-NPM) end to end on the real engine: MongoDB/Valkey and a scriptable local upstream."""

import base64
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from ws_fake_upstream import FakeUpstream, Respond, Seen

from waystation_common.db.collections import ASSETS, REPOSITORIES
from waystation_common.settings import Settings
from waystation_server.problems import ResponseAborted
from waystation_server.proxy.http import Timeouts
from waystation_server.services import Services

pytestmark = pytest.mark.integration

BASE = 'https://registry.example.com/repository/npm/'
TGZ = bytes(range(256)) * 1200  # 307200 bytes: more than one 256 KiB chunk
SCOPED_TGZ = b'scoped tarball' * 1000
ABBREVIATED = 'application/vnd.npm.install-v1+json'


def sri(data: bytes) -> str:
    return 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()


def packument(upstream: str, name: str, files: dict[str, dict[str, Any]]) -> bytes:
    """``files``: version -> dist fields (``file`` = tarball file name)."""
    versions = {}
    for version, dist in files.items():
        dist = dict(dist)
        filename = dist.pop('file')
        versions[version] = {
            'name': name,
            'version': version,
            'dist': {'tarball': f'{upstream}{name}/-/{filename}', **dist},
        }
    return json.dumps({'name': name, 'dist-tags': {'latest': max(files)}, 'versions': versions}).encode()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'])


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        yield fake


async def add_npm_repo(services: Services, url: str, **proxy: Any) -> str:
    now = datetime.now(UTC)
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': 'repo-npm',
            'name': 'npm',
            'format': 'npm',
            'type': 'proxy',
            'online': True,
            'proxy': {'upstream_url': url, **proxy},
            'format_options': {},
            'created_at': now,
            'updated_at': now,
        }
    )
    services.catalog.invalidate()
    return 'repo-npm'


@pytest.fixture
async def repo(services: Services, upstream: FakeUpstream) -> str:
    services.upstream.timeouts = Timeouts(connect=2.0, read=1.0)
    return await add_npm_repo(services, upstream.url)


async def stored(services: Services, path: str) -> dict[str, Any] | None:
    doc: dict[str, Any] | None = await services.db[ASSETS].find_one({'path': path})
    return doc


async def stored_json(services: Services, path: str) -> Any:
    asset = await stored(services, path)
    assert asset is not None, path
    return json.loads(services.blobs.blob_path(asset['blob_sha256']).read_bytes())


def route_is_number(upstream: FakeUpstream, dist: dict[str, Any] | None = None) -> None:
    files = {'7.0.0': dist or {'file': 'is-number-7.0.0.tgz', 'integrity': sri(TGZ)}}
    body = packument(upstream.url, 'is-number', files)
    upstream.routes['/is-number'] = Respond(200, {'Content-Type': 'application/json', 'ETag': '"p1"'}, body)
    upstream.routes['/is-number/-/is-number-7.0.0.tgz'] = Respond(200, {}, TGZ, chunk=65536)


async def test_packument_full_and_abbreviated_cached_separately(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    route_is_number(upstream)
    full = await client.get('/repository/npm/is-number')
    assert full.status_code == 200
    assert full.headers['x-ws-cache'] == 'FETCHING'
    assert full.headers['content-type'].startswith('application/json')
    assert 'x-accel-redirect' in full.headers

    abbreviated = await client.get('/repository/npm/is-number', headers={'Accept': ABBREVIATED})
    assert abbreviated.headers['content-type'].startswith(ABBREVIATED)
    assert [s.headers['accept'] for s in upstream.seen] == [
        'application/json',
        f'{ABBREVIATED}; q=1.0, application/json; q=0.8, */*',
    ]

    for key in ('is-number/-/packument.json', 'is-number/-/packument.install-v1.json'):
        doc = await stored_json(services, key)
        assert doc['versions']['7.0.0']['dist']['tarball'] == f'{BASE}is-number/-/is-number-7.0.0.tgz'
        assert (await stored(services, key) or {})['kind'] == 'metadata'

    again = await client.get('/repository/npm/is-number')
    assert again.headers['x-ws-cache'] == 'HIT'
    assert upstream.hits('/is-number') == 2


async def test_packument_revalidated_when_stale(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    await add_npm_repo(services, upstream.url, metadata_max_age_s=0)
    route_is_number(upstream)
    assert (await client.get('/repository/npm/is-number')).status_code == 200

    def not_modified(seen: Seen) -> Respond:
        assert seen.headers.get('if-none-match') == '"p1"'
        return Respond(304, {'ETag': '"p1"'})

    upstream.routes['/is-number'] = not_modified
    response = await client.get('/repository/npm/is-number')
    assert response.headers['x-ws-cache'] == 'REVALIDATED'


async def test_scoped_packument_encoded_and_plain(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    files = {'4.6.0': {'file': 'is-4.6.0.tgz', 'integrity': sri(SCOPED_TGZ)}}
    body = packument(upstream.url, '@sindresorhus/is', files)
    upstream.routes['/@sindresorhus%2Fis'] = Respond(200, {'Content-Type': 'application/json'}, body)
    upstream.routes['/@sindresorhus/is/-/is-4.6.0.tgz'] = Respond(200, {}, SCOPED_TGZ)

    assert (await client.get('/repository/npm/@sindresorhus%2fis')).status_code == 200
    assert (await client.get('/repository/npm/@sindresorhus/is')).headers['x-ws-cache'] == 'HIT'
    doc = await stored_json(services, '@sindresorhus/is/-/packument.json')
    assert doc['versions']['4.6.0']['dist']['tarball'] == f'{BASE}@sindresorhus/is/-/is-4.6.0.tgz'

    tarball = await client.get('/repository/npm/@sindresorhus/is/-/is-4.6.0.tgz')
    assert tarball.status_code == 200
    assert tarball.content == SCOPED_TGZ
    assert upstream.hits('/@sindresorhus%2Fis') == 2  # full (client) + abbreviated (integrity lookup)


async def test_tarball_verified_against_integrity_then_hit(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    route_is_number(upstream)
    first = await client.get('/repository/npm/is-number/-/is-number-7.0.0.tgz')
    assert first.status_code == 200
    assert first.content == TGZ
    assert first.headers['x-ws-cache'] == 'FETCHING'
    asset = await stored(services, 'is-number/-/is-number-7.0.0.tgz')
    assert asset is not None
    assert asset['kind'] == 'content'
    assert asset['checksums']['sha512'] == hashlib.sha512(TGZ).hexdigest()

    second = await client.get('/repository/npm/is-number/-/is-number-7.0.0.tgz')
    assert second.headers['x-ws-cache'] == 'HIT'
    assert second.headers['x-accel-redirect'].endswith(hashlib.sha256(TGZ).hexdigest())
    assert upstream.hits('/is-number') == 1  # the hit never looks at the packument
    assert upstream.hits('/is-number/-/is-number-7.0.0.tgz') == 1


@pytest.mark.parametrize(
    'dist',
    [
        {'file': 'is-number-7.0.0.tgz', 'integrity': sri(b'something else')},
        {'file': 'is-number-7.0.0.tgz', 'shasum': hashlib.sha1(b'other', usedforsecurity=False).hexdigest()},
    ],
)
async def test_tarball_integrity_mismatch_caches_nothing(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str, dist: dict[str, Any]
) -> None:
    route_is_number(upstream, dist)
    with pytest.raises(ResponseAborted):
        await client.get('/repository/npm/is-number/-/is-number-7.0.0.tgz')
    assert await stored(services, 'is-number/-/is-number-7.0.0.tgz') is None
    assert list(services.blobs.uploads_dir.iterdir()) == []


async def test_tarball_with_shasum_only(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    route_is_number(upstream, {'file': 'is-number-7.0.0.tgz', 'shasum': hashlib.sha1(TGZ).hexdigest()})
    response = await client.get('/repository/npm/is-number/-/is-number-7.0.0.tgz')
    assert response.content == TGZ
    asset = await stored(services, 'is-number/-/is-number-7.0.0.tgz')
    assert asset is not None
    assert asset['checksums']['sha1'] == hashlib.sha1(TGZ).hexdigest()


async def test_unlisted_tarball_refreshes_packument_once_then_404(
    client: httpx.AsyncClient, upstream: FakeUpstream, repo: str
) -> None:
    route_is_number(upstream)
    upstream.routes['/is-number/-/is-number-9.9.9.tgz'] = Respond(200, {}, b'not listed')
    response = await client.get('/repository/npm/is-number/-/is-number-9.9.9.tgz')
    assert response.status_code == 404
    assert response.json() == {'error': 'Not Found'}
    assert upstream.hits('/is-number') == 2  # cached, then forced revalidation
    assert upstream.hits('/is-number/-/is-number-9.9.9.tgz') == 0


async def test_packument_for_another_package_is_refused(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    upstream.routes['/is-number'] = Respond(200, {}, packument(upstream.url, 'evil', {'1.0.0': {'file': 'e.tgz'}}))
    response = await client.get('/repository/npm/is-number')
    assert response.status_code == 502
    assert response.json() == {'error': 'upstream packument is for a different package'}
    assert await services.db[ASSETS].count_documents({}) == 0


async def test_missing_package_is_npm_json_404(client: httpx.AsyncClient, upstream: FakeUpstream, repo: str) -> None:
    response = await client.get('/repository/npm/no-such-package')
    assert response.status_code == 404
    assert response.json() == {'error': 'Not Found'}
    assert (await client.get('/repository/npm/is-number/1.0.0')).status_code == 404
    assert (await client.get('/repository/npm/-/whoami')).status_code == 401


async def test_ping_search_and_audit(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    ping = await client.get('/repository/npm/-/ping')
    assert ping.status_code == 200
    assert ping.json() == {}

    results = json.dumps({'objects': [], 'total': 0}).encode()
    upstream.routes['/-/v1/search?text=is-number&size=2'] = Respond(200, {'Content-Type': 'application/json'}, results)
    for _ in range(2):
        search = await client.get('/repository/npm/-/v1/search', params={'text': 'is-number', 'size': '2'})
        assert search.status_code == 200
    assert upstream.hits('/-/v1/search?text=is-number&size=2') == 1

    advisories = json.dumps({'is-number': []}).encode()
    upstream.routes['/-/npm/v1/security/advisories/bulk'] = Respond(
        200, {'Content-Type': 'application/json'}, advisories
    )
    for _ in range(2):  # anonymous: audit is a read; never cached
        audit = await client.post(
            '/repository/npm/-/npm/v1/security/advisories/bulk',
            content=b'{"is-number":["7.0.0"]}',
            headers={'Content-Type': 'application/json'},
        )
        assert audit.status_code == 200
        assert audit.json() == {'is-number': []}
    assert upstream.hits('/-/npm/v1/security/advisories/bulk') == 2
    assert [s.method for s in upstream.seen if s.path.endswith('/bulk')] == ['POST', 'POST']
    assert await stored(services, '-/npm/v1/security/advisories/bulk') is None

    upstream.routes['/-/npm/v1/security/audits/quick'] = Respond(500, {}, b'boom')
    failed = await client.post('/repository/npm/-/npm/v1/security/audits/quick', content=b'{}')
    assert failed.status_code == 502
    assert failed.json() == {'error': 'upstream answered 500'}


async def test_write_methods_need_write_access(client: httpx.AsyncClient, upstream: FakeUpstream, repo: str) -> None:
    assert (await client.put('/repository/npm/is-number', content=b'{}')).status_code == 401
    assert (await client.post('/repository/npm/is-number', content=b'{}')).status_code == 401
