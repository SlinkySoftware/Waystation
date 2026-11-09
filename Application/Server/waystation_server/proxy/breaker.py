"""Auto-block (SPECIFICATION §4.1): stop calling an upstream that keeps failing, probe it, resume automatically.

A circuit breaker per proxy repository, shared by every server process and replica through the repository's
upstream health hash in Valkey (``upstream:<repo_id>:health``, DATA_MODEL §2), updated by two Lua scripts:

- **record** (after every upstream answer): a success resets ``block_failures`` and lifts a block (``resumed``); a
  *blocking failure* (connection or DNS failure, timeout, reset, HTTP 5xx or 429; not 4xx, not a digest mismatch,
  not an SSRF refusal) increments it, and the ``failures``-th in a row blocks the upstream for ``initial_s``
  (``blocked_until``, ``block_level`` 0). A failure after the block period ended (a failed probe) blocks it again
  for twice the previous period, up to ``max_s``. Failures of requests that were already in flight when the block
  started change nothing.
- **admit** (before every upstream request): open, blocked (``UpstreamAutoBlocked``: 503 with ``Retry-After``; the
  engine serves a stale copy instead when it has one and the repository allows it), or, once ``blocked_until`` has
  passed, **probe**: exactly one caller across the cluster (``probe_until`` lease) gets to send its request, and
  its outcome decides (``record``). Everyone else stays blocked until then.

Each process also runs a small **prober** (``run_prober``): every few seconds it looks for blocked repositories
whose period has ended and probes them with the format's probe path (``proxy.probe``), so an upstream is resumed (or
blocked for longer) even when no client asks for it. Blocks are visible through upstream health
(``/stats/upstreams``: status ``blocked``, ``blocked_until``), ``upstream.state`` events and activity entries,
``waystation_upstream_state`` and ``waystation_upstream_autoblock_total``.

Valkey unavailable = no auto-block (every request goes upstream), never an error. A process remembers a block it has
seen for up to 5 s, so most blocked requests cost no Valkey round trip. Saving a repository clears its block.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum

from redis.asyncio import Redis

from waystation_common.errors import UpstreamError
from waystation_common.events import ActivityEvent, EventEmitter, EventType
from waystation_common.events import keys as event_keys
from waystation_server.metrics import Metrics
from waystation_server.repos.model import RepoConfig, RepoType

log = logging.getLogger('waystation.proxy.autoblock')

PROBE_LEASE_S = 90.0  # a probe that neither succeeds nor fails within this is retried by someone else
PROBER_INTERVAL_S = 5.0
LOCAL_RECHECK_S = 5.0
BLOCK_FIELDS = ('block_failures', 'blocked_until', 'blocked_at', 'block_level', 'probe_until', 'block_reason')

# KEYS[1] health hash; ARGV now (ms), probe lease (ms). Returns {verdict, blocked_until ms}.
_ADMIT = """
local untilv = redis.call('HGET', KEYS[1], 'blocked_until')
if not untilv then return {0, 0} end
local u = tonumber(untilv)
local now = tonumber(ARGV[1])
if now < u then return {1, u} end
local p = tonumber(redis.call('HGET', KEYS[1], 'probe_until') or '0')
if now < p then return {1, u} end
redis.call('HSET', KEYS[1], 'probe_until', tostring(math.floor(now + tonumber(ARGV[2]))))
return {2, u}
"""

# KEYS[1] health hash; ARGV ok (1/0), now (ms), failures threshold, initial (ms), max (ms), reason.
# Returns {transition, blocked_until ms, level}.
_RECORD = """
local now = tonumber(ARGV[2])
local untilv = redis.call('HGET', KEYS[1], 'blocked_until')
if ARGV[1] == '1' then
  if untilv then
    redis.call('HDEL', KEYS[1], 'blocked_until', 'blocked_at', 'block_level', 'probe_until', 'block_reason')
    redis.call('HSET', KEYS[1], 'block_failures', '0')
    return {2, 0, 0}
  end
  local n = redis.call('HGET', KEYS[1], 'block_failures')
  if n and n ~= '0' then redis.call('HSET', KEYS[1], 'block_failures', '0') end
  return {0, 0, 0}
