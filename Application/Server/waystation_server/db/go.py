"""Go module records (DATA_MODEL): ``go_modules`` (one summary per module path a Go proxy has cached something of,
written in the background when a version list or a module file is cached) and range reads of a module's cached
GOPROXY files in ``assets`` (``<escaped module>/@v/...``).

Queries use ``repo_name_unique`` (``go_modules``) and ranges on ``repo_path_unique`` (``assets``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, GO_MODULES
from waystation_common.searchkeys import name_keys

FILE_FIELDS: dict[str, int] = {
    'path': 1,
    'size': 1,
    'blob_sha256': 1,
    'created_at': 1,
    'last_downloaded_at': 1,
    'download_count': 1,
    '_id': 0,
}
MAX_FILES = 100_000


def files_prefix(escaped_module: str) -> str:
    return f'{escaped_module}/@v/'


def _range(prefix: str) -> dict[str, str]:
    return {'$gte': prefix, '$lt': prefix[:-1] + '0'}  # '/' + 1 = '0': the subtree of prefix


# ---------------------------------------------------------------------------- module summaries
async def record_list(db: Database, repo_id: str, module: str, latest: str | None, version_count: int) -> None:
    """A version list was cached: the module's latest version and how many the list has."""
    now = datetime.now(UTC)
    await db[GO_MODULES].update_one(
        {'repo_id': repo_id, 'name': module},
        {
            '$set': {'latest': latest, 'version_count': version_count, **name_keys(module), 'updated_at': now},
            '$setOnInsert': {'created_at': now},
        },
        upsert=True,
    )


async def record_version(db: Database, repo_id: str, module: str, version: str) -> None:
    """A file of ``module@version`` was cached: make sure the module is listed (its first version as ``latest``
    until a version list is cached)."""
    now = datetime.now(UTC)
    await db[GO_MODULES].update_one(
        {'repo_id': repo_id, 'name': module},
        {
            '$set': {**name_keys(module), 'updated_at': now},
            '$setOnInsert': {'created_at': now, 'latest': version, 'version_count': 0},
        },
        upsert=True,
    )


async def get_module(db: Database, repo_id: str, module: str) -> dict[str, Any] | None:
    return await db[GO_MODULES].find_one({'repo_id': repo_id, 'name': module}, {'_id': 0})


async def page(db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` modules by path after ``after``; ``q``: a case-insensitive substring."""
    name: dict[str, Any] = {}
    if after is not None:
        name['$gt'] = after
    if q:
        name['$regex'] = re.escape(q)
        name['$options'] = 'i'
    query: dict[str, Any] = {'repo_id': repo_id}
    if name:
        query['name'] = name
    return await db[GO_MODULES].find(query, {'_id': 0}).sort('name', 1).limit(limit).to_list()


# ---------------------------------------------------------------------------- cached files (assets)
async def module_files(db: Database, repo_id: str, escaped_module: str) -> list[dict[str, Any]]:
    """The assets under ``<escaped module>/@v/`` (list, ``.info``, ``.mod``, ``.zip``), by path."""
    cursor = (
        db[ASSETS]
        .find({'repo_id': repo_id, 'path': _range(files_prefix(escaped_module))}, FILE_FIELDS)
        .sort('path', 1)
        .limit(MAX_FILES)
    )
    return await cursor.to_list()


@dataclass(frozen=True)
class ZipStats:
    count: int
    size: int


async def zip_stats(db: Database, repo_id: str, escaped_modules: Iterable[str]) -> dict[str, ZipStats]:
    """Module zips held per module (by escaped path): count and bytes."""
    ranges = [{'path': _range(files_prefix(m))} for m in sorted(set(escaped_modules))]
    if not ranges:
        return {}
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id, '$or': ranges}},
        {'$match': {'path': re.compile(r'\.zip$')}},
        {
            '$group': {
                '_id': {'$arrayElemAt': [{'$split': ['$path', '/@v/']}, 0]},
                'count': {'$sum': 1},
                'size': {'$sum': '$size'},
            }
        },
    ]
    out: dict[str, ZipStats] = {}
    async for doc in await db[ASSETS].aggregate(pipeline):
        out[str(doc['_id'])] = ZipStats(count=int(doc['count']), size=int(doc['size']))
    return out
