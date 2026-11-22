"""Composer records (DATA_MODEL): ``composer_packages`` (one summary per package a repository has: written in the
background when a proxy caches a package's ``p2`` document, and on every hosted upload or delete; a proxy's archive
digests are pinned in it) and the ``composer`` details kept on the ``assets`` of hosted archives
(``dists/<vendor>/<name>/<version>/<file>.zip``), from which hosted ``p2`` documents are rendered.

Queries use ``repo_name_unique`` (``composer_packages``) and ranges on ``repo_path_unique`` (``assets``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, COMPOSER_PACKAGES
from waystation_common.searchkeys import name_keys

DISTS = 'dists'
MAX_FILES = 20_000
MAX_SCAN = 10_000
MAX_PINS = 20_000
FILE_FIELDS: dict[str, int] = {'path': 1, 'size': 1, 'blob_sha256': 1, 'created_at': 1, 'composer': 1, '_id': 0}


def package_prefix(name: str) -> str:
    return f'{DISTS}/{name}/'


def _range(prefix: str) -> dict[str, str]:
    return {'$gte': prefix, '$lt': prefix[:-1] + '0'}  # '/' + 1 = '0': the subtree of prefix


# ---------------------------------------------------------------------------- package summaries
async def upsert_package(db: Database, repo_id: str, name: str, fields: Mapping[str, Any]) -> None:
    now = datetime.now(UTC)
    await db[COMPOSER_PACKAGES].update_one(
        {'repo_id': repo_id, 'name': name},
        {'$set': {**fields, **name_keys(name), 'updated_at': now}, '$setOnInsert': {'created_at': now}},
        upsert=True,
    )


async def delete_package(db: Database, repo_id: str, name: str) -> None:
    await db[COMPOSER_PACKAGES].delete_one({'repo_id': repo_id, 'name': name})


async def get_package(db: Database, repo_id: str, name: str) -> dict[str, Any] | None:
    return await db[COMPOSER_PACKAGES].find_one({'repo_id': repo_id, 'name': name}, {'_id': 0, 'pins': 0})


async def page(db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` packages by name after ``after``; ``q``: a case-insensitive substring."""
    name: dict[str, Any] = {}
    if after is not None:
        name['$gt'] = after
    if q:
        name['$regex'] = re.escape(q.lower())
    query: dict[str, Any] = {'repo_id': repo_id}
    if name:
        query['name'] = name
    projection = {'_id': 0, 'pins': 0, 'versions': 0}
    return await db[COMPOSER_PACKAGES].find(query, projection).sort('name', 1).limit(limit).to_list()


async def names(db: Database, repo_id: str) -> list[str] | None:
    """Every package name of a repository (None past ``MAX_SCAN``)."""
    cursor = db[COMPOSER_PACKAGES].find({'repo_id': repo_id}, {'_id': 0, 'name': 1}).sort('name', 1)
    found = await cursor.limit(MAX_SCAN + 1).to_list()
    return None if len(found) > MAX_SCAN else [str(d['name']) for d in found]


# ---------------------------------------------------------------------------- proxy archive pins
async def pinned_sha256(db: Database, repo_id: str, name: str, path: str) -> str | None:
    """The sha256 recorded when a proxy first cached the archive at ``path``, else None."""
    doc = await db[COMPOSER_PACKAGES].find_one(
        {'repo_id': repo_id, 'name': name}, {'_id': 0, 'pins': {'$elemMatch': {'path': path}}}
    )
    for pin in (doc or {}).get('pins') or ():
        if pin.get('path') == path and pin.get('sha256'):
            return str(pin['sha256'])
    return None


async def pin(db: Database, repo_id: str, name: str, path: str, sha256: str) -> None:
    """Pin an archive's digest (first fetch wins: an existing pin is kept)."""
    now = datetime.now(UTC)
    await db[COMPOSER_PACKAGES].update_one(
        {'repo_id': repo_id, 'name': name},
        {'$setOnInsert': {**name_keys(name), 'created_at': now, 'updated_at': now, 'version_count': 0}},
        upsert=True,
    )
    await db[COMPOSER_PACKAGES].update_one(
        {'repo_id': repo_id, 'name': name, 'pins.path': {'$ne': path}},
        {'$push': {'pins': {'$each': [{'path': path, 'sha256': sha256, 'at': now}], '$slice': -MAX_PINS}}},
    )


# ---------------------------------------------------------------------------- hosted archives (assets)
async def package_files(db: Database, repo_id: str, name: str) -> list[dict[str, Any]]:
    """The hosted archives of a package with their details, by path."""
    query = {'repo_id': repo_id, 'path': _range(package_prefix(name)), 'composer': {'$exists': True}}
    return await db[ASSETS].find(query, FILE_FIELDS).sort('path', 1).limit(MAX_FILES).to_list()


async def set_details(db: Database, repo_id: str, path: str, details: Mapping[str, Any]) -> None:
    await db[ASSETS].update_one({'repo_id': repo_id, 'path': path}, {'$set': {'composer': dict(details)}})
