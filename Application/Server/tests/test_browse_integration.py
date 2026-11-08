"""Browse API (T-S09 M0 slice) end to end: repositories populated by pulling through a fake upstream, then browsed
through ``/api/v1``. Every MongoDB query the browse calls issue is explained afterwards and must use an index
(ARCHITECTURE §9)."""

import base64
import hashlib
import json
import lzma
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from pymongo import monitoring
from redis.asyncio import Redis
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import add_repo, asgi_client

from waystation_common.db.client import Database, create_client
from waystation_common.db.collections import (
    APT_PACKAGES,
    APT_POOL_INDEX,
    ASSETS,
    DOCKER_MANIFESTS,
    DOCKER_TAGS,
    NPM_PACKAGES,
    REPOSITORIES,
)
from waystation_common.db.migrations import apply_migrations
from waystation_common.db.schema import ensure_schema
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.services import Services

pytestmark = pytest.mark.integration

API = '/api/v1/repositories'
PASSWORD = 'correct horse battery staple'
WATCHED = {ASSETS, NPM_PACKAGES, APT_PACKAGES, APT_POOL_INDEX, DOCKER_TAGS, DOCKER_MANIFESTS}
NOISE = {'lsid', 'txnNumber', '$db', '$clusterTime', '$readPreference', 'readConcern', 'apiVersion', 'cursor'}


# ---------------------------------------------------------------------------- query recording + explain
class Recorder(monitoring.CommandListener):
    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []
        self.on = False

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if self.on and event.command_name in ('find', 'aggregate'):
            command = dict(event.command)
            if command.get(event.command_name) in WATCHED:
                self.commands.append({k: v for k, v in command.items() if k not in NOISE})

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        pass

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        pass


def _stages(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'rejectedPlans':
                continue
            if key == 'stage' and isinstance(value, str):
                out.append(value)
            _stages(value, out)
    elif isinstance(node, list):
        for item in node:
            _stages(item, out)


async def assert_indexed(db: Database, recorder: Recorder) -> None:
    """Explain every recorded browse query: none may scan a whole collection."""
    assert recorder.commands, 'no browse queries were recorded'
    for command in recorder.commands:
        if 'aggregate' in command:
            command = {**command, 'cursor': {}}
        plan = await db.command({'explain': command, 'verbosity': 'queryPlanner'})
        stages: list[str] = []
        _stages(plan, stages)
        assert 'COLLSCAN' not in stages, f'collection scan: {command}'
        assert any(s in stages for s in ('IXSCAN', 'COUNT_SCAN', 'DISTINCT_SCAN', 'EXPRESS_IXSCAN')), command
    recorder.commands.clear()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD))


@pytest.fixture
async def services(
    settings: Settings, mongo_db: Database, valkey: Redis, recorder: Recorder
) -> AsyncIterator[Services]:
    await ensure_schema(mongo_db)
    await apply_migrations(mongo_db)
    svc = Services.create(settings, mongo_client=create_client(settings, event_listeners=[recorder]))
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        yield fake


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


async def insert_repo(services: Services, name: str, fmt: str, url: str, **options: Any) -> str:
    now = datetime.now(UTC)
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': f'repo-{name}',
            'name': name,
            'format': fmt,
            'type': 'proxy',
            'online': True,
            'proxy': {'upstream_url': url, 'metadata_max_age_s': 3600},
            'format_options': options,
            'created_at': now,
            'updated_at': now,
        }
    )
    services.catalog.invalidate()
    return f'repo-{name}'


async def drain(services: Services, fmt: str) -> None:
    plugin = services.formats.plugin(fmt)
    await plugin.drain()  # type: ignore[union-attr]


