"""Producer API for jobs: what server code uses (it never imports the worker; ARCHITECTURE §3, §10)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from redis.asyncio import Redis

from waystation_common.db import jobs as jobs_db
from waystation_common.db.client import Database, Session
from waystation_common.jobs.model import JobEnvelope, JobPayload, Queue
from waystation_common.jobs.queue import EnqueueResult, JobQueue


class JobClient:
    """Enqueue typed jobs.

    * ``enqueue_durable`` for jobs triggered by a write (rebuild an index after an upload): it inserts into
      ``job_outbox``, **inside the caller's transaction** when ``session`` is given, so the job exists iff the
      change committed. The worker-side dispatcher moves it to Valkey within about a second and re-dispatches it if
      Valkey loses it. Returns the outbox id.
    * ``enqueue`` for best-effort jobs (run now, prefetch): straight to Valkey; lost if Valkey loses its data.

    Both apply the dirty-flag dedupe of the payload's ``dedupe_key()``: a pending job absorbs the request, a running
    one runs once more after it finishes.
    """

    def __init__(self, valkey: Redis, db: Database, *, clock: Callable[[], datetime] | None = None) -> None:
        self.queue = JobQueue(valkey)
        self._db = db
        self._clock = clock or (lambda: datetime.now(UTC))

    def envelope(
        self,
        payload: JobPayload,
        *,
        run_after: datetime | None = None,
        dedupe_key: str | None = None,
        queue: Queue | None = None,
    ) -> JobEnvelope:
        return JobEnvelope.for_payload(
            payload, run_after=run_after, dedupe_key=dedupe_key, queue=queue, now=self._clock()
        )

    async def enqueue(
        self,
        payload: JobPayload,
        *,
        run_after: datetime | None = None,
        dedupe_key: str | None = None,
        queue: Queue | None = None,
    ) -> EnqueueResult:
        return await self.queue.enqueue(self.envelope(payload, run_after=run_after, dedupe_key=dedupe_key, queue=queue))

    async def enqueue_durable(
        self,
        payload: JobPayload,
        *,
        session: Session | None = None,
        run_after: datetime | None = None,
        dedupe_key: str | None = None,
    ) -> str:
        envelope = self.envelope(payload, run_after=run_after, dedupe_key=dedupe_key)
        return await jobs_db.outbox_add(self._db, envelope, now=self._clock(), session=session)
