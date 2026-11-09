"""``npm_packages`` access (DATA_MODEL): one summary per package whose packument a repository has cached, written in
the background when a packument is stored (never per request), read by the npm browse views.

Queries use ``repo_name_unique``; per-package tarball counts are one aggregation over ``assets`` ranges
(``repo_path_unique``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, NPM_PACKAGES
from waystation_common.searchkeys import name_keys


@dataclass(frozen=True)
class TarballStats:
    count: int
    size: int


async def upsert(db: Database, repo_id: str, name: str, fields: Mapping[str, Any]) -> None:
    """Set the summary ``fields`` the packument provided (others are kept: an abbreviated packument has no
    description or licence)."""
    now = datetime.now(UTC)
    await db[NPM_PACKAGES].update_one(
        {'repo_id': repo_id, 'name': name},
        {'$set': {**fields, **name_keys(name), 'updated_at': now}, '$setOnInsert': {'created_at': now}},
        upsert=True,
    )


async def get(db: Database, repo_id: str, name: str) -> dict[str, Any] | None:
    return await db[NPM_PACKAGES].find_one({'repo_id': repo_id, 'name': name}, {'_id': 0})


async def page(db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` packages by name after ``after``; ``q`` is a case-insensitive substring of the name."""
    name: dict[str, Any] = {}
    if after is not None:
        name['$gt'] = after
    if q:
        name['$regex'] = re.escape(q)
        name['$options'] = 'i'
    query: dict[str, Any] = {'repo_id': repo_id}
    if name:
        query['name'] = name
    return await db[NPM_PACKAGES].find(query, {'_id': 0}).sort('name', 1).limit(limit).to_list()


async def tarball_stats(db: Database, repo_id: str, names: Iterable[str]) -> dict[str, TarballStats]:
    """Cached tarballs (``<name>/-/*.tgz`` assets) per package: count and bytes."""
    ranges = [{'path': {'$gte': f'{n}/-/', '$lt': f'{n}/-0'}} for n in sorted(set(names))]
    if not ranges:
        return {}
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id, '$or': ranges}},
        {'$match': {'path': {'$regex': r'\.tgz$'}}},
        {
            '$group': {
                '_id': {'$arrayElemAt': [{'$split': ['$path', '/-/']}, 0]},
                'count': {'$sum': 1},
                'size': {'$sum': '$size'},
            }
        },
    ]
    out: dict[str, TarballStats] = {}
    async for doc in await db[ASSETS].aggregate(pipeline):
        out[str(doc['_id'])] = TarballStats(count=int(doc['count']), size=int(doc['size']))
    return out
