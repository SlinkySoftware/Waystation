"""Typed job payloads and the envelope that travels through the queue (ARCHITECTURE §10).

A job type is a frozen pydantic model subclassing ``JobPayload`` with class-level ``kind`` (unique, dotted),
``queue`` and optionally ``max_attempts`` and ``dedupe_key()``. Defining the class registers it in ``JOB_TYPES``
so a worker can decode envelopes of that kind::

    class RebuildAptIndex(JobPayload):
        kind: ClassVar[str] = 'index.apt'
        queue: ClassVar[Queue] = 'index'
        repo_id: str

        def dedupe_key(self) -> str | None:
            return f'index.apt:{self.repo_id}'

Job types live here in ``waystation_common`` (both the server, which enqueues, and the worker, which runs them,
import them); their handlers live in the worker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, get_args

from pydantic import BaseModel, ConfigDict

from waystation_common.ids import new_id

Queue = Literal['index', 'maintenance', 'stats', 'prefetch', 'import']
QUEUES: tuple[Queue, ...] = get_args(Queue)

JOB_TYPES: dict[str, type[JobPayload]] = {}
RUN_DAYS = 90
ROUTINE_RUN_DAYS = 1


class JobPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: ClassVar[str] = ''
    queue: ClassVar[Queue] = 'maintenance'
    max_attempts: ClassVar[int] = 5  # deliveries (failures + lost workers) before the job is dead-lettered
    # High-frequency housekeeping (e.g. the 10 s stats rollup): success is logged at DEBUG and its ``job_runs``
    # documents expire after ROUTINE_RUN_DAYS instead of RUN_DAYS.
    routine: ClassVar[bool] = False

    def dedupe_key(self) -> str | None:
        """Requests with the same key are the same work (ARCH §10 dirty flag); ``None`` = never merged."""
        return None

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        kind = cls.__dict__.get('kind')
        if not kind:
            return
        existing = JOB_TYPES.get(kind)
        if existing is not None and existing.__qualname__ != cls.__qualname__:
            raise ValueError(f'job kind {kind!r} is already registered by {existing.__qualname__}')
        JOB_TYPES[kind] = cls


class JobEnvelope(BaseModel):
    """One unit of work in a queue: the stream entry's ``job`` field, the outbox document's ``job``, ``jobs:dead``.

    ``id`` identifies the request: it stays the same across retries and re-dispatches of the same outbox entry, so
    a re-dispatched job merges with itself. ``dedupe_key`` is the payload's key, else ``id:<id>``.
    """

    model_config = ConfigDict(frozen=True, extra='ignore')

    id: str
    kind: str
    queue: Queue
    payload: dict[str, Any]
    dedupe_key: str
    attempt: int = 0  # failed attempts so far (a lost worker counts through the stream's delivery count)
    enqueued_at: datetime
    run_after: datetime | None = None
    definition_id: str | None = None
    # Why it runs: a schedule fired, someone asked (run now), or a dead letter was retried. None (older envelopes):
    # derived from definition_id.
    trigger: Literal['schedule', 'manual', 'retry'] | None = None

    @classmethod
    def for_payload(
        cls,
        payload: JobPayload,
        *,
        run_after: datetime | None = None,
        dedupe_key: str | None = None,
        queue: Queue | None = None,
        definition_id: str | None = None,
        trigger: Literal['schedule', 'manual', 'retry'] | None = None,
        now: datetime | None = None,
    ) -> JobEnvelope:
        if not payload.kind or JOB_TYPES.get(payload.kind) is not type(payload):
            raise ValueError(f'{type(payload).__qualname__} is not a registered job type')
        job_id = new_id()
        return cls(
            id=job_id,
            kind=payload.kind,
            queue=queue or payload.queue,
            payload=payload.model_dump(mode='json'),
            dedupe_key=dedupe_key or payload.dedupe_key() or f'id:{job_id}',
            enqueued_at=now or datetime.now(UTC),
            run_after=run_after,
            definition_id=definition_id,
            trigger=trigger,
        )

    def decode(self) -> JobPayload:
        """The typed payload; ``KeyError`` for an unknown kind, ``ValidationError`` for a bad payload."""
        return JOB_TYPES[self.kind].model_validate(self.payload)

    def to_json(self) -> str:
        return self.model_dump_json()


# ---------------------------------------------------------------- built-in job types


class StatsRollup(JobPayload):
    """Roll closed minutes of per-minute counters into ``stats_minute`` (ARCH §8); scheduled every 10 s."""

    kind: ClassVar[str] = 'stats.rollup'
    queue: ClassVar[Queue] = 'stats'
    routine: ClassVar[bool] = True

    def dedupe_key(self) -> str | None:
        return 'stats.rollup'


class CleanupRun(JobPayload):
    """Run a cleanup policy on its attached repositories (SPEC §4.8; ``waystation_worker.cleanup``). Scheduled runs
    come from the ``job_definitions`` entry ``cleanup:<policy id>``; manual runs share the dedupe key."""

    kind: ClassVar[str] = 'cleanup.run'
    queue: ClassVar[Queue] = 'maintenance'
    max_attempts: ClassVar[int] = 3

    policy_id: str

    def dedupe_key(self) -> str | None:
        return f'cleanup:{self.policy_id}'


class BlobGc(JobPayload):
    """Mark-and-sweep blob garbage collection plus the orphan upload sweep (SPEC §4.8; ``waystation_worker.gc``)."""

    kind: ClassVar[str] = 'blob.gc'
    queue: ClassVar[Queue] = 'maintenance'
    max_attempts: ClassVar[int] = 3

    grace_s: int = 24 * 3600  # a blob is deleted only after being unreferenced (and unclaimed) this long
    upload_age_s: int = 24 * 3600  # orphan upload files older than this are removed

    def dedupe_key(self) -> str | None:
        return 'blob.gc'


class QuotaReconcile(JobPayload):
    """Recount every repository's stored bytes from ``assets`` (``repo_usage``; quotas, SPEC §4.8) and announce quota
    thresholds the recount crossed (``waystation_worker.quota``)."""

    kind: ClassVar[str] = 'quota.reconcile'
    queue: ClassVar[Queue] = 'maintenance'
    max_attempts: ClassVar[int] = 3

    def dedupe_key(self) -> str | None:
        return 'quota.reconcile'


class StorageCheck(JobPayload):
    """Compare the data filesystem's free space with the low-disk guard and announce ``storage.low`` /
    ``storage.ok`` on a change (``waystation_worker.quota``); scheduled every minute."""

    kind: ClassVar[str] = 'storage.check'
    queue: ClassVar[Queue] = 'maintenance'
    routine: ClassVar[bool] = True

    def dedupe_key(self) -> str | None:
        return 'storage.check'


class WebhookEvent(JobPayload):
    """An event some webhook may want (SPEC §4.12, T-O05): the worker matches it against the enabled webhooks, records
    one delivery per match and attempts each at once (``waystation_worker.webhooks``). Enqueued through the outbox."""

    kind: ClassVar[str] = 'webhook.event'
    queue: ClassVar[Queue] = 'maintenance'
    max_attempts: ClassVar[int] = 5

    event_id: str
    event: str
    repository: str | None = None
    document: dict[str, Any]  # the JSON document receivers get (``waystation_common.webhooks.build_event``)

    def dedupe_key(self) -> str | None:
        return f'webhook.event:{self.event_id}'


class WebhookDeliver(JobPayload):
    """Attempt one delivery now (a test event or a redelivery from the delivery log). Best effort: the retry sweep
    finds the delivery anyway if this job is lost."""

    kind: ClassVar[str] = 'webhook.deliver'
    queue: ClassVar[Queue] = 'maintenance'
    max_attempts: ClassVar[int] = 2

    delivery_id: str

    def dedupe_key(self) -> str | None:
        return f'webhook.deliver:{self.delivery_id}'


class WebhookSweep(JobPayload):
    """Attempt the webhook deliveries whose retry is due; scheduled every 15 s."""

    kind: ClassVar[str] = 'webhook.sweep'
    queue: ClassVar[Queue] = 'maintenance'
    routine: ClassVar[bool] = True

    def dedupe_key(self) -> str | None:
        return 'webhook.sweep'
