"""Prefetch (T-O12, adr/0012) end to end: the worker's prefetch jobs (``waystation_worker.prefetch``) calling the
server's internal prefetch route (in process, ``httpx.ASGITransport``) with a signed system grant, against real
MongoDB/Valkey and scriptable upstreams.

Warm: a raw file, an npm package (packument + tarball verified against it) and a docker image (index → the chosen
platform's manifest → config and layer; other platforms and attestations skipped) are cached, nothing is counted as
a download or as client traffic, and a client asking for the same file at the same moment shares the fill. Refresh:
stale popular metadata is revalidated (304: only ``fetched_at`` moves), fresh metadata and content are left alone,
offline and auto-blocked repositories are skipped, the selection uses its index. The route answers 404 without a
valid grant (other repository, forged, none). The repository API validates prefetch settings, keeps the warm
schedule in step and runs a warm job on request.
"""

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond, Seen
from ws_query_audit import request_scope
from ws_testing import BASE_URL, add_repo, asgi_client

from waystation_common.db import prefetch as prefetch_db
from waystation_common.db.collections import ASSETS, JOB_DEFINITIONS, REPOSITORIES
from waystation_common.events import keys
from waystation_common.settings import Settings
from waystation_common.system_grants import HEADER, PREFETCH, SystemGrants
from waystation_server.app import create_app
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.proxy.http import Timeouts
from waystation_server.services import Services
from waystation_worker import prefetch

pytestmark = pytest.mark.integration

DATA = bytes(range(256)) * 2000  # 512000 bytes
TGZ = b'npm tarball bytes ' * 4000
OCI_INDEX = 'application/vnd.oci.image.index.v1+json'
OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(data: bytes) -> str:
    return f'sha256:{sha(data)}'


PASSWORD = 'correct horse battery staple'


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD))


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        yield fake


@pytest.fixture
def grants(settings: Settings) -> SystemGrants:
    return SystemGrants.from_secret_keys([key.get_secret_value() for key in settings.secret_keys])


@pytest.fixture
async def server(services: Services, grants: SystemGrants) -> AsyncIterator[prefetch.ServerClient]:
    """The worker's client, talking to the server app in process."""
    services.upstream.timeouts = Timeouts(connect=2.0, read=2.0)
    transport = httpx.ASGITransport(app=request_scope(create_app(services.settings, services=services)))
    client = prefetch.ServerClient(BASE_URL, grants, transport=transport)
    try:
        yield client
    finally:
        await client.aclose()


async def add(services: Services, repo_id: str, fmt: str, url: str, **proxy: Any) -> str:
    now = datetime.now(UTC)
    name = repo_id.removeprefix('repo-')
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': repo_id,
            'name': name,
            'format': fmt,
            'type': 'proxy',
            'online': True,
            'proxy': {'upstream_url': url, **proxy},
            'format_options': {},
            'created_at': now,
            'updated_at': now,
        }
    )
    services.catalog.invalidate()
    return repo_id


async def set_warm(services: Services, repo_id: str, *items: str) -> None:
    await services.db[REPOSITORIES].update_one({'_id': repo_id}, {'$set': {'proxy.prefetch.warm': list(items)}})
    services.catalog.invalidate()


async def no_downloads(services: Services) -> None:
    await services.emitter.flush()
    assert await services.valkey.hlen(keys.DL_ASSETS) == 0
    assert await services.valkey.hlen(keys.SEEN_ASSETS) == 0
    minute = int(time.time()) // 60
    for m in (minute - 1, minute):
        for repo in ('repo-files', 'repo-npm', 'repo-hub'):
            assert int(await services.valkey.hget(keys.cnt_key(m, repo), 'requests') or 0) == 0


# ---------------------------------------------------------------------------- warm
def route_npm(upstream: FakeUpstream) -> None:
    integrity = 'sha512-' + base64.b64encode(hashlib.sha512(TGZ).digest()).decode()
    versions = {
        v: {'name': 'is-number', 'version': v, 'dist': {'tarball': f'{upstream.url}is-number/-/is-number-{v}.tgz'}}
        for v in ('6.0.0', '7.0.0')
    }
    versions['7.0.0']['dist']['integrity'] = integrity
    body = json.dumps({'name': 'is-number', 'dist-tags': {'latest': '7.0.0'}, 'versions': versions}).encode()
    upstream.routes['/is-number'] = Respond(200, {'Content-Type': 'application/json'}, body)
    upstream.routes['/is-number/-/is-number-7.0.0.tgz'] = Respond(200, {}, TGZ, chunk=65536)


