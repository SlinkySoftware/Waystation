"""Cleanup policies (T-O01) against real MongoDB/Valkey: the REST API (CRUD, schedules, RBAC, attachment, dry-run
preview, run now) and the acceptance criterion "preview equals execution result on fixtures" (the worker's run
removes exactly what the preview listed, and nothing else)."""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_testing import asgi_client

from waystation_common.db.collections import (
    ASSETS,
    DOCKER_MANIFESTS,
    DOCKER_TAGS,
    JOB_DEFINITIONS,
    JOB_RUNS,
    REPOSITORIES,
    USERS,
)
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.security import passwords
from waystation_server.services import Services
from waystation_worker import cleanup as worker_cleanup

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
NOW = datetime.now(UTC)
L1, L2, CFG = ('sha256:' + c * 64 for c in '12c')
IDX, M_AMD, M_OLD = ('sha256:' + c * 64 for c in 'abe')


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(PASSWORD))


async def add_repo(services: Services, name: str, fmt: str) -> None:
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': f'id-{name}',
            'name': name,
            'format': fmt,
            'type': 'proxy',
            'online': True,
            'proxy': {'upstream_url': 'https://upstream.example.com/'},
            'format_options': {},
            'created_at': NOW,
            'updated_at': NOW,
        }
    )


def asset_doc(
    repo: str, path: str, *, age: float, size: int = 100, kind: str = 'content', **extra: Any
) -> dict[str, Any]:
    created = NOW - timedelta(days=age)
    return {
        '_id': f'{repo}:{path}',
        'repo_id': f'id-{repo}',
        'path': path,
        'kind': kind,
        'blob_sha256': f'{abs(hash(path)):064x}'[:64],
        'size': size,
        'content_type': 'application/octet-stream',
        'upstream': {'url': f'https://upstream.example.com/{path}', 'fetched_at': created},
        'created_at': created,
        'updated_at': created,
        **extra,
    }


async def seed_content(services: Services) -> None:
    db = services.db
    await db[ASSETS].insert_many(
        [
            asset_doc('npm', 'left-pad/-/left-pad-1.0.0.tgz', age=500, last_downloaded_at=NOW - timedelta(days=300)),
            asset_doc('npm', 'left-pad/-/left-pad-1.1.0.tgz', age=400, size=200),
            asset_doc('npm', 'left-pad/-/left-pad-1.10.0.tgz', age=10, last_downloaded_at=NOW - timedelta(days=1)),
            asset_doc('npm', 'is-odd/-/is-odd-3.0.1.tgz', age=200),
            asset_doc('npm', 'left-pad/-/packument.json', age=500, kind='metadata'),
            asset_doc('dockerhub-proxy', 'library/nginx/manifests/1.27', age=40, kind='metadata'),
            asset_doc('dockerhub-proxy', 'library/nginx/manifests/latest', age=1, kind='metadata'),
            *(asset_doc('dockerhub-proxy', f'library/nginx/manifests/{d}', age=40) for d in (IDX, M_AMD, M_OLD)),
            *(asset_doc('dockerhub-proxy', f'blobs/{d}', age=40, size=1000) for d in (L1, L2, CFG)),
        ]
    )
    image = {'repo_id': 'id-dockerhub-proxy', 'image': 'library/nginx'}
    await db[DOCKER_TAGS].insert_many(
        [
            {**image, 'tag': '1.27', 'manifest_digest': IDX, 'updated_at': NOW - timedelta(days=40)},
            {**image, 'tag': 'latest', 'manifest_digest': M_AMD, 'updated_at': NOW - timedelta(days=1)},
        ]
    )
    created = NOW - timedelta(days=40)
    await db[DOCKER_MANIFESTS].insert_many(
        [
            {
                **image,
                'digest': IDX,
                'is_index': True,
                'manifests': [{'digest': M_AMD}],
                'created_at': created,
                'updated_at': created,
            },
            {
                **image,
                'digest': M_AMD,
                'layers': [{'digest': L1}],
                'config_digest': CFG,
                'created_at': created,
                'updated_at': created,
            },
            {
                **image,
                'digest': M_OLD,
                'layers': [{'digest': L1}, {'digest': L2}],
                'created_at': created,
                'updated_at': created,
            },
        ]
    )


