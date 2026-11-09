"""Docker / OCI hosted repositories (T-P2-DOCKERH) against real MongoDB/Valkey through the ASGI app: uploads
(monolithic, chunked, resumable, mounts), manifest validation, tags and immutability, deletes, referrers, the catalog,
RBAC, audit and the browse API. Every query on the registry request path is explained (no collection scans)."""

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from pymongo import monitoring
from redis.asyncio import Redis
from ws_testing import asgi_client

from waystation_common.db.client import Database, create_client
from waystation_common.db.collections import ASSETS, AUDIT, DOCKER_MANIFESTS, DOCKER_TAGS, UPLOAD_SESSIONS, USERS
from waystation_common.db.migrations import apply_migrations
from waystation_common.db.schema import ensure_schema
from waystation_common.events import keys
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.security import passwords
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
SERVICE = 'registry.example.com'
OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
OCI_INDEX = 'application/vnd.oci.image.index.v1+json'
OCI_CONFIG = 'application/vnd.oci.image.config.v1+json'
LAYER_TYPE = 'application/vnd.oci.image.layer.v1.tar+gzip'
EMPTY = 'application/vnd.oci.empty.v1+json'
WATCHED = {ASSETS, DOCKER_TAGS, DOCKER_MANIFESTS, UPLOAD_SESSIONS}
NOISE = {'lsid', 'txnNumber', '$db', '$clusterTime', '$readPreference', 'readConcern', 'apiVersion', 'cursor'}

LAYER = bytes(range(256)) * 3000  # 768000 bytes
CONFIG = json.dumps({'architecture': 'amd64', 'os': 'linux', 'created': '2026-11-01T00:00:00Z'}).encode()
CONFIG_ARM = json.dumps({'architecture': 'arm64', 'os': 'linux'}).encode()


