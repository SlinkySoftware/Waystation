"""Read-side aggregations for the stats API: ``stats_minute`` (closed minutes), ``assets`` and ``blobs``.

Bucketing is epoch-aligned (``ts - ts mod step``) so it matches the buckets the API builds in Python.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, BLOBS, STATS_MINUTE
from waystation_common.events.keys import COUNTERS


async def minute_buckets(
    db: Database,
    *,
    since: datetime,
    until: datetime,
    step_s: int,
    repo_ids: Sequence[str] | None = None,
    group_repo: bool = False,
) -> list[dict[str, Any]]:
    """Counter sums per bucket (``bucket`` = bucket start, epoch ms) and, when ``group_repo``, per repository
    (``repo_id``, ``format``). Also ``last_ts``: the newest minute with requests in the group."""
    match: dict[str, Any] = {'ts': {'$gte': since, '$lt': until}}
    if repo_ids is not None:
        match['m.repo_id'] = {'$in': list(repo_ids)}
    ms = {'$toLong': '$ts'}
    key: dict[str, Any] = {'bucket': {'$subtract': [ms, {'$mod': [ms, step_s * 1000]}]}}
    if group_repo:
        key['repo_id'] = '$m.repo_id'
        key['format'] = '$m.format'
    group: dict[str, Any] = {'_id': key, **{field: {'$sum': f'${field}'} for field in COUNTERS}}
    group['last_ts'] = {'$max': {'$cond': [{'$gt': ['$requests', 0]}, '$ts', None]}}
    pipeline = [{'$match': match}, {'$group': group}]
    out = []
    async for doc in await db[STATS_MINUTE].aggregate(pipeline):
        row = {**doc.pop('_id'), **doc}
        out.append(row)
    return out


async def existing_minutes(
    db: Database, minutes: Sequence[datetime], repo_ids: Sequence[str]
) -> set[tuple[datetime, str]]:
    """Which (minute, repo) pairs are already rolled up (so live Valkey counters are not counted twice)."""
    if not minutes or not repo_ids:
        return set()
    cursor = db[STATS_MINUTE].find(
        {'ts': {'$in': list(minutes)}, 'm.repo_id': {'$in': list(repo_ids)}}, {'ts': 1, 'm.repo_id': 1}
    )
    return {(doc['ts'], doc['m']['repo_id']) async for doc in cursor}


async def asset_totals(db: Database) -> list[dict[str, Any]]:
    """Per repository: stored bytes and artifact (content asset) count."""
    pipeline = [
        {
            '$group': {
                '_id': '$repo_id',
                'bytes': {'$sum': '$size'},
                'artifacts': {'$sum': {'$cond': [{'$eq': ['$kind', 'content']}, 1, 0]}},
            }
        }
    ]
    return [doc async for doc in await db[ASSETS].aggregate(pipeline)]


async def blob_totals(db: Database) -> tuple[int, int]:
    """(blob count, bytes held by blobs)."""
    pipeline = [{'$group': {'_id': None, 'count': {'$sum': 1}, 'bytes': {'$sum': '$size'}}}]
    async for doc in await db[BLOBS].aggregate(pipeline):
        return int(doc['count']), int(doc['bytes'])
    return 0, 0
