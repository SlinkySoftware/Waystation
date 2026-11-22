"""Cargo records (DATA_MODEL): ``cargo_crates`` (one summary per crate a repository has: written in the background
when a proxy caches an index file, and on every hosted publish or yank) and the ``cargo`` details kept on the assets
of hosted ``.crate`` files (``crates/<crate>/<crate>-<version>.crate``: the index entry, yanked state, publisher),
from which hosted index files are rendered.

Queries use ``repo_name_unique`` / ``repo_canonical`` (``cargo_crates``) and ranges on ``repo_path_unique``
(``assets``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, CARGO_CRATES
from waystation_common.searchkeys import name_keys

FILE_FIELDS: dict[str, int] = {
    'path': 1,
    'size': 1,
    'blob_sha256': 1,
    'created_at': 1,
    'last_downloaded_at': 1,
    'download_count': 1,
    'cargo': 1,
    '_id': 0,
}
MAX_FILES = 100_000


def crate_prefix(crate: str) -> str:
    return f'crates/{crate}/'


def _range(prefix: str) -> dict[str, str]:
    return {'$gte': prefix, '$lt': prefix[:-1] + '0'}  # '/' + 1 = '0': the subtree of prefix


# ---------------------------------------------------------------------------- crate summaries
async def upsert_crate(db: Database, repo_id: str, crate: str, fields: Mapping[str, Any]) -> None:
    """``crate``: the lower-case name; ``fields``: ``display_name``, ``canonical``, ``latest``, ``version_count``,
    ``description``."""
    now = datetime.now(UTC)
    display = str(fields.get('display_name') or crate)
    await db[CARGO_CRATES].update_one(
        {'repo_id': repo_id, 'name': crate},
        {'$set': {**fields, **name_keys(display), 'updated_at': now}, '$setOnInsert': {'created_at': now}},
        upsert=True,
    )


async def delete_crate(db: Database, repo_id: str, crate: str) -> None:
    await db[CARGO_CRATES].delete_one({'repo_id': repo_id, 'name': crate})


async def get_crate(db: Database, repo_id: str, crate: str) -> dict[str, Any] | None:
    return await db[CARGO_CRATES].find_one({'repo_id': repo_id, 'name': crate}, {'_id': 0})


async def same_crate(db: Database, repo_id: str, canonical: str) -> list[str]:
    """Lower-case names of the repository's crates with this canonical name (``-`` = ``_``)."""
    cursor = db[CARGO_CRATES].find({'repo_id': repo_id, 'canonical': canonical}, {'name': 1, '_id': 0}).limit(10)
    return [str(d['name']) async for d in cursor]


async def page(db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` crates by (lower-case) name after ``after``; ``q``: a case-insensitive substring."""
    name: dict[str, Any] = {}
    if after is not None:
        name['$gt'] = after
    if q:
        name['$regex'] = re.escape(q.lower())
    query: dict[str, Any] = {'repo_id': repo_id}
    if name:
        query['name'] = name
    return await db[CARGO_CRATES].find(query, {'_id': 0}).sort('name', 1).limit(limit).to_list()


# ---------------------------------------------------------------------------- crate files (assets)
async def crate_files(db: Database, repo_id: str, crate: str) -> list[dict[str, Any]]:
    """The assets under ``crates/<crate>/`` (lower-case name), by path."""
    cursor = (
        db[ASSETS]
        .find({'repo_id': repo_id, 'path': _range(crate_prefix(crate))}, FILE_FIELDS)
        .sort('path', 1)
        .limit(MAX_FILES)
    )
    return await cursor.to_list()


async def set_details(db: Database, repo_id: str, path: str, details: Mapping[str, Any]) -> None:
    """Record the Cargo details of a hosted ``.crate`` (``assets.cargo``)."""
    await db[ASSETS].update_one({'repo_id': repo_id, 'path': path}, {'$set': {'cargo': dict(details)}})


async def set_yanked(db: Database, repo_id: str, path: str, yanked: bool) -> bool:
    """Yank or un-yank a hosted version; False when the repository has no such version."""
    result = await db[ASSETS].update_one(
        {'repo_id': repo_id, 'path': path, 'cargo': {'$exists': True}}, {'$set': {'cargo.yanked': yanked}}
    )
    return bool(result.matched_count)


@dataclass(frozen=True)
class FileStats:
    count: int
    size: int


async def file_stats(db: Database, repo_id: str, crates: Iterable[str]) -> dict[str, FileStats]:
    """``.crate`` files held per crate (lower-case names): count and bytes."""
    ranges = [{'path': _range(crate_prefix(c))} for c in sorted(set(crates))]
    if not ranges:
        return {}
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id, '$or': ranges}},
        {
            '$group': {
                '_id': {'$arrayElemAt': [{'$split': ['$path', '/']}, 1]},
                'count': {'$sum': 1},
                'size': {'$sum': '$size'},
            }
        },
    ]
    out: dict[str, FileStats] = {}
    async for doc in await db[ASSETS].aggregate(pipeline):
        out[str(doc['_id'])] = FileStats(count=int(doc['count']), size=int(doc['size']))
    return out
