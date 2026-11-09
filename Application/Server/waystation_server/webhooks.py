"""Webhook events from the server (SPECIFICATION §4.12, T-O05).

Webhook events are the audit records of content and repository changes (``waystation_common.webhooks.EVENTS``), so
every code path that audits an upload, delete or repository change also notifies webhooks without a second call:
``audit.record`` hands each successful record of those actions to ``WebhookNotifier.offer``.

``offer`` never blocks or raises (like the audit writer): it queues the record in a bounded in-process queue. One
task drains the queue, matches each event against the enabled webhooks (cached in-process, reloaded at most every
5 s and at once after a local change) and, when at least one wants it, writes a ``webhook.event`` job to the durable
outbox (``job_outbox``, ARCH §10). The worker fans it out into deliveries. Events nobody subscribed to cost nothing
beyond the match. On overflow the event is dropped, counted and logged at ERROR.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from waystation_common import webhooks as wh
from waystation_common.audit import AuditEvent, Outcome
from waystation_common.db import webhooks as webhooks_db
from waystation_common.db.client import Database, Document
from waystation_common.jobs import JobClient, WebhookEvent

log = logging.getLogger('waystation.webhooks')

QUEUE_SIZE = 10_000
CACHE_TTL_S = 5.0
_EVENTS = frozenset(wh.EVENTS)


class WebhookNotifier:
    def __init__(
        self,
        db: Database,
        jobs: JobClient,
        *,
        source: str | None = None,
        loader: Callable[[], Awaitable[list[Document]]] | None = None,
        maxsize: int = QUEUE_SIZE,
    ) -> None:
        self._jobs = jobs
        self._source = source
        self._loader = loader or (lambda: webhooks_db.list_enabled(db))
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task[None] | None = None
        self._hooks: list[Document] = []
        self._loaded_at = -1.0
        self.dropped = 0
        self.enqueued = 0

    # ---------------------------------------------------------------- producer side (request path)
    def offer(self, event: AuditEvent) -> None:
        """Queue an audit record if it is a webhook event (never blocks, never raises)."""
        if event.action not in _EVENTS or event.outcome is not Outcome.SUCCESS:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            log.error('WEBHOOK EVENTS DROPPED', extra={'action': event.action, 'dropped': self.dropped})

    def invalidate(self) -> None:
        """The webhook definitions changed (API): match against fresh ones from now on."""
        self._loaded_at = -1.0

    # ---------------------------------------------------------------- consumer side
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name='webhook-notifier')

    async def aclose(self) -> None:
        await self.flush(timeout_s=5.0)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def flush(self, timeout_s: float = 10.0) -> None:
        """Wait until every queued event has been handled (tests, shutdown)."""
        if self._task is None:
            while not self._queue.empty():
                await self._handle(self._queue.get_nowait())
                self._queue.task_done()
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_s):
                await self._queue.join()

    async def _enabled(self) -> list[Document]:
        if self._loaded_at < 0 or time.monotonic() - self._loaded_at >= CACHE_TTL_S:
            self._hooks = await self._loader()
            self._loaded_at = time.monotonic()
        return self._hooks

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('webhook event not enqueued', extra={'action': event.action, 'event_id': event.id})
            finally:
                self._queue.task_done()

    async def _handle(self, event: AuditEvent) -> None:
        target = event.target.model_dump(mode='json') if event.target else None
        repository = wh.event_repository(event.action, target, event.details)
        hooks = await self._enabled()
        if not any(wh.wants(h, event.action, repository) for h in hooks):
            return
        document = wh.build_event(
            event_id=event.id,
            event=event.action,
            occurred_at=event.ts,
            repository=repository,
            actor=event.actor.model_dump(mode='json'),
            target=target,
            details=event.details,
            changes=[c.model_dump(mode='json') for c in event.changes],
            source=self._source,
        )
        await self._jobs.enqueue_durable(
            WebhookEvent(event_id=event.id, event=event.action, repository=repository, document=document)
        )
        self.enqueued += 1
