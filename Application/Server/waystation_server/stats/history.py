"""Storage over time from the hourly ``storage_snapshots`` (written by the worker's stats rollup, T-F09): the storage
growth chart (``GET /stats/storage/history``) and the storage sparklines of the dashboard KPIs.

Per bucket, the last snapshot in it: ``used`` = blob bytes on disk (instance-wide), ``stored`` = the visible
repositories' stored bytes (before de-duplication), ``artifacts`` = their artifacts. A bucket without an instance-wide
snapshot is null (the worker was not running, or the range starts before the first snapshot).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from waystation_common.db.client import Database
from waystation_server.db import stats as stats_db
from waystation_server.stats.series import Window, dt

RANGES: dict[str, tuple[int, int]] = {
    '7d': (7 * 86400, 3600),
    '30d': (30 * 86400, 21600),
    '90d': (90 * 86400, 86400),
    '1y': (365 * 86400, 86400),
}


def window(range_name: str, *, now: float | None = None) -> Window:
    seconds, step = RANGES[range_name]
    now = time.time() if now is None else now
    end = (int(now) // step) * step + step
    return Window(range_name, step, end - seconds, end)


@dataclass(frozen=True)
class History:
    win: Window
    used: list[int | None]
    stored: list[int | None]
    artifacts: list[int | None]


async def series(db: Database, win: Window, repo_ids: Sequence[str]) -> History:
    buckets = await stats_db.snapshot_buckets(
        db, since=dt(win.start), until=dt(win.end), step_s=win.step_s, repo_ids=repo_ids
    )
    used: list[int | None] = []
    stored: list[int | None] = []
    artifacts: list[int | None] = []
    for start in win.buckets:
        entry = buckets.get(start * 1000) or {}
        instance = entry.get('')
        if instance is None:
            used.append(None)
            stored.append(None)
            artifacts.append(None)
            continue
        used.append(int(instance.get('blob_bytes') or 0))
        stored.append(sum(int(entry[r].get('bytes') or 0) for r in repo_ids if r in entry))
        artifacts.append(sum(int(entry[r].get('artifacts') or 0) for r in repo_ids if r in entry))
    return History(win, used, stored, artifacts)


def spark(values: Sequence[int | None], current: float) -> list[float]:
    """A sparkline: snapshots carried forward over the buckets between them, the buckets before the first one at the
    first value (the current one when there is none) and the last bucket at the current value."""
    first = next((v for v in values if v is not None), None)
    last = float(first) if first is not None else current
    out: list[float] = []
    for value in values:
        if value is not None:
            last = float(value)
        out.append(last)
    if out:
        out[-1] = current
    return out