class Registry:
    """A fake registry (no auth): tag ``1.0`` is an OCI index with linux/amd64, linux/arm64 and an attestation."""

    def __init__(self, up: FakeUpstream, name: str = 'library/hello') -> None:
        self.layers = {arch: f'{arch} layer '.encode() * 20000 for arch in ('amd64', 'arm64')}
        self.configs = {arch: json.dumps({'architecture': arch, 'os': 'linux'}).encode() for arch in self.layers}
        self.manifests: dict[str, bytes] = {}
        entries = []
        for arch in ('amd64', 'arm64'):
            manifest = json.dumps(
                {
                    'schemaVersion': 2,
                    'mediaType': OCI_MANIFEST,
                    'config': {
                        'mediaType': 'application/vnd.oci.image.config.v1+json',
                        'digest': digest(self.configs[arch]),
                        'size': len(self.configs[arch]),
                    },
                    'layers': [
                        {
                            'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip',
                            'digest': digest(self.layers[arch]),
                            'size': len(self.layers[arch]),
                        }
                    ],
                }
            ).encode()
            self.manifests[arch] = manifest
            entries.append(
                {
                    'mediaType': OCI_MANIFEST,
                    'digest': digest(manifest),
                    'size': len(manifest),
                    'platform': {'os': 'linux', 'architecture': arch},
                }
            )
        entries.append(
            {
                'mediaType': OCI_MANIFEST,
                'digest': digest(b'attestation'),
                'size': 11,
                'platform': {'os': 'unknown', 'architecture': 'unknown'},
                'annotations': {'vnd.docker.reference.type': 'attestation-manifest'},
            }
        )
        self.index = json.dumps({'schemaVersion': 2, 'mediaType': OCI_INDEX, 'manifests': entries}).encode()
        base = f'/v2/{name}'
        up.routes['/v2/'] = Respond(200, {'Content-Type': 'application/json'}, b'{}')
        up.routes[f'{base}/manifests/1.0'] = Respond(
            200, {'Content-Type': OCI_INDEX, 'Docker-Content-Digest': digest(self.index)}, self.index
        )
        for arch, manifest in self.manifests.items():
            up.routes[f'{base}/manifests/{digest(manifest)}'] = Respond(
                200, {'Content-Type': OCI_MANIFEST, 'Docker-Content-Digest': digest(manifest)}, manifest
            )
            for blob in (self.configs[arch], self.layers[arch]):
                up.routes[f'{base}/blobs/{digest(blob)}'] = Respond(200, {}, blob, chunk=100_000)


