"""Stats time series persistence (DATA_MODEL §1): ``stats_minute``, the ``stats_hour`` / ``stats_day`` rollups and
``storage_snapshots``.

Time-series collections cannot upsert, so every writer checks for existing documents of a period before inserting
(the rollup runs one at a time, under ``lease:rollup``). A period document has the counters (``keys.COUNTERS``) and
the latency histograms ``lat_hist`` (request duration) and ``ulat_hist`` (upstream response time) summed over the
period, per repository (``m.repo_id``, ``m.format``); percentiles are computed from the sums at query time.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import (
    STATS_DAY,
    STATS_DAY_RETENTION_DAYS,
    STATS_HOUR,
    STATS_HOUR_RETENTION_DAYS,
    STATS_MINUTE,
    STATS_MINUTE_RETENTION_DAYS,
    STORAGE_SNAPSHOT_RETENTION_DAYS,
    STORAGE_SNAPSHOTS,
)
from waystation_common.events.keys import COUNTERS, LATENCY_FIELDS

HISTOGRAMS = ('lat_hist', 'ulat_hist')
PERIOD_COLLECTIONS = (STATS_MINUTE, STATS_HOUR, STATS_DAY)


async def minute_exists(db: Database, repo_id: str, ts: datetime) -> bool:
    return await db[STATS_MINUTE].find_one({'m.repo_id': repo_id, 'ts': ts}, {'_id': 1}) is not None


async def insert_minute(db: Database, doc: dict[str, Any]) -> None:
    await db[STATS_MINUTE].insert_one(doc)


async def minutes(db: Database, repo_id: str, since: datetime) -> list[dict[str, Any]]:
    """All minute documents of a repo since ``since`` (tests, diagnostics)."""
    return [doc async for doc in db[STATS_MINUTE].find({'m.repo_id': repo_id, 'ts': {'$gte': since}}).sort('ts', 1)]


def sum_fields() -> dict[str, Any]:
    """``$group`` accumulators summing the counters and every histogram bucket (as ``<hist>__<bucket>``)."""
    out: dict[str, Any] = {field: {'$sum': f'${field}'} for field in COUNTERS}
    for hist in HISTOGRAMS:
        for bucket in LATENCY_FIELDS:
            out[f'{hist}__{bucket}'] = {'$sum': f'${hist}.{bucket}'}
    return out


def unflatten(row: dict[str, Any]) -> dict[str, Any]:
    """Fold ``<hist>__<bucket>`` sums back into ``{hist: {bucket: n}}`` (zero buckets dropped)."""
    out: dict[str, Any] = {}
    hists: dict[str, dict[str, int]] = {hist: {} for hist in HISTOGRAMS}
    for key, value in row.items():
        hist, sep, bucket = key.partition('__')
        if sep and hist in hists:
            if value:
                hists[hist][bucket] = int(value)
        else:
            out[key] = value
    out.update(hists)
    return out


async def aggregate_period(db: Database, source: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """One document per repository with the sums of ``source`` documents in ``[start, end)``."""
    pipeline = [
        {'$match': {'ts': {'$gte': start, '$lt': end}}},
        {'$group': {'_id': {'repo_id': '$m.repo_id', 'format': '$m.format'}, **sum_fields()}},
    ]
    out = []
    async for row in await db[source].aggregate(pipeline):
        key = row.pop('_id')
        doc = unflatten(row)
        doc['m'] = {'repo_id': key.get('repo_id') or '', 'format': key.get('format') or ''}
        doc['ts'] = start
        out.append(doc)
    return out


async def repos_at(db: Database, collection: str, ts: datetime) -> set[str]:
    """Repositories that already have a document at ``ts`` in ``collection``."""
    cursor = db[collection].find({'ts': ts}, {'m.repo_id': 1, '_id': 0})
    return {str(doc['m']['repo_id']) async for doc in cursor}


async def insert_period(db: Database, collection: str, docs: Iterable[dict[str, Any]]) -> int:
    """Insert the documents whose repository has none at that time yet (each period is written once)."""
    pending = list(docs)
    if not pending:
        return 0
    done = await repos_at(db, collection, pending[0]['ts'])
    fresh = [doc for doc in pending if doc['m']['repo_id'] not in done]
    if fresh:
        await db[collection].insert_many(fresh, ordered=False)
    return len(fresh)


RETENTION_DAYS = {
    STATS_MINUTE: STATS_MINUTE_RETENTION_DAYS,
    STATS_HOUR: STATS_HOUR_RETENTION_DAYS,
    STATS_DAY: STATS_DAY_RETENTION_DAYS,
    STORAGE_SNAPSHOTS: STORAGE_SNAPSHOT_RETENTION_DAYS,
}


def _kept(collection: str) -> dict[str, Any]:
    """``ts`` within the collection's retention: an unbounded sort on a time-series collection reads every bucket;
    a bounded ``ts`` range is served by its ``ts`` index."""
    since = datetime.now(UTC) - timedelta(days=RETENTION_DAYS[collection] + 1)
    return {'ts': {'$gte': since}}


async def newest(db: Database, collection: str) -> datetime | None:
    doc = await db[collection].find_one(_kept(collection), {'ts': 1}, sort=[('ts', -1)])
    return doc['ts'] if doc else None


async def oldest(db: Database, collection: str) -> datetime | None:
    doc = await db[collection].find_one(_kept(collection), {'ts': 1}, sort=[('ts', 1)])
    return doc['ts'] if doc else None


async def snapshot_exists(db: Database, ts: datetime) -> bool:
    """Is the instance-wide storage snapshot of ``ts`` written (the last document a snapshot writes)?"""
    return await db[STORAGE_SNAPSHOTS].find_one({'m.repo_id': '', 'ts': ts}, {'_id': 1}) is not None


__all__ = [
    'HISTOGRAMS',
    'PERIOD_COLLECTIONS',
    'aggregate_period',
    'insert_minute',
    'insert_period',
    'minute_exists',
    'minutes',
    'newest',
    'oldest',
    'repos_at',
    'snapshot_exists',
    'sum_fields',
    'unflatten',
]