def digest(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def manifest(config: bytes = CONFIG, layers: tuple[bytes, ...] = (LAYER,), **extra: Any) -> bytes:
    return json.dumps(
        {
            'schemaVersion': 2,
            'mediaType': OCI_MANIFEST,
            'config': {'mediaType': OCI_CONFIG, 'digest': digest(config), 'size': len(config)},
            'layers': [{'mediaType': LAYER_TYPE, 'digest': digest(x), 'size': len(x)} for x in layers],
            **extra,
        }
    ).encode()


# ---------------------------------------------------------------------------- query recording + explain
class Recorder(monitoring.CommandListener):
    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []
        self.on = False

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if self.on and event.command_name in ('find', 'aggregate', 'findAndModify', 'update', 'delete'):
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
    assert recorder.commands
    for command in recorder.commands:
        if 'aggregate' in command:
            command = {**command, 'cursor': {}}
        if 'update' in command or 'delete' in command:
            continue  # by _id or by the same filters as the finds
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
    return settings_factory(bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000)


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


async def add_user(services: Services, username: str, roles: list[str]) -> None:
    await services.db[USERS].insert_one(
        {
            '_id': f'id-{username}',
            'username': username,
            'display_name': username.title(),
            'source': 'local',
            'password_hash': await passwords.hash_password(PASSWORD),
            'roles': roles,
            'created_at': datetime.now(UTC),
        }
    )


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


@pytest.fixture
async def v2(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    """A registry client: no session cookie (a signed-in browser session would take precedence)."""
    async with asgi_client(services) as client:
        yield client


async def create_hosted(admin: httpx.AsyncClient, name: str, **hosted: Any) -> dict[str, Any]:
    body: dict[str, Any] = {'name': name, 'format': 'docker', 'type': 'hosted', 'online': True}
    if hosted:
        body['hosted'] = hosted
    response = await admin.post('/api/v1/repositories', json=body)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


class Registry:
    """A docker client's view of the registry: tokens per scope, then requests with them."""

    def __init__(self, client: httpx.AsyncClient, auth: tuple[str, str] | None = ('admin', PASSWORD)) -> None:
        self.client = client
        self.auth = auth

    async def token(self, *scopes: str) -> dict[str, str]:
        params = [('service', SERVICE), *(('scope', s) for s in scopes)]
        issued = await self.client.get('/auth/token', params=params, auth=self.auth)
        assert issued.status_code == 200, issued.text
        return {'Authorization': f'Bearer {issued.json()["token"]}'}

    async def request(
        self,
        method: str,
        name: str,
        suffix: str,
        *,
        actions: str = 'pull,push,delete',
        extra: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> httpx.Response:
        headers = {**await self.token(f'repository:{name}:{actions}', *extra), **kwargs.pop('headers', {})}
        return await self.client.request(method, f'/v2/{name}/{suffix}', headers=headers, **kwargs)

    async def push_blob(self, name: str, data: bytes) -> httpx.Response:
        response = await self.request('POST', name, 'blobs/uploads/', params={'digest': digest(data)}, content=data)
        assert response.status_code == 201, response.text
        return response

    async def push_manifest(self, name: str, ref: str, body: bytes, media_type: str = OCI_MANIFEST) -> httpx.Response:
        return await self.request('PUT', name, f'manifests/{ref}', content=body, headers={'Content-Type': media_type})

    async def push_image(self, name: str, tag: str, config: bytes = CONFIG, layer: bytes = LAYER) -> str:
        await self.push_blob(name, layer)
        await self.push_blob(name, config)
        body = manifest(config, (layer,))
        response = await self.push_manifest(name, tag, body)
        assert response.status_code == 201, response.text
        return digest(body)


# ---------------------------------------------------------------------------- tests
async def test_create_hosted_repository_validation(admin: httpx.AsyncClient) -> None:
    repo = await create_hosted(admin, 'apps', write_policy='disable_redeploy', immutable_tag_patterns=['v.*'])
    assert repo['type'] == 'hosted'
    assert repo['hosted'] == {'write_policy': 'disable_redeploy', 'immutable_tag_patterns': ['v.*']}
    assert repo['proxy'] is None
    defaults = await create_hosted(admin, 'scratch')
    assert defaults['hosted'] == {'write_policy': 'allow_redeploy', 'immutable_tag_patterns': []}
    bad = await admin.post(
        '/api/v1/repositories',
        json={
            'name': 'broken',
            'format': 'docker',
            'type': 'hosted',
            'online': True,
            'hosted': {'immutable_tag_patterns': ['v[']},
            'format_options': {'aliases': ['docker.io'], 'docker_hub': True},
        },
    )
    assert bad.status_code == 422
    fields = {e['field'] for e in bad.json()['errors']}
    assert fields == {'hosted.immutable_tag_patterns.0', 'format_options.aliases', 'format_options.docker_hub'}
    proxy = await admin.post(
        '/api/v1/repositories',
        json={
            'name': 'hub',
            'format': 'docker',
            'type': 'proxy',
            'online': True,
            'proxy': {'upstream_url': 'https://registry-1.docker.io'},
            'hosted': {'write_policy': 'read_only'},
        },
    )
    assert proxy.status_code == 422
    assert [e['field'] for e in proxy.json()['errors']] == ['hosted']
    setup = (await admin.get('/api/v1/repositories/apps/setup')).json()
    snippets = {s['id']: s['content'] for c in setup['clients'] for s in c['snippets']}
    assert 'docker push registry.example.com/apps/myapp:1.0' in snippets['docker-push']
    assert '--platform linux/amd64,linux/arm64' in snippets['docker-buildx']


async def test_push_pull_and_browse(
    services: Services, admin: httpx.AsyncClient, v2: httpx.AsyncClient, recorder: Recorder
) -> None:
    await create_hosted(admin, 'apps')
    reg = Registry(v2)
    name = 'apps/team/web'
    recorder.on = True

    # chunked upload: POST, PATCH x2 (Content-Range), status, PUT ?digest
    start = await reg.request('POST', name, 'blobs/uploads/')
    assert start.status_code == 202
    location = start.headers['location']
    assert location.startswith('/v2/apps/team/web/blobs/uploads/')
    assert start.headers['range'] == '0-0'
    session = start.headers['docker-upload-uuid']
    assert location.endswith(session)
    half = len(LAYER) // 2
    first = await reg.client.patch(
        location,
        content=LAYER[:half],
        headers={'Content-Range': f'0-{half - 1}', **await reg.token(f'repository:{name}:pull,push')},
    )
    assert first.status_code == 202, first.text
    assert first.headers['range'] == f'0-{half - 1}'
    # a chunk that does not start where the upload ends: 416 with the current range
    wrong = await reg.client.patch(
        location, content=b'x', headers={'Content-Range': '5-5', **await reg.token(f'repository:{name}:pull,push')}
    )
    assert wrong.status_code == 416
    assert wrong.headers['range'] == f'0-{half - 1}'
    status = await reg.client.get(location, headers=await reg.token(f'repository:{name}:pull'))
    assert status.status_code == 204
    assert status.headers['range'] == f'0-{half - 1}'
    second = await reg.client.patch(
        location,
        content=LAYER[half:],
        headers={'Content-Range': f'{half}-{len(LAYER) - 1}', **await reg.token(f'repository:{name}:pull,push')},
    )
    assert second.status_code == 202
    done = await reg.client.put(
        location, params={'digest': digest(LAYER)}, headers=await reg.token(f'repository:{name}:pull,push')
    )
    assert done.status_code == 201, done.text
    assert done.headers['location'] == f'/v2/{name}/blobs/{digest(LAYER)}'
    assert done.headers['docker-content-digest'] == digest(LAYER)
    recorder.on = False
    assert await services.db[UPLOAD_SESSIONS].count_documents({}) == 0
    recorder.on = True

    # monolithic upload of the config; a wrong digest is refused and stores nothing
    await reg.push_blob(name, CONFIG)
    bad = await reg.request('POST', name, 'blobs/uploads/', params={'digest': digest(b'other')}, content=b'nope')
    assert bad.status_code == 400
    assert bad.json()['errors'][0]['code'] == 'DIGEST_INVALID'

    head = await reg.request('HEAD', name, f'blobs/{digest(LAYER)}', actions='pull')
    assert head.status_code == 200
    assert head.headers['content-length'] == str(len(LAYER))
    assert head.headers['docker-content-digest'] == digest(LAYER)
    got = await reg.request('GET', name, f'blobs/{digest(LAYER)}', actions='pull')
    assert got.headers['x-accel-redirect'].endswith(digest(LAYER).split(':')[1])
    assert got.content == b''  # served by nginx, never through Python
    assert services.blobs.blob_path(digest(LAYER).split(':')[1]).read_bytes() == LAYER

    # manifest: missing blob refused, then pushed by tag
    missing = await reg.push_manifest(name, 'v1', manifest(CONFIG, (LAYER, b'not pushed')))
    assert missing.status_code == 400
    assert missing.json()['errors'][0]['code'] == 'MANIFEST_BLOB_UNKNOWN'
    lying = json.loads(manifest())
    lying['layers'][0]['size'] = 1
    wrong_size = await reg.push_manifest(name, 'v1', json.dumps(lying).encode())
    assert wrong_size.json()['errors'][0]['code'] == 'MANIFEST_INVALID'
    mismatch = await reg.push_manifest(name, 'v1', manifest(), media_type=OCI_INDEX)
    assert mismatch.json()['errors'][0]['code'] == 'MANIFEST_INVALID'
    body = manifest()
    pushed = await reg.push_manifest(name, 'v1', body)
    assert pushed.status_code == 201, pushed.text
    assert pushed.headers['docker-content-digest'] == digest(body)
    assert pushed.headers['location'] == f'/v2/{name}/manifests/{digest(body)}'
    by_tag = await reg.request('GET', name, 'manifests/v1', actions='pull')
    assert by_tag.status_code == 200
    assert by_tag.headers['content-type'] == OCI_MANIFEST
    assert by_tag.headers['docker-content-digest'] == digest(body)
    by_digest = await reg.request('HEAD', name, f'manifests/{digest(body)}', actions='pull')
    assert by_digest.headers['content-length'] == str(len(body))
    wrong_digest = await reg.push_manifest(name, digest(b'x'), body)
    assert wrong_digest.json()['errors'][0]['code'] == 'DIGEST_INVALID'

    # a multi-platform index over two image manifests
    arm = manifest(CONFIG_ARM, (LAYER,))
    await reg.push_blob(name, CONFIG_ARM)
    assert (await reg.push_manifest(name, digest(arm), arm)).status_code == 201
    entries = [
        {
            'mediaType': OCI_MANIFEST,
            'digest': digest(body),
            'size': len(body),
            'platform': {'os': 'linux', 'architecture': 'amd64'},
        },
        {
            'mediaType': OCI_MANIFEST,
            'digest': digest(arm),
            'size': len(arm),
            'platform': {'os': 'linux', 'architecture': 'arm64'},
        },
    ]
    index = json.dumps({'schemaVersion': 2, 'mediaType': OCI_INDEX, 'manifests': entries}).encode()
    orphan = json.dumps(
        {'schemaVersion': 2, 'mediaType': OCI_INDEX, 'manifests': [{**entries[0], 'digest': digest(b'?')}]}
    ).encode()
    assert (await reg.push_manifest(name, 'multi', orphan, OCI_INDEX)).json()['errors'][0]['code'] == 'MANIFEST_UNKNOWN'
    assert (await reg.push_manifest(name, 'multi', index, OCI_INDEX)).status_code == 201
    assert (await reg.push_manifest(name, 'latest', index, OCI_INDEX)).status_code == 201

    # tags/list with pagination
    tags = await reg.request('GET', name, 'tags/list', actions='pull')
    assert tags.json() == {'name': name, 'tags': ['latest', 'multi', 'v1']}
    page = await reg.request('GET', name, 'tags/list', actions='pull', params={'n': 2})
    assert page.json()['tags'] == ['latest', 'multi']
    assert page.headers['link'] == f'</v2/{name}/tags/list?n=2&last=multi>; rel="next"'
    rest = await reg.request('GET', name, 'tags/list', actions='pull', params={'n': 2, 'last': 'multi'})
    assert rest.json()['tags'] == ['v1']
    assert 'link' not in rest.headers
    unknown = await reg.request('GET', 'apps/nothing', 'tags/list', actions='pull')
    assert unknown.status_code == 404
    await assert_indexed(services.db, recorder)
    recorder.on = False

    # browse API: the pushed image, its tags and the index's platforms
    images = (await admin.get('/api/v1/repositories/apps/docker/images')).json()['items']
    assert [i['name'] for i in images] == ['team/web']
    tag_page = (await admin.get('/api/v1/repositories/apps/docker/tags', params={'image': 'team/web'})).json()
    assert {t['tag'] for t in tag_page['items']} == {'latest', 'multi', 'v1'}
    detail = (
        await admin.get('/api/v1/repositories/apps/docker/manifest', params={'image': 'team/web', 'reference': 'multi'})
    ).json()
    assert detail['is_index'] is True
    assert {m['platform']['architecture'] for m in detail['manifests']} == {'amd64', 'arm64'}
    tag_doc = await services.db[DOCKER_TAGS].find_one({'tag': 'v1'})
    assert tag_doc is not None and tag_doc['pushed_by'] == 'admin'

    # audit + activity
    await services.audit.flush()
    uploads = [d async for d in services.db[AUDIT].find({'action': 'content.upload'})]
    assert {d['details']['tag'] for d in uploads} >= {'v1', 'multi', 'latest'}
    assert uploads[0]['principal']['name'] == 'admin'
    await services.emitter.flush()
    entries_raw = await services.valkey.xrange(keys.ACTIVITY)
    types = [fields['type'] for _, fields in entries_raw]
    assert 'asset.created' in types
    finished = [json.loads(f['data']) for _, f in entries_raw if f['type'] == 'transfer.finished']
    uploaded = [e for e in finished if e['detail'].get('direction') == 'upload']
    assert [e['bytes'] for e in uploaded][:2] == [half, len(LAYER) - half]  # the two PATCH chunks


async def test_immutability_policies_and_deletes(
    services: Services, admin: httpx.AsyncClient, v2: httpx.AsyncClient
) -> None:
    await create_hosted(admin, 'releases', immutable_tag_patterns=[r'v\d+(\.\d+)*'])
    reg = Registry(v2)
    name = 'releases/app'
    first = await reg.push_image(name, 'v1.0')
    other_layer = b'second build' * 1000
    await reg.push_blob(name, other_layer)
    second_body = manifest(CONFIG, (other_layer,))
    moved = await reg.push_manifest(name, 'v1.0', second_body)
    assert moved.status_code == 403
    assert moved.json()['errors'][0]['code'] == 'DENIED'
    assert (await reg.push_manifest(name, 'v1.0', manifest())).status_code == 201  # same digest: fine
    assert (await reg.push_manifest(name, 'latest', manifest())).status_code == 201
    assert (await reg.push_manifest(name, 'latest', second_body)).status_code == 201  # mutable tag moves
    second = digest(second_body)

    # deletes: immutable tag refused, by tag, by digest, blobs only when unreferenced
    refused = await reg.request('DELETE', name, 'manifests/v1.0')
    assert refused.status_code == 403
    refused_digest = await reg.request('DELETE', name, f'manifests/{first}')
    assert refused_digest.status_code == 403
    assert (await reg.request('DELETE', name, 'manifests/latest')).status_code == 202
    assert (await reg.request('GET', name, 'manifests/latest', actions='pull')).status_code == 404
    in_use = await reg.request('DELETE', name, f'blobs/{digest(other_layer)}')
    assert in_use.status_code == 405
    assert (await reg.request('DELETE', name, f'manifests/{second}')).status_code == 202
    assert (await reg.request('HEAD', name, f'manifests/{second}', actions='pull')).status_code == 404
    assert (await reg.request('DELETE', name, f'blobs/{digest(other_layer)}')).status_code == 202
    assert (await reg.request('HEAD', name, f'blobs/{digest(other_layer)}', actions='pull')).status_code == 404
    assert (await reg.request('DELETE', name, f'blobs/{digest(other_layer)}')).status_code == 404
    await services.audit.flush()
    deletes = [d async for d in services.db[AUDIT].find({'action': 'content.delete'})]
    assert {d['target']['kind'] for d in deletes} == {'docker_tag', 'docker_manifest', 'docker_blob'}

    # disable_redeploy: no tag moves or goes away
    await create_hosted(admin, 'frozen', write_policy='disable_redeploy')
    await reg.push_image('frozen/app', 'latest')
    await reg.push_blob('frozen/app', other_layer)
    assert (await reg.push_manifest('frozen/app', 'latest', second_body)).status_code == 403
    assert (await reg.request('DELETE', 'frozen/app', 'manifests/latest')).status_code == 403

    # read_only: nothing is accepted
    await create_hosted(admin, 'readonly', write_policy='read_only')
    blocked = await reg.request('POST', 'readonly/app', 'blobs/uploads/')
    assert blocked.status_code == 403
    assert blocked.json()['errors'][0]['code'] == 'DENIED'


async def test_referrers(services: Services, admin: httpx.AsyncClient, v2: httpx.AsyncClient) -> None:
    await create_hosted(admin, 'apps')
    reg = Registry(v2)
    name = 'apps/signed'
    subject = await reg.push_image(name, 'v1')
    signature = b'signature-bytes'
    await reg.push_blob(name, signature)
    referrer = json.dumps(
        {
            'schemaVersion': 2,
            'mediaType': OCI_MANIFEST,
            'artifactType': 'application/vnd.example.signature',
            'config': {'mediaType': EMPTY, 'digest': digest(b'{}'), 'size': 2},
            'layers': [{'mediaType': 'application/octet-stream', 'digest': digest(signature), 'size': len(signature)}],
            'subject': {'mediaType': OCI_MANIFEST, 'digest': subject, 'size': 100},
            'annotations': {'org.example.kind': 'test'},
        }
    ).encode()
    pushed = await reg.push_manifest(name, digest(referrer), referrer)
    assert pushed.status_code == 201, pushed.text  # the empty config blob is implied
    assert pushed.headers['oci-subject'] == subject
    sbom_layer = b'{"sbom": true}'
    await reg.push_blob(name, sbom_layer)
    sbom = json.dumps(
        {
            'schemaVersion': 2,
            'mediaType': OCI_MANIFEST,
            'config': {'mediaType': 'application/vnd.example.sbom.config', 'digest': digest(b'{}'), 'size': 2},
            'layers': [{'mediaType': 'application/json', 'digest': digest(sbom_layer), 'size': len(sbom_layer)}],
            'subject': {'mediaType': OCI_MANIFEST, 'digest': subject, 'size': 100},
        }
    ).encode()
    assert (await reg.push_manifest(name, digest(sbom), sbom)).status_code == 201

    listed = await reg.request('GET', name, f'referrers/{subject}', actions='pull')
    assert listed.status_code == 200
    assert listed.headers['content-type'] == OCI_INDEX
    body = listed.json()
    assert body['schemaVersion'] == 2 and body['mediaType'] == OCI_INDEX
    by_digest = {m['digest']: m for m in body['manifests']}
    assert by_digest[digest(referrer)] == {
        'mediaType': OCI_MANIFEST,
        'digest': digest(referrer),
        'size': len(referrer),
        'artifactType': 'application/vnd.example.signature',
        'annotations': {'org.example.kind': 'test'},
    }
    assert by_digest[digest(sbom)]['artifactType'] == 'application/vnd.example.sbom.config'  # config fallback
    filtered = await reg.request(
        'GET',
        name,
        f'referrers/{subject}',
        actions='pull',
        params={'artifactType': 'application/vnd.example.signature'},
    )
    assert [m['digest'] for m in filtered.json()['manifests']] == [digest(referrer)]
    assert filtered.headers['oci-filters-applied'] == 'artifactType'
    none = await reg.request('GET', name, f'referrers/{digest(b"nothing")}', actions='pull')
    assert none.status_code == 200 and none.json()['manifests'] == []


async def test_mount_catalog_and_rbac(services: Services, admin: httpx.AsyncClient, v2: httpx.AsyncClient) -> None:
    await create_hosted(admin, 'source')
    await create_hosted(admin, 'target')
    await create_hosted(admin, 'secret')
    reg = Registry(v2)
    await reg.push_image('source/base', 'v1')
    await reg.push_image('secret/hidden', 'v1')

    # mount: the token carries pull on the source; the blob is linked, no bytes sent
    mounted = await reg.request(
        'POST',
        'target/app',
        'blobs/uploads/',
        params={'mount': digest(LAYER), 'from': 'source/base'},
        extra=('repository:source/base:pull',),
    )
    assert mounted.status_code == 201, mounted.text
    assert mounted.headers['location'] == f'/v2/target/app/blobs/{digest(LAYER)}'
    assert (await reg.request('HEAD', 'target/app', f'blobs/{digest(LAYER)}', actions='pull')).status_code == 200
    # without pull on the source the mount falls back to an upload session
    fallback = await reg.request(
        'POST', 'target/app', 'blobs/uploads/', params={'mount': digest(CONFIG), 'from': 'source/base'}
    )
    assert fallback.status_code == 202
    assert fallback.headers['location'].startswith('/v2/target/app/blobs/uploads/')
    cancel = await reg.client.delete(
        fallback.headers['location'], headers=await reg.token('repository:target/app:pull,push,delete')
    )
    assert cancel.status_code == 204
    gone = await reg.client.get(fallback.headers['location'], headers=await reg.token('repository:target/app:pull'))
    assert gone.status_code == 404
    assert gone.json()['errors'][0]['code'] == 'BLOB_UPLOAD_UNKNOWN'

    # a reader may pull but a push asks for a scope it cannot get; a deployer may push but not delete
    await add_user(services, 'rita', ['reader'])
    await add_user(services, 'dave', ['deployer'])
    reader = Registry(v2, ('rita', PASSWORD))
    assert (await reader.request('GET', 'source/base', 'manifests/v1', actions='pull')).status_code == 200
    denied = await reader.request('POST', 'source/base', 'blobs/uploads/')
    assert denied.status_code == 401
    assert 'insufficient_scope' in denied.headers['www-authenticate']
    deployer = Registry(v2, ('dave', PASSWORD))
    await deployer.push_image('target/app', 'dave-1')
    no_delete = await deployer.request('DELETE', 'target/app', 'manifests/dave-1')
    assert no_delete.status_code == 401

    # catalog: admin sees every tagged image, anonymous sees none of the hosted ones
    catalog_token = await reg.token('registry:catalog:*')
    listed = await v2.get('/v2/_catalog', headers=catalog_token)
    assert listed.json() == {'repositories': ['secret/hidden', 'source/base', 'target/app']}
    paged = await v2.get('/v2/_catalog', headers=catalog_token, params={'n': 1, 'last': 'secret/hidden'})
    assert paged.json() == {'repositories': ['source/base']}
    assert paged.headers['link'] == '</v2/_catalog?n=1&last=source%2Fbase>; rel="next"'
    anon = Registry(v2, None)
    anonymous = await v2.get('/v2/_catalog', headers=await anon.token('registry:catalog:*'))
    assert anonymous.json() == {'repositories': []}


async def test_offset_recovery_and_limits(services: Services, admin: httpx.AsyncClient, v2: httpx.AsyncClient) -> None:
    await create_hosted(admin, 'apps')
    reg = Registry(v2)
    name = 'apps/big'
    start = await reg.request('POST', name, 'blobs/uploads/')
    location = start.headers['location']
    auth = await reg.token(f'repository:{name}:pull,push')
    # streamed PATCH without Content-Range appends at the current offset
    assert (await v2.patch(location, content=LAYER[:1000], headers=auth)).status_code == 202
    session_id = start.headers['docker-upload-uuid']
    # a crashed request left unacknowledged bytes: the next one cuts them off
    path = services.blobs.upload_path(session_id)
    with path.open('ab') as fh:
        fh.write(b'garbage')
    resumed = await v2.patch(
        location, content=LAYER[1000:], headers={'Content-Range': f'1000-{len(LAYER) - 1}', **auth}
    )
    assert resumed.status_code == 202, resumed.text
    done = await v2.put(location, params={'digest': digest(LAYER)}, headers=auth)
    assert done.status_code == 201, done.text
    # a manifest over 4 MiB is refused before it is read
    huge = await reg.request(
        'PUT',
        name,
        'manifests/big',
        content=b'{' + b' ' * (4 * 1024 * 1024) + b'}',
        headers={'Content-Type': OCI_MANIFEST},
    )
    assert huge.status_code == 413
    # an upload larger than WAYSTATION_MAX_UPLOAD_GB drops the session
    services.blobs.max_size = 10
    small = await reg.request('POST', name, 'blobs/uploads/')
    too_big = await v2.patch(small.headers['location'], content=b'x' * 11, headers=auth)
    assert too_big.status_code == 413
    assert (await v2.get(small.headers['location'], headers=auth)).status_code == 404
