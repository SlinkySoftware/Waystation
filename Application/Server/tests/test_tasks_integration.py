"""Tasks and workers (T-O04) against real MongoDB/Valkey: scheduled tasks (list, schedule edits, run now), job runs
(filters, cursor paging, detail with the log), workers and queues, dead letters (retry and discard, both idempotent),
RBAC, audit records and the ``tasks`` topic of the event stream."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, TypeAdapter
from ws_testing import asgi_client

from waystation_common.db.collections import AUDIT, CLEANUP_POLICIES, JOB_DEFINITIONS, JOB_RUNS, USERS, WORKERS
from waystation_common.events import ActivityEvent, EventType, JobLiveMessage, JobProgress, keys
from waystation_common.jobs import BlobGc, JobEnvelope, StatsRollup
from waystation_common.jobs import keys as job_keys
from waystation_common.settings import Settings
from waystation_server.api.live import LiveEvent, _may_follow_tasks
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.security import passwords
from waystation_server.security.principal import AuthMethod, Principal, PrincipalKind
from waystation_server.services import Services
from waystation_server.sse.hub import Subscriber
from waystation_server.sse.stream import event_stream

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
NOW = datetime.now(UTC).replace(microsecond=0)
API = '/api/v1/tasks'
EVENTS: TypeAdapter[Any] = TypeAdapter(LiveEvent)


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(PASSWORD))


async def login(client: httpx.AsyncClient, username: str) -> httpx.AsyncClient:
    response = await client.post('/api/v1/auth/login', json={'username': username, 'password': PASSWORD})
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return client


@pytest.fixture
async def world(services: Services) -> None:
    db = services.db
    await ensure_bootstrap_admin(db, services.settings)
    await db[USERS].insert_one(
        {
            '_id': 'id-vera',
            'username': 'vera',
            'display_name': 'Vera',
            'source': 'local',
            'password_hash': await passwords.hash_password(PASSWORD),
            'roles': ['viewer'],
            'created_at': NOW,
        }
    )
    await db[CLEANUP_POLICIES].insert_one(
        {'_id': 'p1', 'name': 'old-snapshots', 'format': '*', 'criteria': {}, 'schedule': '0 3 * * *', 'enabled': True}
    )
    definitions = [
        {'_id': 'blob.gc', 'kind': BlobGc.kind, 'params': {'grace_s': 86400, 'upload_age_s': 86400}},
        {'_id': 'stats.rollup', 'kind': StatsRollup.kind, 'params': {}, 'schedule': '@every 10s', 'queue': 'stats'},
        {'_id': 'cleanup:p1', 'kind': 'cleanup.run', 'params': {'policy_id': 'p1'}, 'schedule': '0 3 * * *'},
    ]
    await db[JOB_DEFINITIONS].insert_many(
        [
            {
                'schedule': '0 */6 * * *',
                'queue': None,
                'enabled': True,
                'next_run_at': None,
                'last_fired_at': None,
                **d,
            }
            for d in definitions
        ]
    )


@pytest.fixture
async def admin(services: Services, world: None) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(services) as client:
        yield await login(client, 'admin')


@pytest.fixture
async def vera(services: Services, world: None) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(services) as client:
        yield await login(client, 'vera')


def run_doc(run_id: str, *, minutes_ago: float, **extra: Any) -> dict[str, Any]:
    started = NOW - timedelta(minutes=minutes_ago)
    return {
        '_id': run_id,
        'job_id': f'job-{run_id}',
        'definition_id': None,
        'kind': BlobGc.kind,
        'queue': 'maintenance',
        'dedupe_key': 'blob.gc',
        'attempt': 1,
        'status': 'succeeded',
        'started_at': started,
        'finished_at': started + timedelta(seconds=3),
        'progress': {'done': 10, 'total': 10, 'message': None},
        'worker_id': 'w-1',
        'log': [],
        'result': {},
        'expires_at': NOW + timedelta(days=90),
        **extra,
    }


async def audit_records(services: Services, action: str) -> list[dict[str, Any]]:
    await services.audit.flush()
    return [doc async for doc in services.db[AUDIT].find({'action': action}, sort=[('ts', 1)])]


# ---------------------------------------------------------------------------- scheduled tasks
async def test_scheduled_tasks_list_edit_and_run(admin: httpx.AsyncClient, services: Services) -> None:
    db = services.db
    await db[JOB_RUNS].insert_many(
        [
            run_doc('r-old', minutes_ago=400, definition_id='blob.gc'),
            run_doc('r-new', minutes_ago=30, definition_id='blob.gc', status='dead', error='disk full'),
        ]
    )
    listing = await admin.get(f'{API}/scheduled')
    assert listing.status_code == 200, listing.text
    tasks = {t['id']: t for t in listing.json()['items']}
    assert list(tasks) == ['blob.gc', 'cleanup:p1', 'stats.rollup']
    gc = tasks['blob.gc']
    assert (gc['last_run']['id'], gc['last_run']['status'], gc['last_run']['trigger']) == ('r-new', 'dead', 'schedule')
    assert gc['next_run_at'] is not None  # derived from the schedule until the scheduler sets it
    assert gc['owner'] is None and gc['routine'] is False
    assert tasks['stats.rollup']['routine'] is True
    assert tasks['stats.rollup']['queue'] == 'stats'
    assert tasks['cleanup:p1']['owner'] == {'kind': 'cleanup_policy', 'id': 'p1', 'name': 'old-snapshots'}

    # edit: schedule validated and normalised; a changed schedule restarts the timer
    await db[JOB_DEFINITIONS].update_one({'_id': 'blob.gc'}, {'$set': {'next_run_at': NOW + timedelta(hours=5)}})
    bad = await admin.patch(f'{API}/scheduled/blob.gc', json={'schedule': '61 * * * *'})
    assert bad.status_code == 422
    assert bad.json()['errors'][0]['field'] == 'schedule'
    too_often = await admin.patch(f'{API}/scheduled/blob.gc', json={'schedule': '@every 2s'})
    assert too_often.status_code == 422
    changed = await admin.patch(f'{API}/scheduled/blob.gc', json={'schedule': ' 30  2 * * * '})
    assert changed.status_code == 200, changed.text
    assert changed.json()['schedule'] == '30 2 * * *'
    stored = await db[JOB_DEFINITIONS].find_one({'_id': 'blob.gc'})
    assert stored is not None and stored['schedule'] == '30 2 * * *' and stored['next_run_at'] is None
    disabled = await admin.patch(f'{API}/scheduled/blob.gc', json={'enabled': False})
    assert (disabled.json()['enabled'], disabled.json()['next_run_at']) == (False, None)
    unchanged = await admin.patch(f'{API}/scheduled/blob.gc', json={'enabled': False})
    assert unchanged.status_code == 200
    owned = await admin.patch(f'{API}/scheduled/cleanup:p1', json={'schedule': '@daily'})
    assert owned.status_code == 409
    assert (await admin.patch(f'{API}/scheduled/nope', json={'enabled': True})).status_code == 404
    assert (await admin.patch(f'{API}/scheduled/blob.gc', json={'params': {}})).status_code == 422

    updates = await audit_records(services, 'task.update')
    assert len(updates) == 2  # the no-op PATCH records nothing
    assert {c['field']: (c['before'], c['after']) for c in updates[0]['changes']} == {
        'schedule': ('0 */6 * * *', '30 2 * * *')
    }
    assert updates[0]['target'] == {'kind': 'task', 'id': 'blob.gc', 'name': 'blob.gc'}

    # run now: queued once for the task (a disabled task can still be run by hand), then merged
    first = await admin.post(f'{API}/scheduled/blob.gc/run')
    assert first.status_code == 202, first.text
    assert first.json()['status'] == 'enqueued'
    again = await admin.post(f'{API}/scheduled/blob.gc/run')
    assert again.json() == {'job_id': first.json()['job_id'], 'status': 'merged'}
    entries = await services.valkey.xrange(job_keys.stream('maintenance'))
    assert len(entries) == 1
    envelope = JobEnvelope.model_validate_json(entries[0][1]['job'])
    assert (envelope.kind, envelope.definition_id, envelope.trigger) == ('blob.gc', 'blob.gc', 'manual')
    assert envelope.payload == {'grace_s': 86400, 'upload_age_s': 86400}
    assert (await admin.post(f'{API}/scheduled/nope/run')).status_code == 404
    runs = await audit_records(services, 'task.run')
    assert [r['details']['status'] for r in runs] == ['enqueued', 'merged']


# ---------------------------------------------------------------------------- runs
async def test_runs_filters_paging_and_detail(admin: httpx.AsyncClient, services: Services) -> None:
    docs = [run_doc(f'r{i:02d}', minutes_ago=i) for i in range(1, 8)]
    docs[0].update(status='running', finished_at=None, progress={'done': 3, 'total': 10, 'message': 'sweeping'})
    docs[1].update(status='failed', error='boom', attempt=2, trigger='retry')
    docs[2].update(kind='cleanup.run', dedupe_key='cleanup:p1', definition_id='cleanup:p1')
    docs.append(run_doc('roll', minutes_ago=0.5, kind=StatsRollup.kind, queue='stats', definition_id='stats.rollup'))
    docs[0]['log'] = [f'2026-11-09T10:00:0{i}+00:00 line {i}' for i in range(3)]
    await services.db[JOB_RUNS].insert_many(docs)

    page = (await admin.get(f'{API}/runs', params={'limit': 3})).json()
    assert [r['id'] for r in page['items']] == ['r01', 'r02', 'r03']  # newest first, the routine rollup hidden
    assert page['items'][0]['progress'] == {'done': 3, 'total': 10, 'message': 'sweeping'}
    assert page['items'][0]['duration_ms'] is None
    assert page['items'][1]['trigger'] == 'retry'
    seen = [r['id'] for r in page['items']]
    cursor = page['next_cursor']
    while cursor:
        page = (await admin.get(f'{API}/runs', params={'limit': 3, 'cursor': cursor})).json()
        seen += [r['id'] for r in page['items']]
        cursor = page['next_cursor']
    assert seen == [f'r{i:02d}' for i in range(1, 8)]

    routine = (await admin.get(f'{API}/runs', params={'include_routine': 'true'})).json()
    assert routine['items'][0]['id'] == 'roll'
    by_task = (await admin.get(f'{API}/runs', params={'task': 'stats.rollup'})).json()
    assert [r['id'] for r in by_task['items']] == ['roll']
    failing = (await admin.get(f'{API}/runs', params=[('status', 'failed'), ('status', 'running')])).json()
    assert [r['id'] for r in failing['items']] == ['r01', 'r02']
    cleanup = (await admin.get(f'{API}/runs', params={'kind': 'cleanup.run'})).json()
    assert [(r['id'], r['task_id'], r['trigger']) for r in cleanup['items']] == [('r03', 'cleanup:p1', 'schedule')]
    assert (await admin.get(f'{API}/runs', params={'queue': 'nope'})).status_code == 422
    assert (await admin.get(f'{API}/runs', params={'cursor': '!!'})).status_code == 422

    detail = (await admin.get(f'{API}/runs/r01')).json()
    assert detail['log'] == docs[0]['log']
    assert detail['status'] == 'running'
    assert (await admin.get(f'{API}/runs/missing')).status_code == 404


# ---------------------------------------------------------------------------- workers
async def test_workers_and_queues(admin: httpx.AsyncClient, services: Services) -> None:
    await services.db[WORKERS].insert_many(
        [
            {
                '_id': 'host-a-1-abc',
                'hostname': 'host-a',
                'version': '0.1.0',
                'queues': ['maintenance', 'stats'],
                'concurrency': 4,
                'started_at': NOW - timedelta(hours=2),
                'heartbeat_at': datetime.now(UTC) - timedelta(seconds=4),
                'leader': True,
                'running_jobs': [
                    {'job_id': 'j1', 'run_id': 'r1', 'kind': 'blob.gc', 'queue': 'maintenance', 'started_at': NOW}
                ],
            },
            {
                '_id': 'host-b-7-def',
                'hostname': 'host-b',
                'version': '0.1.0',
                'queues': ['index'],
                'started_at': NOW - timedelta(hours=1),
                'heartbeat_at': datetime.now(UTC) - timedelta(minutes=3),
                'leader': False,
                'running_jobs': [{'job_id': 'j2', 'kind': 'index.apt', 'queue': 'index'}],  # older worker: no run id
            },
        ]
    )
    await services.jobs.queue.enqueue(JobEnvelope.for_payload(BlobGc()))
    overview = (await admin.get(f'{API}/workers')).json()
    a, b = overview['workers']
    assert (a['state'], a['leader'], a['concurrency']) == ('ok', True, 4)
    assert 3 <= a['heartbeat_age_s'] < 30
    assert a['running_jobs'][0]['run_id'] == 'r1'
    assert (b['state'], b['running_jobs'][0]['run_id'], b['concurrency']) == ('lost', None, None)
    queues = {q['name']: q for q in overview['queues']}
    assert set(queues) == {'index', 'maintenance', 'stats', 'prefetch', 'import'}
    assert queues['maintenance']['waiting'] == 1
    assert overview['dead_letters'] == 0


# ---------------------------------------------------------------------------- dead letters
async def add_dead(services: Services, envelope: JobEnvelope | None, error: str) -> str:
    fields = {'job': envelope.to_json() if envelope else '{broken', 'error': error, 'ts': NOW.isoformat()}
    return str(await services.valkey.xadd(job_keys.DEAD, fields))  # type: ignore[arg-type]


async def test_dead_letters_retry_and_discard(admin: httpx.AsyncClient, services: Services) -> None:
    gc = JobEnvelope.for_payload(BlobGc(grace_s=60), definition_id='blob.gc').model_copy(update={'attempt': 3})
    gc_id = await add_dead(services, gc, 'OSError: disk full')
    broken_id = await add_dead(services, None, 'malformed job entry')
    other = JobEnvelope.for_payload(StatsRollup()).model_copy(update={'attempt': 5})
    other_id = await add_dead(services, other, 'gave up')

    page = (await admin.get(f'{API}/dead-letters', params={'limit': 2})).json()
    assert page['total'] == 3
    assert [d['id'] for d in page['items']] == [other_id, broken_id]
    assert page['items'][1]['retryable'] is False
    rest = (await admin.get(f'{API}/dead-letters', params={'cursor': page['next_cursor']})).json()
    (letter,) = rest['items']
    assert rest['next_cursor'] is None
    assert (letter['id'], letter['kind'], letter['attempts'], letter['task_id']) == (gc_id, 'blob.gc', 3, 'blob.gc')
    assert letter['payload'] == {'grace_s': 60, 'upload_age_s': 86400}
    assert letter['error'] == 'OSError: disk full'

    # retry: a fresh job (attempt 0, trigger retry) and the dead letter is gone; retrying again changes nothing
    retried = await admin.post(f'{API}/dead-letters/{gc_id}/retry')
    assert retried.status_code == 202, retried.text
    accepted = retried.json()
    assert accepted['status'] == 'enqueued' and accepted['job_id'] != gc.id
    entries = await services.valkey.xrange(job_keys.stream('maintenance'))
    (queued,) = [JobEnvelope.model_validate_json(fields['job']) for _, fields in entries]
    assert (queued.id, queued.attempt, queued.trigger, queued.definition_id) == (
        accepted['job_id'],
        0,
        'retry',
        'blob.gc',
    )
    assert queued.payload == gc.payload
    again = await admin.post(f'{API}/dead-letters/{gc_id}/retry')
    assert again.status_code == 202
    assert again.json() == {'job_id': accepted['job_id'], 'status': 'merged'}
    assert await services.valkey.xlen(job_keys.stream('maintenance')) == 1
    assert (await admin.post(f'{API}/dead-letters/{broken_id}/retry')).status_code == 409

    # discard: idempotent
    assert (await admin.delete(f'{API}/dead-letters/{other_id}')).status_code == 204
    assert (await admin.delete(f'{API}/dead-letters/{other_id}')).status_code == 204
    assert (await admin.post(f'{API}/dead-letters/{other_id}/retry')).status_code == 404
    assert (await admin.get(f'{API}/dead-letters')).json()['total'] == 1
    assert (await admin.delete(f'{API}/dead-letters/not-an-id')).status_code == 422

    (retry_record,) = await audit_records(services, 'task.retry')
    assert retry_record['target'] == {'kind': 'job', 'id': gc.id, 'name': 'blob.gc'}
    assert retry_record['details']['job_id'] == accepted['job_id']
    (discard_record,) = await audit_records(services, 'task.discard')
    assert discard_record['details']['dead_letter'] == other_id


async def test_concurrent_retries_queue_once(admin: httpx.AsyncClient, services: Services) -> None:
    letter = await add_dead(services, JobEnvelope.for_payload(BlobGc()), 'boom')
    results = await asyncio.gather(*(admin.post(f'{API}/dead-letters/{letter}/retry') for _ in range(5)))
    assert {r.status_code for r in results} == {202}
    assert len({r.json()['job_id'] for r in results}) == 1
    assert await services.valkey.xlen(job_keys.stream('maintenance')) == 1
    assert len(await audit_records(services, 'task.retry')) == 1


# ---------------------------------------------------------------------------- RBAC
@pytest.mark.parametrize(
    ('method', 'path'),
    [
        ('GET', '/scheduled'),
        ('PATCH', '/scheduled/blob.gc'),
        ('POST', '/scheduled/blob.gc/run'),
        ('GET', '/runs'),
        ('GET', '/runs/r1'),
        ('GET', '/workers'),
        ('GET', '/dead-letters'),
        ('POST', '/dead-letters/1-0/retry'),
        ('DELETE', '/dead-letters/1-0'),
    ],
)
async def test_tasks_need_admin(services: Services, vera: httpx.AsyncClient, method: str, path: str) -> None:
    body = {'enabled': False} if method == 'PATCH' else None
    assert (await vera.request(method, f'{API}{path}', json=body)).status_code == 403
    async with asgi_client(services) as anonymous:
        assert (await anonymous.request(method, f'{API}{path}', json=body)).status_code == 401


# ---------------------------------------------------------------------------- live
async def _collect(gen: AsyncIterator[bytes], frames: list[tuple[str, dict[str, Any]]]) -> None:
    buffer = ''
    async for chunk in gen:
        buffer += chunk.decode()
        while '\n\n' in buffer:
            block, buffer = buffer.split('\n\n', 1)
            fields = dict(
                line.split(': ', 1) for line in block.split('\n') if ': ' in line and not line.startswith(':')
            )
            if 'event' in fields:
                frames.append((fields['event'], json.loads(fields['data'])))


async def test_tasks_topic_streams_job_progress(vera: httpx.AsyncClient, services: Services) -> None:
    refused = await vera.get('/api/v1/events/stream', params={'topics': 'tasks'})
    assert refused.status_code == 403
    services.hub.tasks_access = _may_follow_tasks  # what the route installs
    admin = Principal(PrincipalKind.USER, 'id-admin', 'admin', ('admin',), AuthMethod.SESSION)
    viewer = Principal(PrincipalKind.USER, 'id-vera', 'vera', ('viewer',), AuthMethod.SESSION)
    frames: list[tuple[str, dict[str, Any]]] = []
    hidden: list[tuple[str, dict[str, Any]]] = []
    tasks = [
        asyncio.create_task(
            _collect(event_stream(services.hub, Subscriber(p, frozenset({'tasks'})), last_event_id=None), out)
        )
        for p, out in ((admin, frames), (viewer, hidden))
    ]
    try:
        await asyncio.sleep(0.3)  # the hub subscribes to live:jobs and the activity stream
        services.emitter.job_live(
            JobLiveMessage(
                type=EventType.JOB_PROGRESS,
                ts=datetime.now(UTC),
                run_id='r1',
                job_id='j1',
                kind='blob.gc',
                queue='maintenance',
                progress=JobProgress(done=4, total=8, message='sweeping'),
            )
        )
        services.emitter.activity(
            ActivityEvent(
                type=EventType.JOB_FINISHED,
                ts=datetime.now(UTC),
                detail={
                    'job_id': 'j1',
                    'run_id': 'r1',
                    'kind': 'blob.gc',
                    'queue': 'maintenance',
                    'status': 'succeeded',
                },
            )
        )
        await services.emitter.flush()
        await services.valkey.publish(keys.JOBS_LIVE, 'garbage')
        async with asyncio.timeout(5):
            while len(frames) < 2:  # noqa: ASYNC110 - polling the collector is simplest here
                await asyncio.sleep(0.05)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
    by_type = dict(frames)  # pub/sub and the activity stream are separate loops: either may arrive first
    assert sorted(by_type) == ['job.progress', 'job.update']
    progress, update = by_type['job.progress'], by_type['job.update']
    assert (progress['run_id'], progress['done'], progress['total']) == ('r1', 4, 8)
    assert (update['event'], update['status']) == ('finished', 'succeeded')
    for _, data in frames:
        EVENTS.validate_python(data)
    assert hidden == []  # a viewer's subscription never carries job events
