"""Maven proxy, hosted and group repositories (T-P5-MAVEN) end to end on real MongoDB/Valkey through the ASGI app,
with a scriptable local upstream. Queries on the request path are explained (no collection scans)."""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from pymongo import monitoring
from redis.asyncio import Redis
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import asgi_client

from waystation_common.db.client import Database, create_client
from waystation_common.db.collections import ASSETS, AUDIT, COMPONENTS
from waystation_common.db.migrations import apply_migrations
from waystation_common.db.schema import ensure_schema
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.maven import metadata as md
from waystation_server.formats.maven.plugin import MavenPlugin
from waystation_server.problems import ResponseAborted
from waystation_server.proxy.http import Timeouts
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
JAR = bytes(range(256)) * 1200  # 307200 bytes: more than one 256 KiB chunk
POM = b'<project><modelVersion>4.0.0</modelVersion><groupId>org.example</groupId></project>\n'
GA = 'org/example/lib'
NOISE = {'lsid', 'txnNumber', '$db', '$clusterTime', '$readPreference', 'readConcern', 'apiVersion', 'cursor'}


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def ga_metadata(versions: list[str]) -> bytes:
    return md.render(md.versions_metadata('org.example', 'lib', versions, '20261101000000'))


# ---------------------------------------------------------------------------- query recording + explain
class Recorder(monitoring.CommandListener):
    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []
        self.on = False

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if self.on and event.command_name in ('find', 'aggregate'):
            command = dict(event.command)
            if command.get(event.command_name) in (ASSETS, COMPONENTS):
                self.commands.append({k: v for k, v in command.items() if k not in NOISE})

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        pass

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        pass


def _stages(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key != 'rejectedPlans':
                if key == 'stage' and isinstance(value, str):
                    out.append(value)
                _stages(value, out)
    elif isinstance(node, list):
        for item in node:
            _stages(item, out)


async def assert_indexed(db: Database, recorder: Recorder) -> None:
    assert recorder.commands
    for command in recorder.commands:
        if 'aggregate' in command:
            command = {**command, 'cursor': {}}
        plan = await db.command({'explain': command, 'verbosity': 'queryPlanner'})
        stages: list[str] = []
        _stages(plan, stages)
        assert 'COLLSCAN' not in stages, f'collection scan: {command}'
    recorder.commands.clear()


# ---------------------------------------------------------------------------- fixtures
@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000, ssrf_allow_cidrs=['127.0.0.0/8']
    )


@pytest.fixture
async def services(
    settings: Settings, mongo_db: Database, valkey: Redis, recorder: Recorder
) -> AsyncIterator[Services]:
    await ensure_schema(mongo_db)
    await apply_migrations(mongo_db)
    svc = Services.create(settings, mongo_client=create_client(settings, event_listeners=[recorder]))
    svc.upstream.timeouts = Timeouts(connect=2.0, read=2.0)
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


