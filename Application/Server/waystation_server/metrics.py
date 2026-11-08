"""Prometheus metrics (SPECIFICATION §10), served at ``/metrics`` (restricted, §4.4).

One registry per app instance. Values are per server process: with ``WAYSTATION_SERVER_WORKERS > 1`` each scrape
reaches one process (multi-process aggregation is not done yet); cluster-wide figures come from the stats API.
"""

from __future__ import annotations

from collections.abc import Callable

from prometheus_client import CONTENT_TYPE_LATEST as CONTENT_TYPE
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

__all__ = ['CONTENT_TYPE', 'Metrics']

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


class Metrics:
    def __init__(self, inflight: Callable[[], float] | None = None) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.requests = Counter(
            'waystation_requests',
            'Format requests by repository, format, status and cache state.',
            ['repo', 'format', 'status', 'cache'],
            registry=self.registry,
        )
        self.bytes = Counter(
            'waystation_bytes',
            'Bytes by repository and direction (out_cache, out_upstream, in_upstream).',
            ['repo', 'direction'],
            registry=self.registry,
        )
        self.upstream_latency = Histogram(
            'waystation_upstream_latency_seconds',
            'Upstream time to response headers.',
            ['repo'],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.inflight = Gauge('waystation_inflight_transfers', 'Transfers in flight.', registry=self.registry)
        if inflight is not None:
            self.inflight.set_function(inflight)
        self.upstream_state = Gauge(
            'waystation_upstream_state',
            'Upstream state per proxy repository (0 ok, 1 degraded, 2 blocked, 3 offline).',
            ['repo'],
            registry=self.registry,
        )

    def request(self, *, repo: str, format: str, status: int, cache: str) -> None:
        self.requests.labels(repo, format, str(status), cache).inc()

    def add_bytes(self, *, repo: str, direction: str, count: int) -> None:
        if count > 0:
            self.bytes.labels(repo, direction).inc(count)

    def render(self) -> bytes:
        return generate_latest(self.registry)
