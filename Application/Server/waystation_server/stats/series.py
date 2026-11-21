"""Counter time series for the stats API (SPECIFICATION §4.7, ARCHITECTURE §8).

Two sources, merged per (minute, repository): closed minutes rolled up into ``stats_minute`` by the worker, and the
still-open (or not yet rolled up) minutes in Valkey (``cnt:<minute>:<repo_id>`` listed in ``stats:pending``). A
minute present in both (the instant between the rollup's insert and its cleanup) is taken from MongoDB only.

Ranges with buckets of an hour or more read the rollups for the periods already rolled up: ``stats_day`` for
whole days (1-day buckets), ``stats_hour`` for whole hours, then minutes (and the live counters) for the rest; the
boundaries are the rollup's progress (``stats:rollup:hour`` / ``stats:rollup:day``, else the newest rolled-up
document), so no period is counted twice.

Ranges and bucket widths: 15m and 1h → 1 min, 24h → 10 min, 7d → 1 h, 30d → 6 h, 90d and 1y → 1 day. 15m stays at
one-minute buckets: the counters are per minute (finer buckets would multiply the Valkey keys and the minute
documents by six), and the dashboard's live edge comes from the 1 Hz ``stats.tick`` event instead. Minutes are kept
7 days, hours 90 days, days 2 years (DATA_MODEL).

Every row also carries the latency histograms (``lat_hist``: request duration; ``ulat_hist``: upstream response
time) summed over its bucket; ``percentiles`` reads p50/p95 from any number of them (``keys.percentile``).
"""

from __future__ import annotations

import contextlib
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from waystation_common.db.client import Database
from waystation_common.db.collections import STATS_DAY, STATS_HOUR
from waystation_common.events import keys
from waystation_server.db import stats as stats_db

RANGES: dict[str, tuple[int, int]] = {
    '15m': (900, 60),
    '1h': (3600, 60),
    '24h': (86400, 600),
    '7d': (7 * 86400, 3600),
    '30d': (30 * 86400, 21600),
    '90d': (90 * 86400, 86400),
    '1y': (365 * 86400, 86400),
}
HOUR = 3600
DAY = 86400

Row = dict[str, Any]


@dataclass(frozen=True)
class Window:
    range: str
    step_s: int
    start: int  # epoch seconds, bucket-aligned (inclusive)
    end: int  # epoch seconds, bucket-aligned (exclusive): the current bucket is the last one

    @property
    def buckets(self) -> list[int]:
        return list(range(self.start, self.end, self.step_s))

    def index(self, bucket_s: int) -> int | None:
        if bucket_s < self.start or bucket_s >= self.end:
            return None
        return (bucket_s - self.start) // self.step_s