async def collect(server: prefetch.ServerClient, repo_id: str, op: str, items: list[str]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []

    async def on_item(line: dict[str, Any]) -> None:
        lines.append(line)

    await server.call(repo_id, op, items, on_item)
    return lines


async def test_warm_raw_npm_and_docker_without_counting_downloads(
    services: Services, server: prefetch.ServerClient, upstream: FakeUpstream
) -> None:
    raw = await add_repo(services, upstream.url)
    upstream.routes['/tools/tool.tar.gz'] = Respond(200, {}, DATA, chunk=100_000)
    await set_warm(services, raw, 'tools/tool.tar.gz', 'tools/missing.bin')
    result = await prefetch.warm(services.db, services.valkey, server, repo_id=raw)
    assert (result['items'], result['ok'], result['failed']) == (2, 1, 1)
    assert 'tools/missing.bin' in result['errors'][0]
    asset = await services.db[ASSETS].find_one({'repo_id': raw, 'path': 'tools/tool.tar.gz'})
    assert asset is not None and asset['blob_sha256'] == sha(DATA)

    async with FakeUpstream() as npm_up:
        route_npm(npm_up)
        npm = await add(services, 'repo-npm', 'npm', npm_up.url)
        await set_warm(services, npm, 'is-number')
        result = await prefetch.warm(services.db, services.valkey, server, repo_id=npm)
        assert (result['ok'], result['files'], result['fetched']) == (1, 2, 2)
        assert npm_up.hits('/is-number/-/is-number-7.0.0.tgz') == 1
        assert npm_up.hits('/is-number/-/is-number-6.0.0.tgz') == 0
        tarball = await services.db[ASSETS].find_one({'repo_id': npm, 'path': 'is-number/-/is-number-7.0.0.tgz'})
        assert tarball is not None and tarball['blob_sha256'] == sha(TGZ)
        assert await services.db[ASSETS].find_one({'repo_id': npm, 'path': 'is-number/-/packument.install-v1.json'})
        again = await prefetch.warm(services.db, services.valkey, server, repo_id=npm)
        assert (again['ok'], again['fetched']) == (1, 0)  # all cached: nothing from the upstream

    async with FakeUpstream() as reg_up:
        registry = Registry(reg_up)
        hub = await add(services, 'repo-hub', 'docker', reg_up.url)
        await set_warm(services, hub, 'library/hello:1.0')
        result = await prefetch.warm(services.db, services.valkey, server, repo_id=hub)
        assert (result['ok'], result['files']) == (1, 4), result
        paths = {d['path'] async for d in services.db[ASSETS].find({'repo_id': hub}, {'path': 1})}
        amd64 = registry.manifests['amd64']
        assert f'library/hello/manifests/{digest(amd64)}' in paths
        assert f'blobs/{digest(registry.layers["amd64"])}' in paths
        assert f'blobs/{digest(registry.configs["amd64"])}' in paths
        assert f'blobs/{digest(registry.layers["arm64"])}' not in paths
        assert not any(s.path.endswith(digest(b'attestation')) for s in reg_up.seen)
        everything = await collect(server, hub, 'warm', ['library/hello:1.0 all'])
        assert everything[0]['outcome'] == 'ok' and everything[0]['files'] == 7

    await no_downloads(services)


async def test_warm_cargo_crates(services: Services, server: prefetch.ServerClient, upstream: FakeUpstream) -> None:
    lines = [
        {'name': 'itoa', 'vers': v, 'deps': [], 'cksum': sha(TGZ + v.encode()), 'features': {}, 'yanked': v == '1.1.0'}
        for v in ('1.0.0', '1.0.1', '1.1.0')
    ]
    upstream.routes['/config.json'] = Respond(200, {}, json.dumps({'dl': f'{upstream.url}dl'}).encode())
    upstream.routes['/it/oa/itoa'] = Respond(200, {}, b''.join(json.dumps(e).encode() + b'\n' for e in lines))
    for version in ('1.0.0', '1.0.1', '1.1.0'):
        upstream.routes[f'/dl/itoa/{version}/download'] = Respond(200, {}, TGZ + version.encode())
    crates = await add(services, 'repo-crates', 'cargo', upstream.url)
    await set_warm(services, crates, 'itoa', 'itoa@1.0.0', 'itoa@9.9.9')
    result = await prefetch.warm(services.db, services.valkey, server, repo_id=crates)
    assert (result['items'], result['ok'], result['failed']) == (3, 2, 1), result
    paths = {d['path'] async for d in services.db[ASSETS].find({'repo_id': crates}, {'path': 1})}
    assert {'crates/itoa/itoa-1.0.1.crate', 'crates/itoa/itoa-1.0.0.crate', 'index/it/oa/itoa'} <= paths
    assert 'crates/itoa/itoa-1.1.0.crate' not in paths  # yanked: not the latest


async def test_a_client_shares_the_warm_fill(
    client: httpx.AsyncClient, services: Services, server: prefetch.ServerClient, upstream: FakeUpstream
) -> None:
    raw = await add_repo(services, upstream.url)
    upstream.routes['/big.bin'] = Respond(200, {}, DATA, chunk=32_768, chunk_delay=0.02)
    warming = asyncio.create_task(collect(server, raw, 'warm', ['big.bin']))
    for _ in range(500):  # until the warm call's fill has started
        if upstream.hits('/big.bin'):
            break
        await asyncio.sleep(0.01)
    response = await client.get('/repository/files/big.bin')
    lines = await warming
    assert response.status_code == 200 and response.content == DATA
    assert lines[0]['outcome'] == 'ok'
    assert upstream.hits('/big.bin') == 1  # one upstream fetch for both


# ---------------------------------------------------------------------------- refresh
async def popular(services: Services, repo_id: str, path: str, *, count: int, age_s: int, days_ago: int = 1) -> None:
    now = datetime.now(UTC)
    await services.db[ASSETS].update_one(
        {'repo_id': repo_id, 'path': path},
        {
            '$set': {
                'download_count': count,
                'last_downloaded_at': now - timedelta(days=days_ago),
                'upstream.fetched_at': now - timedelta(seconds=age_s),
            }
        },
    )


async def test_refresh_revalidates_stale_popular_metadata_only(
    client: httpx.AsyncClient, services: Services, server: prefetch.ServerClient, upstream: FakeUpstream
) -> None:
    raw = await add_repo(services, upstream.url, metadata_max_age_s=3600, prefetch={'refresh_popular': True})
    await services.db[REPOSITORIES].update_one(
        {'_id': raw}, {'$set': {'format_options': {'metadata_path_regex': '\\.json$'}}}
    )
    services.catalog.invalidate()
    for name in ('stale', 'fresh', 'old'):

        def answer(seen: Seen, name: str = name) -> Respond:
            if seen.headers.get('if-none-match') == f'"{name}"':
                return Respond(304, {'ETag': f'"{name}"'}, b'')
            return Respond(200, {'ETag': f'"{name}"', 'Content-Type': 'application/json'}, b'{}')

        upstream.routes[f'/{name}.json'] = answer
        assert (await client.get(f'/repository/files/{name}.json')).status_code == 200
    upstream.routes['/file.bin'] = Respond(200, {}, b'content')
    assert (await client.get('/repository/files/file.bin')).status_code == 200
    await services.emitter.flush()
    await services.valkey.delete(keys.DL_ASSETS, keys.SEEN_ASSETS)  # the client's downloads above
    await popular(services, raw, 'stale.json', count=9, age_s=3500)  # goes stale before the next run
    await popular(services, raw, 'fresh.json', count=5, age_s=60)
    await popular(services, raw, 'old.json', count=50, age_s=7200, days_ago=30)  # not downloaded lately
    await popular(services, raw, 'file.bin', count=99, age_s=99999)  # content: never refreshed
    before = len(upstream.seen)

    result = await prefetch.refresh(
        services.db, services.valkey, server, repo_id=None, window_days=7, top_n=200, ahead_s=900, now=datetime.now(UTC)
    )
    assert result['repositories'] == 1
    [outcome] = result['results']
    assert (outcome['popular'], outcome['due'], outcome['fetched'], outcome['failed']) == (2, 1, 1, 0)
    seen = upstream.seen[before:]
    assert [(s.path, s.headers.get('if-none-match')) for s in seen] == [('/stale.json', '"stale"')]
    stale = await services.db[ASSETS].find_one({'repo_id': raw, 'path': 'stale.json'})
    assert stale is not None
    assert datetime.now(UTC) - stale['upstream']['fetched_at'].replace(tzinfo=UTC) < timedelta(minutes=1)
    await services.emitter.flush()
    assert await services.valkey.hlen(keys.DL_ASSETS) == 0  # the refresh is not a download

    # Offline, then auto-blocked: skipped without calling anything.
    await services.db[REPOSITORIES].update_one({'_id': raw}, {'$set': {'proxy.offline': True}})
    skipped = await prefetch.refresh(
        services.db,
        services.valkey,
        server,
        repo_id=raw,
        window_days=7,
        top_n=200,
        ahead_s=99999,
        now=datetime.now(UTC),
    )
    assert skipped['results'] == [{'repository': 'files', 'skipped': 'offline mode'}]
    await services.db[REPOSITORIES].update_one({'_id': raw}, {'$set': {'proxy.offline': False}})
    until = str(int(time.time() * 1000) + 60_000)
    await services.valkey.hset(keys.upstream_health_key(raw), 'blocked_until', until)
    blocked = await prefetch.refresh(
        services.db,
        services.valkey,
        server,
        repo_id=raw,
        window_days=7,
        top_n=200,
        ahead_s=99999,
        now=datetime.now(UTC),
    )
    assert blocked['results'] == [{'repository': 'files', 'skipped': 'the upstream is auto-blocked'}]
    assert len(upstream.seen) == before + 1


async def test_popular_selection_uses_its_index(services: Services) -> None:
    query = {
        'repo_id': 'r',
        'kind': 'metadata',
        'download_count': {'$gt': 0},
        'last_downloaded_at': {'$gte': datetime.now(UTC)},
    }
    # The shape ``db.prefetch.popular_metadata`` issues (it names the index: the planner may prefer
    # ``repo_last_downloaded_at`` plus an in-memory sort on a small collection).
    plan = (
        await services.db[ASSETS]
        .find(query)
        .sort([('download_count', -1)])
        .hint('prefetch_popular')
        .limit(10)
        .explain()
    )
    text = json.dumps(plan, default=str)
    assert 'prefetch_popular' in text
    assert '"stage": "SORT"' not in text
    assert await prefetch_db.popular_metadata(services.db, 'r', since=datetime.now(UTC), limit=10) == []


# ---------------------------------------------------------------------------- the grant
async def test_the_route_needs_a_grant_for_that_repository(
    client: httpx.AsyncClient, services: Services, grants: SystemGrants, upstream: FakeUpstream
) -> None:
    raw = await add_repo(services, upstream.url)
    other = await add_repo(services, upstream.url, name='other')
    body = {'op': 'warm', 'items': ['x.bin']}
    url = f'/_internal/prefetch/{raw}'
    assert (await client.post(url, json=body)).status_code == 404
    assert (await client.post(url, json=body, headers={HEADER: grants.sign(PREFETCH, other)})).status_code == 404
    assert (await client.post(url, json=body, headers={HEADER: grants.sign('other', raw)})).status_code == 404
    forged = SystemGrants([b'x' * 32]).sign(PREFETCH, raw)
    assert (await client.post(url, json=body, headers={HEADER: forged})).status_code == 404
    expired = grants.sign(PREFETCH, raw, now=time.time() - 3600)
    assert (await client.post(url, json=body, headers={HEADER: expired})).status_code == 404
    ok = await client.post(url, json=body, headers={HEADER: grants.sign(PREFETCH, raw)})
    assert ok.status_code == 200 and ok.headers['content-type'].startswith('application/x-ndjson')
    assert (
        await client.post(url, json={'op': 'nope'}, headers={HEADER: grants.sign(PREFETCH, raw)})
    ).status_code == 400
    # A grant is nothing anywhere else: format routes and the REST API ignore the header.
    await services.db[REPOSITORIES].update_one({'_id': raw}, {'$set': {'type': 'hosted', 'proxy': None}})
    services.catalog.invalidate()
    headers = {HEADER: grants.sign(PREFETCH, raw)}
    assert (await client.post(url, json=body, headers=headers)).status_code == 404  # not a proxy any more
    assert (await client.put('/repository/files/x.bin', content=b'x', headers=headers)).status_code == 401
    assert (await client.get('/api/v1/tasks/scheduled', headers=headers)).status_code == 401


# ---------------------------------------------------------------------------- repository API
async def test_repository_api_validates_schedules_and_runs(admin: httpx.AsyncClient, services: Services) -> None:
    client = admin
    body: dict[str, Any] = {
        'name': 'hub',
        'format': 'docker',
        'type': 'proxy',
        'online': True,
        'proxy': {
            'upstream_url': 'https://registry.example.com',
            'prefetch': {'refresh_popular': True, 'warm': ['Bad Name', 'nginx:1.27'], 'schedule': '@every 1m'},
        },
    }
    refused = await client.post('/api/v1/repositories', json=body)
    assert refused.status_code == 422
    fields = {e['field'] for e in refused.json()['errors']}
    assert fields == {'proxy.prefetch.warm.0', 'proxy.prefetch.schedule'}
    body['proxy']['prefetch'] = {'refresh_popular': True, 'warm': [' nginx:1.27 ', 'nginx:1.27'], 'schedule': '@daily'}
    created = await client.post('/api/v1/repositories', json=body)
    assert created.status_code == 201, created.text
    assert created.json()['proxy']['prefetch'] == {
        'refresh_popular': True,
        'warm': ['nginx:1.27'],
        'schedule': '@daily',
    }
    stored = await services.db[REPOSITORIES].find_one({'name': 'hub'})
    assert stored is not None
    repo_id = stored['_id']
    definition = await services.db[JOB_DEFINITIONS].find_one({'_id': f'prefetch:{repo_id}'})
    assert (
        definition is not None
        and definition['kind'] == 'prefetch.warm'
        and definition['params'] == {'repo_id': repo_id}
    )

    status = await client.get('/api/v1/repositories/hub/prefetch')
    assert status.status_code == 200
    assert status.json()['warm_supported'] and status.json()['warm_items'] == 1 and status.json()['next_run_at']
    run = await client.post('/api/v1/repositories/hub/prefetch/run')
    assert run.status_code == 202 and run.json()['status'] == 'enqueued'
    tasks = (await client.get('/api/v1/tasks/scheduled')).json()['items']
    owned = next(t for t in tasks if t['id'] == f'prefetch:{repo_id}')
    assert owned['owner'] == {'kind': 'repository', 'id': repo_id, 'name': 'hub'}
    edit = await client.patch(f'/api/v1/tasks/scheduled/prefetch:{repo_id}', json={'schedule': '@hourly'})
    assert edit.status_code == 409

    body['proxy']['prefetch'] = {'warm': []}
    assert (await client.put('/api/v1/repositories/hub', json=body)).status_code == 200
    assert await services.db[JOB_DEFINITIONS].find_one({'_id': f'prefetch:{repo_id}'}) is None
    assert (await client.post('/api/v1/repositories/hub/prefetch/run')).status_code == 409
