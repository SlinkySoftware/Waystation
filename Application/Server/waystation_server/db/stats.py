"""Read-side aggregations for the stats API: the counter time series (``stats_minute`` / ``stats_hour`` /
``stats_day``), ``storage_snapshots``, ``repo_usage`` and ``blobs``.

Bucketing is epoch-aligned (``ts - ts mod step``) so it matches the buckets the API builds in Python. Every query
has a ``ts`` range and, unless the principal sees every repository, an ``m.repo_id`` ``$in``: the ``repo_ts`` / ``ts``
indexes of the time-series collections.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import BLOBS, REPO_USAGE, STATS_MINUTE, STORAGE_SNAPSHOTS
from waystation_common.db.stats import newest, sum_fields, unflatten

__all__ = [
    'blob_totals',
    'existing_minutes',
    'latest_snapshots',
    'minute_buckets',
    'newest',
    'period_buckets',
    'repo_totals',
    'snapshot_buckets',
]


async def period_buckets(
    db: Database,
    collection: str,
    *,
    since: datetime,
    until: datetime,
    step_s: int,
    repo_ids: Sequence[str] | None = None,
    group_repo: bool = False,
) -> list[dict[str, Any]]:
    """Counter and latency-histogram sums per bucket (``bucket`` = bucket start, epoch ms) and, when ``group_repo``,
    per repository (``repo_id``, ``format``), from one of the counter time series. Also ``last_ts``: the newest
    period with requests in the group."""
    if since >= until:
        return []
    match: dict[str, Any] = {'ts': {'$gte': since, '$lt': until}}
    if repo_ids is not None:
        match['m.repo_id'] = {'$in': list(repo_ids)}
    ms = {'$toLong': '$ts'}
    key: dict[str, Any] = {'bucket': {'$subtract': [ms, {'$mod': [ms, step_s * 1000]}]}}
    if group_repo:
        key['repo_id'] = '$m.repo_id'
        key['format'] = '$m.format'
    group: dict[str, Any] = {'_id': key, **sum_fields()}
    group['last_ts'] = {'$max': {'$cond': [{'$gt': ['$requests', 0]}, '$ts', None]}}
    pipeline = [{'$match': match}, {'$group': group}]
    out = []
    async for doc in await db[collection].aggregate(pipeline):
        out.append(unflatten({**doc.pop('_id'), **doc}))
    return out


async def minute_buckets(
    db: Database,
    *,
    since: datetime,
    until: datetime,
    step_s: int,
    repo_ids: Sequence[str] | None = None,
    group_repo: bool = False,
) -> list[dict[str, Any]]:
    return await period_buckets(
        db, STATS_MINUTE, since=since, until=until, step_s=step_s, repo_ids=repo_ids, group_repo=group_repo
    )


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


async def snapshot_buckets(
    db: Database, *, since: datetime, until: datetime, step_s: int, repo_ids: Sequence[str] | None
) -> dict[int, dict[str, dict[str, Any]]]:
    """The last storage snapshot document per bucket (epoch ms) and repository ('' = the instance-wide one) in
    ``[since, until)``: ``{bucket: {repo_id: doc}}``."""
    match: dict[str, Any] = {'ts': {'$gte': since, '$lt': until}}
    if repo_ids is not None:
        match['m.repo_id'] = {'$in': ['', *repo_ids]}
    ms = {'$toLong': '$ts'}
    pipeline: list[dict[str, Any]] = [
        {'$match': match},
        {'$sort': {'ts': 1}},
        {
            '$group': {
                '_id': {'bucket': {'$subtract': [ms, {'$mod': [ms, step_s * 1000]}]}, 'repo_id': '$m.repo_id'},
                'doc': {'$last': '$$ROOT'},
            }
        },
    ]
    out: dict[int, dict[str, dict[str, Any]]] = {}
    async for row in await db[STORAGE_SNAPSHOTS].aggregate(pipeline):
        out.setdefault(int(row['_id']['bucket']), {})[str(row['_id']['repo_id'] or '')] = row['doc']
    return out


async def latest_snapshots(db: Database) -> dict[str, dict[str, Any]]:
    """The newest storage snapshot, per repository ('' = instance-wide); empty before the first one."""
    ts = await newest(db, STORAGE_SNAPSHOTS)
    if ts is None:
        return {}
    cursor = db[STORAGE_SNAPSHOTS].find({'ts': ts})
    return {str(doc['m'].get('repo_id') or ''): doc async for doc in cursor}


async def repo_totals(db: Database) -> dict[str, tuple[int, int]]:
    """Per repository: stored bytes and artifact (content asset) count, from the ``repo_usage`` counters (one
    document per repository, ``waystation_common.db.usage``) rather than an aggregation over ``assets``."""
    out: dict[str, tuple[int, int]] = {}
    async for doc in db[REPO_USAGE].find({}, {'bytes': 1, 'artifacts': 1}):
        out[str(doc['_id'])] = (max(0, int(doc.get('bytes') or 0)), max(0, int(doc.get('artifacts') or 0)))
    return out


async def blob_totals(db: Database) -> tuple[int, int]:
    """(blob count, bytes held by blobs)."""
    pipeline = [{'$group': {'_id': None, 'count': {'$sum': 1}, 'bytes': {'$sum': '$size'}}}]
    async for doc in await db[BLOBS].aggregate(pipeline):
        return int(doc['count']), int(doc['bytes'])
    return 0, 0
