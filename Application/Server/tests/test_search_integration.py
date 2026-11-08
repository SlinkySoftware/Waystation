"""Global search (T-U06) end to end: content written through the format index writers (and one backfilled by the
search-keys migration), searched through ``/api/v1/search``. RBAC: hidden repositories never reach results or facet
counts. Every MongoDB query the search issues is explained afterwards: it must use an index and never sort in memory
(ARCHITECTURE §9)."""

import hashlib
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
from waystation_common.db.collections import (
    APT_PACKAGES,
    ASSETS,
    DOCKER_MANIFESTS,
    DOCKER_TAGS,
    NPM_PACKAGES,
    REPOSITORIES,
    YUM_PACKAGES,
)
from waystation_common.db.migrations import MIGRATIONS, apply_migrations
from waystation_common.db.schema import ensure_schema
from waystation_common.ids import new_id
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.db import apt as apt_db
from waystation_server.db import assets as assets_db
from waystation_server.db import docker as docker_db
from waystation_server.db import npm as npm_db
from waystation_server.db import yum as yum_db
from waystation_server.proxy.records import AssetKind, StoredAsset, UpstreamMeta
from waystation_server.services import Services

pytestmark = pytest.mark.integration

API = '/api/v1/search'
PASSWORD = 'correct horse battery staple'
WATCHED = {ASSETS, NPM_PACKAGES, APT_PACKAGES, YUM_PACKAGES, DOCKER_TAGS, DOCKER_MANIFESTS}
NOISE = {'lsid', 'txnNumber', '$db', '$clusterTime', '$readPreference', 'readConcern', 'apiVersion', 'cursor'}
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
NGINX_DIGEST = 'sha256:' + 'a1' * 32
REDIS_DIGEST = 'sha256:' + 'b2' * 32
SETUP_BODY = b'#!/bin/sh\necho setup\n'


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
    """Explain every recorded search query: none may scan a whole collection or sort in memory."""
    assert recorder.commands, 'no search queries were recorded'
    for command in recorder.commands:
        if 'aggregate' in command:
            command = {**command, 'cursor': {}}
        plan = await db.command({'explain': command, 'verbosity': 'queryPlanner'})
        stages: list[str] = []
        _stages(plan, stages)
        assert 'COLLSCAN' not in stages, f'collection scan: {command}'
        assert 'SORT' not in stages, f'in-memory sort: {command}'
        assert any(s in stages for s in ('IXSCAN', 'COUNT_SCAN', 'DISTINCT_SCAN', 'EXPRESS_IXSCAN')), command
    recorder.commands.clear()


# ---------------------------------------------------------------------------- fixtures
@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(PASSWORD))


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
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


async def insert_repo(services: Services, name: str, fmt: str, repo_type: str = 'proxy') -> str:
    repo_id = f'repo-{name}'
    doc: dict[str, Any] = {
        '_id': repo_id,
        'name': name,
        'format': fmt,
        'type': repo_type,
        'online': True,
        'format_options': {},
        'created_at': NOW,
        'updated_at': NOW,
    }
    if repo_type == 'proxy':
        doc['proxy'] = {'upstream_url': 'https://upstream.example.com/', 'metadata_max_age_s': 3600}
    await services.db[REPOSITORIES].insert_one(doc)
    services.catalog.invalidate()
    return repo_id


async def save_file(
    services: Services, repo_id: str, path: str, body: bytes, kind: AssetKind = AssetKind.CONTENT
) -> str:
    sha = hashlib.sha256(body).hexdigest()
    await assets_db.save(
        services.db,
        StoredAsset(
            _id=new_id(),
            repo_id=repo_id,
            path=path,
            kind=kind,
            blob_sha256=sha,
            size=len(body),
            content_type='application/octet-stream',
            upstream=UpstreamMeta(url=f'https://upstream.example.com/{path}', fetched_at=NOW),
            created_at=NOW,
            updated_at=NOW,
        ),
    )
    return sha


