"""One worker process's job system: runner (consumer), coordinator (leader duties) and presence (``workers``)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version

from redis.asyncio import Redis

from waystation_common.db import jobs as jobs_db
from waystation_common.db.client import Database
from waystation_common.events import ActivityEvent, EventEmitter, EventType
from waystation_common.ids import new_id
from waystation_common.jobs.model import QUEUES
from waystation_common.jobs.queue import JobQueue
from waystation_common.settings import Settings
from waystation_worker.jobs.config import JobConfig
from waystation_worker.jobs.context import Clock, quietly
from waystation_worker.jobs.coordinator import Coordinator
from waystation_worker.jobs.registry import Handler
from waystation_worker.jobs.runner import Runner

log = logging.getLogger('waystation.worker.jobs')


def _version() -> str:
    try:
        return version('waystation-worker')
    except PackageNotFoundError:  # pragma: no cover - source checkout without metadata
        return 'unknown'


def new_worker_id() -> str:
    return f'{socket.gethostname()}-{os.getpid()}-{new_id()[-6:].lower()}'


class JobSystem:
    def __init__(
        self,
        db: Database,
        valkey: Redis,
        *,
        handlers: Mapping[str, Handler],
        worker_id: str | None = None,
        queues: Sequence[str] = QUEUES,
        concurrency: int = 4,
        definitions: Sequence[jobs_db.Definition] = (),
        config: JobConfig | None = None,
        clock: Clock = time.time,
        emitter: EventEmitter | None = None,
        settings: Settings | None = None,
    ) -> None:
        unknown = set(queues) - set(QUEUES)
        if unknown:
            raise ValueError(f'unknown job queues: {", ".join(sorted(unknown))}')
        self.db = db
        self.valkey = valkey
        self.worker_id = worker_id or new_worker_id()
        self.config = config or JobConfig()
        self.clock = clock
        self.emitter = emitter
        self.queues = list(queues)
        self.queue = JobQueue(valkey)
        self.runner = Runner(
            self.queue,
            db,
            valkey,
            worker_id=self.worker_id,
            queues=self.queues,
            concurrency=concurrency,
            handlers=handlers,
            config=self.config,
            clock=clock,
            emitter=emitter,
            settings=settings,
        )
        self.coordinator = Coordinator(
            self.queue,
            db,
            valkey,
            worker_id=self.worker_id,
            config=self.config,
            definitions=definitions,
            clock=clock,
        )
        self.started_at = datetime.fromtimestamp(clock(), UTC)

    def alive(self, max_age_s: float) -> bool:
        """The consumer loop polled recently (the container healthcheck's signal)."""
        return time.monotonic() - self.runner.last_poll <= max_age_s

    async def run(self, stop: asyncio.Event) -> None:
        """Run until ``stop`` is set (graceful: running jobs get ``shutdown_grace_s``) or cancelled (killed)."""
        log.info('job system started', extra={'worker_id': self.worker_id, 'queues': self.queues})
        self._worker_event(EventType.WORKER_ONLINE)
        tasks = [
            asyncio.create_task(self.runner.run(stop), name='job-runner'),
            asyncio.create_task(self._coordinate(stop), name='job-coordinator'),
            asyncio.create_task(self._presence(stop), name='job-presence'),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await quietly(jobs_db.worker_remove(self.db, self.worker_id))
        self._worker_event(EventType.WORKER_OFFLINE)
        log.info('job system stopped', extra={'worker_id': self.worker_id})

    async def _coordinate(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.coordinator.tick()
            except Exception:  # Valkey/MongoDB outage: retry next tick
                log.exception('scheduler tick failed')
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.config.tick_s)

    async def _presence(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            fields = {
                'hostname': socket.gethostname(),
                'version': _version(),
                'queues': self.queues,
                'started_at': self.started_at,
                'leader': self.coordinator.is_leader,
                'running_jobs': [
                    {'job_id': env.id, 'kind': env.kind, 'queue': env.queue}
                    for env in list(self.runner.running.values())
                ],
            }
            now = datetime.fromtimestamp(self.clock(), UTC)
            await quietly(jobs_db.worker_heartbeat(self.db, self.worker_id, fields, now=now))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.config.presence_s)

    def _worker_event(self, kind: EventType) -> None:
        if self.emitter is not None:
            ts = datetime.fromtimestamp(self.clock(), UTC)
            self.emitter.activity(
                ActivityEvent(type=kind, ts=ts, detail={'worker_id': self.worker_id, 'queues': self.queues})
            )
