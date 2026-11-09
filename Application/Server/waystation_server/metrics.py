"""Prometheus metrics (SPECIFICATION §10), served at ``/metrics`` (restricted, §4.4).

Two kinds of metric:

- **Process metrics** (this class): counters, histograms and gauges updated where things happen (O(1) per update).
- **Cluster figures** (``stats.prometheus``): upstream state and rate limits, job queues, workers, blob store; read
  from Valkey/MongoDB/the filesystem when ``/metrics`` is scraped (short cache) and passed to ``render(extra)``.
  They describe the whole installation, so every process (and every replica) reports the same values.

**Several server processes** (``WAYSTATION_SERVER_WORKERS > 1``): ``python -m waystation_server`` points
``PROMETHEUS_MULTIPROC_DIR`` at a fresh directory on the container's tmpfs before uvicorn starts its processes, and
``prometheus_client`` keeps every process's values in memory-mapped files there. Whichever process answers a scrape
merges all of them (``MultiProcessCollector``): counters and histograms are summed (including processes that have
exited, so they never go backwards), the per-process gauges (``inflight``, ``sse_clients``) are summed over live
processes. Each process refreshes its gauges every ``SAMPLE_INTERVAL_S`` in that mode (callbacks cannot run in
another process). With one process the gauges are read on scrape.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable, Iterable, Iterator, Sequence

from prometheus_client import CONTENT_TYPE_LATEST as CONTENT_TYPE
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector

__all__ = ['CONTENT_TYPE', 'MULTIPROC_ENV', 'Metrics', 'status_class']

MULTIPROC_ENV = 'PROMETHEUS_MULTIPROC_DIR'
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
HTTP_METHODS = frozenset({'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'})
SAMPLE_INTERVAL_S = 2.0


def status_class(status: int) -> str:
    return f'{status // 100}xx' if 100 <= status <= 599 else 'other'


class _Combined(Collector):
    def __init__(self, base: CollectorRegistry, extra: Iterable[Metric]) -> None:
        self._base, self._extra = base, extra

    def collect(self) -> Iterator[Metric]:
        yield from self._base.collect()
        yield from self._extra


class Metrics:
    def __init__(self, inflight: Callable[[], float] | None = None) -> None:
        self.multiprocess = bool(os.environ.get(MULTIPROC_ENV))
        self.registry = CollectorRegistry(auto_describe=True)
        self._sampled: list[tuple[Gauge, Callable[[], float]]] = []
        self._sampler: asyncio.Task[None] | None = None
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
        self.inflight = self._gauge('waystation_inflight_transfers', 'Transfers in flight.')
        if inflight is not None:
            self.track(self.inflight, inflight)
        self.sse_clients = self._gauge('waystation_sse_clients', 'Connected event-stream (SSE) clients.')
        self.coalescing = Counter(
            'waystation_coalesce',
            'Coalescing (ARCHITECTURE §6.3) by repository and event: owner (owns a fetch), solo (owns it without '
            'Valkey), tail (followed a fetch by streaming its file), wait (followed it to the commit), takeover (its '
            'owner went away).',
            ['repo', 'event'],
            registry=self.registry,
        )
        self.http_latency = Histogram(
            'waystation_http_request_duration_seconds',
            'Server time per HTTP request by route template, method and status class (the event stream excluded).',
            ['route', 'method', 'status'],
            buckets=HTTP_BUCKETS,
            registry=self.registry,
        )
        self.autoblock = Counter(
            'waystation_upstream_autoblock',
            'Auto-block transitions by repository and event: blocked (after N consecutive failures), reblocked (a '
            'probe failed; the period doubled), resumed (the upstream answered again).',
            ['repo', 'event'],
            registry=self.registry,
        )
        self.storage_refusals = Counter(
            'waystation_storage_refusals',
            'Blob store writes refused by the free-space guard (WAYSTATION_MIN_FREE_GB).',
            registry=self.registry,
        )

    def _gauge(self, name: str, documentation: str) -> Gauge:
        return Gauge(name, documentation, registry=self.registry, multiprocess_mode='livesum')

    # ------------------------------------------------------------------ updates (O(1))
    def coalesce(self, *, repo: str, event: str) -> None:
        self.coalescing.labels(repo, event).inc()

    def upstream_autoblock(self, *, repo: str, event: str) -> None:
        self.autoblock.labels(repo, event).inc()

    def request(self, *, repo: str, format: str, status: int, cache: str) -> None:
        self.requests.labels(repo, format, str(status), cache).inc()

    def add_bytes(self, *, repo: str, direction: str, count: int) -> None:
        if count > 0:
            self.bytes.labels(repo, direction).inc(count)

    def http_request(self, *, route: str, method: str, status: int, seconds: float) -> None:
        method = method if method in HTTP_METHODS else 'OTHER'
        self.http_latency.labels(route, method, status_class(status)).observe(seconds)

    def storage_refused(self) -> None:
        self.storage_refusals.inc()

    # ------------------------------------------------------------------ per-process gauges
    def track(self, gauge: Gauge, read: Callable[[], float]) -> None:
        """Report ``read()`` as ``gauge``: on scrape with one process, every ``SAMPLE_INTERVAL_S`` with several."""
        if self.multiprocess:
            self._sampled.append((gauge, read))
            gauge.set(read())
        else:
            gauge.set_function(read)

    def sample(self) -> None:
        for gauge, read in self._sampled:
            gauge.set(read())

    def start(self) -> None:
        if self._sampled and self._sampler is None:
            self._sampler = asyncio.create_task(self._sample_loop(), name='metrics-sampler')

    async def stop(self) -> None:
        if self._sampler is not None:
            self._sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sampler
            self._sampler = None
        if self.multiprocess:
            from prometheus_client import multiprocess

            # Drops this process's live gauges.
            multiprocess.mark_process_dead(os.getpid())  # type: ignore[no-untyped-call]

    async def _sample_loop(self) -> None:
        while True:
            self.sample()
            await asyncio.sleep(SAMPLE_INTERVAL_S)

    # ------------------------------------------------------------------ output
    def render(self, extra: Sequence[Metric] = ()) -> bytes:
        base = self.registry
        if self.multiprocess:
            from prometheus_client import multiprocess

            self.sample()
            base = CollectorRegistry()
            multiprocess.MultiProcessCollector(base)  # type: ignore[no-untyped-call]
        return generate_latest(_Combined(base, extra))
