"""Docker / OCI group repositories (T-P2-DOCKERG) against real MongoDB/Valkey and a fake upstream registry: members
resolved in order, blobs served from any member's copy without upstream requests, merged tag lists, pushes to the
writable member, RBAC on the member, validation."""

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import asgi_client

from waystation_common.db.collections import USERS
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.security import passwords
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
SERVICE = 'registry.example.com'
OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
LAYER = b'upstream layer ' * 1000
CONFIG = b'{"architecture":"amd64","os":"linux"}'


def digest(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def manifest(config: bytes = CONFIG, layer: bytes = LAYER) -> bytes:
    return json.dumps(
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


UPSTREAM_MANIFEST = manifest()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    """An anonymous OCI registry with library/hello:latest and :1.0."""
    headers = {'Content-Type': OCI_MANIFEST, 'Docker-Content-Digest': digest(UPSTREAM_MANIFEST)}
    routes: dict[str, Any] = {
        '/v2/': Respond(200, {'Content-Type': 'application/json'}, b'{}'),
        '/v2/library/hello/manifests/latest': Respond(200, headers, UPSTREAM_MANIFEST),
        f'/v2/library/hello/manifests/{digest(UPSTREAM_MANIFEST)}': Respond(200, headers, UPSTREAM_MANIFEST),
        f'/v2/library/hello/blobs/{digest(LAYER)}': Respond(200, {'Content-Type': 'application/octet-stream'}, LAYER),
        f'/v2/library/hello/blobs/{digest(CONFIG)}': Respond(200, {'Content-Type': 'application/octet-stream'}, CONFIG),
        '/v2/library/hello/tags/list': Respond(
            200,
            {'Content-Type': 'application/json'},
            json.dumps({'name': 'library/hello', 'tags': ['latest', '1.0']}).encode(),
        ),
    }
    async with FakeUpstream(routes) as fake:
        yield fake


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


@pytest.fixture
async def v2(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(services) as client:
        yield client


async def create(admin: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await admin.post('/api/v1/repositories', json={'format': 'docker', 'online': True, **body})


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    assert (await create(admin, {'name': 'hosted', 'type': 'hosted'})).status_code == 201
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True}
    made = await create(admin, {'name': 'hub', 'type': 'proxy', 'proxy': proxy, 'format_options': {'docker_hub': True}})
    assert made.status_code == 201, made.text
    group = {'members': ['hosted', 'hub'], 'writable_member': 'hosted'}
    made = await create(admin, {'name': 'all', 'type': 'group', 'group': group})
    assert made.status_code == 201, made.text
    assert made.json()['group'] == group


class Registry:
    def __init__(self, client: httpx.AsyncClient, auth: tuple[str, str] = ('admin', PASSWORD)) -> None:
        self.client, self.auth = client, auth

    async def request(
        self, method: str, name: str, suffix: str, *, actions: str = 'pull,push,delete', **kw: Any
    ) -> httpx.Response:
        params = [('service', SERVICE), ('scope', f'repository:{name}:{actions}')]
        issued = await self.client.get('/auth/token', params=params, auth=self.auth)
        headers = {'Authorization': f'Bearer {issued.json()["token"]}', **kw.pop('headers', {})}
        return await self.client.request(method, f'/v2/{name}/{suffix}', headers=headers, **kw)

    async def push_image(self, name: str, tag: str, config: bytes, layer: bytes) -> httpx.Response:
        for blob in (layer, config):
            pushed = await self.request('POST', name, 'blobs/uploads/', params={'digest': digest(blob)}, content=blob)
            assert pushed.status_code == 201, pushed.text
        return await self.request(
            'PUT', name, f'manifests/{tag}', content=manifest(config, layer), headers={'Content-Type': OCI_MANIFEST}
        )


async def test_group_validation(admin: httpx.AsyncClient, repos: None) -> None:
    async def errors(group: dict[str, Any], name: str = 'bad') -> set[str]:
        response = await create(admin, {'name': name, 'type': 'group', 'group': group})
        assert response.status_code == 422, response.text
        return {e['field'] for e in response.json()['errors']}

    assert await errors({'members': ['nope']}) == {'group.members.0'}
    assert await errors({'members': ['all']}) == {'group.members.0'}  # no groups in groups
    assert await errors({'members': ['hosted', 'hosted']}) == {'group.members.1'}
    assert await errors({'members': ['hosted', 'hub'], 'writable_member': 'hub'}) == {'group.writable_member'}
    assert await errors({'members': ['hub'], 'writable_member': 'hosted'}) == {'group.writable_member'}
    missing = await create(admin, {'name': 'bad', 'type': 'group'})
    assert [e['field'] for e in missing.json()['errors']] == ['group']
    hosted_with_group = await create(admin, {'name': 'bad', 'type': 'hosted', 'group': {'members': ['hub']}})
    assert [e['field'] for e in hosted_with_group.json()['errors']] == ['group']
    in_use = await admin.delete('/api/v1/repositories/hosted')
    assert in_use.status_code == 409
    assert 'member of all' in in_use.json()['detail']
    setup = (await admin.get('/api/v1/repositories/all/setup')).json()
    snippets = {s['id']: s for c in setup['clients'] for s in c['snippets']}
    assert 'stored in hosted' in snippets['docker-push']['title']


async def test_pull_in_order_push_to_writable_member(
    services: Services, v2: httpx.AsyncClient, repos: None, upstream: FakeUpstream
) -> None:
    reg = Registry(v2)
    # a push through the group lands in the writable member; checking for existing layers never goes upstream
    mine_layer, mine_config = b'my own layer', b'{"architecture":"arm64","os":"linux"}'
    head = await reg.request('HEAD', 'all/team/app', f'blobs/{digest(mine_layer)}')
    assert head.status_code == 404
    pushed = await reg.push_image('all/team/app', '1.0', mine_config, mine_layer)
    assert pushed.status_code == 201, pushed.text
    assert pushed.headers['location'].startswith('/v2/all/team/app/manifests/')
    assert not [s for s in upstream.seen if 'team/app' in s.path]  # the pushed name never reached the upstream
    stored = await reg.request('GET', 'hosted/team/app', 'manifests/1.0', actions='pull')
    assert stored.status_code == 200
    via_group = await reg.request('GET', 'all/team/app', 'manifests/1.0', actions='pull')
    assert via_group.headers['x-ws-group-member'] == 'hosted'
    assert via_group.headers['docker-content-digest'] == pushed.headers['docker-content-digest']

    # a chunked upload through the group continues through the group
    started = await reg.request('POST', 'all/team/app', 'blobs/uploads/')
    assert started.status_code == 202
    assert started.headers['location'].startswith('/v2/all/team/app/blobs/uploads/')
    session = started.headers['location'].rsplit('/', 1)[1]
    status = await reg.request('GET', 'all/team/app', f'blobs/uploads/{session}', actions='pull,push')
    assert status.status_code == 204
    cancelled = await reg.request('DELETE', 'all/team/app', f'blobs/uploads/{session}', actions='pull,push')
    assert cancelled.status_code == 204

    # pulls fall through to the proxy member (hosted has no hello)
    tag = await reg.request('GET', 'all/hello', 'manifests/latest', actions='pull')
    assert tag.status_code == 200, tag.text
    assert tag.headers['x-ws-group-member'] == 'hub'
    assert tag.headers['docker-content-digest'] == digest(UPSTREAM_MANIFEST)
    layer = await reg.request('GET', 'all/hello', f'blobs/{digest(LAYER)}', actions='pull')
    assert layer.status_code == 200
    assert layer.headers['x-ws-group-member'] == 'hub'
    fetched = upstream.hits(f'/v2/library/hello/blobs/{digest(LAYER)}')
    again = await reg.request('HEAD', 'all/other', f'blobs/{digest(LAYER)}', actions='pull')
    assert again.status_code == 200  # any member's copy, whatever the image name
    assert again.headers['content-length'] == str(len(LAYER))
    assert upstream.hits(f'/v2/library/hello/blobs/{digest(LAYER)}') == fetched
    unknown = await reg.request('HEAD', 'all/hello', f'blobs/{digest(b"none")}', actions='pull')
    assert unknown.status_code == 404
    assert not [s for s in upstream.seen if digest(b'none') in s.path]
    missing = await reg.request('GET', 'all/nothing', 'manifests/latest', actions='pull')
    assert missing.status_code == 404
    assert missing.json()['errors'][0]['code'] == 'MANIFEST_UNKNOWN'

    # tags are merged across members
    assert (await reg.push_image('all/hello', 'mine', mine_config, mine_layer)).status_code == 201
    tags = await reg.request('GET', 'all/hello', 'tags/list', actions='pull')
    assert tags.json() == {'name': 'all/hello', 'tags': ['1.0', 'latest', 'mine']}
    page = await reg.request('GET', 'all/hello', 'tags/list', actions='pull', params={'n': 1, 'last': '1.0'})
    assert page.json()['tags'] == ['latest']
    assert page.headers['link'] == '</v2/all/hello/tags/list?n=1&last=latest>; rel="next"'
    hosted_only = await reg.request('GET', 'all/team/app', 'tags/list', actions='pull')
    assert hosted_only.json()['tags'] == ['1.0']
    assert (await reg.request('GET', 'all/nothing', 'tags/list', actions='pull')).status_code == 404

    # deletes go to the writable member too
    deleted = await reg.request('DELETE', 'all/hello', 'manifests/mine')
    assert deleted.status_code == 202
    assert (await reg.request('GET', 'hosted/hello', 'manifests/mine', actions='pull')).status_code == 404


async def test_read_only_group_and_member_rbac(
    services: Services, admin: httpx.AsyncClient, v2: httpx.AsyncClient, repos: None
) -> None:
    made = await create(admin, {'name': 'readonly', 'type': 'group', 'group': {'members': ['hub', 'hosted']}})
    assert made.status_code == 201
    reg = Registry(v2)
    refused = await reg.request('POST', 'readonly/app', 'blobs/uploads/')
    assert refused.status_code == 405
    assert refused.json()['errors'][0]['code'] == 'UNSUPPORTED'

    # write on the group alone is not enough: the content goes to the writable member
    role = {'id': 'group-pushers', 'name': 'Group pushers', 'permissions': [{'action': 'write', 'scope': 'all'}]}
    assert (await admin.post('/api/v1/roles', json=role)).status_code == 201, 'custom role'
    await services.db[USERS].insert_one(
        {
            '_id': 'id-gail',
            'username': 'gail',
            'display_name': 'Gail',
            'source': 'local',
            'password_hash': await passwords.hash_password(PASSWORD),
            'roles': ['group-pushers'],
            'created_at': datetime.now(UTC),
        }
    )
    services.catalog.invalidate()
    gail = Registry(v2, ('gail', PASSWORD))
    denied = await gail.request('POST', 'all/app', 'blobs/uploads/')
    assert denied.status_code == 403, denied.text
    assert denied.json()['errors'][0]['code'] == 'DENIED'
    pulled = await gail.request('GET', 'all/hello', 'manifests/latest', actions='pull')
    assert pulled.status_code == 200  # reading through the group needs read on the group only
