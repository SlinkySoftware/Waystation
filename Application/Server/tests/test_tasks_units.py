"""Tasks page pieces without MongoDB/Valkey (T-O04): the ``tasks`` SSE topic (RBAC, frames), run views, cursors, the
dead-letter view and schedule validation of ``PATCH /tasks/scheduled/{id}``."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from waystation_common.events import ActivityEvent, EventType, JobLiveMessage, JobProgress
from waystation_common.jobs import BlobGc, JobEnvelope
from waystation_common.jobs.queue import DeadLetter
from waystation_server.api import tasks
from waystation_server.api.live import _may_follow_tasks
from waystation_server.problems import FieldErrors
from waystation_server.security.principal import AuthMethod, Principal, PrincipalKind
from waystation_server.sse.hub import Hub, Subscriber

NOW = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


def user(*roles: str) -> Principal:
    return Principal(PrincipalKind.USER, f'id-{"-".join(roles)}', '-'.join(roles) or 'nobody', roles, AuthMethod.BASIC)


async def _no_repos(principal: Principal) -> set[str]:
    return set()


async def _no_storage() -> tuple[int, int]:
    return 0, 0


def frames(sub: Subscriber) -> list[dict[str, Any]]:
    out = []
    while not sub.queue.empty():
        frame = sub.queue.get_nowait()
        assert frame is not None
        out.append(json.loads(frame[2]))
    return out


@pytest.fixture
def hub() -> Hub:
    hub = Hub(None, _no_repos, _no_storage)  # type: ignore[arg-type]
    hub._closed = True  # no background loops: the test feeds the sources directly
    hub.tasks_access = _may_follow_tasks
    return hub


async def test_tasks_topic_is_admin_only(hub: Hub) -> None:
    admin = Subscriber(principal=user('admin'), topics=frozenset({'tasks'}))
    viewer = Subscriber(principal=user('viewer'), topics=frozenset({'tasks', 'activity'}))
    other = Subscriber(principal=user('admin'), topics=frozenset({'activity'}))
    for sub in (admin, viewer, other):
        await hub.subscribe(sub)
    assert (admin.tasks_allowed, viewer.tasks_allowed) == (True, False)
    message = JobLiveMessage(
        type=EventType.JOB_PROGRESS,
        ts=NOW,
        run_id='r1',
        job_id='j1',
        kind='blob.gc',
        queue='maintenance',
        progress=JobProgress(done=3, total=10, message='sweeping'),
    )
    hub._job_live(message.model_dump_json())
    assert frames(admin) == [
        {
            'type': 'job.progress',
            'ts': '2026-11-09T10:00:00Z',
            'run_id': 'r1',
            'job_id': 'j1',
            'kind': 'blob.gc',
            'done': 3,
            'total': 10,
            'message': 'sweeping',
        }
    ]
    assert frames(viewer) == [] and frames(other) == []


async def test_job_log_and_bad_messages(hub: Hub) -> None:
    sub = Subscriber(principal=user('admin'), topics=frozenset({'tasks'}))
    await hub.subscribe(sub)
    log = JobLiveMessage(
        type=EventType.JOB_LOG, ts=NOW, run_id='r1', job_id='j1', kind='cleanup.run', queue='maintenance', lines=['a']
    )
    hub._job_live(log.model_dump_json())
    hub._job_live('not json')
    hub._job_live(log.model_copy(update={'type': EventType.JOB_STARTED}).model_dump_json())  # not a live type
    (event,) = frames(sub)
    assert (event['type'], event['lines']) == ('job.log', ['a'])


async def test_job_and_worker_activity_become_task_frames(hub: Hub) -> None:
    sub = Subscriber(principal=user('admin'), topics=frozenset({'tasks'}))
    await hub.subscribe(sub)
    detail = {
        'job_id': 'j1',
        'run_id': 'r1',
        'kind': 'blob.gc',
        'queue': 'maintenance',
        'attempt': 2,
        'worker_id': 'w1',
        'status': 'dead',
        'error': 'boom',
    }
    hub._task_event(ActivityEvent(type=EventType.JOB_FAILED, ts=NOW, detail=detail))
    hub._task_event(ActivityEvent(type=EventType.WORKER_OFFLINE, ts=NOW, detail={'worker_id': 'w1'}))
    # a job's own summary event (no run id) is for the activity feed only
    hub._task_event(ActivityEvent(type=EventType.JOB_FINISHED, ts=NOW, detail={'summary': 'freed 3 blobs'}))
    update, worker = frames(sub)
    assert update == {
        'type': 'job.update',
        'ts': '2026-11-09T10:00:00Z',
        'event': 'failed',
        'run_id': 'r1',
        'job_id': 'j1',
        'kind': 'blob.gc',
        'queue': 'maintenance',
        'attempt': 2,
        'status': 'dead',
        'worker_id': 'w1',
        'error': 'boom',
    }
    assert worker == {'type': 'worker.update', 'ts': '2026-11-09T10:00:00Z', 'worker_id': 'w1', 'state': 'offline'}


def test_run_summary_and_trigger() -> None:
    doc: dict[str, Any] = {
        '_id': 'r1',
        'job_id': 'j1',
        'kind': 'blob.gc',
        'queue': 'maintenance',
        'definition_id': 'blob.gc',
        'attempt': 1,
        'status': 'succeeded',
        'started_at': NOW.replace(tzinfo=None),  # MongoDB returns naive UTC
        'finished_at': (NOW + timedelta(seconds=2)).replace(tzinfo=None),
        'progress': {'done': 5, 'total': 5, 'message': None},
        'worker_id': 'w1',
        'result': {'freed_blobs': 2},
    }
    view = tasks.run_summary(doc)
    assert (view.trigger, view.task_id, view.duration_ms, view.started_at) == ('schedule', 'blob.gc', 2000.0, NOW)
    assert tasks.run_summary({**doc, 'trigger': 'manual'}).trigger == 'manual'
    assert tasks.run_summary({**doc, 'trigger': 'retry', 'definition_id': None}).trigger == 'retry'
    running = tasks.run_summary({**doc, 'status': 'running', 'finished_at': None, 'definition_id': None})
    assert (running.trigger, running.duration_ms) == ('manual', None)


def test_run_cursor_round_trip_and_rejects_garbage() -> None:
    cursor = tasks.encode_cursor(NOW, 'run-1')
    assert tasks.decode_cursor(cursor) == (NOW, 'run-1')
    for bad in ('***', 'bm90LWEtY3Vyc29y', tasks.encode_cursor(NOW, 'x' * 65)):
        with pytest.raises(FieldErrors):
            tasks.decode_cursor(bad)


def test_dead_letter_view() -> None:
    env = JobEnvelope.for_payload(BlobGc(), definition_id='blob.gc').model_copy(update={'attempt': 3})
    letter = DeadLetter.from_entry(
        '1762682400000-0', {'job': env.to_json(), 'error': 'disk full', 'ts': '2026-11-09T10:00:00+00:00'}
    )
    view = tasks._dead_view(letter)
    assert (view.kind, view.attempts, view.task_id, view.retryable, view.dead_at) == (
        'blob.gc',
        3,
        'blob.gc',
        True,
        NOW,
    )
    broken = tasks._dead_view(DeadLetter.from_entry('1762682400000-1', {'job': '{', 'error': 'malformed job entry'}))
    assert (broken.kind, broken.retryable, broken.payload) == (None, False, None)
    assert broken.dead_at == datetime.fromtimestamp(1762682400, UTC)  # from the entry id when ts is missing
    unknown = env.model_copy(update={'kind': 'gone.kind'})
    assert tasks._dead_view(DeadLetter.from_entry('1-0', {'job': unknown.to_json()})).retryable is False


@pytest.mark.parametrize('schedule', ['@every 1s', '61 * * * *', '@whenever', 'x' * 101])
def test_update_rejects_bad_schedules(schedule: str) -> None:
    with pytest.raises(ValidationError):
        tasks.ScheduledTaskUpdate.model_validate({'schedule': schedule})


def test_update_normalises_and_forbids_extra_fields() -> None:
    assert tasks.ScheduledTaskUpdate.model_validate({'schedule': ' 0  3 * * * '}).schedule == '0 3 * * *'
    with pytest.raises(ValidationError):
        tasks.ScheduledTaskUpdate.model_validate({'params': {'grace_s': 1}})
