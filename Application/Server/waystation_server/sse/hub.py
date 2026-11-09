"""The per-process event hub behind ``GET /api/v1/events/stream`` (ARCHITECTURE §8).

One hub per server process runs (from the first subscriber until shutdown):

- a ``live`` pub/sub listener (transfer started/progress/finished/failed; progress is already coalesced to ≤ 4 Hz
  per transfer by the emitter),
- one blocking ``XREAD`` on the ``activity`` stream (durable events; their stream ids are the SSE ``id:``),
- ``upstream.state`` (the repository's ``UpstreamHealth``) after every upstream state change in the activity stream
  (auto-block blocked / reblocked / resumed), for every subscriber that follows anything global or that repository,
- a 1 Hz ``stats.tick`` computed from the live per-minute counters (rates over the last 10 s, hit ratio over the last
  minute), only while someone subscribes to ``stats``,

and fans each event out to the connected clients whose topics match and who may see the repository (RBAC,
re-evaluated every 30 s). Each client has a bounded queue; a client that cannot keep up loses events (it can resume
the activity stream with ``Last-Event-ID``; live state is re-sent as a snapshot on reconnect).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from waystation_common.events import EventType, LiveMessage, keys
from waystation_server.security.principal import Principal
from waystation_server.stats import live, series

log = logging.getLogger('waystation.sse')

QUEUE_SIZE = 2000
VISIBILITY_REFRESH_S = 30.0
TICK_S = 1.0
RATE_WINDOW_S = 10.0
RATIO_WINDOW_S = 60.0
GLOBAL_TOPICS = frozenset({'transfers', 'stats', 'activity'})

Frame = tuple[str | None, str, str]  # (id, event, data)
VisibleRepos = Callable[[Principal], Awaitable[set[str]]]
StorageFigures = Callable[[], Awaitable[tuple[int, int]]]  # (used bytes, artifacts)
UpstreamHealthSource = Callable[[str], Awaitable[dict[str, Any] | None]]  # repository name -> UpstreamHealth


@dataclass(eq=False)
class Subscriber:
    principal: Principal
    topics: frozenset[str]
    visible: set[str] = field(default_factory=set)
    queue: asyncio.Queue[Frame | None] = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    dropped: int = 0

    @property
    def repo_topics(self) -> set[str]:
        return {t[5:] for t in self.topics if t.startswith('repo:')}

    def wants(self, topic: str, repo: str | None) -> bool:
        if repo is not None and repo not in self.visible:
            return False
        if topic == 'upstream':
            return bool(self.topics & GLOBAL_TOPICS) or (repo is not None and repo in self.repo_topics)
        if topic in self.topics:
            return True
        return repo is not None and repo in self.repo_topics and topic in ('transfers', 'activity')

    def offer(self, frame: Frame) -> None:
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped += 1


def _frame(event: dict[str, Any], stream_id: str | None = None) -> Frame:
    return stream_id, str(event['type']), json.dumps(event, separators=(',', ':'), default=str)


class Hub:
    def __init__(self, valkey: Redis, visible_repos: VisibleRepos, storage: StorageFigures) -> None:
        self.valkey = valkey
        self._visible_repos = visible_repos
        self._storage = storage
        self.upstream_health: UpstreamHealthSource | None = None  # set by the event stream route
        self.subscribers: set[Subscriber] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False

    # ------------------------------------------------------------------ lifecycle
    async def subscribe(self, sub: Subscriber) -> None:
        sub.visible = await self._visible_repos(sub.principal)
        self.subscribers.add(sub)
        if not self._tasks and not self._closed:
            self._tasks = [
                asyncio.create_task(self._live_loop(), name='sse-live'),
                asyncio.create_task(self._activity_loop(), name='sse-activity'),
                asyncio.create_task(self._tick_loop(), name='sse-stats'),
            ]

    def unsubscribe(self, sub: Subscriber) -> None:
        self.subscribers.discard(sub)

    async def stop(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        for sub in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(None)

    def publish(self, topic: str, repo: str | None, frame: Frame) -> None:
        for sub in list(self.subscribers):
            if sub.wants(topic, repo):
                sub.offer(frame)

    # ------------------------------------------------------------------ sources
    async def _live_loop(self) -> None:
        while True:
            pubsub = self.valkey.pubsub()
            try:
                await pubsub.subscribe(keys.LIVE)
                async for message in pubsub.listen():
                    if message.get('type') != 'message':
                        continue
                    try:
                        parsed = LiveMessage.model_validate_json(message['data'])
                    except ValueError:
                        continue
                    event = live.live_event(parsed)
                    if event is not None:
                        self.publish('transfers', parsed.transfer.repo, _frame(event))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('live channel lost; reconnecting', extra={'error': type(exc).__name__})
                await asyncio.sleep(1.0)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def _activity_loop(self) -> None:
        last_id = '$'
        while True:
            try:
                entries = await live.xread(self.valkey, after=last_id, block_ms=5000, count=200)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('activity stream read failed', extra={'error': type(exc).__name__})
                await asyncio.sleep(1.0)
                continue
            for stream_id, fields in entries:
                last_id = stream_id
                parsed = live.parse_activity(fields)
                if parsed is None:
                    continue
                view = live.activity_view(stream_id, parsed)
                if view is not None:
                    event = {'type': 'activity', 'ts': view['ts'], 'event': view}
                    self.publish('activity', parsed.repo, _frame(event, stream_id))
                if parsed.type is EventType.UPSTREAM_STATE and parsed.repo:
                    await self._upstream_state(parsed.repo, parsed.ts)

    async def _upstream_state(self, repo: str, ts: datetime) -> None:
        if self.upstream_health is None:
            return
        try:
            health = await self.upstream_health(repo)
        except Exception as exc:
            log.warning('upstream health lookup failed', extra={'error': type(exc).__name__})
            return
        if health is not None:
            stamp = ts.astimezone(UTC).isoformat().replace('+00:00', 'Z')
            self.publish('upstream', repo, _frame({'type': 'upstream.state', 'ts': stamp, 'health': health}))

    async def _tick_loop(self) -> None:
        ticker = StatsTicker(self.valkey, self._storage)
        next_refresh = time.monotonic() + VISIBILITY_REFRESH_S
        while True:
            await asyncio.sleep(TICK_S)
            if time.monotonic() >= next_refresh:
                next_refresh = time.monotonic() + VISIBILITY_REFRESH_S
                for sub in list(self.subscribers):
                    with contextlib.suppress(Exception):
                        sub.visible = await self._visible_repos(sub.principal)
            if not any('stats' in sub.topics for sub in self.subscribers):
                ticker.reset()
                continue
            try:
                event = await ticker.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('stats tick failed', extra={'error': type(exc).__name__})
                continue
            if event is not None:
                self.publish('stats', None, _frame(event))


class StatsTicker:
    """Rates from successive reads of the live per-minute counters: the increase of each (minute, repository)
    counter since the previous tick, summed over a sliding window."""

    FIELDS = (
        'requests',
        'hits',
        'stale',
        'misses',
        'coalesced',
        'bytes_out_cache',
        'bytes_out_upstream',
        'bytes_in_upstream',
    )

    def __init__(self, valkey: Redis, storage: StorageFigures, *, clock: Callable[[], float] = time.time) -> None:
        self.valkey = valkey
        self.storage = storage
        self.clock = clock
        self.reset()

    def reset(self) -> None:
        self._seen: dict[tuple[int, str], dict[str, int]] | None = None
        self._window: deque[tuple[float, dict[str, int]]] = deque()

    async def tick(self) -> dict[str, Any] | None:
        now = self.clock()
        minute = int(now // 60)
        rows = await series.live_minutes(self.valkey, since_minute=minute - 1)
        current = {(m, r): {f: int(c.get(f, 0) or 0) for f in self.FIELDS} for m, r, c in rows}
        delta = dict.fromkeys(self.FIELDS, 0)
        if self._seen is not None:
            for key, values in current.items():
                before = self._seen.get(key)
                for f in self.FIELDS:
                    delta[f] += max(0, values[f] - (before[f] if before else 0))
        self._seen = current
        self._window.append((now, delta))
        while self._window and now - self._window[0][0] > RATIO_WINDOW_S:
            self._window.popleft()
        recent = [d for t, d in self._window if now - t <= RATE_WINDOW_S]
        span = min(RATE_WINDOW_S, max(TICK_S, now - self._window[0][0])) if self._window else RATE_WINDOW_S

        def rate(f: str) -> float:
            return round(sum(d[f] for d in recent) / span, 2)

        minute_totals = {f: sum(d[f] for _, d in self._window) for f in self.FIELDS}
        used, artifacts = await self.storage()
        return {
            'type': 'stats.tick',
            'ts': datetime.fromtimestamp(now, UTC).isoformat().replace('+00:00', 'Z'),
            'requests_per_s': rate('requests'),
            'bytes_out_per_s': round(rate('bytes_out_cache') + rate('bytes_out_upstream'), 1),
            'bytes_upstream_per_s': rate('bytes_in_upstream'),
            'bytes_saved_per_s': rate('bytes_out_cache'),
            'hit_ratio_1m': series.hit_ratio(minute_totals),
            'active_transfers': int(await self.valkey.hlen(keys.TRANSFERS)),
            'storage_used_bytes': used,
            'artifacts_cached': artifacts,
        }
