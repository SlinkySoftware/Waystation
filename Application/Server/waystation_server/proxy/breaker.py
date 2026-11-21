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

The shared core (Lua scripts, ``AutoBlockBreaker``) lives in ``waystation_common.breaker`` so the worker's upstream
requests feed the same state; this module re-exports it and adds the announcements and the prober.

Valkey unavailable = no auto-block (every request goes upstream), never an error. A process remembers a block it has
seen for up to 5 s, so most blocked requests cost no Valkey round trip. Saving a repository clears its block.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from redis.asyncio import Redis

from waystation_common.breaker import (
    BLOCK_FIELDS,
    LOCAL_RECHECK_S,
    PROBE_LEASE_S,
    AutoBlockBreaker,
    BlockState,
    BreakerTarget,
    Transition,
    UpstreamAutoBlocked,
    Verdict,
    blocking_status,
    ms_to_s,
)
from waystation_common.events import ActivityEvent, EventEmitter, EventType
from waystation_common.events import keys as event_keys
from waystation_server.metrics import Metrics
from waystation_server.repos.model import RepoConfig, RepoType

__all__ = [
    'BLOCK_FIELDS',
    'LOCAL_RECHECK_S',
    'PROBER_INTERVAL_S',
    'PROBE_LEASE_S',
    'BlockState',
    'BreakerTarget',
    'Transition',
    'UpstreamAutoBlocked',
    'UpstreamBreaker',
    'Verdict',
    'blocking_status',
]

log = logging.getLogger('waystation.proxy.autoblock')

PROBER_INTERVAL_S = 5.0


Probe = Callable[[RepoConfig], Awaitable[tuple[bool, str | None]]]


class UpstreamBreaker(AutoBlockBreaker):
    def __init__(
        self,
        valkey: Redis,
        emitter: EventEmitter,
        metrics: Metrics,
        *,
        clock: Callable[[], float] = time.time,
        probe_lease_s: float = PROBE_LEASE_S,
    ) -> None:
        super().__init__(valkey, clock=clock, probe_lease_s=probe_lease_s)
        self.emitter = emitter
        self.metrics = metrics
        self._prober: asyncio.Task[None] | None = None

    @staticmethod
    def _target(repo: RepoConfig) -> BreakerTarget:
        assert repo.proxy is not None
        cfg = repo.proxy.auto_block
        return BreakerTarget(
            repo_id=repo.id,
            name=repo.name,
            format=repo.format,
            enabled=cfg.enabled and not repo.proxy.offline,
            failures=cfg.failures,
            initial_s=cfg.initial_s,
            max_s=cfg.max_s,
        )

    # ------------------------------------------------------------------ request path
    async def admit(self, repo: RepoConfig) -> Verdict:
        """Before an upstream request: OPEN or PROBE (send it), else raise ``UpstreamAutoBlocked``."""
        if not self._enabled(repo):
            return Verdict.OPEN
        return await self.check(self._target(repo))

    async def success(self, repo: RepoConfig) -> None:
        if repo.proxy is not None and (self._enabled(repo) or self.remembers(repo.id)):
            await self.record(self._target(repo), ok=True)

    async def failure(self, repo: RepoConfig, reason: str) -> None:
        if self._enabled(repo):
            await self.record(self._target(repo), ok=False, reason=reason)

    @staticmethod
    def _enabled(repo: RepoConfig) -> bool:
        return repo.proxy is not None and repo.proxy.auto_block.enabled and not repo.proxy.offline

    def on_transition(
        self, target: BreakerTarget, change: Transition, until: float, level: int, reason: str, now: float
    ) -> None:
        event = {Transition.BLOCKED: 'blocked', Transition.REBLOCKED: 'reblocked', Transition.RESUMED: 'resumed'}[
            change
        ]
        self.metrics.upstream_autoblock(repo=target.name, event=event)
        detail: dict[str, object] = {'state': 'ok' if change is Transition.RESUMED else 'blocked', 'event': event}
        if change is Transition.RESUMED:
            summary = f'Upstream of {target.name} answers again; auto-block lifted'
            log.info('upstream auto-block lifted', extra={'repo': target.name})
        else:
            stamp = datetime.fromtimestamp(until, UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')
            period = round(until - now)
            if change is Transition.BLOCKED:
                summary = (
                    f'Upstream of {target.name} auto-blocked for {period} s after '
                    f'{target.failures} consecutive failures ({reason})'
                )
            else:
                summary = f'Upstream of {target.name} still failing ({reason}); blocked for {period} s'
            detail.update(blocked_until=stamp, level=level, reason=reason)
            log.warning('upstream auto-blocked', extra={'repo': target.name, 'error': reason, 'until': stamp})
        detail['summary'] = summary
        self.emitter.activity(
            ActivityEvent(
                type=EventType.UPSTREAM_STATE,
                ts=datetime.fromtimestamp(now, UTC),
                repo_id=target.repo_id,
                repo=target.name,
                format=target.format,
                detail=detail,
            )
        )

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
            due = ms_to_s(until)
            if due is None or due > now or ((p := ms_to_s(probing)) is not None and p > now):
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
