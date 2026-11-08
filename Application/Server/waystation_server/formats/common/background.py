"""Fire-and-forget indexing work for format plugins (browse summaries recorded after a cache fill).

The response never waits for it (ARCHITECTURE §8: no MongoDB writes on the request's critical path beyond the fill
itself). Work with the same key runs once at a time; failures are logged, never raised. ``drain`` waits for
everything (tests, shutdown).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Hashable

log = logging.getLogger('waystation.formats.background')


class Background:
    def __init__(self, name: str) -> None:
        self.name = name
        self._tasks: set[asyncio.Task[None]] = set()
        self._running: set[Hashable] = set()

    def spawn(self, key: Hashable, work: Callable[[], Awaitable[None]]) -> None:
        """Run ``work()`` in the background unless work with ``key`` is already running."""
        if key in self._running:
            return
        self._running.add(key)

        async def run() -> None:
            try:
                await work()
            except Exception as exc:
                log.warning('background indexing failed', extra={'job': self.name, 'error': type(exc).__name__})
            finally:
                self._running.discard(key)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