@pytest.fixture
async def world(services: Services) -> None:
    await ensure_bootstrap_admin(services.db, services.settings)
    await services.db[USERS].insert_one(
        {
            '_id': 'id-rita',
            'username': 'rita',
            'display_name': 'Rita',
            'source': 'local',
            'password_hash': await passwords.hash_password(PASSWORD),
            'roles': ['reader'],
            'created_at': NOW,
        }
    )
    for name, fmt in (('npm', 'npm'), ('dockerhub-proxy', 'docker'), ('files', 'raw')):
        await add_repo(services, name, fmt)
    services.catalog.invalidate()
    await seed_content(services)


async def login(client: httpx.AsyncClient, username: str) -> httpx.AsyncClient:
    response = await client.post('/api/v1/auth/login', json={'username': username, 'password': PASSWORD})
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return client


@pytest.fixture
async def admin(services: Services, world: None) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(services) as client:
        yield await login(client, 'admin')


@pytest.fixture
async def rita(services: Services, world: None) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(services) as client:
        yield await login(client, 'rita')


async def create(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post('/api/v1/cleanup-policies', json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def attach(client: httpx.AsyncClient, repo: str, *policy_ids: str) -> httpx.Response:
    return await client.put(f'/api/v1/repositories/{repo}/cleanup', json={'policy_ids': list(policy_ids)})


# ---------------------------------------------------------------------------- API
async def test_create_update_delete_with_schedule(admin: httpx.AsyncClient, services: Services) -> None:
    policy = await create(
        admin,
        name='npm-unused-90d',
        format='npm',
        criteria={'last_downloaded_older_than_days': 90},
        schedule=' 0  3 * * * ',
    )
    assert policy['schedule'] == '0 3 * * *' and policy['enabled'] and policy['repositories'] == []
    assert policy['next_run_at'] is not None and policy['last_run'] is None
    assert policy['criteria']['last_downloaded_older_than_days'] == 90
    definition = await services.db[JOB_DEFINITIONS].find_one({'_id': f'cleanup:{policy["id"]}'})
    assert definition is not None and definition['kind'] == 'cleanup.run' and definition['enabled']
    assert definition['params'] == {'policy_id': policy['id']}

    duplicate = await admin.post(
        '/api/v1/cleanup-policies',
        json={'name': 'NPM-unused-90D', 'format': 'npm', 'criteria': {'prerelease_only': True}},
    )
    assert duplicate.status_code == 409

    body = {'name': 'npm-unused-90d', 'format': 'npm', 'criteria': {'prerelease_only': True}, 'enabled': False}
    updated = (await admin.put(f'/api/v1/cleanup-policies/{policy["id"]}', json=body)).json()
    assert updated['schedule'] is None and updated['next_run_at'] is None and not updated['enabled']
    assert await services.db[JOB_DEFINITIONS].find_one({'_id': f'cleanup:{policy["id"]}'}) is None

    assert (await attach(admin, 'npm', policy['id'])).status_code == 200
    assert (await admin.get(f'/api/v1/cleanup-policies/{policy["id"]}')).json()['repositories'] == ['npm']
    assert (await admin.delete(f'/api/v1/cleanup-policies/{policy["id"]}')).status_code == 204
    assert (await admin.get(f'/api/v1/cleanup-policies/{policy["id"]}')).status_code == 404
    assert (await admin.get('/api/v1/repositories/npm/cleanup')).json()['policies'] == []
    repo = await services.db[REPOSITORIES].find_one({'_id': 'id-npm'})
    assert repo is not None and repo['cleanup_policy_ids'] == []


@pytest.mark.parametrize(
    ('body', 'field'),
    [
        ({'format': 'npm', 'criteria': {}}, 'criteria'),
        ({'format': 'npm', 'criteria': {'name_exclude': 'x'}}, 'criteria'),
        ({'format': 'npm', 'criteria': {'untagged_older_than_days': 3}}, 'criteria.untagged_older_than_days'),
        ({'format': 'npm', 'criteria': {'name_include': '('}}, 'criteria.name_include'),
        ({'format': 'npm', 'criteria': {'prerelease_only': True}, 'schedule': 'often'}, 'schedule'),
        ({'format': 'nope', 'criteria': {'prerelease_only': True}}, 'format'),
    ],
)
async def test_validation(admin: httpx.AsyncClient, body: dict[str, Any], field: str) -> None:
    response = await admin.post('/api/v1/cleanup-policies', json={'name': 'p1', **body})
    assert response.status_code == 422, response.text
    assert field in [e['field'] for e in response.json()['errors']]


async def test_rbac(admin: httpx.AsyncClient, rita: httpx.AsyncClient, services: Services) -> None:
    policy = await create(admin, name='p1', format='*', criteria={'published_older_than_days': 30})
    assert (await rita.get('/api/v1/cleanup-policies')).status_code == 403
    assert (await rita.get(f'/api/v1/cleanup-policies/{policy["id"]}')).status_code == 404
    assert (await rita.get(f'/api/v1/cleanup-policies/{policy["id"]}/runs')).status_code == 404
    assert (await rita.post(f'/api/v1/cleanup-policies/{policy["id"]}/run')).status_code == 403
    assert (await rita.post('/api/v1/cleanup-policies', json={'name': 'x'})).status_code in (403, 422)
    assert (await rita.get('/api/v1/repositories/npm/cleanup')).status_code == 403
    assert (await attach(rita, 'npm', policy['id'])).status_code == 403
    preview = {'format': 'npm', 'criteria': {'published_older_than_days': 1}}
    assert (await rita.post('/api/v1/cleanup-policies/preview', json=preview)).status_code == 403
    scoped = await rita.post('/api/v1/cleanup-policies/preview', json={**preview, 'repositories': ['npm']})
    assert scoped.status_code == 403
    async with asgi_client(services) as anon:
        assert (await anon.get('/api/v1/cleanup-policies')).status_code == 401
        assert (await anon.get('/api/v1/repositories/npm/cleanup')).status_code == 401  # visible proxy, no manage


async def test_attach_checks_format_and_ids(admin: httpx.AsyncClient) -> None:
    npm = await create(admin, name='npm-old', format='npm', criteria={'published_older_than_days': 30})
    every = await create(admin, name='all-old', format='*', criteria={'published_older_than_days': 365})
    bad = await attach(admin, 'dockerhub-proxy', npm['id'], 'missing')
    assert bad.status_code == 422
    assert [(e['field'], e['code']) for e in bad.json()['errors']] == [
        ('policy_ids.0', 'format'),
        ('policy_ids.1', 'unknown'),
    ]
    ok = await attach(admin, 'npm', every['id'], npm['id'], every['id'])
    assert [p['name'] for p in ok.json()['policies']] == ['all-old', 'npm-old']  # run order, deduplicated
    # a format change that would orphan an attachment is refused
    body = {'name': 'npm-old', 'format': 'docker', 'criteria': {'published_older_than_days': 30}}
    clash = await admin.put(f'/api/v1/cleanup-policies/{npm["id"]}', json=body)
    assert clash.status_code == 422 and clash.json()['errors'][0]['field'] == 'format'


async def test_preview_pages_and_totals(admin: httpx.AsyncClient) -> None:
    body = {'format': 'npm', 'criteria': {'last_downloaded_older_than_days': 100}, 'limit': 2}
    first = (await admin.post('/api/v1/cleanup-policies/preview', json=body)).json()
    assert first['count'] == 3 and first['bytes'] == 400 and not first['truncated']
    assert first['repositories'] == [{'name': 'npm', 'format': 'npm', 'count': 3, 'bytes': 400, 'truncated': False}]
    assert [i['path'] for i in first['items']] == ['is-odd/-/is-odd-3.0.1.tgz', 'left-pad/-/left-pad-1.0.0.tgz']
    assert first['items'][1]['reason'] == 'not downloaded for 300 days'
    second = (
        await admin.post('/api/v1/cleanup-policies/preview', json={**body, 'cursor': first['next_cursor']})
    ).json()
    assert [i['path'] for i in second['items']] == ['left-pad/-/left-pad-1.1.0.tgz'] and second['next_cursor'] is None
    totals = (await admin.post('/api/v1/cleanup-policies/preview', json={**body, 'limit': 0})).json()
    assert totals['count'] == 3 and totals['items'] == [] and totals['next_cursor'] is None
    wrong = await admin.post('/api/v1/cleanup-policies/preview', json={**body, 'repositories': ['dockerhub-proxy']})
    assert wrong.status_code == 422
    missing = await admin.post('/api/v1/cleanup-policies/preview', json={**body, 'repositories': ['nope']})
    assert missing.status_code == 404
    bad_cursor = await admin.post('/api/v1/cleanup-policies/preview', json={**body, 'cursor': '!!'})
    assert bad_cursor.status_code == 400


async def test_run_now_enqueues_once(admin: httpx.AsyncClient, services: Services) -> None:
    policy = await create(admin, name='p1', format='npm', criteria={'prerelease_only': True})
    first = await admin.post(f'/api/v1/cleanup-policies/{policy["id"]}/run')
    assert first.status_code == 202 and first.json()['status'] == 'enqueued'
    again = (await admin.post(f'/api/v1/cleanup-policies/{policy["id"]}/run')).json()
    assert again == {'job_id': first.json()['job_id'], 'status': 'merged'}
    started = NOW
    await services.db[JOB_RUNS].insert_one(
        {
            '_id': 'run-1',
            'job_id': first.json()['job_id'],
            'definition_id': None,
            'kind': 'cleanup.run',
            'queue': 'maintenance',
            'dedupe_key': f'cleanup:{policy["id"]}',
            'attempt': 1,
            'status': 'succeeded',
            'progress': {'done': 3, 'total': 3, 'message': None},
            'started_at': started,
            'finished_at': started,
            'result': {'removed': 3, 'bytes': 400},
            'expires_at': started + timedelta(days=90),
        }
    )
    runs = (await admin.get(f'/api/v1/cleanup-policies/{policy["id"]}/runs')).json()['items']
    assert [(r['id'], r['trigger'], r['removed'], r['bytes']) for r in runs] == [('run-1', 'manual', 3, 400)]
    listed = (await admin.get('/api/v1/cleanup-policies')).json()['items']
    assert listed[0]['last_run']['id'] == 'run-1'


# ---------------------------------------------------------------------------- blob GC (T-O02)
async def test_gc_status_run_and_metrics(admin: httpx.AsyncClient, rita: httpx.AsyncClient, services: Services) -> None:
    status = (await admin.get('/api/v1/maintenance/gc')).json()
    assert status['enabled'] and status['schedule'] == '0 */6 * * *' and status['grace_hours'] == 24
    assert status['next_run_at'] is not None and status['runs'] == []
    assert (status['pending_blobs'], status['pending_bytes']) == (0, 0)
    await services.db[JOB_DEFINITIONS].insert_one(
        {'_id': 'blob.gc', 'kind': 'blob.gc', 'schedule': '@daily', 'params': {'grace_s': 7200}, 'enabled': False}
    )
    await services.db['blobs'].insert_one({'_id': 'f' * 64, 'size': 5, 'gc_marked_at': NOW})
    await services.db[JOB_RUNS].insert_one(
        {
            '_id': 'gc-1',
            'job_id': 'j',
            'definition_id': 'blob.gc',
            'kind': 'blob.gc',
            'status': 'succeeded',
            'progress': {'done': 9, 'total': 9, 'message': None},
            'started_at': NOW,
            'finished_at': NOW,
            'result': {'freed_blobs': 2, 'freed_bytes': 2048, 'marked': 1, 'uploads_removed': 3},
        }
    )
    status = (await admin.get('/api/v1/maintenance/gc')).json()
    assert not status['enabled'] and status['next_run_at'] is None and status['grace_hours'] == 2
    assert (status['pending_blobs'], status['pending_bytes']) == (1, 5)
    run = status['runs'][0]
    assert (run['trigger'], run['freed_bytes'], run['uploads_removed']) == ('schedule', 2048, 3)
    accepted = await admin.post('/api/v1/maintenance/gc/run')
    assert accepted.status_code == 202 and accepted.json()['status'] == 'enqueued'
    assert (await rita.get('/api/v1/maintenance/gc')).status_code == 403
    assert (await rita.post('/api/v1/maintenance/gc/run')).status_code == 403

    await services.valkey.hset('maintenance:totals', mapping={'gc_freed_bytes': '4096', 'cleanup_removed': '7'})
    metrics = (await admin.get('/metrics')).text
    assert 'waystation_gc_freed_bytes_total 4096.0' in metrics
    assert 'waystation_cleanup_removed_items_total 7.0' in metrics
    assert 'waystation_gc_freed_blobs_total 0.0' in metrics


# ---------------------------------------------------------------------------- preview == run
async def remaining(services: Services) -> dict[str, set[str]]:
    return {
        'assets': {d['_id'] async for d in services.db[ASSETS].find({}, {'_id': 1})},
        'tags': {f'{d["image"]}:{d["tag"]}' async for d in services.db[DOCKER_TAGS].find({})},
        'manifests': {d['digest'] async for d in services.db[DOCKER_MANIFESTS].find({})},
    }


@pytest.mark.parametrize(
    'criteria',
    [
        {'last_downloaded_older_than_days': 100},
        {'keep_latest_n_versions': 1},
        {'published_older_than_days': 30, 'untagged_older_than_days': 30},
        {'version_include': '^1\\.27$'},
        {'untagged_older_than_days': 7},
    ],
)
async def test_preview_equals_run(admin: httpx.AsyncClient, services: Services, criteria: dict[str, Any]) -> None:
    policy = await create(admin, name='every', format='*', criteria=criteria)
    for repo in ('npm', 'dockerhub-proxy'):
        assert (await attach(admin, repo, policy['id'])).status_code == 200
    preview = (
        await admin.post(
            '/api/v1/cleanup-policies/preview',
            json={'format': '*', 'criteria': criteria, 'policy_id': policy['id'], 'limit': 500},
        )
    ).json()
    assert preview['count'] > 0
    before = await remaining(services)

    _, outcomes = await worker_cleanup.run_policy(services.db, policy['id'], now=datetime.now(UTC))

    after = await remaining(services)
    removed_assets = before['assets'] - after['assets']
    expected_assets = {
        f'{i["repository"]}:{i["path"]}'
        for i in preview['items']
        if f'{i["repository"]}:{i["path"]}' in before['assets']
    }
    assert removed_assets == expected_assets
    assert before['tags'] - after['tags'] == {
        f'{i["name"]}:{i["version"]}' for i in preview['items'] if i['kind'] == 'tag'
    }
    assert before['manifests'] - after['manifests'] == {
        i['version'] for i in preview['items'] if i['kind'] == 'manifest'
    }
    assert sum(o.removed.items for o in outcomes) == preview['count']
    assert sum(o.removed.bytes for o in outcomes) == preview['bytes']
    # metadata (packuments) never goes; a second run finds nothing left to do
    assert 'npm:left-pad/-/packument.json' in after['assets']
    again = (
        await admin.post(
            '/api/v1/cleanup-policies/preview', json={'format': '*', 'criteria': criteria, 'policy_id': policy['id']}
        )
    ).json()
    if 'keep_latest_n_versions' not in criteria:
        assert again['count'] == 0
