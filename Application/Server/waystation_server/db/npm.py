"""``npm_packages`` and ``npm_versions`` access (DATA_MODEL).

- ``npm_packages``: one summary per package of a repository, read by the npm browse views. Proxies: written in the
  background when a packument is stored (never per request). Hosted repositories (T-P5-NPMH): the package's own
  state, changed only by compare-and-set on ``rev`` (``bump``): dist-tags, readme, the summary of its versions.
- ``npm_versions``: one published version of a hosted package (its manifest as JSON text, so keys with dots or
  ``$`` such as ``exports`` entries survive), the source the packuments are generated from.

Queries use ``repo_name_unique`` / ``repo_name_version_unique``; per-package tarball counts are one aggregation over
``assets`` ranges (``repo_path_unique``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, NPM_PACKAGES, NPM_VERSIONS
from waystation_common.searchkeys import name_keys

MAX_VERSIONS = 10_000  # per package (hosted)


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
    return await db[NPM_PACKAGES].find(query, {'_id': 0, 'readme': 0}).sort('name', 1).limit(limit).to_list()


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


# ---------------------------------------------------------------------------- hosted packages (T-P5-NPMH)
async def bump(db: Database, repo_id: str, name: str, rev: int, fields: Mapping[str, Any]) -> bool:
    """Set ``fields`` on the package and move its ``rev`` from ``rev`` to ``rev + 1`` (``rev`` 0: create it). False
    when another change came first (the caller reads again and retries)."""
    now = datetime.now(UTC)
    values = {**fields, **name_keys(name), 'updated_at': now}
    if rev == 0:
        try:
            await db[NPM_PACKAGES].insert_one({'repo_id': repo_id, 'name': name, **values, 'rev': 1, 'created_at': now})
        except DuplicateKeyError:
            return False
        return True
    result = await db[NPM_PACKAGES].update_one(
        {'repo_id': repo_id, 'name': name, 'rev': rev}, {'$set': values, '$inc': {'rev': 1}}
    )
    return result.matched_count == 1


async def drop_package(db: Database, repo_id: str, name: str, rev: int) -> bool:
    """Remove the package document if it is still at ``rev``."""
    result = await db[NPM_PACKAGES].delete_one({'repo_id': repo_id, 'name': name, 'rev': rev})
    return result.deleted_count == 1


async def versions(db: Database, repo_id: str, name: str) -> list[dict[str, Any]]:
    """Every published version of a hosted package (``version``, ``manifest`` JSON, ``tarball``, ``size``,
    ``published_at``, ``published_by``)."""
    cursor = db[NPM_VERSIONS].find({'repo_id': repo_id, 'name': name}, {'_id': 0}).limit(MAX_VERSIONS + 1)
    return await cursor.to_list()


async def count_versions(db: Database, repo_id: str, name: str) -> int:
    return await db[NPM_VERSIONS].count_documents({'repo_id': repo_id, 'name': name})


async def get_version(db: Database, repo_id: str, name: str, version: str) -> dict[str, Any] | None:
    return await db[NPM_VERSIONS].find_one({'repo_id': repo_id, 'name': name, 'version': version}, {'_id': 0})


async def put_version(db: Database, repo_id: str, name: str, version: str, fields: Mapping[str, Any]) -> None:
    """Create or replace a version."""
    await db[NPM_VERSIONS].replace_one(
        {'repo_id': repo_id, 'name': name, 'version': version},
        {'repo_id': repo_id, 'name': name, 'version': version, **fields},
        upsert=True,
    )


async def claim_version(db: Database, repo_id: str, name: str, version: str, fields: Mapping[str, Any]) -> bool:
    """Create a version; False when it exists already."""
    try:
        await db[NPM_VERSIONS].insert_one({'repo_id': repo_id, 'name': name, 'version': version, **fields})
    except DuplicateKeyError:
        return False
    return True


async def set_manifest(db: Database, repo_id: str, name: str, version: str, manifest: str) -> None:
    await db[NPM_VERSIONS].update_one(
        {'repo_id': repo_id, 'name': name, 'version': version}, {'$set': {'manifest': manifest}}
    )


async def delete_version(db: Database, repo_id: str, name: str, version: str) -> dict[str, Any] | None:
    doc: dict[str, Any] | None = await db[NPM_VERSIONS].find_one_and_delete(
        {'repo_id': repo_id, 'name': name, 'version': version}, {'_id': 0}
    )
    return doc
