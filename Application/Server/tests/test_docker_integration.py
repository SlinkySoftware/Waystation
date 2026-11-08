"""Docker / OCI proxy (T-P2-DOCKERP) against real MongoDB/Valkey and a scriptable fake registry + CDN."""

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from ws_fake_upstream import FakeUpstream, Respond, Seen

from waystation_common.db.collections import ASSETS, REPOSITORIES
from waystation_common.events import keys
from waystation_common.settings import Settings
from waystation_server.problems import ResponseAborted
from waystation_server.services import Services

pytestmark = pytest.mark.integration

SERVICE = 'registry.example.com'
TOKEN = 'upstream-token-1'
OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
DOCKER_MANIFEST = 'application/vnd.docker.distribution.manifest.v2+json'
LAYER = bytes(range(256)) * 2000  # 512000 bytes: several chunks
CONFIG = b'{"architecture":"amd64","os":"linux"}'


def digest(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


MANIFEST = json.dumps(
    {
        'schemaVersion': 2,
        'mediaType': OCI_MANIFEST,
        'config': {'mediaType': 'application/vnd.oci.image.config.v1+json', 'digest': digest(CONFIG), 'size': 37},
        'layers': [
            {'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip', 'digest': digest(LAYER), 'size': len(LAYER)}
        ],
    }
).encode()
MANIFEST_DIGEST = digest(MANIFEST)


class Registry:
    """A fake upstream registry with Bearer token auth whose blobs redirect to a separate CDN origin."""

    def __init__(self, registry: FakeUpstream, cdn: FakeUpstream, name: str = 'library/hello') -> None:
        self.up, self.cdn, self.name = registry, cdn, name
        self.manifest, self.manifest_type = MANIFEST, OCI_MANIFEST
        self.headers: dict[str, str] = {}
        self.realm = f'http://127.0.0.1:{registry.port}/token'
        registry.routes[f'/token?service=fake&scope=repository:{name}:pull'] = Respond(
            200, {'Content-Type': 'application/json'}, json.dumps({'token': TOKEN, 'expires_in': 300}).encode()
        )
        registry.routes['/v2/'] = self.guard(lambda seen: Respond(200, {'Content-Type': 'application/json'}, b'{}'))
        base = f'/v2/{name}'
        registry.routes[f'{base}/manifests/latest'] = self.guard(self.tag)
        registry.routes[f'{base}/manifests/{MANIFEST_DIGEST}'] = self.guard(
            lambda seen: Respond(
                200, {'Content-Type': OCI_MANIFEST, 'Docker-Content-Digest': MANIFEST_DIGEST}, MANIFEST
            )
        )
        registry.routes[f'{base}/blobs/{digest(LAYER)}'] = self.guard(
            lambda seen: Respond(307, {'Location': f'{cdn.url}layer?sig=x'})
        )
        registry.routes[f'{base}/blobs/{digest(CONFIG)}'] = self.guard(
            lambda seen: Respond(200, {'Content-Type': 'application/octet-stream'}, CONFIG)
        )
        cdn.routes['/layer?sig=x'] = Respond(200, {'Content-Type': 'application/octet-stream'}, LAYER, chunk=100_000)

    def guard(self, inner: Callable[[Seen], Respond]) -> Callable[[Seen], Respond]:
        def route(seen: Seen) -> Respond:
            if seen.headers.get('authorization') != f'Bearer {TOKEN}':
                challenge = f'Bearer realm="{self.realm}",service="fake",scope="repository:{self.name}:pull"'
                return Respond(401, {'WWW-Authenticate': challenge, 'Content-Type': 'application/json'}, b'{}')
            return inner(seen)

        return route

    def tag(self, seen: Seen) -> Respond:
        headers = {'Content-Type': self.manifest_type, 'Docker-Content-Digest': digest(self.manifest), **self.headers}
        return Respond(200, headers, self.manifest)

    def gets(self, suffix: str) -> list[Seen]:
        return [s for s in self.up.seen if s.path == f'/v2/{self.name}/{suffix}']


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'])


@pytest.fixture
async def registry() -> AsyncIterator[Registry]:
    async with FakeUpstream() as up, FakeUpstream() as cdn:
        yield Registry(up, cdn)


async def add_docker(services: Services, url: str, name: str = 'hub', **options: Any) -> str:
    now = datetime.now(UTC)
    proxy = {'upstream_url': url, **options.pop('proxy', {})}
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': f'id-{name}',
            'name': name,
            'format': 'docker',
            'type': 'proxy',
            'online': True,
            'proxy': proxy,
            'format_options': options,
            'created_at': now,
            'updated_at': now,
        }
    )
    services.catalog.invalidate()
    return f'id-{name}'


