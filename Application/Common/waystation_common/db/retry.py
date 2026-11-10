"""One bounded retry for idempotent MongoDB reads (a T-P2-PERF finding).

PyMongo's own ``retryReads`` retries a read once, at once, on a network error. Under a connection storm (hundreds of
concurrent requests opening connections at the same moment) that immediate retry can meet the same condition, and a
cache hit then answers 500. ``read_retry`` wraps a *read* in one more attempt after a short random pause, so a
transient ``AutoReconnect`` (a pooled connection the server closed, a refused or reset connection, a primary
stepping down) costs a few tens of milliseconds instead of an error.

Only for reads: a write that failed with ``AutoReconnect`` may still have been applied, so writes are never retried
here (PyMongo's ``retryWrites`` covers the ones that are safe to retry).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from collections.abc import Awaitable, Callable

from pymongo.errors import AutoReconnect

log = logging.getLogger('waystation.db')

RETRY_DELAY_S = (0.02, 0.1)  # jitter: concurrent failures do not retry in lock step


def read_retry[**P, T](fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Decorate an idempotent read: on ``AutoReconnect`` wait 20-100 ms and try once more (the second failure
    propagates)."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await fn(*args, **kwargs)
        except AutoReconnect as exc:
            log.warning('mongodb read retried', extra={'operation': fn.__qualname__, 'error': str(exc)[:200]})
            await asyncio.sleep(random.uniform(*RETRY_DELAY_S))  # noqa: S311 - jitter, not security
        return await fn(*args, **kwargs)

    return wrapper
