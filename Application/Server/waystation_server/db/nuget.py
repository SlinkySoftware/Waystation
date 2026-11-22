"""NuGet records (DATA_MODEL): ``nuget_packages`` (one summary per package id a repository has: written in the
background when a proxy caches a version list or registration, and on every hosted push, delete or listing change)
and the ``nuget`` details kept on the ``assets`` of hosted packages (``v3-flatcontainer/<id>/<version>/<id>.<version>
.nupkg``), from which hosted version lists, registrations and search answers are rendered.

Queries use ``repo_name_unique`` (``nuget_packages``) and ranges on ``repo_path_unique`` (``assets``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, NUGET_PACKAGES
from waystation_common.searchkeys import name_keys

FLAT = 'v3-flatcontainer'
MAX_FILES = 100_000
MAX_SCAN = 10_000
PACKAGE_FIELDS: dict[str, int] = {
    'path': 1,
    'size': 1,
    'blob_sha256': 1,
    'checksums': 1,
    'created_at': 1,
    'nuget': 1,
    '_id': 0,
}


def package_prefix(package_id: str) -> str:
    return f'{FLAT}/{package_id.lower()}/'


def _range(prefix: str) -> dict[str, str]:
    return {'$gte': prefix, '$lt': prefix[:-1] + '0'}  # '/' + 1 = '0': the subtree of prefix


# ---------------------------------------------------------------------------- package summaries
async def upsert_package(
    db: Database, repo_id: str, name: str, fields: Mapping[str, Any], *, on_insert: Mapping[str, Any] | None = None
) -> None:
    """Set ``fields`` on the package's summary (``on_insert``: fields written only when it is new)."""
    now = datetime.now(UTC)
    insert = {k: v for k, v in (on_insert or {}).items() if k not in fields}
    await db[NUGET_PACKAGES].update_one(
        {'repo_id': repo_id, 'name': name},
        {'$set': {**fields, **name_keys(name), 'updated_at': now}, '$setOnInsert': {**insert, 'created_at': now}},
        upsert=True,
    )


async def delete_package(db: Database, repo_id: str, name: str) -> None:
    await db[NUGET_PACKAGES].delete_one({'repo_id': repo_id, 'name': name})


async def get_package(db: Database, repo_id: str, name: str) -> dict[str, Any] | None:
    return await db[NUGET_PACKAGES].find_one({'repo_id': repo_id, 'name': name}, {'_id': 0})


async def page(db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` packages by (lower-case) id after ``after``; ``q``: a case-insensitive substring."""
    name: dict[str, Any] = {}
    if after is not None:
        name['$gt'] = after
    if q:
        name['$regex'] = re.escape(q.lower())
    query: dict[str, Any] = {'repo_id': repo_id}
    if name:
        query['name'] = name
    return await db[NUGET_PACKAGES].find(query, {'_id': 0}).sort('name', 1).limit(limit).to_list()


async def scan(db: Database, repo_id: str, *, name_is: str | None = None) -> list[dict[str, Any]]:
    """A hosted repository's package summaries (search filters them), by id; bounded."""
    query: dict[str, Any] = {'repo_id': repo_id}
    if name_is is not None:
        query['name'] = name_is
    cursor = db[NUGET_PACKAGES].find(query, {'_id': 0, 'versions': 0}).sort('name', 1).limit(MAX_SCAN)
    return await cursor.to_list()


# ---------------------------------------------------------------------------- hosted packages (assets)
async def package_files(db: Database, repo_id: str, package_id: str) -> list[dict[str, Any]]:
    """The ``.nupkg`` assets of a package with their details, by path."""
    query = {
        'repo_id': repo_id,
        'path': _range(package_prefix(package_id)),
        'nuget': {'$exists': True},
    }
    return await db[ASSETS].find(query, PACKAGE_FIELDS).sort('path', 1).limit(MAX_FILES).to_list()


async def set_details(db: Database, repo_id: str, path: str, details: Mapping[str, Any]) -> None:
    """Record the NuGet details of a hosted package (``assets.nuget``)."""
    await db[ASSETS].update_one({'repo_id': repo_id, 'path': path}, {'$set': {'nuget': dict(details)}})


async def set_listed(db: Database, repo_id: str, path: str, listed: bool) -> bool:
    """Unlist or relist a hosted package; False when there is no such package."""
    result = await db[ASSETS].update_one(
        {'repo_id': repo_id, 'path': path, 'nuget': {'$exists': True}}, {'$set': {'nuget.listed': listed}}
    )
    return bool(result.matched_count)
