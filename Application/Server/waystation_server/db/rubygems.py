"""RubyGems records (DATA_MODEL): ``rubygems_gems`` (one summary per gem a repository has: written in the background
when a proxy caches a gem's info file, and on every hosted push or yank) and the ``rubygems`` details kept on the
``assets`` of hosted gems (``gems/<full name>.gem``), from which hosted compact index files are rendered.

Queries use ``repo_name_unique`` (``rubygems_gems``) and a range on ``repo_path_unique`` (``assets``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, RUBYGEMS_GEMS
from waystation_common.searchkeys import name_keys

GEMS_PREFIX = 'gems/'
MAX_FILES = 50_000
FILE_FIELDS: dict[str, int] = {'path': 1, 'size': 1, 'created_at': 1, 'rubygems': 1, '_id': 0}


def _range(prefix: str) -> dict[str, str]:
    return {'$gte': prefix, '$lt': prefix[:-1] + '0'}  # '/' + 1 = '0': the subtree of prefix


# ---------------------------------------------------------------------------- gem summaries
async def upsert_gem(db: Database, repo_id: str, name: str, fields: Mapping[str, Any]) -> None:
    now = datetime.now(UTC)
    await db[RUBYGEMS_GEMS].update_one(
        {'repo_id': repo_id, 'name': name},
        {'$set': {**fields, **name_keys(name), 'updated_at': now}, '$setOnInsert': {'created_at': now}},
        upsert=True,
    )


async def delete_gem(db: Database, repo_id: str, name: str) -> None:
    await db[RUBYGEMS_GEMS].delete_one({'repo_id': repo_id, 'name': name})


async def get_gem(db: Database, repo_id: str, name: str) -> dict[str, Any] | None:
    return await db[RUBYGEMS_GEMS].find_one({'repo_id': repo_id, 'name': name}, {'_id': 0})


async def page(db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` gems by name after ``after``; ``q``: a case-insensitive substring."""
    name: dict[str, Any] = {}
    if after is not None:
        name['$gt'] = after
    if q:
        name['$regex'] = re.escape(q)
        name['$options'] = 'i'
    query: dict[str, Any] = {'repo_id': repo_id}
    if name:
        query['name'] = name
    return await db[RUBYGEMS_GEMS].find(query, {'_id': 0, 'versions': 0}).sort('name', 1).limit(limit).to_list()


# ---------------------------------------------------------------------------- hosted gems (assets)
async def gem_files(db: Database, repo_id: str, name: str | None = None) -> list[dict[str, Any]]:
    """The hosted gems (of one gem name, else all) with their details, oldest first."""
    query: dict[str, Any] = {'repo_id': repo_id, 'path': _range(GEMS_PREFIX), 'rubygems': {'$exists': True}}
    if name is not None:  # gems/<name>-...: '-' + 1 = '.'
        query['path'] = {'$gte': f'{GEMS_PREFIX}{name}-', '$lt': f'{GEMS_PREFIX}{name}.'}
        query['rubygems.name'] = name
    found = await db[ASSETS].find(query, FILE_FIELDS).limit(MAX_FILES).to_list()
    return sorted(found, key=lambda d: (d['rubygems'].get('published') or d.get('created_at'), d['path']))


async def set_details(db: Database, repo_id: str, path: str, details: Mapping[str, Any]) -> None:
    await db[ASSETS].update_one({'repo_id': repo_id, 'path': path}, {'$set': {'rubygems': dict(details)}})
