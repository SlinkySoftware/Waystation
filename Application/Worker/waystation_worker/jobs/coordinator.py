"""The leader's duties (one worker per deployment holds ``lease:scheduler``; ARCHITECTURE §10).

Every ``tick_s`` each worker tries to hold the lease; the holder, in one tick:

1. **Outbox dispatcher**: moves ``job_outbox`` entries to Valkey (``JobQueue.enqueue`` with the outbox id, so the
   job that runs them marks them done) and records ``sent_at``/``sent_epoch``. In-flight entries are dispatched
   again when the Valkey epoch changed (Valkey lost its data) or they are older than ``outbox_redispatch_s``;
   dedupe merges such repeats into the job that is still pending or running.
2. **Scheduler**: for each enabled ``job_definitions`` entry whose ``next_run_at`` has passed, advances
   ``next_run_at`` with compare-and-set and, only if that succeeded, enqueues the job (dedupe key ``def:<id>``
   unless the payload has its own). Missed fires collapse into one. After a Valkey loss nothing needs restoring:
   the next fire is derived from MongoDB.
3. Promotes due delayed jobs (``run_after``, retry backoff) and prunes consumers of departed workers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from redis.asyncio import Redis

from waystation_common.db import jobs as jobs_db
from waystation_common.db.client import Database
from waystation_common.jobs import keys
from waystation_common.jobs.model import JOB_TYPES, QUEUES, JobEnvelope
from waystation_common.jobs.queue import JobQueue
from waystation_common.jobs.schedule import parse_schedule
from waystation_common.leases import hold_lease
from waystation_worker.jobs.config import JobConfig
from waystation_worker.jobs.context import Clock

log = logging.getLogger('waystation.worker.scheduler')


class Coordinator:
    def __init__(
        self,
        queue: JobQueue,
        db: Database,
        valkey: Redis,
        *,
        worker_id: str,
        config: JobConfig,
        definitions: Sequence[jobs_db.Definition] = (),
        clock: Clock = time.time,
    ) -> None:
        self.queue = queue
        self.db = db
        self.valkey = valkey
        self.worker_id = worker_id
        self.config = config
        self.definitions = list(definitions)
        self.clock = clock
        self.is_leader = False
        self._definitions_ensured = False
        self._last_prune = 0.0

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    async def tick(self) -> bool:
        """One leader cycle if we hold (or win) the lease; returns whether we are the leader."""
        leader = await hold_lease(
            self.valkey, self.worker_id, key=keys.SCHEDULER_LEASE, ttl_ms=int(self.config.lease_ttl_s * 1000)
        )
        if leader != self.is_leader:
            log.info('scheduler leadership changed', extra={'leader': leader, 'worker_id': self.worker_id})
            self.is_leader = leader
        if not leader:
            return False
        if not self._definitions_ensured:
            await jobs_db.ensure_definitions(self.db, self.definitions)
            self._definitions_ensured = True
        await self.dispatch_outbox()
        await self.fire_schedules()
        now = self.clock()
        for queue in QUEUES:
            await self.queue.promote_due(queue, now=now)
        if time.monotonic() - self._last_prune > 60:
            self._last_prune = time.monotonic()
            for queue in QUEUES:
                await self.queue.prune_consumers(
                    queue, idle_ms=int(self.config.consumer_prune_s * 1000), keep=self.worker_id
                )
        return True

    async def dispatch_outbox(self) -> int:
        epoch = await self.queue.epoch()
        now = self._now()
        stale_before = now - timedelta(seconds=self.config.outbox_redispatch_s)
        sent = 0
        for doc in await jobs_db.outbox_to_dispatch(self.db, epoch=epoch, stale_before=stale_before):
            try:
                envelope = JobEnvelope.model_validate(doc['job'])
            except ValidationError:
                log.error('malformed outbox entry skipped', extra={'outbox_id': doc['_id']})
                await jobs_db.outbox_mark_done(self.db, [doc['_id']], now=now)
                continue
            result = await self.queue.enqueue(envelope, outbox_id=str(doc['_id']))
            await jobs_db.outbox_mark_sent(self.db, str(doc['_id']), epoch=epoch, now=now)
            if doc.get('sent_at') is not None:
                log.info('outbox entry re-dispatched', extra={'outbox_id': doc['_id'], 'status': result.status})
            sent += 1
        return sent

    async def fire_schedules(self) -> int:
        """Enqueue every due definition once; returns the number of fires."""
        now_ts = self.clock()
        now = self._now()
        fired = 0
        for doc in await jobs_db.enabled_definitions(self.db):
            definition_id = str(doc['_id'])
            try:
                schedule = parse_schedule(str(doc.get('schedule', '')))
                payload = JOB_TYPES[doc['kind']].model_validate(doc.get('params') or {})
            except (KeyError, ValueError, ValidationError) as exc:
                log.warning('job definition skipped', extra={'definition_id': definition_id, 'error': repr(exc)})
                continue
            current: datetime | None = doc.get('next_run_at')
            following = datetime.fromtimestamp(schedule.next_after(now_ts), UTC)
            if current is None:
                await jobs_db.advance_definition(
                    self.db, definition_id, expected=None, next_run_at=following, fired_at=None
                )
                continue
            if current > now:
                continue
            if not await jobs_db.advance_definition(
                self.db, definition_id, expected=current, next_run_at=following, fired_at=now
            ):
                continue  # another scheduler fired it
            queue = doc.get('queue')
            envelope = JobEnvelope.for_payload(
                payload,
                dedupe_key=payload.dedupe_key() or f'def:{definition_id}',
                queue=queue if queue in QUEUES else None,
                definition_id=definition_id,
                trigger='schedule',
                now=now,
            )
            await self.queue.enqueue(envelope)
            fired += 1
        return fired
