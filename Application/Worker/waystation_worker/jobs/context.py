"""What a job handler gets besides its payload: resources, progress and log recording (``job_runs``)."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from waystation_common.db import jobs as jobs_db
from waystation_common.db.client import Database
from waystation_common.jobs.model import JobEnvelope
from waystation_common.settings import Settings

log = logging.getLogger('waystation.worker.jobs')

Clock = Callable[[], float]

PROGRESS_INTERVAL_S = 1.0  # progress is written at most once a second (the last value always lands at the end)


@dataclass
class JobContext:
    job: JobEnvelope
    run_id: str
    attempt: int  # 1 for the first attempt
    worker_id: str
    db: Database
    valkey: Redis
    clock: Clock = time.time
    settings: Settings | None = None
    _last_progress: float = field(default=0.0, repr=False)
    _pending_progress: tuple[int, int | None, str | None] | None = field(default=None, repr=False)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    async def progress(self, done: int, total: int | None = None, message: str | None = None) -> None:
        """Report progress (throttled to one write per second)."""
        self._pending_progress = (done, total, message)
        if time.monotonic() - self._last_progress >= PROGRESS_INTERVAL_S:
            await self.flush_progress()

    async def flush_progress(self) -> None:
        if self._pending_progress is None:
            return
        done, total, message = self._pending_progress
        self._pending_progress = None
        self._last_progress = time.monotonic()
        await quietly(jobs_db.run_progress(self.db, self.run_id, done=done, total=total, message=message))

    async def log(self, line: str) -> None:
        """Append a line to the run's log (capped at the last 500 lines)."""
        await quietly(jobs_db.run_log(self.db, self.run_id, [f'{self.now().isoformat()} {line}'[:2000]]))


async def quietly(op: Awaitable[Any]) -> None:
    """Run bookkeeping never fails a job (MongoDB hiccup): log and carry on."""
    try:
        await op
    except Exception as exc:
        log.warning('job bookkeeping failed', extra={'error': repr(exc)})