@pytest.fixture
async def anon(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(services) as client:
        yield client


@pytest.fixture
async def mvn(admin: httpx.AsyncClient, services: Services) -> AsyncIterator[httpx.AsyncClient]:
    """A Maven client: basic auth with the admin's username and an API token (settings.xml <server>)."""
    made = await admin.post('/api/v1/auth/me/tokens', json={'name': 'mvn', 'scopes': ['write', 'delete']})
    assert made.status_code == 201, made.text
    async with asgi_client(services) as client:
        client.auth = httpx.BasicAuth('admin', made.json()['secret'])
        yield client


def plugin(services: Services) -> MavenPlugin:
    found = services.formats.plugin('maven')
    assert isinstance(found, MavenPlugin)
    return found


async def create(admin: httpx.AsyncClient, name: str, repo_type: str, **body: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {'name': name, 'format': 'maven', 'type': repo_type, 'online': True, **body}
    response = await admin.post('/api/v1/repositories', json=payload)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def create_proxy(admin: httpx.AsyncClient, upstream: FakeUpstream, name: str = 'central', **options: Any) -> None:
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True, 'auto_block': {'enabled': False}}
    await create(admin, name, 'proxy', proxy=proxy, format_options=options)


def route_lib(upstream: FakeUpstream, *, jar: bytes = JAR, checksum: str | None = None) -> None:
    base = f'/{GA}/1.0/lib-1.0'
    upstream.routes[f'/{GA}/maven-metadata.xml'] = Respond(200, {'ETag': '"m1"'}, ga_metadata(['1.0']))
    upstream.routes[f'{base}.jar'] = Respond(200, {}, jar, chunk=65536)
    upstream.routes[f'{base}.jar.sha1'] = Respond(200, {}, (checksum or sha1(JAR)).encode() + b'  lib-1.0.jar\n')
    upstream.routes[f'{base}.pom'] = Respond(200, {}, POM)
    upstream.routes[f'{base}.pom.sha1'] = Respond(200, {}, sha1(POM).encode())
    upstream.routes[f'{base}.jar.asc'] = Respond(200, {}, b'-----BEGIN PGP SIGNATURE-----\n')


# ---------------------------------------------------------------------------- proxy
async def test_proxy_metadata_artifacts_and_checksums(
    admin: httpx.AsyncClient, anon: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    await create_proxy(admin, upstream)
    route_lib(upstream)
    meta = await anon.get(f'/repository/central/{GA}/maven-metadata.xml')
    assert meta.status_code == 200
    assert md.parse(services.blobs.blob_path(_sha(meta)).read_bytes()).versions == ['1.0']

    jar = await anon.get(f'/repository/central/{GA}/1.0/lib-1.0.jar')
    assert jar.status_code == 200 and jar.content == JAR  # a fill streams through (tee)
    assert jar.headers['content-type'] == 'application/java-archive'
    asset = await services.db[ASSETS].find_one({'path': f'{GA}/1.0/lib-1.0.jar'})
    assert asset is not None and asset['checksums']['sha1'] == sha1(JAR)
    assert upstream.hits(f'/{GA}/1.0/lib-1.0.jar.sha1') == 1

    # Checksum files of cached files come from the stored digests (md5 hashed from the blob), not the upstream.
    for alg in ('sha1', 'md5', 'sha256', 'sha512'):
        response = await anon.get(f'/repository/central/{GA}/1.0/lib-1.0.jar.{alg}')
        assert response.status_code == 200, alg
        assert response.text == hashlib.new(alg, JAR).hexdigest()
    assert upstream.hits(f'/{GA}/1.0/lib-1.0.jar.md5') == 0
    etag = (await anon.get(f'/repository/central/{GA}/1.0/lib-1.0.jar.sha1')).headers['etag']
    assert (
        await anon.get(f'/repository/central/{GA}/1.0/lib-1.0.jar.sha1', headers={'If-None-Match': etag})
    ).status_code == 304
    metasum = await anon.get(f'/repository/central/{GA}/maven-metadata.xml.sha1')
    assert metasum.text == sha1(ga_metadata(['1.0']))

    # A checksum of a file that is not cached is fetched from the upstream; signatures pass through.
    pom_sum = await anon.get(f'/repository/central/{GA}/1.0/lib-1.0.pom.sha1')
    assert pom_sum.status_code == 200 and 'x-accel-redirect' in pom_sum.headers
    assert services.blobs.blob_path(_sha(pom_sum)).read_bytes().strip() == sha1(POM).encode()
    asc = await anon.get(f'/repository/central/{GA}/1.0/lib-1.0.jar.asc')
    assert asc.status_code == 200 and asc.content.startswith(b'-----BEGIN PGP')

    await plugin(services).drain()
    component = await services.db[COMPONENTS].find_one({'format': 'maven', 'name': 'lib'})
    assert component is not None and component['namespace'] == 'org.example' and component['version'] == '1.0'


def _sha(response: httpx.Response) -> str:
    return response.headers['x-accel-redirect'].rsplit('/', 1)[-1]


async def test_proxy_refuses_mismatched_content_and_bad_metadata(
    admin: httpx.AsyncClient, anon: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    await create_proxy(admin, upstream)
    route_lib(upstream, checksum='0' * 40)
    with pytest.raises(ResponseAborted):  # the tee cuts the connection: never a complete wrong file
        await anon.get(f'/repository/central/{GA}/1.0/lib-1.0.jar')
    assert await services.db[ASSETS].find_one({'path': f'{GA}/1.0/lib-1.0.jar'}) is None
    upstream.routes[f'/{GA}/maven-metadata.xml'] = Respond(200, {}, b'<html>not metadata</html>')
    assert (await anon.get(f'/repository/central/{GA}/maven-metadata.xml')).status_code == 502
    entity = b'<!DOCTYPE m [<!ENTITY x "y">]><metadata>&x;</metadata>'
    upstream.routes['/org/other/maven-metadata.xml'] = Respond(200, {}, entity)
    assert (await anon.get('/repository/central/org/other/maven-metadata.xml')).status_code == 502
    upstream.routes[f'/{GA}/2.0/lib-2.0.jar.sha1'] = Respond(200, {}, b'<html>')
    upstream.routes[f'/{GA}/2.0/lib-2.0.jar'] = Respond(200, {}, JAR)
    assert (await anon.get(f'/repository/central/{GA}/2.0/lib-2.0.jar')).status_code == 502


async def test_proxy_without_upstream_checksums_and_policies(
    admin: httpx.AsyncClient, anon: httpx.AsyncClient, services: Services, upstream: FakeUpstream
) -> None:
    await create_proxy(admin, upstream)
    upstream.routes[f'/{GA}/3.0/lib-3.0.jar'] = Respond(200, {}, JAR)
    upstream.routes[f'/{GA}/3.0/lib-3.0.jar.sha256'] = Respond(200, {}, hashlib.sha256(JAR).hexdigest().encode())
    assert (await anon.get(f'/repository/central/{GA}/3.0/lib-3.0.jar')).content == JAR
    assert upstream.hits(f'/{GA}/3.0/lib-3.0.jar.sha1') == 1  # tried first, then .sha256
    assert upstream.hits(f'/{GA}/3.0/lib-3.0.jar.sha256') == 1
    upstream.routes[f'/{GA}/4.0/lib-4.0.jar'] = Respond(200, {}, JAR)
    assert (await anon.get(f'/repository/central/{GA}/4.0/lib-4.0.jar')).content == JAR  # unverified: stored
    assert (await anon.get(f'/repository/central/{GA}/4.0/lib-4.0.jar.sha1')).text == sha1(JAR)

    # Release policy: SNAPSHOT paths never reach the upstream; strict layout: other paths answer 404.
    before = len(upstream.seen)
    assert (await anon.get(f'/repository/central/{GA}/1.0-SNAPSHOT/maven-metadata.xml')).status_code == 404
    assert (await anon.get(f'/repository/central/{GA}/1.0-SNAPSHOT/lib-1.0-SNAPSHOT.jar')).status_code == 404
    assert (await anon.get('/repository/central/index.html')).status_code == 404
    assert (await anon.get('/repository/central/org/')).status_code == 404
    assert len(upstream.seen) == before
    denied = await anon.put(f'/repository/central/{GA}/1.0/lib-1.0.jar', content=b'x')
    assert denied.status_code == 401 and denied.headers['www-authenticate'].startswith('Basic')


# ---------------------------------------------------------------------------- hosted
async def deploy(client: httpx.AsyncClient, repo: str, path: str, data: bytes, *, sums: bool = True) -> None:
    response = await client.put(f'/repository/{repo}/{path}', content=data)
    assert response.status_code == 201, (path, response.status_code, response.text)
    if sums:
        for alg in ('sha1', 'md5'):
            digest = hashlib.new(alg, data).hexdigest().encode()
            response = await client.put(f'/repository/{repo}/{path}.{alg}', content=digest)
            assert response.status_code == 201, (path, alg, response.text)


async def test_hosted_release_deploy_metadata_and_redeploy(
    admin: httpx.AsyncClient,
    anon: httpx.AsyncClient,
    mvn: httpx.AsyncClient,
    services: Services,
    recorder: Recorder,
) -> None:
    await create(admin, 'releases', 'hosted', hosted={'write_policy': 'disable_redeploy'})
    recorder.on = True
    for version in ('1.0', '1.10', '1.2'):
        await deploy(mvn, 'releases', f'{GA}/{version}/lib-{version}.pom', POM)
        await deploy(mvn, 'releases', f'{GA}/{version}/lib-{version}.jar', JAR)
    client_doc = ga_metadata(['1.0'])  # what mvn uploads: discarded, the server regenerates
    assert (await mvn.put(f'/repository/releases/{GA}/maven-metadata.xml', content=client_doc)).status_code == 201
    assert (await mvn.put(f'/repository/releases/{GA}/maven-metadata.xml.sha1', content=b'0' * 40)).status_code == 201
    await assert_indexed(services.db, recorder)
    recorder.on = False

    meta = await mvn.get(f'/repository/releases/{GA}/maven-metadata.xml')
    assert meta.status_code == 200
    doc = md.parse(services.blobs.blob_path(_sha(meta)).read_bytes())
    assert doc.versions == ['1.0', '1.2', '1.10'] and doc.latest == '1.10' and doc.release == '1.10'
    metasum = await mvn.get(f'/repository/releases/{GA}/maven-metadata.xml.sha1')
    assert metasum.text == sha1(services.blobs.blob_path(_sha(meta)).read_bytes())
    jar = await mvn.get(f'/repository/releases/{GA}/1.2/lib-1.2.jar')
    assert jar.status_code == 200 and 'x-accel-redirect' in jar.headers
    assert (await mvn.get(f'/repository/releases/{GA}/1.2/lib-1.2.jar.sha512')).text == hashlib.sha512(JAR).hexdigest()

    # Redeploying a release file is refused (disable_redeploy); a wrong checksum is refused; anonymous may not write.
    again = await mvn.put(f'/repository/releases/{GA}/1.0/lib-1.0.jar', content=JAR)
    assert again.status_code == 400 and 'redeploy' in again.text
    bad = await mvn.put(f'/repository/releases/{GA}/1.0/lib-1.0.jar.sha1', content=b'f' * 40)
    assert bad.status_code == 400
    denied = await anon.put(f'/repository/releases/{GA}/9.0/lib-9.0.jar', content=JAR)
    assert denied.status_code == 404  # hidden from anonymous (ARCHITECTURE §7)
    snapshot = await mvn.put(f'/repository/releases/{GA}/2.0-SNAPSHOT/lib-2.0-SNAPSHOT.jar', content=JAR)
    assert snapshot.status_code == 400 and 'SNAPSHOT' in snapshot.text
    layout = await mvn.put('/repository/releases/files/readme.txt', content=b'x')
    assert layout.status_code == 400

    audits = await services.db[AUDIT].count_documents({'action': 'content.upload', 'target.kind': 'maven_file'})
    assert audits == 6

    # Browse: artifacts and versions in Maven order, files through the folder listing.
    recorder.on = True
    artifacts = (await admin.get('/api/v1/repositories/releases/maven/artifacts')).json()
    assert artifacts['items'] == [
        {
            'group_id': 'org.example',
            'artifact_id': 'lib',
            'latest': '1.10',
            'version_count': 3,
            'updated_at': artifacts['items'][0]['updated_at'],
        }
    ]
    versions = (
        await admin.get(
            '/api/v1/repositories/releases/maven/versions',
            params={'group_id': 'org.example', 'artifact_id': 'lib', 'limit': 2},
        )
    ).json()
    assert [v['version'] for v in versions['items']] == ['1.10', '1.2']
    assert versions['items'][0]['file_count'] == 2 and versions['items'][0]['size_bytes'] == len(JAR) + len(POM)
    assert versions['items'][0]['uploaded_by']
    rest = (
        await admin.get(
            '/api/v1/repositories/releases/maven/versions',
            params={'group_id': 'org.example', 'artifact_id': 'lib', 'cursor': versions['next_cursor']},
        )
    ).json()
    assert [v['version'] for v in rest['items']] == ['1.0'] and rest['next_cursor'] is None
    missing = await admin.get(
        '/api/v1/repositories/releases/maven/versions', params={'group_id': 'org.example', 'artifact_id': 'nope'}
    )
    assert missing.status_code == 404
    await assert_indexed(services.db, recorder)
    recorder.on = False

    # Delete a version folder: its files, component and metadata entry go.
    assert (await mvn.delete(f'/repository/releases/{GA}/1.2/')).status_code == 204
    meta = await mvn.get(f'/repository/releases/{GA}/maven-metadata.xml')
    assert md.parse(services.blobs.blob_path(_sha(meta)).read_bytes()).versions == ['1.0', '1.10']
    assert (await mvn.get(f'/repository/releases/{GA}/1.2/lib-1.2.jar')).status_code == 404
    assert (await mvn.delete(f'/repository/releases/{GA}/1.10/lib-1.10.jar')).status_code == 204
    assert (await mvn.delete(f'/repository/releases/{GA}/1.10/lib-1.10.pom')).status_code == 204
    meta = await mvn.get(f'/repository/releases/{GA}/maven-metadata.xml')
    assert md.parse(services.blobs.blob_path(_sha(meta)).read_bytes()).latest == '1.0'


async def test_hosted_snapshots_redeploy_and_version_metadata(
    admin: httpx.AsyncClient, anon: httpx.AsyncClient, mvn: httpx.AsyncClient, services: Services
) -> None:
    await create(admin, 'snapshots', 'hosted', format_options={'version_policy': 'snapshot'})
    folder = f'{GA}/1.0-SNAPSHOT'
    for build in (1, 2):
        stamp = f'20261108.12000{build}'
        await deploy(mvn, 'snapshots', f'{folder}/lib-1.0-{stamp}-{build}.pom', POM)
        await deploy(mvn, 'snapshots', f'{folder}/lib-1.0-{stamp}-{build}.jar', JAR + bytes([build]))
    await deploy(mvn, 'snapshots', f'{folder}/lib-1.0-20261108.120002-2-sources.jar', JAR)
    # Snapshots may always be redeployed.
    await deploy(mvn, 'snapshots', f'{folder}/lib-1.0-20261108.120002-2.jar', JAR + b'\x02', sums=False)
    release = await mvn.put(f'/repository/snapshots/{GA}/1.0/lib-1.0.jar', content=JAR)
    assert release.status_code == 400

    meta = await mvn.get(f'/repository/snapshots/{folder}/maven-metadata.xml')
    doc = md.parse(services.blobs.blob_path(_sha(meta)).read_bytes())
    assert (doc.snapshot_timestamp, doc.snapshot_build) == ('20261108.120002', 2)
    assert {(v.classifier, v.extension): v.value for v in doc.snapshot_versions} == {
        (None, 'jar'): '1.0-20261108.120002-2',
        (None, 'pom'): '1.0-20261108.120002-2',
        ('sources', 'jar'): '1.0-20261108.120002-2',
    }
    ga = await mvn.get(f'/repository/snapshots/{GA}/maven-metadata.xml')
    ga_doc = md.parse(services.blobs.blob_path(_sha(ga)).read_bytes())
    assert ga_doc.versions == ['1.0-SNAPSHOT'] and ga_doc.latest == '1.0-SNAPSHOT' and ga_doc.release is None


async def test_hosted_concurrent_deploys_lose_no_versions(
    admin: httpx.AsyncClient, anon: httpx.AsyncClient, mvn: httpx.AsyncClient, services: Services
) -> None:
    await create(admin, 'releases', 'hosted')
    versions = [f'1.{i}' for i in range(12)]
    await asyncio.gather(
        *(deploy(mvn, 'releases', f'{GA}/{v}/lib-{v}.pom', POM + v.encode(), sums=False) for v in versions)
    )
    meta = await mvn.get(f'/repository/releases/{GA}/maven-metadata.xml')
    doc = md.parse(services.blobs.blob_path(_sha(meta)).read_bytes())
    assert doc.versions == [f'1.{i}' for i in range(12)]
    assert doc.latest == '1.11'


async def test_hosted_read_only_and_permissive(
    admin: httpx.AsyncClient, mvn: httpx.AsyncClient, anon: httpx.AsyncClient
) -> None:
    await create(admin, 'frozen', 'hosted', hosted={'write_policy': 'read_only'})
    assert (await mvn.put(f'/repository/frozen/{GA}/1.0/lib-1.0.jar', content=JAR)).status_code == 403
    await create(admin, 'loose', 'hosted', format_options={'version_policy': 'mixed', 'layout_policy': 'permissive'})
    assert (await mvn.put('/repository/loose/files/readme.txt', content=b'hello')).status_code == 201
    assert (await mvn.get('/repository/loose/files/readme.txt')).status_code == 200
    assert (await mvn.put('/repository/loose/files/readme.txt', content=b'again')).status_code == 201
    assert (await mvn.put(f'/repository/loose/{GA}/1.0-SNAPSHOT/lib-1.0-SNAPSHOT.jar', content=JAR)).status_code == 201


# ---------------------------------------------------------------------------- group
async def test_group_resolves_members_in_order_and_merges_metadata(
    admin: httpx.AsyncClient,
    anon: httpx.AsyncClient,
    mvn: httpx.AsyncClient,
    services: Services,
    upstream: FakeUpstream,
) -> None:
    await create(admin, 'releases', 'hosted')
    await create_proxy(admin, upstream)
    await create(admin, 'public', 'group', group={'members': ['releases', 'central']})
    route_lib(upstream)
    await deploy(mvn, 'releases', f'{GA}/2.0/lib-2.0.jar', b'hosted jar', sums=False)

    hosted_jar = await mvn.get(f'/repository/public/{GA}/2.0/lib-2.0.jar')
    assert hosted_jar.status_code == 200 and upstream.hits(f'/{GA}/2.0/lib-2.0.jar') == 0
    proxied = await mvn.get(f'/repository/public/{GA}/1.0/lib-1.0.jar')
    assert proxied.status_code == 200 and proxied.content == JAR
    assert (await mvn.get(f'/repository/public/{GA}/1.0/lib-1.0.jar.sha1')).text == sha1(JAR)
    assert (await mvn.get(f'/repository/public/{GA}/7.0/lib-7.0.jar')).status_code == 404

    meta = await mvn.get(f'/repository/public/{GA}/maven-metadata.xml')
    assert meta.status_code == 200 and meta.headers['content-type'].startswith('text/xml')
    doc = md.parse(meta.content)
    assert doc.versions == ['1.0', '2.0'] and doc.latest == '2.0' and doc.release == '2.0'
    assert (await mvn.get(f'/repository/public/{GA}/maven-metadata.xml.sha1')).text == sha1(meta.content)
    conditional = await mvn.get(
        f'/repository/public/{GA}/maven-metadata.xml', headers={'If-None-Match': meta.headers['etag']}
    )
    assert conditional.status_code == 304
    assert (await mvn.get('/repository/public/org/none/maven-metadata.xml')).status_code == 404
    assert (await mvn.put(f'/repository/public/{GA}/3.0/lib-3.0.jar', content=JAR)).status_code == 405
