"""Cluster figures for ``/metrics`` (SPECIFICATION §10), read when it is scraped and cached for ``CACHE_TTL_S``.

Nothing here runs on a request path: one scrape costs a few pipelined Valkey commands, two indexed MongoDB counts,
the (already cached) storage aggregation and one ``statvfs``. Each source is bounded by ``SOURCE_TIMEOUT_S``; a
source that fails or times out is reported by ``waystation_metrics_source_up{source}`` = 0 and its metrics are
left out of that scrape (never a failed scrape).

Values describe the whole installation (every server process and replica reports the same), so they need no
multi-process aggregation:

- upstream state per proxy repository (0 ok, 1 degraded, 2 blocked, 3 offline; the same rule as the UI) and its
  consecutive failures; the latest upstream rate limit (Docker Hub ``ratelimit-*``) per repository;
- per job queue: waiting, running, delayed jobs and the age of the oldest waiting job; dead letters;
- live workers (heartbeat within ``WORKER_ALIVE_S``) and scheduler leaders;
- blob store: bytes held by blobs, blob count (MongoDB), free and total bytes of the data filesystem, the
  low-disk guard's effective floor and whether it is tripped (``waystation_storage_low``);
- per repository with a quota: the quota, its mode and the stored bytes counted against it (``repo_usage``);
- cleanup and blob GC running totals (Valkey ``maintenance:totals``, written by the workers) as counters.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.metrics_core import Metric

from waystation_common.db import jobs as jobs_db
from waystation_common.events import keys as event_keys
from waystation_common.jobs.model import QUEUES
from waystation_common.jobs.queue import JobQueue
from waystation_server.repos.model import RepoType
from waystation_server.services import Services
from waystation_server.stats import repos as repo_stats

log = logging.getLogger('waystation.metrics')

CACHE_TTL_S = 10.0
SOURCE_TIMEOUT_S = 2.0
WORKER_ALIVE_S = 60.0  # workers heartbeat every 10 s
STATE_CODES = {'ok': 0, 'degraded': 1, 'blocked': 2, 'offline': 3}
MAINTENANCE_COUNTERS = {  # field of `maintenance:totals` -> (counter name without _total, help)
    'cleanup_removed': (
        'waystation_cleanup_removed_items',
        'Items removed by cleanup policies (assets, tags, manifests).',
    ),
    'cleanup_bytes': ('waystation_cleanup_removed_bytes', 'Bytes of the assets removed by cleanup policies.'),
    'gc_freed_blobs': ('waystation_gc_freed_blobs', 'Blobs deleted by blob GC.'),
    'gc_freed_bytes': ('waystation_gc_freed_bytes', 'Bytes freed by blob GC.'),
    'gc_uploads_removed': ('waystation_gc_orphan_uploads_removed', 'Orphan upload files removed by blob GC.'),
    'gc_runs': ('waystation_gc_runs', 'Blob GC runs completed.'),
}

Families = list[Metric]


def _gauge(name: str, doc: str, labels: list[str] | None = None) -> GaugeMetricFamily:
    return GaugeMetricFamily(name, doc, labels=labels)


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _when(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


class ClusterMetrics:
    def __init__(self, services: Services, *, clock: Callable[[], float] = time.time) -> None:
        self._svc = services
        self._clock = clock
        self._cached: Families = []
        self._at = -CACHE_TTL_S
        self._lock = asyncio.Lock()

    async def families(self) -> Families:
        if time.monotonic() - self._at < CACHE_TTL_S:
            return self._cached
        async with self._lock:
            if time.monotonic() - self._at >= CACHE_TTL_S:
                self._cached = await self._collect()
                self._at = time.monotonic()
        return self._cached

    async def _collect(self) -> Families:
        sources: list[tuple[str, Callable[[], Awaitable[Families]]]] = [
            ('valkey', self._upstreams),
            ('valkey', self._jobs),
            ('valkey', self._maintenance),
            ('mongodb', self._workers),
            ('mongodb', self._storage),
            ('mongodb', self._quotas),
            ('filesystem', self._disk),
        ]
        results = await asyncio.gather(*(self._bounded(name, fn) for name, fn in sources))
        out: Families = []
        up: dict[str, int] = {}
        for (name, _), result in zip(sources, results, strict=True):
            up[name] = min(up.get(name, 1), int(result is not None))
            out.extend(result or [])
        family = _gauge('waystation_metrics_source_up', 'Whether a source of the cluster figures answered.', ['source'])
        for name, value in up.items():
            family.add_metric([name], value)
        out.append(family)
        return out

    @staticmethod
    async def _bounded(name: str, fn: Callable[[], Awaitable[Families]]) -> Families | None:
        try:
            async with asyncio.timeout(SOURCE_TIMEOUT_S):
                return await fn()
        except Exception as exc:
            log.warning('metrics source failed', extra={'source': name, 'error': type(exc).__name__})
            return None

    # ------------------------------------------------------------------ sources
    async def _upstreams(self) -> Families:
        proxies = [r for r in await self._svc.catalog.all() if r.type is RepoType.PROXY]
        results: list[dict[str, str]] = []
        if proxies:
            async with self._svc.valkey.pipeline(transaction=False) as pipe:
                for repo in proxies:
                    pipe.hgetall(event_keys.upstream_health_key(repo.id))
                    pipe.hgetall(event_keys.upstream_ratelimit_key(repo.id))
                results = await pipe.execute()
        state = _gauge(
            'waystation_upstream_state',
            'Upstream state per proxy repository (0 ok, 1 degraded, 2 blocked, 3 offline).',
            ['repo'],
        )
        failures = _gauge(
            'waystation_upstream_consecutive_failures', 'Upstream failures since its last success.', ['repo']
        )
        blocked = _gauge(
            'waystation_upstream_blocked_until_timestamp_seconds',
            'When the auto-block of the upstream ends (Unix time; only while it is blocked).',
            ['repo'],
        )
        limit = _gauge('waystation_upstream_ratelimit_limit', 'Latest rate limit reported by the upstream.', ['repo'])
        remaining = _gauge(
            'waystation_upstream_ratelimit_remaining', 'Latest remaining requests reported by the upstream.', ['repo']
        )
        window = _gauge('waystation_upstream_ratelimit_window_seconds', 'Window of the upstream rate limit.', ['repo'])
        seen = _gauge(
            'waystation_upstream_ratelimit_updated_timestamp_seconds',
            'When the rate-limit figures were last reported (Unix time).',
            ['repo'],
        )
        now = datetime.fromtimestamp(self._clock(), UTC)
        for i, repo in enumerate(proxies):
            health = repo_stats.parse_health(results[2 * i], results[2 * i + 1])
            state.add_metric([repo.name], STATE_CODES[repo_stats.status_of(repo, health, now=now)])
            failures.add_metric([repo.name], health.consecutive_failures)
            if health.block.blocked_until is not None and repo.proxy is not None and repo.proxy.auto_block.enabled:
                blocked.add_metric([repo.name], health.block.blocked_until.timestamp())
            rate = health.rate_limit
            lim, rem = _int(rate.get('limit')), _int(rate.get('remaining'))
            if lim is None or rem is None:
                continue
            limit.add_metric([repo.name], lim)
            remaining.add_metric([repo.name], rem)
            if (win := _int(rate.get('window_s'))) is not None:
                window.add_metric([repo.name], win)
            if (at := _when(rate.get('seen_at'))) is not None:
                seen.add_metric([repo.name], at.timestamp())
        return [state, failures, blocked, limit, remaining, window, seen]

    async def _jobs(self) -> Families:
        queue = JobQueue(self._svc.valkey)
        stats = await queue.stats(QUEUES, now=self._clock())
        dead = await queue.dead_count()
        waiting = _gauge('waystation_jobs_waiting', 'Jobs queued and not yet picked up, per queue.', ['queue'])
        running = _gauge(
            'waystation_jobs_running', 'Jobs delivered to a worker and not finished, per queue.', ['queue']
        )
        delayed = _gauge('waystation_jobs_delayed', 'Jobs waiting for a retry backoff or their start time.', ['queue'])
        oldest = _gauge(
            'waystation_jobs_oldest_waiting_seconds', 'Age of the oldest waiting job (0 when none).', ['queue']
        )
        for name, figures in stats.items():
            waiting.add_metric([name], figures.waiting)
            running.add_metric([name], figures.running)
            delayed.add_metric([name], figures.delayed)
            oldest.add_metric([name], figures.oldest_waiting_age_s)
        dead_family = _gauge('waystation_jobs_dead_letters', 'Entries in the dead-letter stream (capped near 10000).')
        dead_family.add_metric([], dead)
        return [waiting, running, delayed, oldest, dead_family]

    async def _maintenance(self) -> Families:
        raw = await self._svc.valkey.hgetall(event_keys.MAINTENANCE_TOTALS)
        values: dict[str, int] = {}
        for key, value in raw.items():
            name = key.decode() if isinstance(key, bytes) else str(key)
            text = value.decode() if isinstance(value, bytes) else str(value)
            if name in MAINTENANCE_COUNTERS and (number := _int(text)) is not None:
                values[name] = number
        return [
            CounterMetricFamily(metric, doc, value=values.get(field, 0))
            for field, (metric, doc) in MAINTENANCE_COUNTERS.items()
        ]

    async def _workers(self) -> Families:
        since = datetime.fromtimestamp(self._clock(), UTC) - timedelta(seconds=WORKER_ALIVE_S)
        live, leaders = await jobs_db.worker_presence(self._svc.db, alive_since=since)
        workers = _gauge('waystation_workers', f'Workers with a heartbeat in the last {WORKER_ALIVE_S:.0f} s.')
        workers.add_metric([], live)
        leader = _gauge('waystation_scheduler_leaders', 'Live workers holding the scheduler lease (1 expected).')
        leader.add_metric([], leaders)
        return [workers, leader]

    async def _storage(self) -> Families:
        current = await self._svc.storage.get()
        used = _gauge('waystation_blobstore_used_bytes', 'Bytes held by blobs (each stored once).')
        used.add_metric([], current.used_bytes)
        blobs = _gauge('waystation_blobstore_blobs', 'Blobs in the blob store.')
        blobs.add_metric([], current.blob_count)
        return [used, blobs]

    async def _disk(self) -> Families:
        st = await asyncio.to_thread(os.statvfs, self._svc.settings.data_dir)
        free_bytes, total_bytes = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
        free = _gauge('waystation_blobstore_free_bytes', 'Free bytes on the data directory filesystem.')
        free.add_metric([], free_bytes)
        size = _gauge('waystation_blobstore_size_bytes', 'Size of the data directory filesystem.')
        size.add_metric([], total_bytes)
        floor_bytes = self._svc.guard.current.floor(total_bytes)
        floor = _gauge(
            'waystation_blobstore_min_free_bytes',
            'Low-disk guard: new cache fills and uploads are refused below this many free bytes (the larger of the '
            'minimum free bytes and percent settings).',
        )
        floor.add_metric([], floor_bytes)
        low = _gauge('waystation_storage_low', '1 while free space is below the low-disk guard (writes refused).')
        low.add_metric([], int(free_bytes < floor_bytes))
        return [free, size, floor, low]

    async def _quotas(self) -> Families:
        repos = [r for r in await self._svc.catalog.all() if r.storage.quota_bytes is not None]
        statuses = await self._svc.quotas.statuses(repos)
        quota = _gauge(
            'waystation_repository_quota_bytes', 'Repository quota, by repository and mode.', ['repo', 'mode']
        )
        used = _gauge(
            'waystation_repository_stored_bytes',
            'Bytes stored by a repository with a quota (sum of its assets; shared blobs count in each repository).',
            ['repo'],
        )
        ratio = _gauge('waystation_repository_quota_used_ratio', 'Stored bytes / quota (1 = quota reached).', ['repo'])
        for repo in repos:
            status = statuses.get(repo.id)
            if status is None:
                continue
            quota.add_metric([repo.name, status.mode.value], status.quota_bytes)
            used.add_metric([repo.name], status.used_bytes)
            ratio.add_metric([repo.name], round(status.ratio, 4))
        return [quota, used, ratio]
