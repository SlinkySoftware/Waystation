"""yum proxy (T-P2-YUM) against real MongoDB/Valkey and a scriptable upstream: repomd.xml passthrough and
validation, repodata served by the repomd checksum (revalidation once on mismatch), by-checksum names, package
validation via yum_packages (listed, found through the enclosing repomd, unverified then checked)."""

import gzip
import hashlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import httpx
import pytest
from ws_fake_upstream import FakeUpstream, Respond

from waystation_common.db.collections import ASSETS, REPOSITORIES, YUM_PACKAGES
from waystation_common.settings import Settings
from waystation_server.formats.yum.plugin import YumPlugin
from waystation_server.problems import ResponseAborted
from waystation_server.proxy.http import Timeouts
from waystation_server.services import Services

pytestmark = pytest.mark.integration

DIR = '10/extras/x86_64/os'
RPM = b'\xed\xab\xee\xdb' + bytes(range(256)) * 40
RPM_HREF = 'Packages/h/hello-1.0-1.el10.noarch.rpm'
RPM_PATH = f'{DIR}/{RPM_HREF}'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def primary(*entries: tuple[str, bytes]) -> bytes:
    packages = ''.join(
        f'<package type="rpm"><name>p{i}</name><arch>noarch</arch><version epoch="0" ver="1.0" rel="1.el10"/>'
        f'<checksum type="sha256" pkgid="YES">{sha(data)}</checksum><size package="{len(data)}"/>'
        f'<location href="{href}"/></package>'
        for i, (href, data) in enumerate(entries)
    )
    return gzip.compress(
        f'<?xml version="1.0"?><metadata xmlns="http://linux.duke.edu/metadata/common" '
        f'packages="{len(entries)}">{packages}</metadata>'.encode(),
        mtime=0,
    )


def repomd(files: dict[str, tuple[str, bytes]]) -> bytes:
    """``files``: href -> (type, bytes)."""
    data = ''.join(
        f'<data type="{kind}"><checksum type="sha256">{sha(body)}</checksum><location href="{href}"/>'
        f'<size>{len(body)}</size></data>'
        for href, (kind, body) in files.items()
    )
    return f'<?xml version="1.0"?><repomd xmlns="http://linux.duke.edu/metadata/repo">{data}</repomd>'.encode()


def publish(upstream: FakeUpstream, files: dict[str, tuple[str, bytes]], *, directory: str = DIR) -> bytes:
    """Serve a yum repository directory: repomd.xml (+ .asc) and every file it lists."""
    doc = repomd(files)
    upstream.routes[f'/{directory}/repodata/repomd.xml'] = Respond(200, {'ETag': f'"{sha(doc)}"'}, doc)
    upstream.routes[f'/{directory}/repodata/repomd.xml.asc'] = Respond(200, {}, b'sig of ' + sha(doc).encode())
    for href, (_, body) in files.items():
        upstream.routes[f'/{directory}/{href}'] = Respond(200, {}, body)
    return doc


def by_checksum(kind: str, body: bytes, ext: str = '.xml.gz') -> tuple[str, tuple[str, bytes]]:
    return f'repodata/{sha(body)}-{kind}{ext}', (kind, body)


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'])


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        yield fake


@pytest.fixture
def plugin(services: Services) -> YumPlugin:
    services.upstream.timeouts = Timeouts(connect=2.0, read=1.0)
    plugin = services.formats.plugin('yum')
    assert isinstance(plugin, YumPlugin)
    return plugin


@pytest.fixture
async def repo(services: Services, upstream: FakeUpstream, plugin: YumPlugin) -> str:
    repo_id = 'repo-rocky'
    now = datetime.now(UTC)
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': repo_id,
            'name': 'rocky',
            'format': 'yum',
            'type': 'proxy',
            'online': True,
            'proxy': {'upstream_url': upstream.url, 'metadata_max_age_s': 3600},
            'format_options': {},
            'created_at': now,
            'updated_at': now,
        }
    )
    services.catalog.invalidate()
    return repo_id