@pytest.fixture
async def hub(services: Services, registry: Registry) -> str:
    return await add_docker(services, registry.up.url, aliases=['docker.io'], docker_hub=True, default_repo=True)


async def bearer(client: httpx.AsyncClient, name: str) -> dict[str, str]:
    issued = await client.get('/auth/token', params={'service': SERVICE, 'scope': f'repository:{name}:pull'})
    assert issued.status_code == 200
    return {'Authorization': f'Bearer {issued.json()["token"]}'}


async def test_pull_by_tag_digest_and_blobs(
    client: httpx.AsyncClient, services: Services, registry: Registry, hub: str
) -> None:
    auth = await bearer(client, 'hub/library/hello')
    base = '/v2/hub/library/hello'

    tag = await client.get(f'{base}/manifests/latest', headers={'Accept': OCI_MANIFEST, **auth})
    assert tag.status_code == 200
    assert tag.headers['docker-content-digest'] == MANIFEST_DIGEST
    assert tag.headers['content-type'] == OCI_MANIFEST
    assert tag.headers['docker-distribution-api-version'] == 'registry/2.0'
    assert tag.headers['x-ws-cache'] == 'FETCHING'
    assert tag.headers['x-accel-redirect'].endswith(MANIFEST_DIGEST.split(':')[1])
    # the /v2/ ping taught the challenge: token first, then every request carries it
    assert [s.headers.get('authorization') for s in registry.gets('manifests/latest')] == [f'Bearer {TOKEN}']
    assert [s.path for s in registry.up.seen[:2]] == [
        '/v2/',
        f'/token?service=fake&scope=repository:{registry.name}:pull',
    ]

    again = await client.get(f'{base}/manifests/latest', headers={'Accept': OCI_MANIFEST, **auth})
    assert again.headers['x-ws-cache'] == 'HIT'
    assert again.headers['docker-content-digest'] == MANIFEST_DIGEST
    head = await client.head(f'{base}/manifests/latest', headers={'Accept': OCI_MANIFEST, **auth})
    assert head.headers['content-length'] == str(len(MANIFEST))
    assert head.headers['docker-content-digest'] == MANIFEST_DIGEST
    assert len(registry.gets('manifests/latest')) == 1
    not_modified = await client.get(
        f'{base}/manifests/latest', headers={'Accept': OCI_MANIFEST, 'If-None-Match': f'"{MANIFEST_DIGEST}"', **auth}
    )
    assert not_modified.status_code == 304

    by_digest = await client.get(f'{base}/manifests/{MANIFEST_DIGEST}', headers=auth)
    assert by_digest.content == MANIFEST
    assert by_digest.headers['docker-content-digest'] == MANIFEST_DIGEST
    assert by_digest.headers['content-type'] == OCI_MANIFEST
    # the cached token was sent up front this time
    assert registry.gets(f'manifests/{MANIFEST_DIGEST}')[0].headers['authorization'] == f'Bearer {TOKEN}'

    layer = await client.get(f'{base}/blobs/{digest(LAYER)}', headers=auth)
    assert layer.content == LAYER
    assert layer.headers['docker-content-digest'] == digest(LAYER)
    assert layer.headers['content-type'] == 'application/octet-stream'
    assert registry.gets(f'blobs/{digest(LAYER)}')[0].headers['authorization'] == f'Bearer {TOKEN}'
    assert 'authorization' not in registry.cdn.seen[0].headers  # never to the CDN

    cached = await client.get(f'{base}/blobs/{digest(LAYER)}', headers=auth)
    assert cached.headers['x-ws-cache'] == 'HIT'
    assert cached.headers['docker-content-digest'] == digest(LAYER)
    assert await services.db[ASSETS].find_one({'repo_id': hub, 'path': f'blobs/{digest(LAYER)}'}) is not None
    assert await services.db[ASSETS].find_one({'repo_id': hub, 'path': 'library/hello/manifests/latest@accept=om'})


async def test_mirror_mode_alias_and_library_prefix(client: httpx.AsyncClient, registry: Registry, hub: str) -> None:
    for name in ('library/hello', 'docker.io/hello', 'hub/hello'):
        auth = await bearer(client, name)
        response = await client.get(f'/v2/{name}/blobs/{digest(CONFIG)}', headers=auth)
        assert response.status_code == 200, name
        assert response.headers['docker-content-digest'] == digest(CONFIG)
    # one upstream fetch: the blob is cached per digest whatever the route
    assert len(registry.gets(f'blobs/{digest(CONFIG)}')) == 1