@pytest.fixture
async def content(services: Services) -> dict[str, str]:
    """One repository per source, written through the index writers, plus a hosted repository anonymous cannot see."""
    db = services.db
    dock = await insert_repo(services, 'docker-hub', 'docker')
    await docker_db.record_tag(db, dock, 'library/nginx', 'latest', NGINX_DIGEST, authoritative=True)
    await docker_db.record_tag(db, dock, 'library/nginx', '1.27', NGINX_DIGEST, authoritative=True)
    await docker_db.record_tag(db, dock, 'bitnami/redis', '7', REDIS_DIGEST, authoritative=True)
    await docker_db.record_manifest(db, dock, 'library/nginx', NGINX_DIGEST, {'size_total': 4242, 'is_index': False})
    await save_file(services, dock, f'blobs/{NGINX_DIGEST}', b'layer')  # docker files are never name results

    npm = await insert_repo(services, 'npmjs', 'npm')
    for name in ('react', '@types/node', 'nginx-conf'):
        await npm_db.upsert(db, npm, name, {'latest': '2.0.0', 'version_count': 3})
    await save_file(services, npm, 'react/-/react-2.0.0.tgz', b'x' * 300)
    await save_file(services, npm, 'react', b'{}', AssetKind.METADATA)  # metadata is not searchable

    apt = await insert_repo(services, 'debian', 'apt')
    for arch in ('amd64', 'arm64'):
        stanzas = [
            apt_db.Stanza(
                f'pool/main/n/nginx/nginx_1.22.1-9_{arch}.deb', 'c3' * 32, 900, 'nginx', '1.22.1-9', arch, 'httpd'
            ),
            apt_db.Stanza(
                f'pool/main/o/openssl/openssl_3.0.15_{arch}.deb', 'd4' * 32, 800, 'openssl', '3.0.15', arch, ''
            ),
        ]
        await apt_db.record_packages(db, apt, stanzas, suite='bookworm', component='main', arch=arch, gen='g1')
    await save_file(services, apt, 'pool/main/n/nginx/nginx_1.22.1-9_amd64.deb', b'deb!')

    yum = await insert_repo(services, 'rocky', 'yum')
    listing = yum_db.Listing(
        'BaseOS/x86_64/os/Packages/n/nginx-1.24.0-1.el9.x86_64.rpm', 'sha256', 'e5' * 32, 700,
        name='nginx', epoch='1', version='1.24.0', release='1.el9', arch='x86_64',
    )  # fmt: skip
    await yum_db.record_primary(db, yum, 'BaseOS/x86_64/os/', [listing], gen='p1')

    raw = await insert_repo(services, 'tools', 'raw')
    setup = await save_file(services, raw, 'scripts/NGINX-Setup.sh', SETUP_BODY)
    await save_file(services, raw, 'docs/readme.txt', b'read me')

    secret = await insert_repo(services, 'secret', 'raw', 'hosted')
    await save_file(services, secret, 'private/nginx-private.tar', b'private')
    await save_file(services, secret, 'private/copy-of-setup.sh', SETUP_BODY)
    return {'docker': dock, 'npm': npm, 'apt': apt, 'yum': yum, 'raw': raw, 'secret': secret, 'setup_sha': setup}


