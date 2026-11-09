"""Tasks and workers (SPECIFICATION §4.12, UI_DESIGN §4.9; OpenAPI tag ``tasks``). Every route needs ``admin``.

- ``/tasks/scheduled``: the ``job_definitions`` (built-ins such as ``stats.rollup`` and ``blob.gc``, and one per
  scheduled cleanup policy) with their last run and next fire. ``PATCH`` changes the schedule (validated cron, macro
  or ``@every``) or the enabled flag; a cleanup policy's schedule belongs to the policy (409: edit the policy).
  ``.../run`` enqueues the task now (the usual dedupe applies: a pending run absorbs it).
- ``/tasks/runs``: ``job_runs`` newest first with filters and a keyset cursor; routine jobs (the 10 s stats rollup)
  are hidden unless asked for. ``/tasks/runs/{id}`` adds the capped log. Live progress and log lines come from the
  event stream topic ``tasks`` (``api.live``).
- ``/tasks/workers``: worker presence (``workers``), queue figures and the dead-letter count.
- ``/tasks/dead-letters``: ``jobs:dead`` newest first; retry (a fresh job with the same work, attempt 0) and discard.
  Both are idempotent: a dead letter is retried at most once, discarding a missing one is a no-op.

Changes are audited (``task.update``, ``task.run``, ``task.retry``, ``task.discard``).
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from waystation_common.db import cleanup as cleanup_db
from waystation_common.db import jobs as jobs_db
from waystation_common.db.client import Document
from waystation_common.errors import Conflict, NotFound
from waystation_common.ids import new_id
from waystation_common.jobs import JOB_TYPES, QUEUES, JobEnvelope, Queue
from waystation_common.jobs.queue import DeadLetter as StoredDeadLetter
from waystation_common.jobs.schedule import parse_schedule, validate_schedule
from waystation_server import audit
from waystation_server.api.cleanup import JobAccepted, JobProgress, RunStatus, run_trigger
from waystation_server.api.deps import ServicesDep
from waystation_server.audit import AuditAction
from waystation_server.problems import FieldError, FieldErrors
from waystation_server.security import access
from waystation_server.security.access import Action

router = APIRouter(tags=['tasks'])

TaskId = Annotated[str, Path(min_length=1, max_length=128)]
RunId = Annotated[str, Path(min_length=1, max_length=64)]
LetterId = Annotated[str, Path(pattern=r'^\d{1,20}-\d{1,20}$')]
Trigger = Literal['schedule', 'manual', 'retry']
WorkerState = Literal['ok', 'late', 'lost']
LATE_AFTER_S = 30.0  # three missed 10 s heartbeats
LOST_AFTER_S = 90.0


# ---------------------------------------------------------------------------- schemas
class JobRunSummary(BaseModel):
    id: str = Field(description='Run id (one per attempt)')
    job_id: str
    kind: str
    queue: str
    task_id: str | None = Field(description='The scheduled task (job definition) it belongs to, if any')
    trigger: Trigger
    attempt: int
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    duration_ms: float | None
    progress: JobProgress
    worker_id: str | None
    error: str | None
    result: dict[str, Any] = Field(description='What the run reported (kind-specific)')


class JobRun(JobRunSummary):
    log: list[str] = Field(description='The last 500 log lines, oldest first')


class JobRunPage(BaseModel):
    items: list[JobRunSummary]
    next_cursor: str | None


class TaskOwner(BaseModel):
    kind: Literal['cleanup_policy']
    id: str
    name: str | None


class ScheduledTask(BaseModel):
    id: str
    kind: str
    queue: str
    schedule: str = Field(description='5-field UTC cron, a macro such as `@daily`, or `@every <n>s|m|h`')
    enabled: bool
    routine: bool = Field(description='Housekeeping that runs every few seconds; its runs are hidden by default')
    params: dict[str, Any]
    next_run_at: datetime | None
    last_fired_at: datetime | None
    last_run: JobRunSummary | None
    owner: TaskOwner | None = Field(description='Set when another object owns the schedule (edit it there)')


class ScheduledTaskList(BaseModel):
    items: list[ScheduledTask]


class ScheduledTaskUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    schedule: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None

    @field_validator('schedule')
    @classmethod
    def _schedule(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_schedule(value)
        except ValueError as exc:
            raise ValueError(f'not a valid schedule: {exc}') from None


class QueueFigures(BaseModel):
    name: str
    waiting: int
    running: int
    delayed: int
    oldest_waiting_age_s: float


class WorkerJob(BaseModel):
    job_id: str
    run_id: str | None
    kind: str
    queue: str
    started_at: datetime | None


class Worker(BaseModel):
    id: str
    hostname: str
    version: str
    queues: list[str]
    concurrency: int | None
    leader: bool = Field(description='Holds the scheduler lease (fires schedules, dispatches the outbox)')
    started_at: datetime | None
    heartbeat_at: datetime
    heartbeat_age_s: float
    state: WorkerState = Field(description='`late` after 30 s without a heartbeat, `lost` after 90 s')
    running_jobs: list[WorkerJob]


class WorkerOverview(BaseModel):
    workers: list[Worker]
    queues: list[QueueFigures]
    dead_letters: int


class DeadLetter(BaseModel):
    id: str = Field(description='Dead-letter entry id')
    job_id: str | None
    kind: str | None
    queue: str | None
    task_id: str | None
    attempts: int
    error: str
    dead_at: datetime
    enqueued_at: datetime | None
    payload: dict[str, Any] | None
    retryable: bool = Field(description='False for a malformed entry or a kind this version does not know')


class DeadLetterPage(BaseModel):
    items: list[DeadLetter]
    total: int
    next_cursor: str | None


# ---------------------------------------------------------------------------- views
def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _summary_fields(doc: Document) -> dict[str, Any]:
    progress = doc.get('progress') or {}
    started = _aware(doc['started_at'])
    finished = _aware(doc['finished_at']) if doc.get('finished_at') else None
    return {
        'id': str(doc['_id']),
        'job_id': str(doc.get('job_id') or ''),
        'kind': str(doc.get('kind') or ''),
        'queue': str(doc.get('queue') or ''),
        'task_id': doc.get('definition_id'),
        'trigger': run_trigger(doc),
        'attempt': int(doc.get('attempt') or 1),
        'status': doc.get('status', 'running'),
        'started_at': started,
        'finished_at': finished,
        'duration_ms': round((finished - started).total_seconds() * 1000, 1) if finished else None,
        'progress': JobProgress(
            done=int(progress.get('done') or 0), total=progress.get('total'), message=progress.get('message')
        ),
        'worker_id': doc.get('worker_id'),
        'error': doc.get('error'),
        'result': doc.get('result') or {},
    }


def run_summary(doc: Document) -> JobRunSummary:
    return JobRunSummary(**_summary_fields(doc))


def _routine_kinds() -> list[str]:
    return sorted(kind for kind, payload in JOB_TYPES.items() if payload.routine)


def _next_run(doc: Document, now: datetime) -> datetime | None:
    if not doc.get('enabled', True):
        return None
    if doc.get('next_run_at') is not None:
        return _aware(doc['next_run_at'])
    try:
        return datetime.fromtimestamp(parse_schedule(str(doc.get('schedule') or '')).next_after(now.timestamp()), UTC)
    except ValueError:
        return None


async def _task_views(svc: Any, docs: list[Document]) -> list[ScheduledTask]:
    now = datetime.now(UTC)
    last = await jobs_db.last_runs_by_definition(svc.db, [str(d['_id']) for d in docs])
    policy_ids = [
        str((d.get('params') or {}).get('policy_id'))
        for d in docs
        if d.get('kind') == 'cleanup.run' and (d.get('params') or {}).get('policy_id')
    ]
    names = {str(p['_id']): str(p.get('name') or '') for p in await cleanup_db.get_policies(svc.db, policy_ids)}
    out: list[ScheduledTask] = []
    for doc in docs:
        task_id = str(doc['_id'])
        kind = str(doc.get('kind') or '')
        payload_type = JOB_TYPES.get(kind)
        params = doc.get('params') or {}
        owner = None
        if kind == 'cleanup.run' and params.get('policy_id'):
            policy_id = str(params['policy_id'])
            owner = TaskOwner(kind='cleanup_policy', id=policy_id, name=names.get(policy_id))
        queue = doc.get('queue') or (payload_type.queue if payload_type else 'maintenance')
        out.append(
            ScheduledTask(
                id=task_id,
                kind=kind,
                queue=str(queue),
                schedule=str(doc.get('schedule') or ''),
                enabled=bool(doc.get('enabled', True)),
                routine=bool(payload_type and payload_type.routine),
                params=params,
                next_run_at=_next_run(doc, now),
                last_fired_at=_aware(doc['last_fired_at']) if doc.get('last_fired_at') else None,
                last_run=run_summary(last[task_id]) if task_id in last else None,
                owner=owner,
            )
        )
    return out


async def _task(svc: Any, task_id: str) -> Document:
    doc = await jobs_db.get_definition(svc.db, task_id)
    if doc is None:
        raise NotFound('no such task')
    return doc


def _dead_view(letter: StoredDeadLetter) -> DeadLetter:
    env = letter.envelope
    return DeadLetter(
        id=letter.id,
        job_id=env.id if env else None,
        kind=env.kind if env else None,
        queue=env.queue if env else None,
        task_id=env.definition_id if env else None,
        attempts=env.attempt if env else 0,
        error=letter.error,
        dead_at=letter.dead_at,
        enqueued_at=env.enqueued_at if env else None,
        payload=env.payload if env else None,
        retryable=env is not None and env.kind in JOB_TYPES,
    )


def encode_cursor(started_at: datetime, run_id: str) -> str:
    millis = int(_aware(started_at).timestamp() * 1000)
    return base64.urlsafe_b64encode(f'{millis}:{run_id}'.encode()).decode().rstrip('=')


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)).decode()
        millis, _, run_id = raw.partition(':')
        if not run_id or len(run_id) > 64:
            raise ValueError(cursor)
        return datetime.fromtimestamp(int(millis) / 1000, UTC), run_id
    except ValueError, binascii.Error, UnicodeDecodeError, OverflowError:
        raise FieldErrors([FieldError(field='cursor', message='invalid cursor', code='invalid')]) from None


# ---------------------------------------------------------------------------- scheduled tasks
@router.get('/tasks/scheduled', response_model=ScheduledTaskList, operation_id='listScheduledTasks')
@access.requires(Action.ADMIN)
async def list_tasks(request: Request, svc: ServicesDep) -> ScheduledTaskList:
    return ScheduledTaskList(items=await _task_views(svc, await jobs_db.list_definitions(svc.db)))


@router.patch('/tasks/scheduled/{task_id}', response_model=ScheduledTask, operation_id='updateScheduledTask')
@access.requires(Action.ADMIN)
async def update_task(request: Request, svc: ServicesDep, task_id: TaskId, body: ScheduledTaskUpdate) -> ScheduledTask:
    before = await _task(svc, task_id)
    if (before.get('params') or {}).get('policy_id') and before.get('kind') == 'cleanup.run':
        raise Conflict('this schedule belongs to a cleanup policy: change it on the Cleanup page', field='schedule')
    fields: dict[str, Any] = {}
    if body.schedule is not None and body.schedule != before.get('schedule'):
        fields['schedule'] = body.schedule
    if body.enabled is not None and body.enabled != bool(before.get('enabled', True)):
        fields['enabled'] = body.enabled
    after = before
    if fields:
        updated = await jobs_db.update_definition(svc.db, task_id, fields)
        if updated is None:
            raise NotFound('no such task')
        after = updated
        audit.record(
            AuditAction.TASK_UPDATE,
            target=audit.target('task', task_id, task_id),
            changes=audit.diff(
                {k: before.get(k) for k in ('schedule', 'enabled')}, {k: after.get(k) for k in ('schedule', 'enabled')}
            ),
        )
    return (await _task_views(svc, [after]))[0]


@router.post(
    '/tasks/scheduled/{task_id}/run', status_code=202, response_model=JobAccepted, operation_id='runScheduledTask'
)
@access.requires(Action.ADMIN)
async def run_task(request: Request, svc: ServicesDep, task_id: TaskId) -> JobAccepted:
    doc = await _task(svc, task_id)
    payload_type = JOB_TYPES.get(str(doc.get('kind') or ''))
    if payload_type is None:
        raise Conflict(f'this version cannot run tasks of kind {doc.get("kind")!r}')
    try:
        payload = payload_type.model_validate(doc.get('params') or {})
    except ValueError:
        raise Conflict('the task has invalid parameters') from None
    queue = doc.get('queue')
    envelope = JobEnvelope.for_payload(
        payload,
        dedupe_key=payload.dedupe_key() or f'def:{task_id}',
        queue=queue if queue in QUEUES else None,
        definition_id=task_id,
        trigger='manual',
    )
    result = await svc.jobs.queue.enqueue(envelope)
    audit.record(
        AuditAction.TASK_RUN,
        target=audit.target('task', task_id, task_id),
        details={'kind': payload.kind, 'job_id': result.job_id, 'status': result.status},
    )
    return JobAccepted(job_id=result.job_id, status=result.status)


# ---------------------------------------------------------------------------- runs
@router.get('/tasks/runs', response_model=JobRunPage, operation_id='listJobRuns')
@access.requires(Action.ADMIN)
async def list_runs(
    request: Request,
    svc: ServicesDep,
    status: Annotated[list[RunStatus] | None, Query(max_length=5)] = None,
    kind: Annotated[str | None, Query(max_length=64)] = None,
    queue: Queue | None = None,
    task: Annotated[str | None, Query(max_length=128, description='Runs of one scheduled task')] = None,
    job: Annotated[str | None, Query(max_length=64, description='Attempts of one job')] = None,
    include_routine: Annotated[bool, Query(description='Include routine housekeeping (the stats rollup)')] = False,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JobRunPage:
    query = jobs_db.RunQuery(
        status=status or (),
        kind=kind,
        queue=queue,
        definition_id=task,
        job_id=job,
        exclude_kinds=() if include_routine or task or job else _routine_kinds(),
    )
    docs = await jobs_db.page_runs(svc.db, query, after=decode_cursor(cursor) if cursor else None, limit=limit)
    items = [run_summary(d) for d in docs[:limit]]
    last = docs[limit - 1] if len(docs) > limit else None
    return JobRunPage(items=items, next_cursor=encode_cursor(last['started_at'], str(last['_id'])) if last else None)


@router.get('/tasks/runs/{run_id}', response_model=JobRun, operation_id='getJobRun')
@access.requires(Action.ADMIN)
async def get_run(request: Request, svc: ServicesDep, run_id: RunId) -> JobRun:
    doc = await jobs_db.get_run(svc.db, run_id)
    if doc is None:
        raise NotFound('no such run')
    return JobRun(**_summary_fields(doc), log=[str(line) for line in doc.get('log') or []])


# ---------------------------------------------------------------------------- workers
def _worker_view(doc: Document, now: datetime) -> Worker:
    heartbeat = _aware(doc['heartbeat_at'])
    age = max(0.0, (now - heartbeat).total_seconds())
    state: WorkerState = 'ok' if age <= LATE_AFTER_S else 'late' if age <= LOST_AFTER_S else 'lost'
    return Worker(
        id=str(doc['_id']),
        hostname=str(doc.get('hostname') or ''),
        version=str(doc.get('version') or 'unknown'),
        queues=[str(q) for q in doc.get('queues') or []],
        concurrency=doc.get('concurrency'),
        leader=bool(doc.get('leader')),
        started_at=_aware(doc['started_at']) if doc.get('started_at') else None,
        heartbeat_at=heartbeat,
        heartbeat_age_s=round(age, 1),
        state=state,
        running_jobs=[
            WorkerJob(
                job_id=str(j.get('job_id') or ''),
                run_id=j.get('run_id'),
                kind=str(j.get('kind') or ''),
                queue=str(j.get('queue') or ''),
                started_at=_aware(j['started_at']) if j.get('started_at') else None,
            )
            for j in doc.get('running_jobs') or []
        ],
    )


@router.get('/tasks/workers', response_model=WorkerOverview, operation_id='getWorkers')
@access.requires(Action.ADMIN)
async def get_workers(request: Request, svc: ServicesDep) -> WorkerOverview:
    now = datetime.now(UTC)
    docs = await jobs_db.list_workers(svc.db)
    stats = await svc.jobs.queue.stats(QUEUES)
    return WorkerOverview(
        workers=[_worker_view(d, now) for d in docs if d.get('heartbeat_at') is not None],
        queues=[
            QueueFigures(
                name=name,
                waiting=s.waiting,
                running=s.running,
                delayed=s.delayed,
                oldest_waiting_age_s=round(s.oldest_waiting_age_s, 1),
            )
            for name, s in stats.items()
        ],
        dead_letters=await svc.jobs.queue.dead_count(),
    )


# ---------------------------------------------------------------------------- dead letters
@router.get('/tasks/dead-letters', response_model=DeadLetterPage, operation_id='listDeadLetters')
@access.requires(Action.ADMIN)
async def list_dead_letters(
    request: Request,
    svc: ServicesDep,
    cursor: Annotated[str | None, Query(pattern=r'^\d{1,20}-\d{1,20}$')] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DeadLetterPage:
    letters = await svc.jobs.queue.dead_page(before=cursor, count=limit + 1)
    items = [_dead_view(letter) for letter in letters[:limit]]
    return DeadLetterPage(
        items=items,
        total=await svc.jobs.queue.dead_count(),
        next_cursor=letters[limit - 1].id if len(letters) > limit else None,
    )


@router.post(
    '/tasks/dead-letters/{letter_id}/retry',
    status_code=202,
    response_model=JobAccepted,
    operation_id='retryDeadLetter',
)
@access.requires(Action.ADMIN)
async def retry_dead_letter(request: Request, svc: ServicesDep, letter_id: LetterId) -> JobAccepted:
    queue = svc.jobs.queue
    letter = await queue.dead_get(letter_id)
    result = None
    if letter is not None:
        env = letter.envelope
        if env is None or env.kind not in JOB_TYPES:
            raise Conflict('this dead letter cannot be retried (malformed, or a job kind this version does not know)')
        fresh = env.model_copy(
            update={
                'id': new_id(),
                'attempt': 0,
                'enqueued_at': datetime.now(UTC),
                'run_after': None,
                'trigger': 'retry',
            }
        )
        result = await queue.retry_dead(letter_id, fresh)
        if result is not None:
            audit.record(
                AuditAction.TASK_RETRY,
                target=audit.target('job', env.id, env.kind),
                details={'dead_letter': letter_id, 'job_id': result.job_id, 'status': result.status},
            )
    if result is None:  # gone: retried before (answer with that job), or discarded / trimmed (404)
        previous = await queue.retried_job(letter_id)
        if previous is None:
            raise NotFound('no such dead letter')
        return JobAccepted(job_id=previous, status='merged')
    return JobAccepted(job_id=result.job_id, status=result.status)


@router.delete('/tasks/dead-letters/{letter_id}', status_code=204, operation_id='discardDeadLetter')
@access.requires(Action.ADMIN)
async def discard_dead_letter(request: Request, svc: ServicesDep, letter_id: LetterId) -> Response:
    letter = await svc.jobs.queue.dead_get(letter_id)
    if await svc.jobs.queue.discard_dead(letter_id):
        env = letter.envelope if letter is not None else None
        audit.record(
            AuditAction.TASK_DISCARD,
            target=audit.target('job', env.id if env else None, env.kind if env else None),
            details={'dead_letter': letter_id, 'error': letter.error[:200] if letter else None},
        )
    return Response(status_code=204)
