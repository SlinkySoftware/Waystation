"""Worker process: runs the job system (ARCHITECTURE §10) and the liveness heartbeat until SIGTERM/SIGINT.

``jobs_loop`` connects to MongoDB and Valkey, refuses to run against a database at another migration level, and
runs ``JobSystem`` for ``WAYSTATION_WORKER_QUEUES`` with ``WAYSTATION_WORKER_CONCURRENCY`` jobs at a time. Every
worker also competes for the scheduler lease (outbox dispatch, cron schedules incl. the 10 s stats rollup).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Callable, Coroutine
from typing import Any

from waystation_common.db.client import create_client, database
from waystation_common.db.migrations import check_schema
from waystation_common.events import EventEmitter
from waystation_common.logs import configure_logging
from waystation_common.settings import Settings, get_settings
from waystation_common.valkey import create_valkey
from waystation_worker import health
from waystation_worker.jobs import BUILTIN_DEFINITIONS, HANDLERS, JobSystem

log = logging.getLogger('waystation.worker')

Loop = Callable[[Settings, asyncio.Event], Coroutine[Any, Any, None]]


async def heartbeat_loop(settings: Settings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        if health.watchdog_ok():
            await asyncio.to_thread(health.beat)
        else:
            log.error('job runner stalled; withholding the liveness heartbeat')
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=health.INTERVAL_S)


async def jobs_loop(settings: Settings, stop: asyncio.Event) -> None:  # pragma: no cover - wiring, run by the stack
    redis = create_valkey(settings)
    client = create_client(settings, app_name='waystation-worker')
    db = database(client, settings)
    try:
        await check_schema(db)
        async with EventEmitter(redis) as emitter:
            system = JobSystem(
                db,
                redis,
                handlers=HANDLERS,
                queues=settings.worker_queues,
                concurrency=settings.worker_concurrency,
                definitions=BUILTIN_DEFINITIONS,
                emitter=emitter,
                settings=settings,
            )
            health.set_watchdog(lambda: system.alive(health.MAX_AGE_S))
            try:
                await system.run(stop)
            finally:
                health.set_watchdog(None)
    finally:
        await redis.aclose()
        await client.close()


LOOPS: list[Loop] = [heartbeat_loop, jobs_loop]


async def run(settings: Settings, stop: asyncio.Event, loops: list[Loop] | None = None) -> None:
    """Run every loop until ``stop`` is set; a crashing loop stops the worker (the container restarts it)."""
    tasks = [
        asyncio.create_task(loop(settings, stop), name=getattr(loop, '__name__', 'loop')) for loop in loops or LOOPS
    ]
    log.info('worker started', extra={'loops': [t.get_name() for t in tasks]})
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()  # re-raise a loop's failure
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info('worker stopped')


async def _main() -> None:  # pragma: no cover - process entry point
    settings = get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(settings, stop)


def main() -> None:  # pragma: no cover - process entry point
    os.umask(0o027)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    asyncio.run(_main())
