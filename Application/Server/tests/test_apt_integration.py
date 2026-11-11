"""apt proxy (T-P2-APT) against real MongoDB/Valkey and a scriptable upstream: Release passthrough, index files
served by the Release's hash, by-hash, Release revalidation on mismatch, pool validation via apt_pool_index."""

import hashlib
import lzma
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from ws_fake_upstream import FakeUpstream, Respond

from waystation_common.db.collections import APT_PACKAGES, APT_POOL_INDEX, ASSETS, REPOSITORIES
from waystation_common.settings import Settings
from waystation_server.formats.apt.plugin import AptPlugin
from waystation_server.problems import ResponseAborted
from waystation_server.proxy.http import Timeouts
from waystation_server.services import Services

pytestmark = pytest.mark.integration

DEB = b'!<arch>\n' + bytes(range(256)) * 40
DEB_SHA = hashlib.sha256(DEB).hexdigest()
DEB_PATH = 'pool/main/h/hello/hello_2.10-5_amd64.deb'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def packages(*entries: tuple[str, bytes]) -> bytes:
    stanzas = [
        f'Package: p{i}\nVersion: 1\nFilename: {name}\nSize: {len(data)}\nSHA256: {sha(data)}\n'
        for i, (name, data) in enumerate(entries)
    ]
    return lzma.compress('\n'.join(stanzas).encode())


def release(files: dict[str, bytes], *, by_hash: bool = True, signed: bool = False) -> bytes:
    lines = ['Origin: Test', 'Suite: trixie', f'Acquire-By-Hash: {"yes" if by_hash else "no"}', 'SHA256:']
    lines += [f' {sha(data)} {len(data)} {path}' for path, data in files.items()]
    text = '\n'.join(lines) + '\n'
    if signed:
        return (
            f'-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n{text}'
            '-----BEGIN PGP SIGNATURE-----\n\nZmFrZQ==\n-----END PGP SIGNATURE-----\n'
        ).encode()
    return text.encode()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'])


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        yield fake


@pytest.fixture
def plugin(services: Services) -> AptPlugin:
    services.upstream.timeouts = Timeouts(connect=2.0, read=1.0)
    plugin = services.formats.plugin('apt')
    assert isinstance(plugin, AptPlugin)
    return plugin


async def add_apt_repo(services: Services, url: str, *, name: str = 'debian', **options: Any) -> str:
    repo_id = f'repo-{name}'
    now = datetime.now(UTC)
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': repo_id,
            'name': name,
            'format': 'apt',
            'type': 'proxy',
            'online': True,
            'proxy': {'upstream_url': url, 'metadata_max_age_s': 3600},
            'format_options': {'distribution': 'trixie', **options},
            'created_at': now,
            'updated_at': now,
        }
    )
    services.catalog.invalidate()
    return repo_id


@pytest.fixture
async def repo(services: Services, upstream: FakeUpstream, plugin: AptPlugin) -> str:
    return await add_apt_repo(services, upstream.url)


def blob_of(response: httpx.Response) -> str:
    """The sha256 an X-Accel response points nginx at."""
    return response.headers['x-accel-redirect'].rsplit('/', 1)[-1]


def publish(upstream: FakeUpstream, suite: str, files: dict[str, bytes], *, by_hash: bool = True) -> bytes:
    """Serve a suite: InRelease + every file at its plain and by-hash path."""
    inrelease = release(files, by_hash=by_hash, signed=True)
    upstream.routes[f'/dists/{suite}/InRelease'] = Respond(200, {'ETag': f'"{sha(inrelease)}"'}, inrelease)
    for path, data in files.items():
        directory = path.rsplit('/', 1)[0]
        upstream.routes[f'/dists/{suite}/{path}'] = Respond(200, {}, data)
        upstream.routes[f'/dists/{suite}/{directory}/by-hash/SHA256/{sha(data)}'] = Respond(200, {}, data)
    return inrelease