async def test_nexus_path_and_version_check(client: httpx.AsyncClient, registry: Registry, hub: str) -> None:
    base = await client.get('/repository/hub/v2/')
    assert base.status_code == 200
    assert base.json() == {}
    assert base.headers['docker-distribution-api-version'] == 'registry/2.0'
    response = await client.get('/repository/hub/v2/library/hello/manifests/latest')
    assert response.status_code == 200
    assert response.headers['docker-content-digest'] == MANIFEST_DIGEST
    other = await client.get('/repository/hub/elsewhere')
    assert other.status_code == 404
    assert other.json()['errors'][0]['code'] == 'NAME_UNKNOWN'


async def test_accept_list_is_passed_upstream_and_keys_the_tag(
    client: httpx.AsyncClient, services: Services, registry: Registry, hub: str
) -> None:
    full = ', '.join(
        [
            'application/vnd.docker.distribution.manifest.list.v2+json',
            DOCKER_MANIFEST,
            OCI_MANIFEST,
            'application/vnd.oci.image.index.v1+json',
            '*/*',
        ]
    )
    await client.get('/repository/hub/v2/library/hello/manifests/latest', headers={'Accept': full})
    await client.get('/repository/hub/v2/library/hello/manifests/latest', headers={'Accept': DOCKER_MANIFEST})
    sent = [s.headers.get('accept') for s in registry.gets('manifests/latest') if s.headers.get('authorization')]
    assert sent == [
        'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, '
        'application/vnd.docker.distribution.manifest.list.v2+json, '
        'application/vnd.docker.distribution.manifest.v2+json',
        DOCKER_MANIFEST,
    ]
    paths = sorted([a['path'] async for a in services.db[ASSETS].find({'repo_id': hub})])
    assert paths == ['library/hello/manifests/latest', 'library/hello/manifests/latest@accept=dm']


async def test_stale_tag_is_rechecked_with_head(
    client: httpx.AsyncClient, services: Services, registry: Registry
) -> None:
    await add_docker(services, registry.up.url, docker_hub=True, proxy={'metadata_max_age_s': 0})
    url = '/repository/hub/v2/library/hello/manifests/latest'
    assert (await client.get(url)).headers['x-ws-cache'] == 'FETCHING'
    revalidated = await client.get(url)
    assert revalidated.headers['x-ws-cache'] == 'REVALIDATED'
    methods = [s.method for s in registry.gets('manifests/latest') if s.headers.get('authorization')]
    assert methods == ['GET', 'HEAD']

    registry.manifest = MANIFEST.replace(b'"schemaVersion": 2', b'"schemaVersion":2')
    changed = await client.get(url)
    assert changed.headers['x-ws-cache'] == 'FETCHING'
    assert changed.headers['docker-content-digest'] == digest(registry.manifest)
    methods = [s.method for s in registry.gets('manifests/latest') if s.headers.get('authorization')]
    assert methods == ['GET', 'HEAD', 'HEAD', 'GET']


async def test_head_miss_asks_upstream_with_head(
    client: httpx.AsyncClient, services: Services, registry: Registry, hub: str
) -> None:
    head = await client.head('/repository/hub/v2/library/hello/manifests/latest')
    assert head.status_code == 200
    assert head.headers['docker-content-digest'] == MANIFEST_DIGEST
    assert head.headers['content-length'] == str(len(MANIFEST))
    assert {s.method for s in registry.gets('manifests/latest')} == {'HEAD'}
    blob = await client.head(f'/repository/hub/v2/library/hello/blobs/{digest(LAYER)}')
    assert blob.headers['content-length'] == str(len(LAYER))
    assert blob.headers['docker-content-digest'] == digest(LAYER)
    assert await services.db[ASSETS].count_documents({}) == 0


async def test_blob_digest_mismatch_caches_nothing(
    client: httpx.AsyncClient, services: Services, registry: Registry, hub: str
) -> None:
    registry.cdn.routes['/layer?sig=x'] = Respond(200, {}, LAYER[:-1] + b'X', chunk=100_000)
    with pytest.raises(ResponseAborted):
        await client.get(f'/repository/hub/v2/library/hello/blobs/{digest(LAYER)}')
    assert await services.db[ASSETS].count_documents({}) == 0
    assert list(services.blobs.uploads_dir.iterdir()) == []


