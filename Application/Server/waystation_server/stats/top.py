"""Top-N figures (SPECIFICATION §4.7 "Top N"): artifacts (components) by downloads, bytes served, size or recency,
and repositories by requests, bytes served or bytes saved over a range.

Artifacts come from the component index (``waystation_server.db.content``): all-time figures are the components'
own counters; a download window reads the daily counters (``downloads_daily``, whole UTC days overlapping the
window). Repositories come from the counter time series (``stats.series``).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_server.db import content as content_db
from waystation_server.stats import series

WINDOWS_S = {'24h': 86400, '7d': 7 * 86400, '30d': 30 * 86400}


@dataclass(frozen=True)
class ArtifactRow:
    component: dict[str, Any]
    downloads: int
    bytes_served: int


async def artifacts(
    db: Database, repo_ids: Sequence[str], *, by: str, window: str, limit: int, now: float | None = None
) -> list[ArtifactRow]:
    if by in ('downloads', 'bytes') and window in WINDOWS_S:
        start = (time.time() if now is None else now) - WINDOWS_S[window]
        since = datetime.fromtimestamp(start, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        found = await content_db.top_downloaded_since(db, repo_ids, since, by=by, limit=limit)
        return [ArtifactRow(comp, n, size) for comp, n, size in found]
    comps = await content_db.top_components(db, repo_ids, by=by, limit=limit)
    return [ArtifactRow(c, int(c.get('download_count') or 0), int(c.get('bytes_served') or 0)) for c in comps]


@dataclass(frozen=True)
class RepositoryRow:
    repo_id: str
    requests: int
    bytes_served: int
    bytes_saved: int
    hit_ratio: float | None
    latency_p95_ms: float | None


REPOSITORY_SORTS = {'requests': 'requests', 'bytes': 'bytes_served', 'saved': 'bytes_saved'}


def repositories(rows: Sequence[series.Row], *, by: str, limit: int) -> list[RepositoryRow]:
    """Per repository totals of ``rows`` (from ``series.collect(group_repo=True, latency=True)``), busiest first;
    repositories without requests are left out."""
    grouped: dict[str, list[series.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row.get('repo_id') or ''), []).append(row)
    out = []
    for repo_id, mine in grouped.items():
        t = series.totals(mine)
        if not t['requests']:
            continue
        out.append(
            RepositoryRow(
                repo_id=repo_id,
                requests=t['requests'],
                bytes_served=t['bytes_out_cache'] + t['bytes_out_upstream'],
                bytes_saved=t['bytes_out_cache'],
                hit_ratio=series.hit_ratio(t),
                latency_p95_ms=series.percentiles(mine)[1],
            )
        )
    field = REPOSITORY_SORTS[by]
    out.sort(key=lambda r: (-int(getattr(r, field)), r.repo_id))
    return out[:limit]
