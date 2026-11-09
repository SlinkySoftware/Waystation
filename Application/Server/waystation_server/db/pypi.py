"""PyPI records (DATA_MODEL): ``pypi_packages`` (one summary per project a repository has: written in the background
when a proxy caches a project page, and on every hosted upload) and the ``pypi`` details kept on the ``assets`` of
hosted distribution files (``packages/<project>/<file>``), from which hosted project pages are rendered.

Queries use ``repo_name_unique`` (``pypi_packages``) and ranges on ``repo_path_unique`` (``assets``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, PYPI_PACKAGES
from waystation_common.searchkeys import name_keys

FILE_FIELDS: dict[str, int] = {
    'path': 1,
    'size': 1,
    'blob_sha256': 1,
    'checksums': 1,
    'created_at': 1,
    'last_downloaded_at': 1,
    'download_count': 1,
    'pypi': 1,
    '_id': 0,
}
MAX_FILES = 100_000


def project_prefix(project: str) -> str:
    return f'packages/{project}/'


def _range(prefix: str) -> dict[str, str]:
    return {'$gte': prefix, '$lt': prefix[:-1] + '0'}  # '/' + 1 = '0': the subtree of prefix


# ---------------------------------------------------------------------------- project summaries
async def upsert_package(db: Database, repo_id: str, name: str, fields: Mapping[str, Any]) -> None:
    now = datetime.now(UTC)
    await db[PYPI_PACKAGES].update_one(
        {'repo_id': repo_id, 'name': name},
        {'$set': {**fields, **name_keys(name), 'updated_at': now}, '$setOnInsert': {'created_at': now}},
        upsert=True,
    )


async def get_package(db: Database, repo_id: str, name: str) -> dict[str, Any] | None:
    return await db[PYPI_PACKAGES].find_one({'repo_id': repo_id, 'name': name}, {'_id': 0})


async def page(db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` projects by (normalised) name after ``after``; ``q``: a case-insensitive substring."""
    name: dict[str, Any] = {}
    if after is not None:
        name['$gt'] = after
    if q:
        name['$regex'] = re.escape(q.lower())
    query: dict[str, Any] = {'repo_id': repo_id}
    if name:
        query['name'] = name
    return await db[PYPI_PACKAGES].find(query, {'_id': 0}).sort('name', 1).limit(limit).to_list()


async def project_names(db: Database, repo_id: str, *, limit: int) -> list[str]:
    """Display names of every project of a repository (hosted ``/simple/``), by normalised name."""
    cursor = db[PYPI_PACKAGES].find({'repo_id': repo_id}, {'name': 1, 'display_name': 1, '_id': 0}).sort('name', 1)
    return [str(d.get('display_name') or d['name']) async for d in cursor.limit(limit)]


# ---------------------------------------------------------------------------- distribution files (assets)
async def project_files(db: Database, repo_id: str, project: str) -> list[dict[str, Any]]:
    """The assets under ``packages/<project>/`` (files and ``.metadata`` sidecars), by path."""
    cursor = (
        db[ASSETS]
        .find({'repo_id': repo_id, 'path': _range(project_prefix(project))}, FILE_FIELDS)
        .sort('path', 1)
        .limit(MAX_FILES)
    )
    return await cursor.to_list()


async def set_file(db: Database, repo_id: str, path: str, details: Mapping[str, Any]) -> None:
    """Record the PyPI details of a hosted distribution file (``assets.pypi``)."""
    await db[ASSETS].update_one({'repo_id': repo_id, 'path': path}, {'$set': {'pypi': dict(details)}})


async def set_yanked(db: Database, repo_id: str, project: str, version: str, reason: str | None) -> int:
    """Yank (``reason`` set, may be '') or un-yank (None) every file of ``version``. Returns the files changed."""
    query = {'repo_id': repo_id, 'path': _range(project_prefix(project)), 'pypi.version': version}
    if reason is None:
        update: dict[str, Any] = {'$set': {'pypi.yanked': False}, '$unset': {'pypi.yanked_reason': ''}}
    else:
        update = {'$set': {'pypi.yanked': True, 'pypi.yanked_reason': reason}}
    result = await db[ASSETS].update_many(query, update)
    return int(result.matched_count)


@dataclass(frozen=True)
class FileStats:
    count: int
    size: int


async def file_stats(db: Database, repo_id: str, names: Iterable[str]) -> dict[str, FileStats]:
    """Distribution files (not ``.metadata`` sidecars) held per project: count and bytes."""
    ranges = [{'path': _range(project_prefix(n))} for n in sorted(set(names))]
    if not ranges:
        return {}
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id, '$or': ranges}},
        {'$match': {'path': {'$not': re.compile(r'\.metadata$')}}},
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
