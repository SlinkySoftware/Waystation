"""Valkey keys, counter fields and latency buckets for live stats (DATA_MODEL §2)."""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping
from typing import Final

TRANSFERS: Final = 'transfers'  # hash: transfer id -> Transfer JSON
LIVE: Final = 'live'  # pub/sub channel: LiveMessage JSON
JOBS_LIVE: Final = 'live:jobs'  # pub/sub channel: JobLiveMessage JSON (job progress and log lines; no history)
ACTIVITY: Final = 'activity'  # stream: fields {type, data(ActivityEvent JSON)}
ACTIVITY_MAXLEN: Final = 200_000
STATS_PENDING: Final = 'stats:pending'  # set of '<minute>:<repo_id>' with counters not yet rolled up
COUNTER_TTL_S: Final = 3 * 3600  # safety net; rollups normally delete the keys within a minute
SEEN_ASSETS: Final = 'seen:assets'  # hash: asset id -> epoch seconds of the last download (flushed by the rollup)
DL_ASSETS: Final = 'dl:assets'  # hash: asset id -> downloads since the last flush
# Running totals of cleanup and blob GC (hash; fields in MAINTENANCE_FIELDS), exported on /metrics.
MAINTENANCE_TOTALS: Final = 'maintenance:totals'
MAINTENANCE_FIELDS: Final = (
    'cleanup_removed',
    'cleanup_bytes',
    'gc_freed_blobs',
    'gc_freed_bytes',
    'gc_uploads_removed',
    'gc_runs',
)


def minute_of(ts: float) -> int:
    """Epoch minute (``int(ts // 60)``): the ``<minute>`` in counter keys."""
    return int(ts // 60)


def cnt_key(minute: int, repo_id: str) -> str:
    return f'cnt:{minute}:{repo_id}'


def lat_key(minute: int, repo_id: str) -> str:
    return f'lat:{minute}:{repo_id}'


def upstream_health_key(repo_id: str) -> str:
    """Hash: ``last_ok_at``, ``last_error``, ``last_error_at``, ``consecutive_failures``, ``last_latency_ms``."""
    return f'upstream:{repo_id}:health'


def upstream_ratelimit_key(repo_id: str) -> str:
    """Hash: ``limit``, ``remaining``, ``window_s``, ``reset_at``, ``source``, ``seen_at`` (DATA_MODEL §2)."""
    return f'upstream:{repo_id}:ratelimit'


def pending_member(minute: int, repo_id: str) -> str:
    return f'{minute}:{repo_id}'


def parse_pending(member: str) -> tuple[int, str]:
    minute, _, repo_id = member.partition(':')
    return int(minute), repo_id


# Counter fields of `cnt:<minute>:<repo_id>` (and of `stats_minute` documents). The hash also holds `format`.
COUNTERS: Final = (
    'requests',
    'hits',
    'misses',
    'coalesced',
    'stale',
    'errors',
    'bytes_out_cache',
    'bytes_out_upstream',
    'bytes_in_upstream',
    'bytes_in_upload',
    'upstream_requests',  # requests sent to the upstream (fetches, revalidations, HEAD pass-through)
    'upstream_errors',  # of which failed (connect/timeout/5xx/validation)
)

# Fixed log-scale latency buckets (upper bounds, ms). Field name 'le_<bound>', last bucket 'le_inf'.
LATENCY_BOUNDS_MS: Final = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000)
LATENCY_FIELDS: Final = (*(f'le_{b}' for b in LATENCY_BOUNDS_MS), 'le_inf')


def latency_bucket(ms: float) -> str:
    return LATENCY_FIELDS[bisect.bisect_left(LATENCY_BOUNDS_MS, ms)]


def percentile(hist: Mapping[str, int], q: float) -> float | None:
    """Upper bound (ms) of the bucket holding quantile ``q`` (0..1); ``inf`` for the overflow bucket; None if empty.

    Histograms from any number of minutes can be summed first: percentiles are always computed at query time.
    """
    total = sum(hist.get(f, 0) for f in LATENCY_FIELDS)
    if total == 0:
        return None
    rank = max(1, math.ceil(q * total))
    seen = 0
    for bound, field in zip((*LATENCY_BOUNDS_MS, math.inf), LATENCY_FIELDS, strict=True):
        seen += hist.get(field, 0)
        if seen >= rank:
            return float(bound)
    raise AssertionError('unreachable')  # pragma: no cover