end
local initial = tonumber(ARGV[4])
local maximum = tonumber(ARGV[5])
if untilv then
  local u = tonumber(untilv)
  if now < u then return {1, u, tonumber(redis.call('HGET', KEYS[1], 'block_level') or '0')} end
  local level = tonumber(redis.call('HGET', KEYS[1], 'block_level') or '0') + 1
  local period = math.min(maximum, initial * math.pow(2, level))
  u = math.floor(now + period)
  redis.call('HSET', KEYS[1], 'blocked_until', tostring(u), 'block_level', tostring(level), 'block_reason', ARGV[6])
  redis.call('HINCRBY', KEYS[1], 'block_failures', 1)
  redis.call('HDEL', KEYS[1], 'probe_until')
  return {3, u, level}
end
local n = redis.call('HINCRBY', KEYS[1], 'block_failures', 1)
if n >= tonumber(ARGV[3]) then
  local u = math.floor(now + math.min(maximum, initial))
  redis.call('HSET', KEYS[1], 'blocked_until', tostring(u), 'blocked_at', tostring(math.floor(now)),
    'block_level', '0', 'block_reason', ARGV[6])
  redis.call('HDEL', KEYS[1], 'probe_until')
  return {4, u, 0}
end
return {0, 0, 0}
"""


class Verdict(IntEnum):
    OPEN = 0
    BLOCKED = 1
    PROBE = 2


class Transition(IntEnum):
    NONE = 0
    STILL_BLOCKED = 1
    RESUMED = 2
    REBLOCKED = 3
    BLOCKED = 4


class UpstreamAutoBlocked(UpstreamError):  # noqa: N818 - reads as a state, like the other upstream errors
    """The upstream is auto-blocked; nothing was sent to it."""

    code = 'upstream_auto_blocked'
    status = 503

    def __init__(self, until: float, *, now: float | None = None) -> None:
        stamp = datetime.fromtimestamp(until, UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')
        super().__init__(f'upstream is auto-blocked after repeated failures until {stamp}')
        self.until = until
        wait = math.ceil(until - (time.time() if now is None else now))
        self.headers = {'Retry-After': str(max(1, wait))}


def blocking_status(status: int) -> bool:
    """Upstream HTTP answers that count as failures for auto-block (the upstream is down or refusing service)."""
    return status >= 500 or status == 429


@dataclass(frozen=True)
class BlockState:
    """The auto-block fields of an ``upstream:<id>:health`` hash."""

    failures: int = 0
    blocked_until: datetime | None = None
    blocked_at: datetime | None = None
    level: int = 0
    probing: bool = False
    reason: str | None = None

    @classmethod
    def parse(cls, raw: dict[str, str], *, now: float | None = None) -> BlockState:
        until = _ms(raw.get('blocked_until'))
        probe = _ms(raw.get('probe_until'))
        current = time.time() if now is None else now
        return cls(
            failures=_int(raw.get('block_failures')),
            blocked_until=datetime.fromtimestamp(until, UTC) if until is not None else None,
            blocked_at=datetime.fromtimestamp(at, UTC) if (at := _ms(raw.get('blocked_at'))) is not None else None,
            level=_int(raw.get('block_level')),
            probing=probe is not None and probe > current,
            reason=raw.get('block_reason') or None,
        )

    @property
    def blocked(self) -> bool:
        """Blocked, or its period ended and the next request (or the prober) probes it."""
        return self.blocked_until is not None


def _ms(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return int(float(value)) / 1000
    except ValueError:
        return None


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


Probe = Callable[[RepoConfig], Awaitable[tuple[bool, str | None]]]


class UpstreamBreaker:
    def __init__(
        self,
        valkey: Redis,
        emitter: EventEmitter,
        metrics: Metrics,
        *,
        clock: Callable[[], float] = time.time,
        probe_lease_s: float = PROBE_LEASE_S,
    ) -> None:
        self.valkey = valkey
        self.emitter = emitter
        self.metrics = metrics
        self.clock = clock
        self.probe_lease_s = probe_lease_s
        self._admit = valkey.register_script(_ADMIT)
        self._record = valkey.register_script(_RECORD)
        # repo id -> (blocked until, re-check Valkey after), as last seen by this process: blocked requests skip
        # Valkey for at most LOCAL_RECHECK_S, so a block lifted elsewhere (a probe, a saved repository) ends here soon.
        self._blocked: dict[str, tuple[float, float]] = {}
        self._prober: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ request path
    async def admit(self, repo: RepoConfig) -> Verdict:
        """Before an upstream request: OPEN or PROBE (send it), else raise ``UpstreamAutoBlocked``."""
        if not self._enabled(repo):
            return Verdict.OPEN
        now = self.clock()
        known = self._blocked.get(repo.id)
        if known is not None and now < known[1]:
            raise UpstreamAutoBlocked(known[0], now=now)
        try:
            verdict, until_ms = await self._admit(
                keys=[event_keys.upstream_health_key(repo.id)], args=[int(now * 1000), int(self.probe_lease_s * 1000)]
            )
        except Exception as exc:  # Valkey down: no auto-block
            log.warning('auto-block check failed', extra={'repo': repo.name, 'error': type(exc).__name__})
            return Verdict.OPEN
        if Verdict(int(verdict)) is Verdict.OPEN:
            self._blocked.pop(repo.id, None)
            return Verdict.OPEN
        until = int(until_ms) / 1000
        if Verdict(int(verdict)) is Verdict.PROBE:
            log.info('probing auto-blocked upstream', extra={'repo': repo.name})
            return Verdict.PROBE
        self._remember(repo.id, until, now)
        raise UpstreamAutoBlocked(max(until, now + 1), now=now)

    def _remember(self, repo_id: str, until: float, now: float) -> None:
        if until > now:
            self._blocked[repo_id] = (until, min(until, now + LOCAL_RECHECK_S))

    async def success(self, repo: RepoConfig) -> None:
        if self._enabled(repo) or repo.id in self._blocked:
            await self._run(repo, ok=True, reason='')

    async def failure(self, repo: RepoConfig, reason: str) -> None:
        if self._enabled(repo):
            await self._run(repo, ok=False, reason=reason)

    @staticmethod
    def _enabled(repo: RepoConfig) -> bool:
        return repo.proxy is not None and repo.proxy.auto_block.enabled and not repo.proxy.offline

    async def _run(self, repo: RepoConfig, *, ok: bool, reason: str) -> None:
        assert repo.proxy is not None
        cfg = repo.proxy.auto_block
        now = self.clock()
        try:
            transition, until_ms, level = await self._record(
                keys=[event_keys.upstream_health_key(repo.id)],
                args=[
                    '1' if ok else '0',
                    int(now * 1000),
                    cfg.failures,
                    cfg.initial_s * 1000,
                    max(cfg.max_s, cfg.initial_s) * 1000,
                    reason[:300],
                ],
            )
        except Exception as exc:  # Valkey down: no auto-block
            log.warning('auto-block update failed', extra={'repo': repo.name, 'error': type(exc).__name__})
            return
        change = Transition(int(transition))
        until = int(until_ms) / 1000
        if change in (Transition.BLOCKED, Transition.REBLOCKED, Transition.STILL_BLOCKED):
            self._remember(repo.id, until, now)
        elif change is Transition.RESUMED or ok:
            self._blocked.pop(repo.id, None)
        if change in (Transition.BLOCKED, Transition.REBLOCKED, Transition.RESUMED):
            self._announce(repo, change, until, int(level), reason, now)

    def _announce(
        self, repo: RepoConfig, change: Transition, until: float, level: int, reason: str, now: float
    ) -> None:
        assert repo.proxy is not None
        event = {Transition.BLOCKED: 'blocked', Transition.REBLOCKED: 'reblocked', Transition.RESUMED: 'resumed'}[
            change
        ]
        self.metrics.upstream_autoblock(repo=repo.name, event=event)
        detail: dict[str, object] = {'state': 'ok' if change is Transition.RESUMED else 'blocked', 'event': event}
        if change is Transition.RESUMED:
            summary = f'Upstream of {repo.name} answers again; auto-block lifted'
            log.info('upstream auto-block lifted', extra={'repo': repo.name})
        else:
            stamp = datetime.fromtimestamp(until, UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')
            period = round(until - now)
            if change is Transition.BLOCKED:
                summary = (
                    f'Upstream of {repo.name} auto-blocked for {period} s after '
                    f'{repo.proxy.auto_block.failures} consecutive failures ({reason})'
                )
            else:
                summary = f'Upstream of {repo.name} still failing ({reason}); blocked for {period} s'
            detail.update(blocked_until=stamp, level=level, reason=reason)
            log.warning('upstream auto-blocked', extra={'repo': repo.name, 'error': reason, 'until': stamp})
        detail['summary'] = summary
        self.emitter.activity(
            ActivityEvent(
                type=EventType.UPSTREAM_STATE,
                ts=datetime.fromtimestamp(now, UTC),
                repo_id=repo.id,
                repo=repo.name,
                format=repo.format,
                detail=detail,
            )
        )

    async def reset(self, repo_id: str) -> None:
        """Forget a repository's auto-block state (its configuration was saved)."""
        self._blocked.pop(repo_id, None)
        try:
            await self.valkey.hdel(event_keys.upstream_health_key(repo_id), *BLOCK_FIELDS)
        except Exception as exc:  # live state only
            log.warning('could not clear auto-block state', extra={'error': type(exc).__name__})

    # ------------------------------------------------------------------ prober
    def start_prober(self, repos: Callable[[], Awaitable[Sequence[RepoConfig]]], probe: Probe) -> None:
        if self._prober is None:
            self._prober = asyncio.create_task(self._probe_loop(repos, probe), name='autoblock-prober')

    async def stop(self) -> None:
        task, self._prober = self._prober, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _probe_loop(self, repos: Callable[[], Awaitable[Sequence[RepoConfig]]], probe: Probe) -> None:
        while True:
            await asyncio.sleep(PROBER_INTERVAL_S)
            try:
                await self.probe_due(await repos(), probe)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('auto-block prober failed', extra={'error': type(exc).__name__})

    async def probe_due(self, repos: Sequence[RepoConfig], probe: Probe) -> int:
        """Probe every blocked upstream whose period has ended (one prober per upstream across the cluster).
        Returns the number of probes sent."""
        candidates = [r for r in repos if r.type is RepoType.PROXY and r.online and self._enabled(r)]
        if not candidates:
            return 0
        async with self.valkey.pipeline(transaction=False) as pipe:
            for repo in candidates:
                pipe.hmget(event_keys.upstream_health_key(repo.id), ['blocked_until', 'probe_until'])
            rows = await pipe.execute()
        now = self.clock()
        sent = 0
        for repo, (until, probing) in zip(candidates, rows, strict=True):
            due = _ms(until)
            if due is None or due > now or ((p := _ms(probing)) is not None and p > now):
                continue
            try:
                verdict = await self.admit(repo)
            except UpstreamAutoBlocked:
                continue
            if verdict is not Verdict.PROBE:
                continue
            sent += 1
            ok, error = await probe(repo)
            if ok:
                await self.success(repo)
            else:
                await self.failure(repo, error or 'probe failed')
        return sent
