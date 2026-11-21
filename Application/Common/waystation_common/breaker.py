"""Auto-block (SPECIFICATION §4.1, ARCHITECTURE §6.1): the circuit breaker per proxy repository, shared by every
server and worker process through the repository's upstream health hash in Valkey (``upstream:<repo_id>:health``,
DATA_MODEL §2), updated by two Lua scripts:

- **record** (after every upstream answer): a success resets ``block_failures`` and lifts a block (``resumed``); a
  *blocking failure* (connection or DNS failure, timeout, reset, HTTP 5xx or 429; not 4xx, not a digest mismatch,
  not an SSRF refusal) increments it, and the ``failures``-th in a row blocks the upstream for ``initial_s``
  (``blocked_until``, ``block_level`` 0). A failure after the block period ended (a failed probe) blocks it again
  for twice the previous period, up to ``max_s``. Failures of requests that were already in flight when the block
  started change nothing.
- **admit** (before every upstream request): open, blocked (``UpstreamAutoBlocked``), or, once ``blocked_until`` has
  passed, **probe**: exactly one caller across the cluster (``probe_until`` lease) gets to send its request, and its
  outcome decides (``record``). Everyone else stays blocked until then.

``AutoBlockBreaker`` is the shared core, keyed by a ``BreakerTarget`` (repository id, name, auto-block settings).
The server's ``waystation_server.proxy.breaker.UpstreamBreaker`` adds the request-path API on repository configs,
the announcements (events, metrics) and the prober; the worker's upstream requests (``waystation_worker.upstream``)
use the core directly. Callers decide whether auto-block applies (``BreakerTarget.enabled``).

Valkey unavailable = no auto-block (every request goes upstream), never an error. A process remembers a block it has
seen for up to 5 s, so most blocked requests cost no Valkey round trip.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any

from redis.asyncio import Redis

from waystation_common.errors import UpstreamError
from waystation_common.events import keys as event_keys

log = logging.getLogger('waystation.proxy.autoblock')

DEFAULT_FAILURES = 5
DEFAULT_INITIAL_S = 30
DEFAULT_MAX_S = 1800

PROBE_LEASE_S = 90.0  # a probe that neither succeeds nor fails within this is retried by someone else
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
        until = ms_to_s(raw.get('blocked_until'))
        probe = ms_to_s(raw.get('probe_until'))
        current = time.time() if now is None else now
        return cls(
            failures=_int(raw.get('block_failures')),
            blocked_until=datetime.fromtimestamp(until, UTC) if until is not None else None,
            blocked_at=datetime.fromtimestamp(at, UTC) if (at := ms_to_s(raw.get('blocked_at'))) is not None else None,
            level=_int(raw.get('block_level')),
            probing=probe is not None and probe > current,
            reason=raw.get('block_reason') or None,
        )

    @property
    def blocked(self) -> bool:
        """Blocked, or its period ended and the next request (or the prober) probes it."""
        return self.blocked_until is not None


def ms_to_s(value: str | None) -> float | None:
    """An epoch-milliseconds field of the health hash as epoch seconds (None when absent or invalid)."""
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


@dataclass(frozen=True)
class BreakerTarget:
    """One repository's upstream as the breaker sees it."""

    repo_id: str
    name: str
    format: str = ''
    enabled: bool = True  # auto-block on and the repository not offline
    failures: int = DEFAULT_FAILURES
    initial_s: int = DEFAULT_INITIAL_S
    max_s: int = DEFAULT_MAX_S

    @classmethod
    def of_document(cls, repo: Mapping[str, Any]) -> BreakerTarget:
        """From a ``repositories`` document (proxy settings ``auto_block``, ``offline``)."""
        proxy: Mapping[str, Any] = repo.get('proxy') or {}
        cfg: Mapping[str, Any] = proxy.get('auto_block') or {}
        return cls(
            repo_id=str(repo['_id']),
            name=str(repo.get('name') or ''),
            format=str(repo.get('format') or ''),
            enabled=bool(proxy) and bool(cfg.get('enabled', True)) and not proxy.get('offline'),
            failures=int(cfg.get('failures', DEFAULT_FAILURES)),
            initial_s=int(cfg.get('initial_s', DEFAULT_INITIAL_S)),
            max_s=int(cfg.get('max_s', DEFAULT_MAX_S)),
        )