def blob_of(response: httpx.Response) -> str:
    """The sha256 an X-Accel response points nginx at."""
    return response.headers['x-accel-redirect'].rsplit('/', 1)[-1]


URL = f'/repository/rocky/{DIR}'


async def test_repomd_passes_through_and_is_validated(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    doc = publish(upstream, dict([by_checksum('primary', primary())]))
    response = await client.get(f'{URL}/repodata/repomd.xml')
    assert response.status_code == 200
    assert blob_of(response) == sha(doc)
    asset = await services.db[ASSETS].find_one({'path': f'{DIR}/repodata/repomd.xml'})
    assert asset is not None
    assert asset['kind'] == 'metadata'
    assert (await client.get(f'{URL}/repodata/repomd.xml')).headers['x-ws-cache'] == 'HIT'
    asc = await client.get(f'{URL}/repodata/repomd.xml.asc')
    assert asc.status_code == 200
    # Not a repomd document, or one with a DTD: refused, nothing cached.
    for bad in (b'<html>nope</html>', b'<!DOCTYPE repomd [<!ENTITY a "b">]><repomd/>'):
        upstream.routes['/bad/repodata/repomd.xml'] = Respond(200, {}, bad)
        assert (await client.get('/repository/rocky/bad/repodata/repomd.xml')).status_code == 502
        assert await services.db[ASSETS].find_one({'path': 'bad/repodata/repomd.xml'}) is None


async def test_new_repomd_drops_the_cached_signature(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: YumPlugin, repo: str
) -> None:
    publish(upstream, dict([by_checksum('primary', primary())]))
    assert (await client.get(f'{URL}/repodata/repomd.xml')).status_code == 200
    first = await client.get(f'{URL}/repodata/repomd.xml.asc')
    # The upstream publishes a new repomd.xml (and signature); a refresh stores it and forgets the old signature.
    new = publish(upstream, dict([by_checksum('primary', primary((RPM_HREF, RPM)))]))
    await services.db[ASSETS].update_one(
        {'path': f'{DIR}/repodata/repomd.xml'}, {'$set': {'upstream.fetched_at': datetime(2020, 1, 1, tzinfo=UTC)}}
    )
    assert blob_of(await client.get(f'{URL}/repodata/repomd.xml')) == sha(new)
    second = await client.get(f'{URL}/repodata/repomd.xml.asc')
    assert second.headers['x-ws-cache'] != 'HIT'
    assert blob_of(second) != blob_of(first)


async def test_repodata_is_served_by_the_repomd_checksum(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: YumPlugin, repo: str
) -> None:
    body = primary((RPM_HREF, RPM))
    href, entry = by_checksum('primary', body)
    publish(upstream, {href: entry, 'repodata/comps.xml': ('group', b'<comps/>')})
    response = await client.get(f'{URL}/{href}')  # fetches repomd.xml itself first
    assert response.status_code == 200
    assert blob_of(response) == sha(body)
    assert upstream.hits(f'/{DIR}/repodata/repomd.xml') == 1
    assert (await client.get(f'{URL}/{href}')).headers['x-ws-cache'] == 'HIT'
    assert upstream.hits(f'/{DIR}/{href}') == 1
    # A plain (mutable) name is stored under its checksum, never as a mutable asset.
    comps = await client.get(f'{URL}/repodata/comps.xml')
    assert comps.status_code == 200
    assert await services.db[ASSETS].find_one({'path': f'{DIR}/repodata/{sha(b"<comps/>")}-comps.xml'}) is not None
    assert await services.db[ASSETS].find_one({'path': f'{DIR}/repodata/comps.xml'}) is None
    # Not listed and not a by-checksum name: cannot be verified, so 404 without asking upstream.
    upstream.routes[f'/{DIR}/repodata/updateinfo.xml.gz'] = Respond(200, {}, b'x')
    assert (await client.get(f'{URL}/repodata/updateinfo.xml.gz')).status_code == 404
    assert upstream.hits(f'/{DIR}/repodata/updateinfo.xml.gz') == 0
    await plugin.drain()
    entry_doc = await services.db[YUM_PACKAGES].find_one({'repo_id': repo, 'path': RPM_PATH})
    assert entry_doc is not None
    assert (entry_doc['digest'], entry_doc['size'], entry_doc['verified']) == (sha(RPM), len(RPM), True)
    assert (entry_doc['repo_dir'], entry_doc['name'], entry_doc['version']) == (DIR, 'p0', '1.0')


async def test_repodata_mismatch_revalidates_repomd_once(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    old, new = b'<comps>old</comps>', b'<comps>new</comps>'
    publish(upstream, {'repodata/comps.xml': ('group', old)})
    assert (await client.get(f'{URL}/repodata/repomd.xml')).status_code == 200
    # The upstream moved on: new repomd.xml and the plain path now has the new file.
    doc = publish(upstream, {'repodata/comps.xml': ('group', new)})
    response = await client.get(f'{URL}/repodata/comps.xml')
    assert response.status_code == 200
    assert blob_of(response) == sha(new)
    assert upstream.hits(f'/{DIR}/repodata/repomd.xml') == 2  # revalidated once
    cached = await services.db[ASSETS].find_one({'path': f'{DIR}/repodata/repomd.xml'})
    assert cached is not None
    assert cached['blob_sha256'] == sha(doc)


async def test_repodata_mismatch_fails_when_repomd_still_disagrees(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    body = primary()
    href, entry = by_checksum('primary', body)
    publish(upstream, {href: entry})
    upstream.routes[f'/{DIR}/{href}'] = Respond(200, {}, primary((RPM_HREF, RPM)))  # corrupt mirror
    assert (await client.get(f'{URL}/{href}')).status_code == 502
    assert upstream.hits(f'/{DIR}/{href}') == 1
    assert upstream.hits(f'/{DIR}/repodata/repomd.xml') == 2
    assert await services.db[ASSETS].find_one({'path': f'{DIR}/{href}'}) is None


async def test_unlisted_by_checksum_name_is_self_validating(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    publish(upstream, dict([by_checksum('primary', primary())]))
    assert (await client.get(f'{URL}/repodata/repomd.xml')).status_code == 200
    older = b'an older generation of filelists'
    name = f'{sha(older)}-filelists.xml.gz'
    upstream.routes[f'/{DIR}/repodata/{name}'] = Respond(200, {}, older)
    response = await client.get(f'{URL}/repodata/{name}')
    assert response.status_code == 200
    assert response.content == older
    wrong = f'{"0" * 64}-filelists.xml.gz'
    upstream.routes[f'/{DIR}/repodata/{wrong}'] = Respond(200, {}, older)
    with pytest.raises(ResponseAborted):
        await client.get(f'{URL}/repodata/{wrong}')
    assert await services.db[ASSETS].find_one({'path': f'{DIR}/repodata/{wrong}'}) is None


async def test_package_is_validated_against_primary(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: YumPlugin, repo: str
) -> None:
    bad_href = 'Packages/b/bad-1-1.noarch.rpm'
    href, entry = by_checksum('primary', primary((RPM_HREF, RPM), (bad_href, b'the real bytes')))
    publish(upstream, {href: entry})
    assert (await client.get(f'{URL}/{href}')).status_code == 200
    await plugin.drain()
    upstream.routes[f'/{RPM_PATH}'] = Respond(200, {}, RPM)
    response = await client.get(f'/repository/rocky/{RPM_PATH}')
    assert response.status_code == 200
    assert response.content == RPM
    upstream.routes[f'/{DIR}/{bad_href}'] = Respond(200, {}, b'tampered bytes')
    with pytest.raises(ResponseAborted):
        await client.get(f'{URL}/{bad_href}')
    assert await services.db[ASSETS].find_one({'path': f'{DIR}/{bad_href}'}) is None


async def test_package_before_primary_uses_the_enclosing_repomd(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    href, entry = by_checksum('primary', primary((RPM_HREF, b'what the listing says')))
    publish(upstream, {href: entry})
    assert (await client.get(f'{URL}/repodata/repomd.xml')).status_code == 200
    # The primary was never requested: the package request fetches and indexes it, then validates.
    upstream.routes[f'/{RPM_PATH}'] = Respond(200, {}, RPM)  # not what the listing says (size differs: 502 up front)
    assert (await client.get(f'/repository/rocky/{RPM_PATH}')).status_code == 502
    assert await services.db[ASSETS].find_one({'path': RPM_PATH}) is None
    assert upstream.hits(f'/{DIR}/{href}') == 1
    doc = await services.db[YUM_PACKAGES].find_one({'repo_id': repo, 'path': RPM_PATH})
    assert doc is not None
    assert doc['verified'] is True


async def test_package_without_any_listing_is_unverified_then_checked(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: YumPlugin, repo: str
) -> None:
    other_href = 'Packages/o/other-1-1.noarch.rpm'
    upstream.routes[f'/{RPM_PATH}'] = Respond(200, {}, RPM)
    upstream.routes[f'/{DIR}/{other_href}'] = Respond(200, {}, b'served before the listing')
    assert (await client.get(f'/repository/rocky/{RPM_PATH}')).content == RPM
    assert (await client.get(f'{URL}/{other_href}')).status_code == 200
    doc = await services.db[YUM_PACKAGES].find_one({'repo_id': repo, 'path': RPM_PATH})
    assert doc is not None
    assert (doc['digest'], doc['verified']) == (sha(RPM), False)
    # The listing arrives: the matching package becomes verified, the mismatching cached copy is dropped.
    href, entry = by_checksum('primary', primary((RPM_HREF, RPM), (other_href, b'what the listing says')))
    publish(upstream, {href: entry})
    assert (await client.get(f'{URL}/{href}')).status_code == 200
    await plugin.drain()
    doc = await services.db[YUM_PACKAGES].find_one({'repo_id': repo, 'path': RPM_PATH})
    assert doc is not None
    assert doc['verified'] is True
    assert await services.db[ASSETS].find_one({'path': RPM_PATH}) is not None
    assert await services.db[ASSETS].find_one({'path': f'{DIR}/{other_href}'}) is None
    upstream.routes[f'/{DIR}/{other_href}'] = Respond(200, {}, b'what the listing says')
    assert (await client.get(f'{URL}/{other_href}')).content == b'what the listing says'


async def test_new_primary_generation_replaces_the_listing(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: YumPlugin, repo: str
) -> None:
    gone = 'Packages/g/gone-1-1.noarch.rpm'
    href, entry = by_checksum('primary', primary((RPM_HREF, RPM), (gone, b'gone')))
    publish(upstream, {href: entry})
    assert (await client.get(f'{URL}/{href}')).status_code == 200
    await plugin.drain()
    href2, entry2 = by_checksum('primary', primary((RPM_HREF, RPM)))
    publish(upstream, {href2: entry2})
    await services.db[ASSETS].update_one(
        {'path': f'{DIR}/repodata/repomd.xml'}, {'$set': {'upstream.fetched_at': datetime(2020, 1, 1, tzinfo=UTC)}}
    )
    assert (await client.get(f'{URL}/repodata/repomd.xml')).status_code == 200
    assert (await client.get(f'{URL}/{href2}')).status_code == 200
    await plugin.drain()
    paths = {d['path'] async for d in services.db[YUM_PACKAGES].find({'repo_id': repo})}
    assert paths == {RPM_PATH}


async def test_other_paths_and_methods(client: httpx.AsyncClient, upstream: FakeUpstream, repo: str) -> None:
    upstream.routes['/10/BaseOS/x86_64/os/.treeinfo'] = Respond(200, {}, b'[general]\n')
    assert (await client.get('/repository/rocky/10/BaseOS/x86_64/os/.treeinfo')).status_code == 200
    assert (await client.put(f'/repository/rocky/{RPM_PATH}', content=b'x')).status_code in (401, 403, 405)
    assert (await client.get('/repository/rocky/')).status_code == 404
    assert (await client.get('/repository/rocky/nosuch/repodata/x-primary.xml.gz')).status_code == 404
