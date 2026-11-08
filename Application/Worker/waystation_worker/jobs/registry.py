"""Job handlers by kind. A handler is ``async def h(payload: P, ctx: JobContext) -> dict | None`` registered with
``@handles(P)``; the returned dict is stored as the run's ``result``. Handlers must be idempotent (ARCH §10)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from waystation_common.jobs.model import JOB_TYPES, JobPayload
from waystation_worker.jobs.context import JobContext

Handler = Callable[[Any, JobContext], Awaitable[dict[str, Any] | None]]

HANDLERS: dict[str, Handler] = {}


def handles[P: JobPayload](
    payload_type: type[P], *, registry: dict[str, Handler] | None = None
) -> Callable[[Callable[[P, JobContext], Awaitable[dict[str, Any] | None]]], Handler]:
    if JOB_TYPES.get(payload_type.kind) is not payload_type:
        raise ValueError(f'{payload_type.__qualname__} is not a registered job type')
    target = HANDLERS if registry is None else registry

    def register(fn: Callable[[P, JobContext], Awaitable[dict[str, Any] | None]]) -> Handler:
        if payload_type.kind in target and target[payload_type.kind] is not fn:
            raise ValueError(f'a handler for {payload_type.kind!r} is already registered')
        target[payload_type.kind] = fn
        return fn

    return register
