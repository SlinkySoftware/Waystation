"""Raw format plugin (T-P2-RAW) on the real proxy engine: MongoDB/Valkey plus a scriptable local upstream."""

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from ws_fake_upstream import FakeUpstream, Respond, Seen
from ws_testing import add_repo

from waystation_common.db.collections import ASSETS, REPOSITORIES
from waystation_common.settings import Settings
from waystation_server.problems import ResponseAborted
from waystation_server.proxy.http import Timeouts
from waystation_server.services import Services

pytestmark = pytest.mark.integration

DATA = bytes(range(256)) * 2000  # 512000 bytes: two 256 KiB chunks
SHA = hashlib.sha256(DATA).hexdigest()
RepoFactory = Callable[..., Awaitable[str]]


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'])


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        yield fake


@pytest.fixture
def make(services: Services, upstream: FakeUpstream) -> RepoFactory:
    services.upstream.timeouts = Timeouts(connect=2.0, read=1.0)

    async def factory(options: dict[str, Any] | None = None, *, base: str = '', **proxy: Any) -> str:
        repo_id = await add_repo(services, upstream.url + base, **proxy)
        if options:
            await services.db[REPOSITORIES].update_one({'_id': repo_id}, {'$set': {'format_options': options}})
            services.catalog.invalidate()
        return repo_id

    return factory


async def test_content_miss_then_hit(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, make: RepoFactory
) -> None:
    await make(base='mirror/')
    upstream.routes['/mirror/tools/v1/tool.tar.gz'] = Respond(200, {'Content-Type': 'text/html'}, DATA, chunk=50_000)
    first = await client.get('/repository/files/tools/v1/tool.tar.gz')
    assert first.status_code == 200
    assert first.content == DATA
    assert first.headers['x-ws-cache'] == 'FETCHING'
    assert first.headers['content-type'] == 'application/gzip'  # by extension, not the upstream's claim
    assert first.headers['content-disposition'] == 'attachment; filename="tool.tar.gz"'
    assert first.headers['x-content-type-options'] == 'nosniff'

    hit = await client.get('/repository/files/tools/v1/tool.tar.gz')
    assert hit.headers['x-ws-cache'] == 'HIT'
    assert hit.headers['x-accel-redirect'].endswith(SHA)
    assert hit.headers['content-type'] == 'application/gzip'
    assert hit.headers['content-disposition'] == 'attachment; filename="tool.tar.gz"'
    assert upstream.hits('/mirror/tools/v1/tool.tar.gz') == 1

    head = await client.head('/repository/files/tools/v1/tool.tar.gz')
    assert head.status_code == 200
    assert head.headers['content-length'] == str(len(DATA))

    conditional = await client.get(
        '/repository/files/tools/v1/tool.tar.gz', headers={'If-None-Match': hit.headers['etag']}
    )
    assert conditional.status_code == 304
    since = await client.get(
        '/repository/files/tools/v1/tool.tar.gz', headers={'If-Modified-Since': hit.headers['last-modified']}
    )
    assert since.status_code == 304

    asset = await services.db[ASSETS].find_one({'path': 'tools/v1/tool.tar.gz'})
    assert asset is not None
    assert asset['kind'] == 'content'
    assert asset['blob_sha256'] == SHA

    # directory listings show what is cached (never asked upstream)
    root = await client.get('/repository/files/', headers={'Accept': 'application/json'})
    assert [(e['kind'], e['name']) for e in root.json()['entries']] == [('folder', 'tools')]
    folder = await client.get('/repository/files/tools/v1/')
    assert folder.headers['content-type'].startswith('text/html')
    assert 'tool.tar.gz' in folder.text
    assert (await client.get('/repository/files/tools/v2/')).status_code == 404
    assert upstream.hits('/mirror/tools/v1/') == 0


async def test_metadata_regex_revalidates(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, make: RepoFactory
) -> None:
    await make({'metadata_path_regex': r'.*/(index\.json|latest)$'}, metadata_max_age_s=0)
    versions = {'body': b'1.0.0\n'}

    def latest(seen: Seen) -> Respond:
        if seen.headers.get('if-none-match') == '"v1"' and versions['body'] == b'1.0.0\n':
            return Respond(304, {'ETag': '"v1"'})
        return Respond(200, {'ETag': '"v1"', 'Content-Type': 'text/plain; charset=utf-8'}, versions['body'])

    upstream.routes['/app/latest'] = latest
    upstream.routes['/app/1.0.0/app.bin'] = Respond(200, {}, DATA)

    first = await client.get('/repository/files/app/latest')
    assert first.headers['x-ws-cache'] == 'FETCHING'
    assert first.headers['content-type'] == 'text/plain; charset=utf-8'
    asset = await services.db[ASSETS].find_one({'path': 'app/latest'})
    assert asset is not None
    assert asset['kind'] == 'metadata'

    again = await client.get('/repository/files/app/latest')
    assert again.headers['x-ws-cache'] == 'REVALIDATED'
    assert upstream.seen[-1].headers['if-none-match'] == '"v1"'

    versions['body'] = b'1.1.0\n'
    changed = await client.get('/repository/files/app/latest')
    assert changed.headers['x-ws-cache'] == 'FETCHING'
    stored = await services.db[ASSETS].find_one({'path': 'app/latest'})
    assert stored is not None
    assert services.blobs.blob_path(stored['blob_sha256']).read_bytes() == b'1.1.0\n'

    # Content (not matching the regex) stays cached however old: content_max_age_s defaults to forever.
    assert (await client.get('/repository/files/app/1.0.0/app.bin')).content == DATA
    assert (await client.get('/repository/files/app/1.0.0/app.bin')).headers['x-ws-cache'] == 'HIT'
    assert upstream.hits('/app/1.0.0/app.bin') == 1

    upstream.routes['/app/latest'] = Respond(503, {}, b'down')
    stale = await client.get('/repository/files/app/latest')
    assert stale.status_code == 200
    assert stale.headers['x-ws-cache'] == 'STALE'


