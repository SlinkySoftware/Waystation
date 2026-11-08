"""Non-blocking event emitter (ARCHITECTURE §4 step 6, §8). See the package docstring for the API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import Pipeline

from waystation_common.events import keys
from waystation_common.events.schema import (
    FROM_CACHE,
    ActivityEvent,
    CacheState,
    Direction,
    EventType,
    LiveMessage,
    Transfer,
)

log = logging.getLogger('waystation.events')

Op = Callable[[Pipeline], Any]


@dataclass
class _Live:
    transfer: Transfer
    last_bytes: int
    last_time: float
    dirty: bool = False


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


class EventEmitter:
    def __init__(
        self,
        redis: Redis,
        *,
        flush_interval_s: float = 0.25,
        max_queue: int = 50_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._interval = flush_interval_s
        self._max_queue = max_queue
        self._clock = clock
        self._ops: deque[Op] = deque()
        self._live: dict[str, _Live] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.dropped = 0  # ops dropped because the queue was full
        self.failed_flushes = 0  # pipelines that failed (Valkey down): their ops are lost, never retried
        self._last_error_log = 0.0

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name='event-emitter')

    async def aclose(self) -> None:
        """Stop the flush loop after a final flush."""
        if self._task is not None:
            self._stopping.set()
            await self._task
            self._task = None
        await self.flush()

    async def __aenter__(self) -> EventEmitter:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), self._interval)
            await self.flush()

    async def flush(self) -> None:
        """Send queued ops and dirty progress in one pipeline (called every ``flush_interval_s``)."""
        ops: list[Op] = []
        while self._ops:
            ops.append(self._ops.popleft())
        for live in self._live.values():
            if live.dirty:
                live.dirty = False
                ops.append(self._live_ops(EventType.TRANSFER_PROGRESS, live.transfer))
        if not ops:
            return
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for op in ops:
                    op(pipe)
                await pipe.execute()
        except Exception as exc:
            self.failed_flushes += 1
            now = self._clock()
            if now - self._last_error_log > 60:
                self._last_error_log = now
                log.warning('event flush failed; events dropped', extra={'error': repr(exc), 'ops': len(ops)})

    def _enqueue(self, op: Op) -> None:
        if len(self._ops) >= self._max_queue:
            self.dropped += 1
            return
        self._ops.append(op)

    # ------------------------------------------------------------------ ops
    def call(self, op: Op) -> None:
        """Queue one small Valkey write (``op(pipe)``) for the next flush: fire-and-forget, never raises."""
        self._enqueue(op)

    @staticmethod
    def _live_ops(kind: EventType, transfer: Transfer) -> Op:
        state = transfer.model_dump_json()
        message = LiveMessage(type=kind, transfer=transfer).model_dump_json()

        def op(pipe: Pipeline) -> None:
            if kind in (EventType.TRANSFER_FINISHED, EventType.TRANSFER_FAILED):
                pipe.hdel(keys.TRANSFERS, transfer.id)
            else:
                pipe.hset(keys.TRANSFERS, transfer.id, state)
            pipe.publish(keys.LIVE, message)

        return op

    def activity(self, event: ActivityEvent) -> None:
        """Append to the ``activity`` stream (uploads, deletes, upstream state, jobs...)."""
        data = event.model_dump_json()

        def op(pipe: Pipeline) -> None:
            pipe.xadd(keys.ACTIVITY, {'type': event.type.value, 'data': data}, maxlen=keys.ACTIVITY_MAXLEN)

        self._enqueue(op)

    def request(
        self,
        *,
        repo_id: str,
        format: str,
        cache: CacheState,
        duration_ms: float,
        bytes_out: int = 0,
        bytes_in_upstream: int = 0,
        bytes_in_upload: int = 0,
        ts: float | None = None,
    ) -> None:
        """Count one finished request in the per-minute counters and latency histogram.

        Transfers are counted by ``transfer_finished``; call this for everything else (metadata, HEAD, 404...).
        """
        when = self._clock() if ts is None else ts
        minute = keys.minute_of(when)
        incr: dict[str, int] = {'requests': 1}
        if cache in FROM_CACHE:
            incr['hits' if cache is not CacheState.STALE else 'stale'] = 1
            incr['bytes_out_cache'] = bytes_out
        elif cache is CacheState.ERROR:
            incr['errors'] = 1
        else:
            incr['misses' if cache is CacheState.FETCHING else 'coalesced'] = 1
            incr['bytes_out_upstream'] = bytes_out
        if bytes_in_upstream:
            incr['bytes_in_upstream'] = bytes_in_upstream
        if bytes_in_upload:
            incr['bytes_in_upload'] = bytes_in_upload
        cnt, lat = keys.cnt_key(minute, repo_id), keys.lat_key(minute, repo_id)
        bucket = keys.latency_bucket(duration_ms)

        def op(pipe: Pipeline) -> None:
            for field, value in incr.items():
                if value:
                    pipe.hincrby(cnt, field, value)
            pipe.hsetnx(cnt, 'format', format)
            pipe.hincrby(lat, bucket, 1)
            pipe.expire(cnt, keys.COUNTER_TTL_S)
            pipe.expire(lat, keys.COUNTER_TTL_S)
            pipe.sadd(keys.STATS_PENDING, keys.pending_member(minute, repo_id))

        self._enqueue(op)

    def upstream(
        self,
        *,
        repo_id: str,
        format: str,
        ok: bool,
        count_request: bool = True,
        error: str | None = None,
        latency_ms: float | None = None,
        ts: float | None = None,
    ) -> None:
        """Count one request sent to an upstream (``upstream_requests``; failures also ``upstream_errors``) and
        update the repository's upstream health hash (``upstream:<repo_id>:health``)."""
        now = self._clock() if ts is None else ts
        minute = keys.minute_of(now)
        cnt = keys.cnt_key(minute, repo_id)
        health = keys.upstream_health_key(repo_id)
        stamp = _dt(now).isoformat()

        def op(pipe: Pipeline) -> None:
            if count_request:
                pipe.hincrby(cnt, 'upstream_requests', 1)
            if not ok:
                pipe.hincrby(cnt, 'upstream_errors', 1)
            pipe.hsetnx(cnt, 'format', format)
            pipe.expire(cnt, keys.COUNTER_TTL_S)
            pipe.sadd(keys.STATS_PENDING, keys.pending_member(minute, repo_id))
            if ok:
                fields: dict[Any, Any] = {'last_ok_at': stamp, 'consecutive_failures': '0'}
                if latency_ms is not None:
                    fields['last_latency_ms'] = f'{latency_ms:.1f}'
                pipe.hset(health, mapping=fields)
            else:
                pipe.hset(
                    health,
                    mapping={
                        'last_error': (error or 'error')[:300],
                        'last_error_at': stamp,
                    },
                )
                pipe.hincrby(health, 'consecutive_failures', 1)

        self._enqueue(op)

    # ------------------------------------------------------------------ transfers
    def transfer_started(self, transfer: Transfer) -> None:
        now = self._clock()
        self._live[transfer.id] = _Live(transfer, transfer.bytes_done, now)
        self._enqueue(self._live_ops(EventType.TRANSFER_STARTED, transfer))
        self.activity(
            ActivityEvent(
                type=EventType.TRANSFER_STARTED,
                ts=_dt(now),
                repo_id=transfer.repo_id,
                repo=transfer.repo,
                format=transfer.format,
                path=transfer.path,
                principal=transfer.principal,
                client_ip=transfer.client_ip,
                cache=transfer.cache,
                transfer_id=transfer.id,
            )
        )

    def transfer_progress(
        self,
        transfer_id: str,
        bytes_done: int,
        *,
        bytes_total: int | None = None,
        cache: CacheState | None = None,
        upstream_latency_ms: float | None = None,
        followers: int | None = None,
    ) -> None:
        """Update in memory only (O(1)); the latest state is published at most every ``flush_interval_s``."""
        live = self._live.get(transfer_id)
        if live is None:
            return
        now = self._clock()
        elapsed = now - live.last_time
        rate = live.transfer.rate_bps
        if elapsed >= 0.2:
            instant = (bytes_done - live.last_bytes) / elapsed
            rate = instant if rate == 0 else 0.5 * rate + 0.5 * instant
            live.last_bytes, live.last_time = bytes_done, now
        update: dict[str, Any] = {'bytes_done': bytes_done, 'rate_bps': rate, 'updated_at': _dt(now)}
        if bytes_total is not None:
            update['bytes_total'] = bytes_total
        if cache is not None:
            update['cache'] = cache
        if upstream_latency_ms is not None:
            update['upstream_latency_ms'] = upstream_latency_ms
        if followers is not None:
            update['followers'] = followers
        live.transfer = live.transfer.model_copy(update=update)
        live.dirty = True

    def transfer_followers(self, transfer_id: str, followers: int) -> None:
        """Set the number of requests coalesced onto this transfer (published with the next flush)."""
        live = self._live.get(transfer_id)
        if live is None or live.transfer.followers == followers:
            return
        live.transfer = live.transfer.model_copy(update={'followers': followers})
        live.dirty = True

    def transfer_finished(
        self,
        transfer_id: str,
        *,
        status: int,
        bytes_done: int | None = None,
        cache: CacheState | None = None,
        error: str | None = None,
    ) -> None:
        """Remove the live entry, record activity and count the request. ``error`` marks it failed."""
        live = self._live.pop(transfer_id, None)
        if live is None:
            return
        now = self._clock()
        t = live.transfer
        done = t.bytes_done if bytes_done is None else bytes_done
        final_cache = CacheState.ERROR if error else (cache or t.cache)
        t = t.model_copy(update={'bytes_done': done, 'cache': final_cache, 'updated_at': _dt(now), 'error': error})
        kind = EventType.TRANSFER_FAILED if error else EventType.TRANSFER_FINISHED
        duration_ms = (now - t.started_at.timestamp()) * 1000
        self._enqueue(self._live_ops(kind, t))
        self.activity(
            ActivityEvent(
                type=kind,
                ts=_dt(now),
                repo_id=t.repo_id,
                repo=t.repo,
                format=t.format,
                path=t.path,
                principal=t.principal,
                client_ip=t.client_ip,
                cache=final_cache,
                status=status,
                bytes=done,
                duration_ms=round(duration_ms, 3),
                transfer_id=t.id,
                detail={'error': error} if error else {},
            )
        )
        upload = t.direction is Direction.UPLOAD
        self.request(
            repo_id=t.repo_id,
            format=t.format,
            cache=final_cache,
            duration_ms=duration_ms,
            bytes_out=0 if upload else done,
            bytes_in_upstream=done if final_cache is CacheState.FETCHING and not upload else 0,
            bytes_in_upload=done if upload else 0,
            ts=now,
        )

    @property
    def live_transfers(self) -> int:
        return len(self._live)
