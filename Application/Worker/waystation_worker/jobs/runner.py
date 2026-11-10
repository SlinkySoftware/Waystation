"""The consumer side: read jobs from the queue streams of ``WORKER_QUEUES``, run them, record them (ARCH §10).

Per delivery: ``start`` (dedupe key -> running) -> handler in its own task, heartbeated every ``visibility/3``
(``touch``) -> ``finish``: done (or the queued rerun), retry with backoff, or dead letter after ``max_attempts``.
Attempt number = failed attempts carried in the envelope + the stream's delivery count, so a job whose worker dies
counts an attempt too. Every attempt is a ``job_runs`` document and ``job.*`` activity events.

Stopping: a graceful stop (``stop`` set) lets running jobs finish for ``shutdown_grace_s``, then abandons them;
cancellation (a killed worker) abandons immediately. Abandoned jobs are not acked: another worker reclaims them
once they have been idle for ``visibility_s`` (XAUTOCLAIM) and runs them again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from redis.asyncio import Redis

from waystation_common.db import jobs as jobs_db
from waystation_common.db.client import Database
from waystation_common.events import ActivityEvent, EventEmitter, EventType
from waystation_common.ids import new_id
from waystation_common.jobs.model import JOB_TYPES, ROUTINE_RUN_DAYS, RUN_DAYS, JobEnvelope
from waystation_common.jobs.queue import Delivery, JobQueue
from waystation_common.settings import Settings
from waystation_worker.jobs.config import JobConfig
from waystation_worker.jobs.context import Clock, JobContext, quietly
from waystation_worker.jobs.registry import Handler

log = logging.getLogger('waystation.worker.jobs')


class Runner:
    def __init__(
        self,
        queue: JobQueue,
        db: Database,
        valkey: Redis,
        *,
        worker_id: str,
        queues: Sequence[str],
        concurrency: int,
        handlers: Mapping[str, Handler],
        config: JobConfig,
        clock: Clock = time.time,
        emitter: EventEmitter | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.queue = queue
        self.db = db
        self.valkey = valkey
        self.worker_id = worker_id
        self.queues = list(queues)
        self.concurrency = concurrency
        self.handlers = handlers
        self.config = config
        self.clock = clock
        self.emitter = emitter
        self.settings = settings
        self.tasks: dict[str, asyncio.Task[None]] = {}  # stream entry id -> running job task
        self.running: dict[str, tuple[JobEnvelope, str, datetime]] = {}  # entry id -> (job, run id, started)
        self.last_poll = time.monotonic()
        self._last_reclaim = float('-inf')

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    # ------------------------------------------------------------------ loop
    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    await self.poll()
                except Exception:  # Valkey outage: keep trying
                    log.exception('job poll failed')
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.CancelledError:
            await self._abandon()
            raise
        await self._drain()

    async def poll(self) -> None:
        """One iteration: reclaim stuck entries when due, then read new ones up to the free capacity."""
        self.last_poll = time.monotonic()
        block_s = self.config.block_ms / 1000
        if not self.queues:
            await asyncio.sleep(block_s)
            return
        free = self.concurrency - len(self.tasks)
        if free <= 0:
            await asyncio.wait(set(self.tasks.values()), timeout=block_s, return_when=asyncio.FIRST_COMPLETED)
            return
        if not self._last_reclaim > time.monotonic() - self.config.reclaim_s:
            await self.queue.ensure_groups(self.queues)
            self._last_reclaim = time.monotonic()
            for queue in self.queues:
                min_idle_ms = int(self.config.visibility_s * 1000)
                for delivery in await self.queue.reclaim(self.worker_id, queue, min_idle_ms=min_idle_ms, count=free):
                    if self._spawn(delivery):
                        free -= 1
                        log.info('job reclaimed', extra={'entry': delivery.entry_id, 'queue': queue})
            if free <= 0:
                return
        for delivery in await self.queue.read(self.worker_id, self.queues, count=free, block_ms=self.config.block_ms):
            self._spawn(delivery)

    def _spawn(self, delivery: Delivery) -> bool:
        if delivery.entry_id in self.tasks:
            return False
        task = asyncio.create_task(self._execute_safely(delivery), name=f'job:{delivery.entry_id}')
        self.tasks[delivery.entry_id] = task
        entry = delivery.entry_id

        def forget(_task: asyncio.Task[None]) -> None:
            self.tasks.pop(entry, None)

        task.add_done_callback(forget)
        return True

    async def _drain(self) -> None:
        if self.tasks:
            log.info('waiting for running jobs', extra={'jobs': len(self.tasks)})
            await asyncio.wait(set(self.tasks.values()), timeout=self.config.shutdown_grace_s)
        await self._abandon()

    async def _abandon(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ one job
    async def _execute_safely(self, delivery: Delivery) -> None:
        try:
            await self._execute(delivery)
        except asyncio.CancelledError:
            raise
        except Exception:  # Valkey/MongoDB failure around the job: left pending, reclaimed and re-run later
            log.exception('job bookkeeping failed', extra={'entry': delivery.entry_id})

    async def _execute(self, delivery: Delivery) -> None:
        env = delivery.envelope
        if env is None:
            log.error('malformed job entry dead-lettered', extra={'entry': delivery.entry_id})
            await self.queue.drop(delivery, dead_reason='malformed job entry')
            return
        started = await self.queue.start(delivery, self.worker_id)
        if not started.run:  # a stale duplicate: the job owning its key covers it
            await self.queue.drop(delivery)
            return
        attempt = env.attempt + delivery.deliveries
        payload_type = JOB_TYPES.get(env.kind)
        handler = self.handlers.get(env.kind)
        run_id = new_id()
        retention = ROUTINE_RUN_DAYS if payload_type is not None and payload_type.routine else RUN_DAYS
        now = self._now()  # one clock read: expiry is exactly the retention after the start
        await quietly(
            jobs_db.run_start(
                self.db,
                {
                    '_id': run_id,
                    'job_id': env.id,
                    'definition_id': env.definition_id,
                    'trigger': env.trigger or ('schedule' if env.definition_id else 'manual'),
                    'kind': env.kind,
                    'queue': env.queue,
                    'dedupe_key': env.dedupe_key,
                    'attempt': attempt,
                    'status': 'running',
                    'started_at': now,
                    'finished_at': None,
                    'expires_at': now + timedelta(days=retention),
                    'worker_id': self.worker_id,
                },
                now=now,
            )
        )
        if payload_type is None or handler is None:
            await self._dead(delivery, env, run_id, attempt, started.outbox_ids, f'no handler for kind {env.kind!r}')
            return
        if attempt > payload_type.max_attempts:
            reason = f'gave up after {attempt - 1} attempts (the last worker running it was lost)'
            await self._dead(delivery, env, run_id, attempt, started.outbox_ids, reason)
            return
        try:
            payload = env.decode()
        except ValidationError as exc:
            await self._dead(delivery, env, run_id, attempt, started.outbox_ids, f'invalid payload: {exc}')
            return
        ctx = JobContext(
            job=env,
            run_id=run_id,
            attempt=attempt,
            worker_id=self.worker_id,
            db=self.db,
            valkey=self.valkey,
            clock=self.clock,
            settings=self.settings,
            emitter=self.emitter,
        )
        if not payload_type.routine:  # routine jobs (e.g. the 10 s stats rollup) would flood the activity feed
            self._event(EventType.JOB_STARTED, env, run_id, attempt)
        self.running[delivery.entry_id] = (env, run_id, self._now())
        lost = asyncio.Event()
        handler_task: asyncio.Task[dict[str, Any] | None] = asyncio.create_task(
            _call(handler, payload, ctx), name=f'handler:{env.kind}'
        )
        heartbeat = asyncio.create_task(self._heartbeat(delivery, handler_task, lost))
        try:
            result = await handler_task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if lost.is_set() and (current is None or not current.cancelling()):
                log.warning('job ownership lost; another worker runs it', extra={'job_id': env.id})
                await quietly(
                    jobs_db.run_finish(self.db, run_id, status='interrupted', now=self._now(), error='ownership lost')
                )
                return
            raise  # abandoned (shutdown or killed): not acked, reclaimed later
        except Exception as exc:
            await self._failed(delivery, env, run_id, attempt, payload_type.max_attempts, started.outbox_ids, exc)
            return
        finally:
            heartbeat.cancel()
            self.running.pop(delivery.entry_id, None)
        await ctx.flush_progress()
        outcome, next_job = await self.queue.finish(delivery, 'done')
        await quietly(jobs_db.outbox_mark_done(self.db, started.outbox_ids, now=self._now()))
        await quietly(
            jobs_db.run_finish(
                self.db, run_id, status='succeeded', now=self._now(), result=result if isinstance(result, dict) else {}
            )
        )
        if not payload_type.routine:
            self._event(EventType.JOB_FINISHED, env, run_id, attempt, status='succeeded')
        log.log(
            logging.DEBUG if payload_type.routine else logging.INFO,
            'job finished',
            extra={'job_id': env.id, 'kind': env.kind, 'attempt': attempt, 'outcome': outcome, 'next': next_job},
        )

    async def _heartbeat(self, delivery: Delivery, handler_task: asyncio.Task[Any], lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_s)
            try:
                owned = await self.queue.touch(delivery, self.worker_id)
            except Exception as exc:  # Valkey blip: try again next beat
                log.warning('job heartbeat failed', extra={'error': repr(exc)})
                continue
            if not owned:
                lost.set()
                handler_task.cancel()
                return

    async def _failed(
        self,
        delivery: Delivery,
        env: JobEnvelope,
        run_id: str,
        attempt: int,
        max_attempts: int,
        outbox_ids: list[str],
        exc: Exception,
    ) -> None:
        error = f'{type(exc).__name__}: {exc}'[:2000]
        log.warning('job failed', extra={'job_id': env.id, 'kind': env.kind, 'attempt': attempt, 'error': error})
        if attempt >= max_attempts:
            await self._dead(delivery, env, run_id, attempt, outbox_ids, error)
            return
        retry_at = self._now() + timedelta(seconds=self.config.backoff_s(attempt))
        await self.queue.finish(
            delivery, 'retry', envelope=env.model_copy(update={'attempt': attempt}), retry_at=retry_at
        )
        await quietly(jobs_db.run_finish(self.db, run_id, status='failed', now=self._now(), error=error))
        self._event(EventType.JOB_FAILED, env, run_id, attempt, status='failed', error=error)

    async def _dead(
        self, delivery: Delivery, env: JobEnvelope, run_id: str, attempt: int, outbox_ids: list[str], error: str
    ) -> None:
        log.error('job dead-lettered', extra={'job_id': env.id, 'kind': env.kind, 'attempt': attempt, 'error': error})
        await self.queue.finish(delivery, 'dead', envelope=env.model_copy(update={'attempt': attempt}), error=error)
        await quietly(jobs_db.outbox_mark_done(self.db, outbox_ids, now=self._now()))
        await quietly(jobs_db.run_finish(self.db, run_id, status='dead', now=self._now(), error=error))
        self._event(EventType.JOB_FAILED, env, run_id, attempt, status='dead', error=error)

    def _event(
        self,
        kind: EventType,
        env: JobEnvelope,
        run_id: str,
        attempt: int,
        *,
        status: str = 'running',
        error: str | None = None,
    ) -> None:
        if self.emitter is None:
            return
        detail: dict[str, Any] = {
            'job_id': env.id,
            'run_id': run_id,
            'kind': env.kind,
            'queue': env.queue,
            'attempt': attempt,
            'worker_id': self.worker_id,
            'status': status,
        }
        if error is not None:
            detail['error'] = error
        self.emitter.activity(ActivityEvent(type=kind, ts=self._now(), detail=detail))


async def _call(handler: Handler, payload: Any, ctx: JobContext) -> dict[str, Any] | None:
    return await handler(payload, ctx)