async def pages(client: httpx.AsyncClient, url: str, key: str, **params: Any) -> list[dict[str, Any]]:
    """Every item of a paged listing, one page at a time."""
    items: list[dict[str, Any]] = []
    cursor = None
    for _ in range(50):
        response = await client.get(url, params={**params, **({'cursor': cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        body = response.json()
        items += body[key]
        cursor = body['next_cursor']
        if cursor is None:
            return items
    raise AssertionError('paging did not end')


# ---------------------------------------------------------------------------- generic tree (raw)
RAW_FILES = {
    'top.txt': b'top',
    'a-b.txt': b'dash',
    'a/two.txt': b'two!',
    'a/b/one.txt': b'one',
    'a/b/c/three.bin': b'\x00' * 33,
    'z/last.txt': b'z',
}


async def test_generic_tree_folders_first_paging_and_errors(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, recorder: Recorder
) -> None:
    await add_repo(services, upstream.url, name='files')
    for path, body in RAW_FILES.items():
        upstream.routes[f'/{path}'] = Respond(200, {'Last-Modified': 'Sat, 07 Nov 2026 10:00:00 GMT'}, body)
        assert (await client.get(f'/repository/files/{path}')).status_code == 200

    recorder.on = True
    root = await client.get(f'{API}/files/browse')
    assert root.status_code == 200
    body = root.json()
    assert body['path'] == ''
    assert body['next_cursor'] is None
    assert [(e['kind'], e['name']) for e in body['entries']] == [
        ('folder', 'a'),
        ('folder', 'z'),
        ('file', 'a-b.txt'),
        ('file', 'top.txt'),
    ]
    folder = body['entries'][0]
    assert folder['path'] == 'a'
    assert folder['size'] is None

    sub = (await client.get(f'{API}/files/browse', params={'path': '/a/'})).json()
    assert sub['path'] == 'a'
    assert [(e['kind'], e['path']) for e in sub['entries']] == [('folder', 'a/b'), ('file', 'a/two.txt')]
    two = sub['entries'][1]
    assert two['size'] == 4
    assert two['sha256'] == hashlib.sha256(b'two!').hexdigest()
    assert two['content_type'] == 'text/plain'
    assert two['last_modified'] == '2026-11-07T10:00:00Z'
    assert two['download_count'] is None

    everything = await pages(client, f'{API}/files/browse', 'entries', limit=1)
    assert [e['name'] for e in everything] == ['a', 'z', 'a-b.txt', 'top.txt']
    deep = await pages(client, f'{API}/files/browse', 'entries', path='a/b', limit=1)
    assert [e['path'] for e in deep] == ['a/b/c', 'a/b/one.txt']

    assert (await client.get(f'{API}/files/browse', params={'path': 'nope'})).status_code == 404
    assert (await client.get(f'{API}/files/browse', params={'path': 'top.txt'})).status_code == 404
    assert (await client.get(f'{API}/files/browse', params={'path': 'a/../../x'})).status_code == 400
    assert (await client.get(f'{API}/files/browse', params={'cursor': '!!'})).status_code == 400
    assert (await client.get(f'{API}/files/browse', params={'limit': 501})).status_code == 422
    await assert_indexed(services.db, recorder)


async def test_browse_rbac_and_empty_repository(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    await add_repo(services, upstream.url, name='empty')
    await add_repo(services, upstream.url, name='private', repo_type='hosted')
    # anonymous may browse proxy repositories; an empty one lists nothing
    empty = await client.get(f'{API}/empty/browse')
    assert empty.status_code == 200
    assert empty.json() == {'path': '', 'entries': [], 'next_cursor': None}
    # a repository anonymous cannot see is hidden: 404 on every browse route, never 403
    for suffix in ('browse', 'npm/packages', 'apt/suites', 'docker/images'):
        hidden = await client.get(f'{API}/private/{suffix}')
        assert hidden.status_code == 404, suffix
        assert hidden.headers['content-type'].startswith('application/problem+json')
    assert (await client.get(f'{API}/nosuchrepo/browse')).status_code == 404
    assert (await admin.get(f'{API}/private/browse')).status_code == 200
    # format views answer 404 on a repository of another format
    assert (await admin.get(f'{API}/empty/npm/packages')).status_code == 404
    assert (await admin.get(f'{API}/empty/apt/packages', params={'suite': 'x'})).status_code == 404
    assert (await admin.get(f'{API}/empty/docker/tags', params={'image': 'x'})).status_code == 404


# ---------------------------------------------------------------------------- npm
def sri(data: bytes) -> str:
    return 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()


TGZ = {'1.0.0': b'one' * 100, '2.0.0': b'two' * 200, '2.1.0-beta.1': b'beta' * 10}


def npm_packument(upstream: str, name: str, *, full: bool) -> bytes:
    filename = name.rsplit('/', 1)[-1]
    versions = {
        v: {
            'name': name,
            'version': v,
            'dist': {
                'tarball': f'{upstream}{name}/-/{filename}-{v}.tgz',
                'integrity': sri(data),
                'unpackedSize': len(data) * 3,
            },
            **({'deprecated': 'old'} if v == '1.0.0' else {}),
        }
        for v, data in TGZ.items()
    }
    doc: dict[str, Any] = {'name': name, 'dist-tags': {'latest': '2.0.0', 'beta': '2.1.0-beta.1'}, 'versions': versions}
    if full:
        doc |= {
            'description': f'The {name} package',
            'license': 'MIT',
            'time': {'modified': '2026-05-01T00:00:00.000Z', '2.0.0': '2026-04-01T12:00:00.000Z'},
        }
    else:
        doc['modified'] = '2026-05-01T00:00:00.000Z'
    return json.dumps(doc).encode()


def route_npm(upstream: FakeUpstream, name: str) -> None:
    encoded = name.replace('/', '%2F')
    full, abbreviated = npm_packument(upstream.url, name, full=True), npm_packument(upstream.url, name, full=False)

    def packument(seen: Any) -> Respond:
        short = 'install-v1' in seen.headers.get('accept', '')
        return Respond(200, {'Content-Type': 'application/json'}, abbreviated if short else full)

    upstream.routes[f'/{encoded}'] = packument
    filename = name.rsplit('/', 1)[-1]
    for version, data in TGZ.items():
        upstream.routes[f'/{name}/-/{filename}-{version}.tgz'] = Respond(200, {}, data)


async def test_npm_packages_and_versions(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, recorder: Recorder
) -> None:
    await insert_repo(services, 'npm', 'npm', upstream.url)
    for name in ('is-odd', '@scope/thing', 'zeta'):
        route_npm(upstream, name)
    abbreviated = {'Accept': 'application/vnd.npm.install-v1+json'}
    assert (await client.get('/repository/npm/is-odd', headers=abbreviated)).status_code == 200
    assert (await client.get('/repository/npm/is-odd/-/is-odd-2.0.0.tgz')).status_code == 200
    assert (await client.get('/repository/npm/is-odd/-/is-odd-1.0.0.tgz')).status_code == 200
    assert (await client.get('/repository/npm/@scope%2fthing')).status_code == 200  # full packument
    assert (await client.get('/repository/npm/zeta', headers=abbreviated)).status_code == 200
    await drain(services, 'npm')

    recorder.on = True
    listing = (await client.get(f'{API}/npm/npm/packages')).json()
    assert listing['next_cursor'] is None
    assert [p['name'] for p in listing['items']] == ['@scope/thing', 'is-odd', 'zeta']
    scoped, odd, _ = listing['items']
    assert scoped['description'] == 'The @scope/thing package'
    assert scoped['license'] == 'MIT'
    assert scoped['cached_versions'] == 0
    assert odd == {
        'name': 'is-odd',
        'description': None,  # npm fetched only the abbreviated packument
        'latest': '2.0.0',
        'version_count': 3,
        'cached_versions': 2,
        'size_bytes': len(TGZ['1.0.0']) + len(TGZ['2.0.0']),
        'license': None,
        'last_downloaded_at': None,
        'downloads': 0,
    }
    found = (await client.get(f'{API}/npm/npm/packages', params={'q': 'ODD'})).json()
    assert [p['name'] for p in found['items']] == ['is-odd']
    paged = await pages(client, f'{API}/npm/npm/packages', 'items', limit=2)
    assert [p['name'] for p in paged] == ['@scope/thing', 'is-odd', 'zeta']

    versions = (await client.get(f'{API}/npm/npm/versions', params={'package': 'is-odd'})).json()
    assert versions['dist_tags'] == {'latest': '2.0.0', 'beta': '2.1.0-beta.1'}
    assert versions['package']['cached_versions'] == 2
    assert [(v['version'], v['cached'], v['dist_tags']) for v in versions['items']] == [
        ('2.1.0-beta.1', False, ['beta']),
        ('2.0.0', True, ['latest']),
        ('1.0.0', True, []),
    ]
    beta, two, one = versions['items']
    assert two['size_bytes'] == len(TGZ['2.0.0'])
    assert beta['size_bytes'] is None
    assert two['unpacked_size'] == len(TGZ['2.0.0']) * 3
    assert two['integrity'] == sri(TGZ['2.0.0'])
    assert one['deprecated'] == 'old'
    assert two['published_at'] == '2026-05-01T00:00:00Z'  # abbreviated packument: its modified time

    full = (await client.get(f'{API}/npm/npm/versions', params={'package': '@scope/thing'})).json()
    assert full['items'][1]['published_at'] == '2026-04-01T12:00:00Z'
    assert full['package']['description'] == 'The @scope/thing package'
    paged_versions = await pages(client, f'{API}/npm/npm/versions', 'items', package='is-odd', limit=2)
    assert [v['version'] for v in paged_versions] == ['2.1.0-beta.1', '2.0.0', '1.0.0']

    assert (await client.get(f'{API}/npm/npm/versions', params={'package': 'missing'})).status_code == 404
    assert (await client.get(f'{API}/npm/npm/versions', params={'package': '../x'})).status_code == 404
    assert (await client.get(f'{API}/npm/npm/versions')).status_code == 422
    await assert_indexed(services.db, recorder)


# ---------------------------------------------------------------------------- apt
def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


DEBS = {
    'pool/main/h/hello/hello_2.10-5_amd64.deb': (b'hello-amd64' * 50, 'hello', '2.10-5', 'amd64', 'devel'),
    'pool/main/h/hello/hello_2.10-5_arm64.deb': (b'hello-arm64' * 50, 'hello', '2.10-5', 'arm64', 'devel'),
    'pool/main/b/base-files/base-files_14_all.deb': (b'base' * 70, 'base-files', '14', 'all', 'admin'),
    'pool/contrib/z/zed/zed_1_amd64.deb': (b'zed' * 10, 'zed', '1', 'amd64', 'contrib/misc'),
}


def packages_index(filenames: list[str]) -> bytes:
    stanzas = []
    for filename in filenames:
        data, name, version, arch, section = DEBS[filename]
        stanzas.append(
            f'Package: {name}\nVersion: {version}\nArchitecture: {arch}\nSection: {section}\n'
            f'Filename: {filename}\nSize: {len(data)}\nSHA256: {sha(data)}\n'
        )
    return lzma.compress('\n'.join(stanzas).encode())


def publish_suite(upstream: FakeUpstream, suite: str, files: dict[str, bytes]) -> None:
    lines = [
        'Origin: Debian',
        f'Suite: {suite}',
        f'Codename: {suite}',
        'Date: Sat, 07 Nov 2026 10:00:00 UTC',
        'Architectures: amd64 arm64',
        'Components: main contrib',
        'Acquire-By-Hash: yes',
        'SHA256:',
    ] + [f' {sha(data)} {len(data)} {path}' for path, data in files.items()]
    upstream.routes[f'/dists/{suite}/InRelease'] = Respond(200, {}, ('\n'.join(lines) + '\n').encode())
    for path, data in files.items():
        directory = path.rsplit('/', 1)[0]
        upstream.routes[f'/dists/{suite}/{directory}/by-hash/SHA256/{sha(data)}'] = Respond(200, {}, data)
    for filename, (data, *_) in DEBS.items():
        upstream.routes[f'/{filename}'] = Respond(200, {}, data)


async def test_apt_suites_and_packages(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, recorder: Recorder
) -> None:
    await insert_repo(services, 'debian', 'apt', upstream.url, distribution='trixie')
    amd64 = [
        'pool/main/h/hello/hello_2.10-5_amd64.deb',
        'pool/main/b/base-files/base-files_14_all.deb',
    ]
    arm64 = ['pool/main/h/hello/hello_2.10-5_arm64.deb', 'pool/main/b/base-files/base-files_14_all.deb']
    files = {
        'main/binary-amd64/Packages.xz': packages_index(amd64),
        'main/binary-arm64/Packages.xz': packages_index(arm64),
        'contrib/binary-amd64/Packages.xz': packages_index(['pool/contrib/z/zed/zed_1_amd64.deb']),
    }
    publish_suite(upstream, 'trixie', files)
    publish_suite(upstream, 'trixie-updates', {'main/binary-amd64/Packages.xz': packages_index([])})
    for suite, paths in (('trixie', list(files)), ('trixie-updates', ['main/binary-amd64/Packages.xz'])):
        assert (await client.get(f'/repository/debian/dists/{suite}/InRelease')).status_code == 200
        for path in paths:
            assert (await client.get(f'/repository/debian/dists/{suite}/{path}')).status_code == 200
    await drain(services, 'apt')
    for cached in ('pool/main/h/hello/hello_2.10-5_amd64.deb', 'pool/main/b/base-files/base-files_14_all.deb'):
        assert (await client.get(f'/repository/debian/{cached}')).status_code == 200

    recorder.on = True
    suites = (await client.get(f'{API}/debian/apt/suites')).json()
    assert [s['suite'] for s in suites] == ['trixie', 'trixie-updates']
    trixie = suites[0]
    assert trixie['origin'] == 'Debian'
    assert trixie['codename'] == 'trixie'
    assert trixie['components'] == ['main', 'contrib']
    assert trixie['architectures'] == ['amd64', 'arm64']
    assert trixie['release_date'] == '2026-11-07T10:00:00Z'
    assert trixie['last_refreshed_at'] is not None
    assert trixie['package_count'] == 5  # base-files is listed for amd64 and arm64
    assert trixie['cached_package_count'] == 3
    assert (suites[1]['package_count'], suites[1]['cached_package_count']) == (0, 0)

    url = f'{API}/debian/apt/packages'
    amd = (await client.get(url, params={'suite': 'trixie', 'arch': 'amd64'})).json()
    assert [(p['name'], p['component'], p['cached']) for p in amd['items']] == [
        ('base-files', 'main', True),
        ('hello', 'main', True),
        ('zed', 'contrib', False),
    ]
    base, hello, zed = amd['items']
    assert base['architecture'] == 'all'
    assert hello == {
        'name': 'hello',
        'version': '2.10-5',
        'architecture': 'amd64',
        'component': 'main',
        'section': 'devel',
        'size': len(DEBS['pool/main/h/hello/hello_2.10-5_amd64.deb'][0]),
        'filename': 'pool/main/h/hello/hello_2.10-5_amd64.deb',
        'sha256': sha(DEBS['pool/main/h/hello/hello_2.10-5_amd64.deb'][0]),
        'cached': True,
        'verified': True,
        'last_downloaded_at': None,
        'downloads': 0,
    }
    assert zed['verified'] is None
    arm = (await client.get(url, params={'suite': 'trixie', 'arch': 'arm64', 'cached': 'true'})).json()
    assert [p['filename'] for p in arm['items']] == ['pool/main/b/base-files/base-files_14_all.deb']
    contrib = (await client.get(url, params={'suite': 'trixie', 'component': 'contrib'})).json()
    assert [p['name'] for p in contrib['items']] == ['zed']
    searched = (await client.get(url, params={'suite': 'trixie', 'q': 'HELL'})).json()
    assert [(p['name'], p['architecture']) for p in searched['items']] == [('hello', 'amd64'), ('hello', 'arm64')]
    every = await pages(client, url, 'items', suite='trixie', limit=2)
    assert len(every) == 5
    assert len({(p['filename'], p['architecture']) for p in every}) == 4  # base-files twice (amd64 + arm64 index)
    cached_pages = await pages(client, url, 'items', suite='trixie', cached='true', limit=1)
    assert [p['name'] for p in cached_pages] == ['base-files', 'base-files', 'hello']
    assert (await client.get(url, params={'suite': 'nosuch'})).json() == {'items': [], 'next_cursor': None}
    assert (await client.get(url)).status_code == 422
    await assert_indexed(services.db, recorder)

    # a new generation of an index replaces its listing
    files['main/binary-amd64/Packages.xz'] = packages_index(['pool/main/h/hello/hello_2.10-5_amd64.deb'])
    publish_suite(upstream, 'trixie', files)
    await services.db[ASSETS].delete_one({'path': 'dists/trixie/InRelease'})
    assert (await client.get('/repository/debian/dists/trixie/InRelease')).status_code == 200
    assert (await client.get('/repository/debian/dists/trixie/main/binary-amd64/Packages.xz')).status_code == 200
    await drain(services, 'apt')
    amd = (await client.get(url, params={'suite': 'trixie', 'arch': 'amd64', 'component': 'main'})).json()
    assert [p['name'] for p in amd['items']] == ['hello']


# ---------------------------------------------------------------------------- docker
OCI_INDEX = 'application/vnd.oci.image.index.v1+json'
OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'


def digest(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def image(arch: str) -> tuple[bytes, bytes, bytes]:
    """(config, layer, manifest) of one platform image."""
    config = json.dumps(
        {
            'os': 'linux',
            'architecture': arch,
            'created': '2026-06-01T08:00:00Z',
            'config': {'Labels': {'org.opencontainers.image.version': '1.0'}},
            'history': [{'created_by': 'ENV X=1', 'empty_layer': True}, {'created_by': f'COPY {arch} /'}],
        }
    ).encode()
    layer = arch.encode() * 1000
    manifest = json.dumps(
        {
            'schemaVersion': 2,
            'mediaType': OCI_MANIFEST,
            'config': {
                'mediaType': 'application/vnd.oci.image.config.v1+json',
                'digest': digest(config),
                'size': len(config),
            },
            'layers': [
                {
                    'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip',
                    'digest': digest(layer),
                    'size': len(layer),
                }
            ],
        }
    ).encode()
    return config, layer, manifest


async def test_docker_images_tags_and_manifest(
    client: httpx.AsyncClient, services: Services, upstream: FakeUpstream, recorder: Recorder
) -> None:
    await insert_repo(services, 'hub', 'docker', upstream.url, docker_hub=True)
    amd_config, amd_layer, amd_manifest = image('amd64')
    _, _, arm_manifest = image('arm64')
    index = json.dumps(
        {
            'schemaVersion': 2,
            'mediaType': OCI_INDEX,
            'manifests': [
                {
                    'mediaType': OCI_MANIFEST,
                    'digest': digest(amd_manifest),
                    'size': len(amd_manifest),
                    'platform': {'os': 'linux', 'architecture': 'amd64'},
                },
                {
                    'mediaType': OCI_MANIFEST,
                    'digest': digest(arm_manifest),
                    'size': len(arm_manifest),
                    'platform': {'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'},
                },
                {
                    'mediaType': OCI_MANIFEST,
                    'digest': 'sha256:' + 'a' * 64,
                    'size': 10,
                    'platform': {'os': 'unknown', 'architecture': 'unknown'},
                },
            ],
        }
    ).encode()
    base = '/v2/library/web'
    upstream.routes['/v2/'] = Respond(200, {'Content-Type': 'application/json'}, b'{}')
    for tag in ('1.0', 'latest'):
        upstream.routes[f'{base}/manifests/{tag}'] = Respond(
            200, {'Content-Type': OCI_INDEX, 'Docker-Content-Digest': digest(index)}, index
        )
    upstream.routes[f'{base}/manifests/{digest(amd_manifest)}'] = Respond(
        200, {'Content-Type': OCI_MANIFEST, 'Docker-Content-Digest': digest(amd_manifest)}, amd_manifest
    )
    upstream.routes[f'{base}/blobs/{digest(amd_config)}'] = Respond(200, {}, amd_config)
    upstream.routes[f'{base}/blobs/{digest(amd_layer)}'] = Respond(200, {}, amd_layer)
    other_config, _, other_manifest = image('s390x')
    upstream.routes['/v2/acme/tool/manifests/v2'] = Respond(
        200, {'Content-Type': OCI_MANIFEST, 'Docker-Content-Digest': digest(other_manifest)}, other_manifest
    )
    upstream.routes[f'/v2/acme/tool/blobs/{digest(other_config)}'] = Respond(200, {}, other_config)

    pulls = [
        '/repository/hub/v2/web/manifests/1.0',
        f'/repository/hub/v2/web/manifests/{digest(amd_manifest)}',
        f'/repository/hub/v2/web/blobs/{digest(amd_config)}',
        f'/repository/hub/v2/web/blobs/{digest(amd_layer)}',
        '/repository/hub/v2/web/manifests/latest',
        '/repository/hub/v2/acme/tool/manifests/v2',
        f'/repository/hub/v2/acme/tool/blobs/{digest(other_config)}',
    ]
    for path in pulls:
        assert (await client.get(path)).status_code == 200, path
    await drain(services, 'docker')

    recorder.on = True
    images = (await client.get(f'{API}/hub/docker/images')).json()
    assert images['next_cursor'] is None
    assert [(i['name'], i['tag_count']) for i in images['items']] == [('acme/tool', 1), ('library/web', 2)]
    tool, web = images['items']
    assert web['size_bytes'] == len(amd_config) + len(amd_layer)  # what is cached of what its manifests reference
    assert tool['size_bytes'] == len(other_config)
    assert web['pulls'] == 0
    assert [i['name'] for i in (await client.get(f'{API}/hub/docker/images', params={'q': 'WEB'})).json()['items']] == [
        'library/web'
    ]
    assert [i['name'] for i in await pages(client, f'{API}/hub/docker/images', 'items', limit=1)] == [
        'acme/tool',
        'library/web',
    ]

    tags = (await client.get(f'{API}/hub/docker/tags', params={'image': 'library/web'})).json()
    assert tags['image'] == 'library/web'
    assert [t['tag'] for t in tags['items']] == ['latest', '1.0']  # most recently updated first
    latest = tags['items'][0]
    assert latest['digest'] == digest(index)
    assert latest['media_type'] == OCI_INDEX
    assert latest['platforms'] == [
        {'os': 'linux', 'architecture': 'amd64', 'variant': None},
        {'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'},
    ]
    assert latest['size_bytes'] == len(amd_config) + len(amd_layer)  # the recorded platform manifest
    assert latest['created_at'] == '2026-06-01T08:00:00Z'
    paged = await pages(client, f'{API}/hub/docker/tags', 'items', image='library/web', limit=1)
    assert [t['tag'] for t in paged] == ['latest', '1.0']
    single = (await client.get(f'{API}/hub/docker/tags', params={'image': 'acme/tool'})).json()['items'][0]
    assert single['platforms'] == [{'os': 'linux', 'architecture': 's390x', 'variant': None}]
    assert (await client.get(f'{API}/hub/docker/tags', params={'image': 'library/nope'})).status_code == 404

    manifest_url = f'{API}/hub/docker/manifest'
    detail = (await client.get(manifest_url, params={'image': 'library/web', 'reference': '1.0'})).json()
    assert detail['is_index'] is True
    assert detail['digest'] == digest(index)
    assert [m['digest'] for m in detail['manifests']] == [digest(amd_manifest), digest(arm_manifest)]
    assert detail['platform'] == {'os': 'linux', 'architecture': 'amd64', 'variant': None}
    assert detail['platform_digest'] == digest(amd_manifest)
    assert detail['config_digest'] == digest(amd_config)
    assert detail['layers'] == [
        {
            'digest': digest(amd_layer),
            'size': len(amd_layer),
            'media_type': 'application/vnd.oci.image.layer.v1.tar+gzip',
            'cached': True,
            'created_by': 'COPY amd64 /',
        }
    ]
    assert detail['total_size'] == len(amd_layer)
    assert detail['labels'] == {'org.opencontainers.image.version': '1.0'}
    assert detail['pull_by_tag'] == 'docker pull registry.example.com/hub/web:1.0'
    assert detail['pull_by_digest'] == f'docker pull registry.example.com/hub/web@{digest(index)}'
    # a platform whose manifest was never pulled: listed, no layers
    arm = (
        await client.get(
            manifest_url, params={'image': 'library/web', 'reference': '1.0', 'platform': 'linux/arm64/v8'}
        )
    ).json()
    assert arm['platform_digest'] == digest(arm_manifest)
    assert arm['layers'] == []
    by_digest = (
        await client.get(manifest_url, params={'image': 'library/web', 'reference': digest(amd_manifest)})
    ).json()
    assert by_digest['is_index'] is False
    assert by_digest['platform'] == {'os': 'linux', 'architecture': 'amd64', 'variant': None}
    assert by_digest['pull_by_tag'] == ''
    for params in ({'image': 'library/web', 'reference': 'nope'}, {'image': 'library/x', 'reference': '1.0'}):
        assert (await client.get(manifest_url, params=params)).status_code == 404
    await assert_indexed(services.db, recorder)