async def test_manifest_digest_mismatch_is_refused(
    client: httpx.AsyncClient, services: Services, registry: Registry, hub: str
) -> None:
    registry.headers['Docker-Content-Digest'] = 'sha256:' + 'f' * 64
    response = await client.get('/repository/hub/v2/library/hello/manifests/latest')
    assert response.status_code == 502
    assert response.json()['errors'][0]['code'] == 'DIGEST_INVALID'
    registry.headers.clear()
    registry.manifest_type = 'text/html'
    response = await client.get('/repository/hub/v2/library/hello/manifests/latest')
    assert response.status_code == 502
    assert await services.db[ASSETS].count_documents({}) == 0


async def test_unknown_names_and_errors(client: httpx.AsyncClient, registry: Registry, hub: str) -> None:
    # Docker Hub answers 401 (even with a token) for names that do not exist
    registry.up.routes['/v2/library/hello/manifests/denied'] = Respond(401, {'WWW-Authenticate': 'Bearer x'}, b'{}')
    denied = await client.get('/repository/hub/v2/library/hello/manifests/denied')
    assert denied.status_code == 404
    assert denied.json()['errors'][0]['code'] == 'NAME_UNKNOWN'
    gone = await client.get('/repository/hub/v2/library/hello/manifests/gone')
    assert gone.status_code == 404
    assert gone.json()['errors'][0]['code'] == 'MANIFEST_UNKNOWN'
    blob = await client.get(f'/repository/hub/v2/library/hello/blobs/sha256:{"0" * 64}')
    assert blob.json()['errors'][0]['code'] == 'BLOB_UNKNOWN'
    registry.up.routes['/v2/library/hello/manifests/limited'] = Respond(429, {'Retry-After': '30'}, b'{}')
    limited = await client.get('/repository/hub/v2/library/hello/manifests/limited')
    assert limited.status_code == 429
    assert limited.headers['retry-after'] == '30'
    bad = await client.get('/repository/hub/v2/Bad_Name/manifests/latest')
    assert bad.status_code == 400
    assert bad.json()['errors'][0]['code'] == 'NAME_INVALID'
    referrers = await client.get(f'/repository/hub/v2/library/hello/referrers/{MANIFEST_DIGEST}')
    assert referrers.status_code == 404


async def test_rate_limit_headers_are_recorded(
    client: httpx.AsyncClient, services: Services, registry: Registry, hub: str
) -> None:
    registry.headers.update({'ratelimit-limit': '100;w=21600', 'ratelimit-remaining': '76;w=21600'})
    await client.get('/repository/hub/v2/library/hello/manifests/latest')
    await services.emitter.flush()
    recorded = await services.valkey.hgetall(keys.upstream_ratelimit_key(hub))
    assert recorded['limit'] == '100'
    assert recorded['remaining'] == '76'
    assert recorded['window_s'] == '21600'


async def test_tags_list_with_paging(client: httpx.AsyncClient, registry: Registry, hub: str) -> None:
    body = json.dumps({'name': 'library/hello', 'tags': ['a', 'b']}).encode()
    registry.up.routes['/v2/library/hello/tags/list?n=2'] = registry.guard(
        lambda seen: Respond(
            200,
            {'Content-Type': 'application/json', 'Link': '</v2/library/hello/tags/list?last=b&n=2>; rel="next"'},
            body,
        )
    )
    response = await client.get('/repository/hub/v2/library/hello/tags/list', params={'n': '2'})
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/json'
    assert response.headers['link'] == '</repository/hub/v2/library/hello/tags/list?n=2&last=b>; rel="next"'
    hit = await client.get('/repository/hub/v2/library/hello/tags/list', params={'n': '2'})
    assert hit.headers['x-ws-cache'] == 'HIT'
    assert hit.headers['link'] == '</repository/hub/v2/library/hello/tags/list?n=2&last=b>; rel="next"'
    bad = await client.get('/repository/hub/v2/library/hello/tags/list', params={'n': 'x'})
    assert bad.status_code == 400


async def test_challenge_on_a_later_request_is_learned_and_retried(
    client: httpx.AsyncClient, registry: Registry, hub: str
) -> None:
    # an upstream whose /v2/ is open but whose images need a token: the first 401 teaches the realm
    registry.up.routes['/v2/'] = Respond(200, {'Content-Type': 'application/json'}, b'{}')
    response = await client.get('/repository/hub/v2/library/hello/manifests/latest')
    assert response.status_code == 200
    assert [s.headers.get('authorization') for s in registry.gets('manifests/latest')] == [None, f'Bearer {TOKEN}']
    blob = await client.get(f'/repository/hub/v2/library/hello/blobs/{digest(CONFIG)}')
    assert blob.status_code == 200
    assert [s.headers.get('authorization') for s in registry.gets(f'blobs/{digest(CONFIG)}')] == [f'Bearer {TOKEN}']
    assert [s.path for s in registry.up.seen].count('/v2/') == 1