def window(range_name: str, *, now: float | None = None) -> Window:
    seconds, step = RANGES[range_name]
    now = time.time() if now is None else now
    end = (int(now) // step) * step + step
    return Window(range_name, step, end - seconds, end)


def dt(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, UTC)


def _str(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


async def live_minutes(
    valkey: Redis, *, since_minute: int, repo_ids: Sequence[str] | None = None
) -> list[tuple[int, str, dict[str, str]]]:
    """(minute, repo_id, counters incl. ``format``) for every pending minute since ``since_minute``."""
    wanted = set(repo_ids) if repo_ids is not None else None
    pairs = []
    for member in await valkey.smembers(keys.STATS_PENDING):
        minute, repo_id = keys.parse_pending(_str(member))
        if minute >= since_minute and (wanted is None or repo_id in wanted):
            pairs.append((minute, repo_id))
    if not pairs:
        return []
    async with valkey.pipeline(transaction=False) as pipe:
        for minute, repo_id in pairs:
            pipe.hgetall(keys.cnt_key(minute, repo_id))
        results = await pipe.execute()
    out = []
    for (minute, repo_id), raw in zip(pairs, results, strict=True):
        if raw:
            out.append((minute, repo_id, {_str(k): _str(v) for k, v in raw.items()}))
    return out


async def _boundary(db: Database, valkey: Redis, key: str, collection: str, step: int) -> int:
    """Epoch seconds before which ``collection`` holds every period (0 when nothing is rolled up)."""
    raw = None
    with contextlib.suppress(Exception):  # Valkey down: the newest rolled-up document tells
        raw = await valkey.get(key)
    if raw is not None:
        return int(raw)
    newest = await stats_db.newest(db, collection)
    return int(newest.timestamp()) + step if newest is not None else 0


async def _live_histograms(valkey: Redis, pairs: Sequence[tuple[int, str]]) -> list[tuple[dict[str, int], ...]]:
    async with valkey.pipeline(transaction=False) as pipe:
        for minute, repo_id in pairs:
            pipe.hgetall(keys.lat_key(minute, repo_id))
            pipe.hgetall(keys.ulat_key(minute, repo_id))
        results = await pipe.execute()
    out = []
    for i in range(len(pairs)):
        out.append(
            tuple({_str(k): int(v) for k, v in (results[2 * i + j] or {}).items()} for j in (0, 1))  # lat, ulat
        )
    return out


async def collect(
    db: Database,
    valkey: Redis,
    win: Window,
    *,
    repo_ids: Sequence[str] | None = None,
    group_repo: bool = False,
    latency: bool = False,
) -> list[Row]:
    """Counter rows (``bucket`` in epoch ms, counters, ``lat_hist``/``ulat_hist``, and ``repo_id``/``format`` when
    ``group_repo``). ``latency``: also read the live minutes' histograms (closed periods always carry them)."""
    rows: list[Row] = []
    start = win.start
    sources: list[tuple[str, int]] = []
    if win.step_s >= DAY:
        sources.append((STATS_DAY, await _boundary(db, valkey, keys.ROLLUP_DAY, STATS_DAY, DAY)))
    if win.step_s >= HOUR:
        sources.append((STATS_HOUR, await _boundary(db, valkey, keys.ROLLUP_HOUR, STATS_HOUR, HOUR)))
    for collection, boundary in sources:
        end = min(boundary, win.end)
        if end > start:
            rows += await stats_db.period_buckets(
                db,
                collection,
                since=dt(start),
                until=dt(end),
                step_s=win.step_s,
                repo_ids=repo_ids,
                group_repo=group_repo,
            )
            start = end
    if start >= win.end:
        return rows
    rows += await stats_db.minute_buckets(
        db, since=dt(start), until=dt(win.end), step_s=win.step_s, repo_ids=repo_ids, group_repo=group_repo
    )
    try:
        live = await live_minutes(valkey, since_minute=start // 60, repo_ids=repo_ids)
    except Exception:  # Valkey down: closed minutes only
        live = []
    if live:
        done = await stats_db.existing_minutes(
            db, sorted({dt(m * 60) for m, _, _ in live}), sorted({r for _, r, _ in live})
        )
        live = [(m, r, c) for m, r, c in live if (dt(m * 60), r) not in done and win.start <= m * 60 < win.end]
        hists: list[tuple[dict[str, int], ...]] = []
        if latency and live:
            try:
                hists = await _live_histograms(valkey, [(m, r) for m, r, _ in live])
            except Exception:
                hists = []
        for i, (minute, repo_id, counters) in enumerate(live):
            row: Row = {field: int(counters.get(field, 0) or 0) for field in keys.COUNTERS}
            row['bucket'] = (minute * 60 // win.step_s) * win.step_s * 1000
            row['last_ts'] = dt(minute * 60) if row['requests'] else None
            row['lat_hist'], row['ulat_hist'] = hists[i] if i < len(hists) else ({}, {})
            if group_repo:
                row['repo_id'] = repo_id
                row['format'] = counters.get('format', '')
            rows.append(row)
    return rows


def histogram(rows: Iterable[Row], field: str = 'lat_hist') -> dict[str, int]:
    """The sum of the rows' histograms ``field``."""
    out: dict[str, int] = {}
    for row in rows:
        for bucket, n in (row.get(field) or {}).items():
            out[bucket] = out.get(bucket, 0) + int(n)
    return out


def percentiles(rows: Iterable[Row], field: str = 'lat_hist') -> tuple[float | None, float | None]:
    """(p50, p95) in ms of the summed histograms (upper bounds of the buckets; the overflow bucket reads as the
    largest bound); None without data."""
    hist = histogram(rows, field)

    def q(value: float) -> float | None:
        found = keys.percentile(hist, value)
        if found is None:
            return None
        return float(keys.LATENCY_BOUNDS_MS[-1]) if found == float('inf') else found

    return q(0.5), q(0.95)


def series(
    rows: Iterable[Row], win: Window, value: Callable[[Row], float], key: Callable[[Row], str] = lambda _: ''
) -> dict[str, list[float]]:
    """Sum ``value(row)`` per bucket, one list per ``key(row)``."""
    out: dict[str, list[float]] = defaultdict(lambda: [0.0] * len(win.buckets))
    for row in rows:
        idx = win.index(int(row['bucket']) // 1000)
        if idx is not None:
            out[key(row)][idx] += value(row)
    return dict(out)


def totals(rows: Iterable[Row]) -> dict[str, int]:
    out = dict.fromkeys(keys.COUNTERS, 0)
    for row in rows:
        for field in keys.COUNTERS:
            out[field] += int(row.get(field) or 0)
    return out


def hit_ratio(counts: dict[str, int] | Row) -> float | None:
    """Hits / (hits + misses), counting stale and revalidated serves as hits and coalesced as misses."""
    hits = int(counts.get('hits') or 0) + int(counts.get('stale') or 0)
    misses = int(counts.get('misses') or 0) + int(counts.get('coalesced') or 0)
    return round(hits / (hits + misses), 4) if hits + misses else None


def bytes_served(row: Row) -> float:
    return float(int(row.get('bytes_out_cache') or 0) + int(row.get('bytes_out_upstream') or 0))
