"""``galaxy_collections`` access for the server (DATA_MODEL; fields in ``waystation_common.db.galaxy``).

- Proxy repositories: the versions of a cached version list (generation ``gen`` = digest of the list's blobs),
  replaced per collection when a new list is cached; a version's ``sha256``/``size``/``description`` once its
  details are cached. Written in the background, read by browse.
- Browse: namespaces, collections and versions (newest first) over one repository or a group's members.

Every query uses an index of the collection (``repo_coll_version_unique``, ``repo_coll_sort``).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pymongo import DESCENDING, UpdateOne

from waystation_common.db.client import Database
from waystation_common.db.collections import GALAXY_COLLECTIONS

BATCH = 1000


async def has_gen(db: Database, repo_id: str, namespace: str, name: str, gen: str) -> bool:
    found = await db[GALAXY_COLLECTIONS].find_one(
        {'repo_id': repo_id, 'namespace': namespace, 'name': name, 'gen': gen}, {'_id': 1}
    )
    return found is not None


async def replace_listing(
    db: Database, repo_id: str, namespace: str, name: str, gen: str, records: Sequence[dict[str, Any]]
) -> None:
    """Make ``records`` (a cached version list) the proxy repository's versions of the collection."""
    now = datetime.now(UTC)
    key = {'repo_id': repo_id, 'namespace': namespace, 'name': name}
    for start in range(0, len(records), BATCH):
        ops = [
            UpdateOne(
                {**key, 'version': r['version']},
                {'$set': {**r, **key, 'gen': gen, 'updated_at': now}},
                upsert=True,
            )
            for r in records[start : start + BATCH]
        ]
        await db[GALAXY_COLLECTIONS].bulk_write(ops, ordered=False)
    await db[GALAXY_COLLECTIONS].delete_many({**key, 'gen': {'$ne': gen}})


async def update_details(
    db: Database, repo_id: str, namespace: str, name: str, version: str, fields: dict[str, Any]
) -> None:
    """A proxy repository cached a version's details: remember its checksum, size and description."""
    await db[GALAXY_COLLECTIONS].update_one(
        {'repo_id': repo_id, 'namespace': namespace, 'name': name, 'version': version},
        {'$set': {**fields, 'updated_at': datetime.now(UTC)}},
    )


# ---------------------------------------------------------------------------- browse
def _name_filter(q: str | None, after: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if after is not None:
        out['$gt'] = after
    if q:
        out['$regex'] = re.escape(q)
        out['$options'] = 'i'
    return out


async def namespaces(
    db: Database, repo_ids: Sequence[str], *, after: str | None, q: str | None, limit: int
) -> list[dict[str, Any]]:
    """Up to ``limit`` namespaces after ``after`` with their number of collections."""
    match: dict[str, Any] = {'repo_id': {'$in': list(repo_ids)}}
    name = _name_filter(q, after)
    if name:
        match['namespace'] = name
    pipeline: list[dict[str, Any]] = [
        {'$match': match},
        {'$group': {'_id': '$namespace', 'collections': {'$addToSet': '$name'}}},
        {'$sort': {'_id': 1}},
        {'$limit': limit},
    ]
    return [
        {'namespace': doc['_id'], 'collection_count': len(doc['collections'])}
        async for doc in await db[GALAXY_COLLECTIONS].aggregate(pipeline)
    ]


async def collections(
    db: Database,
    repo_ids: Sequence[str],
    *,
    namespace: str | None,
    after: tuple[str, str] | None,
    q: str | None,
    limit: int,
    name_is: str | None = None,
) -> list[dict[str, Any]]:
    """Up to ``limit`` collections by (namespace, name) after ``after`` (``q``: substring of the name or the
    namespace), each with its newest version's record (``latest``) and its number of versions."""
    match: dict[str, Any] = {'repo_id': {'$in': list(repo_ids)}}
    if namespace is not None:
        match['namespace'] = namespace
    if name_is is not None:
        match['name'] = name_is
    if q:
        pattern = {'$regex': re.escape(q), '$options': 'i'}
        match['$or'] = [{'name': pattern}, {'namespace': pattern}]
    if after is not None:
        match['$and'] = [
            {'$or': [{'namespace': {'$gt': after[0]}}, {'namespace': after[0], 'name': {'$gt': after[1]}}]}
        ]
    pipeline: list[dict[str, Any]] = [
        {'$match': match},
        {'$sort': {'namespace': 1, 'name': 1, 'version_sort': DESCENDING}},
        {
            '$group': {
                '_id': {'namespace': '$namespace', 'name': '$name'},
                'latest': {'$first': '$$ROOT'},
                'versions': {'$addToSet': '$version'},
            }
        },
        {'$sort': {'_id.namespace': 1, '_id.name': 1}},
        {'$limit': limit},
    ]
    out: list[dict[str, Any]] = []
    async for doc in await db[GALAXY_COLLECTIONS].aggregate(pipeline):
        latest = doc['latest']
        latest.pop('_id', None)
        out.append(
            {
                'namespace': doc['_id']['namespace'],
                'name': doc['_id']['name'],
                'latest': latest,
                'version_count': len(doc['versions']),
            }
        )
    return out


async def versions(
    db: Database, repo_ids: Sequence[str], namespace: str, name: str, *, before: str | None, limit: int
) -> list[dict[str, Any]]:
    """A collection's versions newest first (``before``: a ``version_sort`` to continue after); with several
    repositories (a group's members) one record per version, the first repository's."""
    query: dict[str, Any] = {'repo_id': {'$in': list(repo_ids)}, 'namespace': namespace, 'name': name}
    if before is not None:
        query['version_sort'] = {'$lt': before}
    cursor = (
        db[GALAXY_COLLECTIONS]
        .find(query, {'_id': 0})
        .sort('version_sort', DESCENDING)
        .limit(limit * max(1, len(repo_ids)))
    )
    order = {repo_id: i for i, repo_id in enumerate(repo_ids)}
    chosen: dict[str, dict[str, Any]] = {}
    async for doc in cursor:
        current = chosen.get(doc['version'])
        if current is None or order[doc['repo_id']] < order[current['repo_id']]:
            chosen[doc['version']] = doc
    return sorted(chosen.values(), key=lambda d: d['version_sort'], reverse=True)[:limit]