async def test_upstream_404_is_native_and_negative_cached(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, make: RepoFactory
) -> None:
    await make()
    for _ in range(2):
        response = await client.get('/repository/files/no/such/file.txt')
        assert response.status_code == 404
        assert response.headers['content-type'].startswith('text/plain')
    assert upstream.hits('/no/such/file.txt') == 1
    upstream.routes['/gone'] = Respond(410, {}, b'')
    assert (await client.get('/repository/files/gone')).status_code == 404
    assert await services.db[ASSETS].count_documents({}) == 0


async def test_upstream_faults(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, make: RepoFactory
) -> None:
    await make()
    upstream.routes['/boom'] = Respond(500, {}, b'boom')
    upstream.routes['/slow'] = Respond(hang=True)
    upstream.routes['/cut.bin'] = Respond(200, {}, DATA, chunk=65536, reset_after=200_000)
    upstream.routes['/private'] = Respond(401, {}, b'')
    assert (await client.get('/repository/files/boom')).status_code == 502
    assert (await client.get('/repository/files/slow')).status_code == 504
    assert (await client.get('/repository/files/private')).status_code == 502
    with pytest.raises(ResponseAborted):
        await client.get('/repository/files/cut.bin')
    assert await services.db[ASSETS].count_documents({}) == 0
    assert list(services.blobs.uploads_dir.iterdir()) == []
    # Faults are not negative-cached: the next request asks again.
    assert (await client.get('/repository/files/boom')).status_code == 502
    assert upstream.hits('/boom') == 2


async def test_upstream_redirects_are_followed(
    client: httpx.AsyncClient, upstream: FakeUpstream, make: RepoFactory
) -> None:
    await make()
    upstream.routes['/download/tool.zip'] = Respond(302, {'Location': '/cdn/abc/tool.zip'}, b'')
    upstream.routes['/cdn/abc/tool.zip'] = Respond(200, {'Content-Type': 'application/octet-stream'}, DATA)
    response = await client.get('/repository/files/download/tool.zip')
    assert response.content == DATA
    assert response.headers['content-type'] == 'application/zip'


async def test_head_miss_asks_upstream_with_head(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, make: RepoFactory
) -> None:
    await make()
    upstream.routes['/a/file.iso'] = Respond(200, {}, DATA)
    response = await client.head('/repository/files/a/file.iso')
    assert response.status_code == 200
    assert response.headers['content-length'] == str(len(DATA))
    assert response.headers['content-type'] == 'application/x-iso9660-image'
    assert [s.method for s in upstream.seen] == ['HEAD']
    assert await services.db[ASSETS].count_documents({}) == 0


async def test_paths_are_escaped_upstream(client: httpx.AsyncClient, upstream: FakeUpstream, make: RepoFactory) -> None:
    await make(base='base/')
    upstream.routes['/base/a%20b%23c%3F.txt'] = Respond(200, {}, b'odd name')
    upstream.routes['/base/http%3A/x.txt'] = Respond(200, {}, b'not a scheme')
    assert (await client.get('/repository/files/a%20b%23c%3F.txt')).content == b'odd name'
    assert (await client.get('/repository/files/http:/x.txt')).content == b'not a scheme'
    assert [s.path for s in upstream.seen] == ['/base/a%20b%23c%3F.txt', '/base/http%3A/x.txt']


async def test_extensionless_type_comes_from_upstream_sanitised(
    client: httpx.AsyncClient, upstream: FakeUpstream, make: RepoFactory
) -> None:
    await make({'content_disposition': 'inline'})
    upstream.routes['/LICENSE'] = Respond(200, {'Content-Type': 'text/plain; charset=utf-8; q=1'}, b'MIT')
    upstream.routes['/page'] = Respond(200, {'Content-Type': 'text/html'}, b'<script>alert(1)</script>')
    upstream.routes['/blob'] = Respond(200, {'Content-Type': 'not a type'}, b'x')
    upstream.routes['/page.html'] = Respond(200, {}, b'<p>')

    for _ in range(2):  # miss, then hit
        lic = await client.get('/repository/files/LICENSE')
        assert lic.headers['content-type'] == 'text/plain; charset=utf-8'
        assert lic.headers['content-disposition'] == 'inline; filename="LICENSE"'
        page = await client.get('/repository/files/page')
        assert page.headers['content-type'] == 'text/plain; charset=utf-8'  # inline: never rendered as HTML
        assert (await client.get('/repository/files/blob')).headers['content-type'] == 'application/octet-stream'
        html = await client.get('/repository/files/page.html')
        assert html.headers['content-type'] == 'text/plain; charset=utf-8'


async def test_only_get_and_head(client: httpx.AsyncClient, make: RepoFactory) -> None:
    await make()
    # Anonymous callers are refused by RBAC before the plugin (write is not granted on proxies).
    assert (await client.put('/repository/files/x.txt', content=b'x')).status_code == 401
    assert (await client.delete('/repository/files/x.txt')).status_code == 401