async def everything(client: httpx.AsyncClient, **params: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    for _ in range(100):
        response = await client.get(API, params={**params, **({'cursor': cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        body = response.json()
        items += body['items']
        cursor = body['next_cursor']
        if cursor is None:
            return items
    raise AssertionError('paging did not end')


def facet(body: dict[str, Any], name: str) -> dict[str, int]:
    return {v['value']: v['count'] for v in body['facets'][name]}


def summary(items: list[dict[str, Any]]) -> list[tuple[str, str, str, str | None]]:
    return [(i['repository'], i['kind'], i['name'], i['version']) for i in items]


# ---------------------------------------------------------------------------- tests
async def test_name_search_across_sources_with_facets_and_links(
    admin: httpx.AsyncClient, services: Services, content: dict[str, str], recorder: Recorder
) -> None:
    recorder.on = True
    response = await admin.get(API, params={'q': 'NGINX'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['mode'] == 'name'
    assert body['next_cursor'] is None
    # (key, stream, repository, id) order: exact names first; library/nginx matches on its last segment
    assert summary(body['items']) == [
        ('docker-hub', 'image', 'library/nginx', 'latest'),
        ('docker-hub', 'image', 'library/nginx', '1.27'),
        ('debian', 'package', 'nginx', '1.22.1-9'),
        ('debian', 'package', 'nginx', '1.22.1-9'),
        ('rocky', 'package', 'nginx', '1:1.24.0-1.el9'),
        ('npmjs', 'package', 'nginx-conf', '2.0.0'),
        ('secret', 'file', 'nginx-private.tar', None),
        ('tools', 'file', 'NGINX-Setup.sh', None),
        ('debian', 'file', 'nginx_1.22.1-9_amd64.deb', None),
    ]
    image, deb_amd, deb_arm, rpm = body['items'][0], body['items'][2], body['items'][3], body['items'][4]
    assert image['format'] == 'docker'
    assert image['size'] == 4242
    assert image['sha256'] == NGINX_DIGEST
    assert image['browse'] == {'repository': 'docker-hub', 'query': {'image': 'library/nginx', 'ref': 'latest'}}
    assert {deb_amd['qualifier'], deb_arm['qualifier']} == {'bookworm/main amd64', 'bookworm/main arm64'}
    cached = {d['qualifier']: d['cached'] for d in (deb_amd, deb_arm)}
    assert cached == {'bookworm/main amd64': True, 'bookworm/main arm64': False}
    assert deb_amd['browse']['query']['q'] == 'nginx'
    assert rpm['qualifier'] == 'x86_64'
    assert rpm['cached'] is False
    assert rpm['browse']['query'] == {'path': 'BaseOS/x86_64/os/Packages/n'}
    tool = body['items'][-2]
    assert tool['path'] == 'scripts/NGINX-Setup.sh'
    assert tool['sha256'] == content['setup_sha']
    assert tool['browse'] == {'repository': 'tools', 'query': {'path': 'scripts'}}

    facets = body['facets']
    assert facets['total'] == 9
    assert facets['capped'] is False
    assert facet(body, 'format') == {'apt': 3, 'docker': 2, 'raw': 2, 'npm': 1, 'yum': 1}
    assert facet(body, 'kind') == {'package': 4, 'file': 3, 'image': 2}
    assert facet(body, 'type') == {'proxy': 8, 'hosted': 1}

    # npm: the package and its cached tarball; scoped names by scope or by last segment
    react = (await admin.get(API, params={'q': 'react'})).json()
    assert summary(react['items']) == [
        ('npmjs', 'package', 'react', '2.0.0'),
        ('npmjs', 'file', 'react-2.0.0.tgz', None),
    ]
    assert react['items'][0]['size'] == 300
    assert react['items'][1]['browse']['query'] == {'package': 'react'}
    for q in ('@types', 'node', '@TYPES/NO'):
        found = (await admin.get(API, params={'q': q})).json()['items']
        assert [i['name'] for i in found] == ['@types/node'], q
    # files: by the start of their name only; docker blobs and metadata are not name results
    assert (await admin.get(API, params={'q': 'setup'})).json()['items'] == []
    assert (await admin.get(API, params={'q': 'sha256'})).json()['items'] == []
    await assert_indexed(services.db, recorder)


async def test_filters_are_disjunctive_facets_and_version_terms(
    admin: httpx.AsyncClient, services: Services, content: dict[str, str], recorder: Recorder
) -> None:
    recorder.on = True
    apt_only = (await admin.get(API, params={'q': 'nginx', 'format': 'apt'})).json()
    assert {i['format'] for i in apt_only['items']} == {'apt'}
    assert apt_only['facets']['total'] == 3
    assert facet(apt_only, 'format')['docker'] == 2  # a facet still shows its alternatives
    assert facet(apt_only, 'kind') == {'package': 2, 'file': 1}
    assert facet(apt_only, 'repository') == {'debian': 3}

    two = (await admin.get(API, params=[('q', 'nginx'), ('format', 'apt'), ('format', 'yum')])).json()
    assert {i['repository'] for i in two['items']} == {'debian', 'rocky'}

    files = (await admin.get(API, params={'q': 'nginx', 'kind': 'file', 'type': 'proxy'})).json()
    assert summary(files['items']) == [
        ('tools', 'file', 'NGINX-Setup.sh', None),
        ('debian', 'file', 'nginx_1.22.1-9_amd64.deb', None),
    ]
    assert facet(files, 'type') == {'proxy': 2, 'hosted': 1}

    by_repo = (await admin.get(API, params={'q': 'nginx', 'repository': 'docker-hub'})).json()
    assert {i['repository'] for i in by_repo['items']} == {'docker-hub'}
    assert facet(by_repo, 'repository')['debian'] == 3

    # cached only: the apt/yum listings drop out, the cached .deb is still found as a file
    cached = (await admin.get(API, params={'q': 'nginx', 'cached': 'true'})).json()
    assert {i['kind'] for i in cached['items']} == {'image', 'package', 'file'}
    assert {i['format'] for i in cached['items']} == {'docker', 'npm', 'raw', 'apt'}
    assert all(i['cached'] for i in cached['items'])
    deb = (await admin.get(API, params={'q': 'nginx_', 'cached': 'true'})).json()
    assert summary(deb['items']) == [('debian', 'file', 'nginx_1.22.1-9_amd64.deb', None)]

    # "<name> <version>": version (tag) prefix; files have no version
    tagged = (await admin.get(API, params={'q': 'nginx 1.2'})).json()
    assert summary(tagged['items']) == [
        ('docker-hub', 'image', 'library/nginx', '1.27'),
        ('debian', 'package', 'nginx', '1.22.1-9'),
        ('debian', 'package', 'nginx', '1.22.1-9'),
        ('rocky', 'package', 'nginx', '1:1.24.0-1.el9'),
    ]
    assert (await admin.get(API, params={'q': 'nginx 9'})).json()['facets']['total'] == 0
    await assert_indexed(services.db, recorder)


async def test_cursor_paging_is_exact(
    admin: httpx.AsyncClient, services: Services, content: dict[str, str], recorder: Recorder
) -> None:
    recorder.on = True
    whole = (await admin.get(API, params={'limit': 100})).json()
    assert whole['next_cursor'] is None
    names = summary(whole['items'])
    assert len(names) == whole['facets']['total'] == 17
    for limit in (1, 2, 3, 7):
        paged = await everything(admin, limit=limit)
        assert summary(paged) == names, limit
    second = await admin.get(API, params={'q': 'nginx', 'limit': 3})
    page2 = await admin.get(API, params={'q': 'nginx', 'limit': 3, 'cursor': second.json()['next_cursor']})
    assert page2.json()['facets'] is None  # facets come with the first page
    await assert_indexed(services.db, recorder)

    assert (await admin.get(API, params={'cursor': '!!'})).status_code == 400
    assert (await admin.get(API, params={'limit': 101})).status_code == 422
    assert (await admin.get(API, params={'kind': 'folder'})).status_code == 422


async def test_hidden_repositories_never_leak(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, content: dict[str, str]
) -> None:
    # anonymous browses proxy repositories only: the hosted "secret" repository is never searched
    body = (await client.get(API, params={'q': 'nginx'})).json()
    assert 'secret' not in {i['repository'] for i in body['items']}
    assert 'secret' not in facet(body, 'repository')
    assert facet(body, 'type') == {'proxy': 8}
    assert body['facets']['total'] == 8
    everything_anon = await everything(client, limit=5)
    assert all(i['repository'] != 'secret' for i in everything_anon)
    named = (await client.get(API, params={'q': 'nginx', 'repository': 'secret'})).json()
    assert named['items'] == []
    assert named['facets']['repository'] == []
    # digest search too: the same blob is in a visible and in a hidden repository
    sha = content['setup_sha']
    anon = (await client.get(API, params={'q': f'sha256:{sha}'})).json()
    assert [(i['repository'], i['path']) for i in anon['items']] == [('tools', 'scripts/NGINX-Setup.sh')]
    assert facet(anon, 'repository') == {'tools': 1}
    seen = (await admin.get(API, params={'q': sha})).json()
    assert {i['repository'] for i in seen['items']} == {'tools', 'secret'}


async def test_digest_search(
    admin: httpx.AsyncClient, services: Services, content: dict[str, str], recorder: Recorder
) -> None:
    recorder.on = True
    # a manifest digest prefix: the tags pointing at it, and the cached blob with that digest
    body = (await admin.get(API, params={'q': NGINX_DIGEST[:20].upper()})).json()
    assert body['mode'] == 'digest'
    assert summary(body['items']) == [
        ('docker-hub', 'image', 'library/nginx', '1.27'),
        ('docker-hub', 'image', 'library/nginx', 'latest'),
    ]
    assert facet(body, 'kind') == {'image': 2}
    # a layer digest: the blob, also in a docker repository (digest search covers every format)
    layer = hashlib.sha256(b'layer').hexdigest()
    blob = (await admin.get(API, params={'q': f'sha256:{layer}'})).json()
    assert [(i['repository'], i['path'], i['browse']['query']) for i in blob['items']] == [
        ('docker-hub', f'blobs/{NGINX_DIGEST}', {})
    ]
    paged = await everything(admin, q=f'sha256:{content["setup_sha"]}', limit=1)
    assert [i['repository'] for i in paged] == ['secret', 'tools']
    assert (await admin.get(API, params={'q': 'sha256:abc'})).json()['mode'] == 'name'  # too short for a digest
    await assert_indexed(services.db, recorder)


async def test_migration_backfills_search_keys(admin: httpx.AsyncClient, services: Services) -> None:
    npm = await insert_repo(services, 'legacy', 'npm')
    raw = await insert_repo(services, 'legacy-raw', 'raw')
    db = services.db
    # documents written before the search keys existed
    await db[NPM_PACKAGES].insert_one({'repo_id': npm, 'name': '@Legacy/Pkg', 'latest': '1.0.0', 'updated_at': NOW})
    await db[ASSETS].insert_one(
        {
            '_id': new_id(),
            'repo_id': raw,
            'path': 'old/Legacy.bin',
            'kind': 'content',
            'blob_sha256': 'f' * 64,
            'size': 1,
        }
    )
    await db[ASSETS].insert_one(
        {'_id': new_id(), 'repo_id': raw, 'path': 'old/legacy.json', 'kind': 'metadata', 'blob_sha256': 'e' * 64}
    )
    assert (await admin.get(API, params={'q': 'legacy'})).json()['items'] == []
    migration = next(m for m in MIGRATIONS if m.id == '0002_search_keys')
    await migration.apply(db)
    await migration.apply(db)  # idempotent
    found = (await admin.get(API, params={'q': 'legacy'})).json()['items']
    assert [(i['repository'], i['name']) for i in found] == [('legacy-raw', 'Legacy.bin')]
    assert [i['name'] for i in (await admin.get(API, params={'q': 'pkg'})).json()['items']] == ['@Legacy/Pkg']
    doc = await db[NPM_PACKAGES].find_one({'repo_id': npm})
    assert doc is not None
    assert (doc['name_lc'], doc['base_lc']) == ('@legacy/pkg', 'pkg')