class AutoBlockBreaker:
    def __init__(
        self, valkey: Redis, *, clock: Callable[[], float] = time.time, probe_lease_s: float = PROBE_LEASE_S
    ) -> None:
        self.valkey = valkey
        self.clock = clock
        self.probe_lease_s = probe_lease_s
        self._admit = valkey.register_script(_ADMIT)
        self._record = valkey.register_script(_RECORD)
        # repo id -> (blocked until, re-check Valkey after), as last seen by this process: blocked requests skip
        # Valkey for at most LOCAL_RECHECK_S, so a block lifted elsewhere (a probe, a saved repository) ends here soon.
        self._blocked: dict[str, tuple[float, float]] = {}

    async def check(self, target: BreakerTarget) -> Verdict:
        """Before an upstream request: OPEN or PROBE (send it), else raise ``UpstreamAutoBlocked``."""
        now = self.clock()
        known = self._blocked.get(target.repo_id)
        if known is not None and now < known[1]:
            raise UpstreamAutoBlocked(known[0], now=now)
        try:
            verdict, until_ms = await self._admit(
                keys=[event_keys.upstream_health_key(target.repo_id)],
                args=[int(now * 1000), int(self.probe_lease_s * 1000)],
            )
        except Exception as exc:  # Valkey down: no auto-block
            log.warning('auto-block check failed', extra={'repo': target.name, 'error': type(exc).__name__})
            return Verdict.OPEN
        if Verdict(int(verdict)) is Verdict.OPEN:
            self._blocked.pop(target.repo_id, None)
            return Verdict.OPEN
        until = int(until_ms) / 1000
        if Verdict(int(verdict)) is Verdict.PROBE:
            log.info('probing auto-blocked upstream', extra={'repo': target.name})
            return Verdict.PROBE
        self._remember(target.repo_id, until, now)
        raise UpstreamAutoBlocked(max(until, now + 1), now=now)

    def _remember(self, repo_id: str, until: float, now: float) -> None:
        if until > now:
            self._blocked[repo_id] = (until, min(until, now + LOCAL_RECHECK_S))

    def remembers(self, repo_id: str) -> bool:
        """Whether this process has seen the repository blocked recently."""
        return repo_id in self._blocked

    async def record(self, target: BreakerTarget, *, ok: bool, reason: str = '') -> None:
        """After an upstream answer (``ok``: not a blocking failure)."""
        now = self.clock()
        try:
            transition, until_ms, level = await self._record(
                keys=[event_keys.upstream_health_key(target.repo_id)],
                args=[
                    '1' if ok else '0',
                    int(now * 1000),
                    target.failures,
                    target.initial_s * 1000,
                    max(target.max_s, target.initial_s) * 1000,
                    reason[:300],
                ],
            )
        except Exception as exc:  # Valkey down: no auto-block
            log.warning('auto-block update failed', extra={'repo': target.name, 'error': type(exc).__name__})
            return
        change = Transition(int(transition))
        until = int(until_ms) / 1000
        if change in (Transition.BLOCKED, Transition.REBLOCKED, Transition.STILL_BLOCKED):
            self._remember(target.repo_id, until, now)
        elif change is Transition.RESUMED or ok:
            self._blocked.pop(target.repo_id, None)
        if change in (Transition.BLOCKED, Transition.REBLOCKED, Transition.RESUMED):
            self.on_transition(target, change, until, int(level), reason, now)

    def on_transition(
        self, target: BreakerTarget, change: Transition, until: float, level: int, reason: str, now: float
    ) -> None:
        """A block started, was extended or lifted (the server announces it; the default only logs)."""
        if change is Transition.RESUMED:
            log.info('upstream auto-block lifted', extra={'repo': target.name})
        else:
            log.warning('upstream auto-blocked', extra={'repo': target.name, 'error': reason, 'level': level})

    async def reset(self, repo_id: str) -> None:
        """Forget a repository's auto-block state (its configuration was saved)."""
        self._blocked.pop(repo_id, None)
        try:
            await self.valkey.hdel(event_keys.upstream_health_key(repo_id), *BLOCK_FIELDS)
        except Exception as exc:  # live state only
            log.warning('could not clear auto-block state', extra={'error': type(exc).__name__})
