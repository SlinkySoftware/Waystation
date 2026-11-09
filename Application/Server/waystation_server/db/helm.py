"""``helm_charts`` access for the server (DATA_MODEL; fields in ``waystation_common.db.helm``).

- Hosted repositories: one record per uploaded chart version, written (and deleted) inside the transaction that
  also puts the ``index.helm`` job in the outbox (``session``).
- Proxy repositories: the listing of the cached ``index.yaml`` (generation ``gen`` = its blob's sha256), replaced in
  bulk when a new index is cached; chart files are fetched only for paths listed here.
- Browse: charts by name and their versions newest first, over one repository or a group's members.

Every query uses an index of the collection (``repo_name_version_unique``, ``repo_name_sort``, ``repo_path``,
``repo_gen``).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pymongo import DESCENDING, UpdateOne

from waystation_common.db.client import Database, Session
from waystation_common.db.collections import HELM_CHARTS

BATCH = 1000


async def get_version(db: Database, repo_id: str, name: str, version: str) -> dict[str, Any] | None:
    return await db[HELM_CHARTS].find_one({'repo_id': repo_id, 'name': name, 'version': version}, {'_id': 0})


async def by_path(db: Database, repo_id: str, path: str) -> dict[str, Any] | None:
    return await db[HELM_CHARTS].find_one({'repo_id': repo_id, 'path': path}, {'_id': 0})


async def save_hosted(db: Database, repo_id: str, record: dict[str, Any], *, session: Session | None = None) -> None:
    """Insert or replace an uploaded chart version (``record`` holds every field but ``repo_id``)."""
    now = datetime.now(UTC)
    await db[HELM_CHARTS].update_one(
        {'repo_id': repo_id, 'name': record['name'], 'version': record['version']},
        {'$set': {**record, 'repo_id': repo_id, 'updated_at': now}},
        upsert=True,
        session=session,
    )


async def delete_version(
    db: Database, repo_id: str, name: str, version: str, *, session: Session | None = None
) -> dict[str, Any] | None:
    return await db[HELM_CHARTS].find_one_and_delete(
        {'repo_id': repo_id, 'name': name, 'version': version}, projection={'_id': 0}, session=session
    )


async def has_gen(db: Database, repo_id: str, gen: str) -> bool:
    return await db[HELM_CHARTS].find_one({'repo_id': repo_id, 'gen': gen}, {'_id': 1}) is not None


async def replace_listing(db: Database, repo_id: str, gen: str, records: Sequence[dict[str, Any]]) -> None:
    """Make ``records`` (a cached index's chart versions) the proxy repository's listing for generation ``gen``."""
    now = datetime.now(UTC)
    for start in range(0, len(records), BATCH):
        ops = [
            UpdateOne(
                {'repo_id': repo_id, 'name': r['name'], 'version': r['version']},
                {'$set': {**r, 'repo_id': repo_id, 'gen': gen, 'updated_at': now}},
                upsert=True,
            )
            for r in records[start : start + BATCH]
        ]
        await db[HELM_CHARTS].bulk_write(ops, ordered=False)
    await db[HELM_CHARTS].delete_many({'repo_id': repo_id, 'gen': {'$ne': gen}})


# ---------------------------------------------------------------------------- browse
async def charts(
    db: Database,
    repo_ids: Sequence[str],
    *,
    after: str | None,
    q: str | None,
    limit: int,
    name_is: str | None = None,
) -> list[dict[str, Any]]:
    """Up to ``limit`` charts by name after ``after`` (``q``: case-insensitive substring; ``name_is``: one chart),
    each with its newest version's summary (``latest``) and the number of versions."""
    name: dict[str, Any] = {'$eq': name_is} if name_is is not None else {}
    if after is not None:
        name['$gt'] = after
    if q:
        name['$regex'] = re.escape(q)
        name['$options'] = 'i'
    match: dict[str, Any] = {'repo_id': {'$in': list(repo_ids)}}
    if name:
        match['name'] = name
    pipeline: list[dict[str, Any]] = [
        {'$match': match},
        {'$sort': {'name': 1, 'version_sort': DESCENDING}},
        {
            '$group': {
                '_id': '$name',
                'latest': {'$first': '$$ROOT'},
                'versions': {'$addToSet': '$version'},
            }
        },
        {'$sort': {'_id': 1}},
        {'$limit': limit},
    ]
    out: list[dict[str, Any]] = []
    async for doc in await db[HELM_CHARTS].aggregate(pipeline):
        latest = doc['latest']
        latest.pop('_id', None)
        out.append({'name': doc['_id'], 'latest': latest, 'version_count': len(doc['versions'])})
    return out


async def versions(
    db: Database, repo_ids: Sequence[str], name: str, *, before: str | None, limit: int
) -> list[dict[str, Any]]:
    """A chart's versions newest first (``before``: a ``version_sort`` to continue after); with several
    repositories (a group's members) one record per version, the first repository's."""
    query: dict[str, Any] = {'repo_id': {'$in': list(repo_ids)}, 'name': name}
    if before is not None:
        query['version_sort'] = {'$lt': before}
    cursor = db[HELM_CHARTS].find(query, {'_id': 0}).sort('version_sort', DESCENDING).limit(limit * len(repo_ids))
    order = {repo_id: i for i, repo_id in enumerate(repo_ids)}
    chosen: dict[str, dict[str, Any]] = {}
    async for doc in cursor:
        current = chosen.get(doc['version'])
        if current is None or order[doc['repo_id']] < order[current['repo_id']]:
            chosen[doc['version']] = doc
    return sorted(chosen.values(), key=lambda d: d['version_sort'], reverse=True)[:limit]