async def test_inrelease_passes_through_untouched(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    inrelease = publish(upstream, 'trixie', {'main/binary-amd64/Packages.xz': packages()})
    gpg = b'-----BEGIN PGP SIGNATURE-----\nx\n-----END PGP SIGNATURE-----\n'
    upstream.routes['/dists/trixie/Release.gpg'] = Respond(200, {}, gpg)
    response = await client.get('/repository/debian/dists/trixie/InRelease')
    assert response.status_code == 200
    assert blob_of(response) == sha(inrelease)
    asset = await services.db[ASSETS].find_one({'path': 'dists/trixie/InRelease'})
    assert asset is not None
    assert asset['kind'] == 'metadata'
    assert services.blobs.blob_path(sha(inrelease)).read_bytes() == inrelease
    assert blob_of(await client.get('/repository/debian/dists/trixie/Release.gpg')) == sha(gpg)
    # A Release that does not parse is refused and not cached.
    upstream.routes['/dists/broken/InRelease'] = Respond(200, {}, b'SHA256:\n nothex 1 x\n')
    assert (await client.get('/repository/debian/dists/broken/InRelease')).status_code == 502
    assert await services.db[ASSETS].find_one({'path': 'dists/broken/InRelease'}) is None


async def test_plain_index_is_served_by_the_release_hash(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: AptPlugin, repo: str
) -> None:
    index = packages((DEB_PATH, DEB))
    publish(upstream, 'trixie', {'main/binary-amd64/Packages.xz': index})
    assert (await client.get('/repository/debian/dists/trixie/InRelease')).status_code == 200
    response = await client.get('/repository/debian/dists/trixie/main/binary-amd64/Packages.xz')
    assert response.status_code == 200
    assert blob_of(response) == sha(index)
    key = f'dists/trixie/main/binary-amd64/by-hash/SHA256/{sha(index)}'
    # Stored under the by-hash key, never as a mutable asset at the plain path; fetched by-hash upstream.
    assert await services.db[ASSETS].find_one({'path': key}) is not None
    assert await services.db[ASSETS].find_one({'path': 'dists/trixie/main/binary-amd64/Packages.xz'}) is None
    assert upstream.hits(f'/{key}') == 1
    assert upstream.hits('/dists/trixie/main/binary-amd64/Packages.xz') == 0
    again = await client.get('/repository/debian/dists/trixie/main/binary-amd64/Packages.xz')
    assert again.headers['x-ws-cache'] == 'HIT'
    by_hash = await client.get(f'/repository/debian/{key}')
    assert by_hash.headers['x-ws-cache'] == 'HIT'
    assert upstream.hits(f'/{key}') == 1
    # Not listed in the Release: cannot be verified, so 404 without asking upstream.
    assert (await client.get('/repository/debian/dists/trixie/main/binary-amd64/Packages.gz')).status_code == 404
    await plugin.drain()
    entry = await services.db[APT_POOL_INDEX].find_one({'repo_id': repo, 'filename': DEB_PATH})
    assert entry is not None
    assert (entry['sha256'], entry['size'], entry['verified']) == (DEB_SHA, len(DEB), True)
    assert (entry['suite'], entry['component'], entry['arch']) == ('trixie', 'main', 'amd64')


async def test_index_without_acquire_by_hash_fetches_the_plain_path(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    index = packages()
    publish(upstream, 'trixie', {'main/binary-amd64/Packages.xz': index}, by_hash=False)
    # The index request fetches the Release itself when none is cached yet.
    response = await client.get('/repository/debian/dists/trixie/main/binary-amd64/Packages.xz')
    assert response.status_code == 200
    assert blob_of(response) == sha(index)
    assert upstream.hits('/dists/trixie/InRelease') == 1
    assert upstream.hits('/dists/trixie/main/binary-amd64/Packages.xz') == 1


async def test_falls_back_to_release_when_there_is_no_inrelease(
    client: httpx.AsyncClient, upstream: FakeUpstream, repo: str
) -> None:
    index = packages()
    files = {'main/binary-amd64/Packages.xz': index}
    upstream.routes['/dists/old/Release'] = Respond(200, {}, release(files, by_hash=False))
    upstream.routes['/dists/old/main/binary-amd64/Packages.xz'] = Respond(200, {}, index)
    response = await client.get('/repository/debian/dists/old/main/binary-amd64/Packages.xz')
    assert response.status_code == 200
    assert blob_of(response) == sha(index)


async def test_index_mismatch_revalidates_the_release_once(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    old, new = packages(), packages((DEB_PATH, DEB))
    path = 'main/binary-amd64/Packages.xz'
    publish(upstream, 'trixie', {path: old}, by_hash=False)
    assert (await client.get('/repository/debian/dists/trixie/InRelease')).status_code == 200
    # The upstream moved on: new InRelease and the plain path now has the new index.
    publish(upstream, 'trixie', {path: new}, by_hash=False)
    response = await client.get(f'/repository/debian/dists/trixie/{path}')
    assert response.status_code == 200
    assert blob_of(response) == sha(new)
    assert upstream.hits('/dists/trixie/InRelease') == 2  # revalidated once
    cached = await services.db[ASSETS].find_one({'path': 'dists/trixie/InRelease'})
    assert cached is not None
    assert cached['blob_sha256'] == sha(release({path: new}, by_hash=False, signed=True))


async def test_index_mismatch_fails_when_the_release_still_disagrees(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    path = 'main/binary-amd64/Packages.xz'
    publish(upstream, 'trixie', {path: packages()}, by_hash=False)
    upstream.routes[f'/dists/trixie/{path}'] = Respond(200, {}, packages((DEB_PATH, DEB)))  # corrupt mirror
    response = await client.get(f'/repository/debian/dists/trixie/{path}')
    assert response.status_code == 502
    assert upstream.hits(f'/dists/trixie/{path}') == 1
    assert upstream.hits('/dists/trixie/InRelease') == 2
    assert await services.db[ASSETS].count_documents({'path': {'$regex': 'by-hash'}}) == 0


async def test_by_hash_is_self_validating(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: AptPlugin, repo: str
) -> None:
    index = packages((DEB_PATH, DEB))
    publish(upstream, 'trixie', {'main/binary-amd64/Packages.xz': index})
    assert (await client.get('/repository/debian/dists/trixie/InRelease')).status_code == 200
    key = f'dists/trixie/main/binary-amd64/by-hash/SHA256/{sha(index)}'
    response = await client.get(f'/repository/debian/{key}')
    assert response.status_code == 200
    assert response.content == index
    assert (await client.get(f'/repository/debian/{key}')).headers['x-ws-cache'] == 'HIT'
    await plugin.drain()  # the by-hash file is a Packages listed in the Release: indexed too
    assert await services.db[APT_POOL_INDEX].count_documents({'repo_id': repo, 'verified': True}) == 1
    # Wrong bytes behind a by-hash name: aborted, nothing cached.
    wrong = f'dists/trixie/main/binary-amd64/by-hash/SHA256/{"0" * 64}'
    upstream.routes[f'/{wrong}'] = Respond(200, {}, index)
    with pytest.raises(ResponseAborted):
        await client.get(f'/repository/debian/{wrong}')
    assert await services.db[ASSETS].find_one({'path': wrong}) is None
    assert (await client.get('/repository/debian/dists/trixie/by-hash/MD5Sum/' + 'a' * 32)).status_code == 404


async def test_pool_file_is_validated_against_its_listing(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: AptPlugin, repo: str
) -> None:
    bad_path = 'pool/main/b/bad/bad_1_amd64.deb'
    index = packages((DEB_PATH, DEB), (bad_path, b'the real bytes'))
    publish(upstream, 'trixie', {'main/binary-amd64/Packages.xz': index})
    assert (await client.get('/repository/debian/dists/trixie/main/binary-amd64/Packages.xz')).status_code == 200
    await plugin.drain()
    upstream.routes[f'/{DEB_PATH}'] = Respond(200, {}, DEB)
    response = await client.get(f'/repository/debian/{DEB_PATH}')
    assert response.status_code == 200
    assert response.content == DEB
    asset = await services.db[ASSETS].find_one({'path': DEB_PATH})
    assert asset is not None
    assert asset['blob_sha256'] == DEB_SHA
    upstream.routes[f'/{bad_path}'] = Respond(200, {}, b'tampered bytes')
    with pytest.raises(ResponseAborted):
        await client.get(f'/repository/debian/{bad_path}')
    assert await services.db[ASSETS].find_one({'path': bad_path}) is None


async def test_pool_file_before_any_listing_is_unverified_then_checked(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: AptPlugin, repo: str
) -> None:
    other_path = 'pool/main/o/other/other_1_all.deb'
    upstream.routes[f'/{DEB_PATH}'] = Respond(200, {}, DEB)
    upstream.routes[f'/{other_path}'] = Respond(200, {}, b'served before the listing')
    assert (await client.get(f'/repository/debian/{DEB_PATH}')).content == DEB
    assert (await client.get(f'/repository/debian/{other_path}')).status_code == 200
    entry = await services.db[APT_POOL_INDEX].find_one({'repo_id': repo, 'filename': DEB_PATH})
    assert entry is not None
    assert (entry['sha256'], entry['verified']) == (DEB_SHA, False)
    # The listing arrives: matching file becomes verified, the mismatching cached copy is dropped.
    index = packages((DEB_PATH, DEB), (other_path, b'what the listing says'))
    publish(upstream, 'trixie', {'main/binary-amd64/Packages.xz': index})
    assert (await client.get('/repository/debian/dists/trixie/main/binary-amd64/Packages.xz')).status_code == 200
    await plugin.drain()
    entry = await services.db[APT_POOL_INDEX].find_one({'repo_id': repo, 'filename': DEB_PATH})
    assert entry is not None
    assert entry['verified'] is True
    assert await services.db[ASSETS].find_one({'path': DEB_PATH}) is not None
    assert await services.db[ASSETS].find_one({'path': other_path}) is None
    upstream.routes[f'/{other_path}'] = Respond(200, {}, b'what the listing says')
    response = await client.get(f'/repository/debian/{other_path}')
    assert response.content == b'what the listing says'


async def test_unverified_fetch_after_a_listing_landed_is_checked(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, repo: str
) -> None:
    from waystation_server.db import apt as apt_db

    await apt_db.record_listing(
        services.db, repo, [apt_db.Listing(DEB_PATH, '1' * 64, 5)], suite='s', component='c', arch='a'
    )
    # A listing that landed after the request started: the unverified record reports it to the caller.
    assert await apt_db.record_unverified(services.db, repo, DEB_PATH, DEB_SHA, len(DEB)) == apt_db.PoolRecord(
        DEB_PATH, '1' * 64, 5, True
    )
    assert await apt_db.record_unverified(services.db, repo, 'pool/x.deb', DEB_SHA, 1) is None
    assert await apt_db.record_unverified(services.db, repo, 'pool/x.deb', '2' * 64, 2) is None
    entry = await apt_db.lookup(services.db, repo, 'pool/x.deb')
    assert entry == apt_db.PoolRecord('pool/x.deb', '2' * 64, 2, False)
    assert await apt_db.record_listing(services.db, repo, [], suite='s', component='c', arch='a') == []


async def test_multiple_suites_in_one_repository(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: AptPlugin, repo: str
) -> None:
    main_index = packages((DEB_PATH, DEB))
    sec_index = packages(('pool/updates/main/o/openssl/libssl_3_amd64.deb', b'ssl'))
    publish(upstream, 'trixie', {'main/binary-amd64/Packages.xz': main_index})
    publish(upstream, 'trixie-security', {'main/binary-amd64/Packages.xz': sec_index})
    for suite, index in (('trixie', main_index), ('trixie-security', sec_index)):
        assert (await client.get(f'/repository/debian/dists/{suite}/InRelease')).status_code == 200
        response = await client.get(f'/repository/debian/dists/{suite}/main/binary-amd64/Packages.xz')
        assert blob_of(response) == sha(index)
    await plugin.drain()
    suites = {d['suite'] async for d in services.db[APT_POOL_INDEX].find({'repo_id': repo})}
    assert suites == {'trixie', 'trixie-security'}


async def test_other_paths_and_methods(client: httpx.AsyncClient, upstream: FakeUpstream, repo: str) -> None:
    upstream.routes['/project/trace/master'] = Respond(200, {}, b'Date: today\n')
    response = await client.get('/repository/debian/project/trace/master')
    assert response.status_code == 200
    assert (await client.put('/repository/debian/pool/x.deb', content=b'x')).status_code in (401, 403, 405)
    assert (await client.get('/repository/debian/')).status_code == 404
    assert (await client.get('/repository/debian/dists/nosuch/main/binary-amd64/Packages.xz')).status_code == 404


async def test_flat_repository(services: Services, client: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    await add_apt_repo(services, upstream.url, name='flat', flat=True)
    index = packages(('hello_1_amd64.deb', DEB))
    upstream.routes['/Release'] = Respond(200, {}, release({'Packages.xz': index}, by_hash=False))
    upstream.routes['/Packages.xz'] = Respond(200, {}, index)
    upstream.routes['/hello_1_amd64.deb'] = Respond(200, {}, DEB)
    assert (await client.get('/repository/flat/Release')).status_code == 200
    assert blob_of(await client.get('/repository/flat/Packages.xz')) == sha(index)
    plugin = services.formats.plugin('apt')
    assert isinstance(plugin, AptPlugin)
    await plugin.drain()
    assert (await client.get('/repository/flat/hello_1_amd64.deb')).content == DEB


async def test_multi_segment_suite(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: AptPlugin, repo: str
) -> None:
    """``deb <url> bookworm/mongodb-org/8.0 main``: the suite is three segments deep."""
    suite = 'bookworm/mongodb-org/8.0'
    base = f'dists/{suite}'
    arm_path = 'pool/main/h/hello/hello_2.10-5_arm64.deb'
    amd64, arm64 = packages((DEB_PATH, DEB)), packages((arm_path, b'arm'))
    contents = b'Contents of the suite\n'
    files = {'main/binary-amd64/Packages.xz': amd64, 'main/binary-arm64/Packages.xz': arm64, 'Contents-amd64': contents}
    publish(upstream, suite, files)
    upstream.routes[f'/{base}/by-hash/SHA256/{sha(contents)}'] = Respond(200, {}, contents)
    upstream.routes[f'/{DEB_PATH}'] = Respond(200, {}, DEB)
    assert (await client.get(f'/repository/debian/{base}/InRelease')).status_code == 200
    assert await services.db[ASSETS].find_one({'repo_id': repo, 'path': f'{base}/InRelease'}) is not None
    # Plain index path: served by the nested suite's Release hash, fetched by-hash from the nested directory.
    response = await client.get(f'/repository/debian/{base}/main/binary-amd64/Packages.xz')
    assert response.status_code == 200
    assert blob_of(response) == sha(amd64)
    key = f'{base}/main/binary-amd64/by-hash/SHA256/{sha(amd64)}'
    assert upstream.hits(f'/{key}') == 1
    assert await services.db[ASSETS].find_one({'repo_id': repo, 'path': key}) is not None
    # A by-hash path requested directly, and a suite-level index outside any component.
    arm_key = f'{base}/main/binary-arm64/by-hash/SHA256/{sha(arm64)}'
    assert (await client.get(f'/repository/debian/{arm_key}')).content == arm64
    assert blob_of(await client.get(f'/repository/debian/{base}/Contents-amd64')) == sha(contents)
    assert upstream.hits('/dists/bookworm/InRelease') == 0  # the one-segment prefix is never taken for the suite
    await plugin.drain()
    entry = await services.db[APT_POOL_INDEX].find_one({'repo_id': repo, 'filename': DEB_PATH})
    assert entry is not None
    assert (entry['sha256'], entry['verified']) == (DEB_SHA, True)
    assert (entry['suite'], entry['component'], entry['arch']) == (suite, 'main', 'amd64')
    arm_entry = await services.db[APT_POOL_INDEX].find_one({'repo_id': repo, 'filename': arm_path})
    assert arm_entry is not None
    assert (arm_entry['suite'], arm_entry['arch'], arm_entry['verified']) == (suite, 'arm64', True)
    # The pool file is verified against the nested suite's listing, then served as a cache hit.
    response = await client.get(f'/repository/debian/{DEB_PATH}')
    assert response.status_code == 200
    assert response.content == DEB
    assert (await client.get(f'/repository/debian/{DEB_PATH}')).headers['x-ws-cache'] == 'HIT'
    assert upstream.hits(f'/{DEB_PATH}') == 1
    again = await client.get(f'/repository/debian/{base}/main/binary-amd64/Packages.xz')
    assert again.headers['x-ws-cache'] == 'HIT'
    # Too deep a suite is refused without asking upstream.
    deep = '/'.join('abcdefghi')
    assert (await client.get(f'/repository/debian/dists/{deep}/InRelease')).status_code == 404
    assert upstream.hits(f'/dists/{deep}/InRelease') == 0


async def test_package_files_listed_under_dists(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, plugin: AptPlugin, repo: str
) -> None:
    """Some vendors keep the .debs next to the index: ``Filename: dists/<suite>/main/binary-amd64/x.deb``."""
    suite = 'bookworm/mongodb-org/8.0'
    directory = f'dists/{suite}/main/binary-amd64'
    good, bad, big, unlisted = (f'{directory}/{n}_1_amd64.deb' for n in ('good', 'bad', 'big', 'unlisted'))
    index = packages((good, DEB), (bad, b'the listed bytes'), (big, b'small'))
    publish(upstream, suite, {'main/binary-amd64/Packages.xz': index})
    for path, data in ((good, DEB), (bad, b'tampered bytes!!'), (big, b'small but larger'), (unlisted, b'x')):
        upstream.routes[f'/{path}'] = Respond(200, {}, data)
    assert (await client.get(f'/repository/debian/dists/{suite}/InRelease')).status_code == 200
    assert (await client.get(f'/repository/debian/{directory}/Packages.xz')).status_code == 200
    await plugin.drain()
    listed = await services.db[APT_POOL_INDEX].find_one({'repo_id': repo, 'filename': good})
    assert listed is not None
    assert (listed['verified'], listed['suite']) == (True, suite)
    assert await services.db[APT_PACKAGES].find_one({'repo_id': repo, 'filename': good}) is not None
    # Listed: verified against the listing, cached, then a cache hit served by X-Accel.
    response = await client.get(f'/repository/debian/{good}')
    assert response.status_code == 200
    assert response.content == DEB
    hit = await client.get(f'/repository/debian/{good}')
    assert hit.headers['x-ws-cache'] == 'HIT'
    assert blob_of(hit) == DEB_SHA
    assert upstream.hits(f'/{good}') == 1
    asset = await services.db[ASSETS].find_one({'repo_id': repo, 'path': good})
    assert asset is not None
    assert (asset['kind'], asset['blob_sha256']) == ('content', DEB_SHA)
    # Wrong hash: aborted mid-stream; larger than listed: refused before the first byte (502). Nothing cached.
    with pytest.raises(ResponseAborted):
        await client.get(f'/repository/debian/{bad}')
    assert (await client.get(f'/repository/debian/{big}')).status_code == 502
    for path in (bad, big):
        assert await services.db[ASSETS].find_one({'repo_id': repo, 'path': path}) is None
    # Not listed: served and recorded unverified, like a pool file.
    assert (await client.get(f'/repository/debian/{unlisted}')).content == b'x'
    entry = await services.db[APT_POOL_INDEX].find_one({'repo_id': repo, 'filename': unlisted})
    assert entry is not None
    assert (entry['sha256'], entry['verified']) == (sha(b'x'), False)
