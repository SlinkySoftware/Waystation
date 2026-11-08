"""Timing knobs of the job system. Production uses the defaults; tests shrink them (short leases, fast polls)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JobConfig:
    visibility_s: float = 60.0  # a running job not heartbeated for this long is reclaimed by another worker
    block_ms: int = 1000  # XREADGROUP block time (also the reclaim/shutdown check granularity)
    tick_s: float = 1.0  # scheduler/dispatcher tick
    lease_ttl_s: float = 15.0  # scheduler leader lease
    retry_base_s: float = 5.0  # retry backoff: base * 2^(attempt-1), capped
    retry_max_s: float = 300.0
    shutdown_grace_s: float = 8.0  # on SIGTERM running jobs get this long (within docker's 10 s), then are abandoned
    outbox_redispatch_s: float = 3600.0  # in-flight outbox entries older than this are dispatched again
    presence_s: float = 10.0  # ``workers`` heartbeat
    consumer_prune_s: float = 3600.0  # consumers idle this long with nothing pending are deleted

    @property
    def heartbeat_s(self) -> float:
        return self.visibility_s / 3

    @property
    def reclaim_s(self) -> float:
        return self.visibility_s / 2

    def backoff_s(self, attempt: int) -> float:
        return float(min(self.retry_base_s * 2 ** max(attempt - 1, 0), self.retry_max_s))
