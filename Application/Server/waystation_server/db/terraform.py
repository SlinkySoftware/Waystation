"""Terraform provider records (DATA_MODEL): ``terraform_providers`` (one summary per provider address a Terraform
mirror has cached something of, written in the background: the latest version and version count from the registry's
version list, and the ``h1:`` hash of every archive it cached) and range reads of a provider's cached files in
``assets`` (``<hostname>/<namespace>/<type>/...``).

Queries use ``repo_name_unique`` (``terraform_providers``) and ranges on ``repo_path_unique`` (``assets``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, TERRAFORM_PROVIDERS
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
_ZIP = re.compile(r'\.zip$')


def _range(prefix: str) -> dict[str, str]:
    return {'$gte': prefix, '$lt': prefix[:-1] + '0'}  # '/' + 1 = '0': the subtree of prefix


def _identity(name: str) -> dict[str, Any]:
    hostname, namespace, kind = name.split('/')
    return {'hostname': hostname, 'namespace': namespace, 'type': kind, **name_keys(name)}


async def record_versions(db: Database, repo_id: str, name: str, latest: str | None, version_count: int) -> None:
    """A registry version list was cached for the provider ``name`` (``<hostname>/<namespace>/<type>``)."""
    now = datetime.now(UTC)
    await db[TERRAFORM_PROVIDERS].update_one(
        {'repo_id': repo_id, 'name': name},
        {
            '$set': {'latest': latest, 'version_count': version_count, **_identity(name), 'updated_at': now},
            '$setOnInsert': {'created_at': now, 'archives': []},
        },
        upsert=True,
    )


async def record_archive(
    db: Database,
    repo_id: str,
    name: str,
    *,
    version: str,
    os: str,
    arch: str,
    filename: str,
    sha256: str,
    h1: str | None,
) -> None:
    """An archive was cached (verified): remember its hashes (replacing an earlier entry for the platform)."""
    now = datetime.now(UTC)
    entry = {'version': version, 'os': os, 'arch': arch, 'filename': filename, 'sha256': sha256, 'h1': h1}
    same = {
        '$and': [
            {'$eq': ['$$this.version', version]},
            {'$eq': ['$$this.os', os]},
            {'$eq': ['$$this.arch', arch]},
        ]
    }
    identity = {key: {'$literal': value} for key, value in _identity(name).items()}
    await db[TERRAFORM_PROVIDERS].update_one(
        {'repo_id': repo_id, 'name': name},
        [
            {
                '$set': {
                    **identity,
                    'updated_at': now,
                    'created_at': {'$ifNull': ['$created_at', now]},
                    'latest': {'$ifNull': ['$latest', {'$literal': version}]},
                    'version_count': {'$ifNull': ['$version_count', 0]},
                    'archives': {
                        '$concatArrays': [
                            {'$filter': {'input': {'$ifNull': ['$archives', []]}, 'cond': {'$not': [same]}}},
                            [{'$literal': entry}],
                        ]
                    },
                }
            }
        ],
        upsert=True,
    )


async def get_provider(db: Database, repo_id: str, name: str) -> dict[str, Any] | None:
    return await db[TERRAFORM_PROVIDERS].find_one({'repo_id': repo_id, 'name': name}, {'_id': 0})


async def h1_hashes(db: Database, repo_id: str, name: str, version: str) -> dict[str, str]:
    """``<os>_<arch>`` -> ``h1:...`` of the version's archives this repository has cached."""
    doc = await db[TERRAFORM_PROVIDERS].find_one({'repo_id': repo_id, 'name': name}, {'archives': 1, '_id': 0})
    out: dict[str, str] = {}
    for entry in (doc or {}).get('archives') or []:
        if entry.get('version') == version and entry.get('h1'):
            out[f'{entry["os"]}_{entry["arch"]}'] = str(entry['h1'])
    return out


async def page(db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` providers by address after ``after``; ``q``: a case-insensitive substring."""
    name: dict[str, Any] = {}
    if after is not None:
        name['$gt'] = after
    if q:
        name['$regex'] = re.escape(q)
        name['$options'] = 'i'
    query: dict[str, Any] = {'repo_id': repo_id}
    if name:
        query['name'] = name
    projection = {'_id': 0, 'archives': 0}
    return await db[TERRAFORM_PROVIDERS].find(query, projection).sort('name', 1).limit(limit).to_list()


# ---------------------------------------------------------------------------- cached files (assets)
async def provider_archives(db: Database, repo_id: str, name: str) -> list[dict[str, Any]]:
    """The cached archives (``.zip`` assets) under ``<name>/``, by path."""
    cursor = (
        db[ASSETS]
        .find({'repo_id': repo_id, 'path': {**_range(f'{name}/'), '$regex': _ZIP}}, FILE_FIELDS)
        .sort('path', 1)
        .limit(MAX_FILES)
    )
    return await cursor.to_list()


@dataclass(frozen=True)
class ArchiveStats:
    count: int
    size: int


async def archive_stats(db: Database, repo_id: str, names: Iterable[str]) -> dict[str, ArchiveStats]:
    """Archives held per provider: count and bytes."""
    ranges = [{'path': _range(f'{n}/')} for n in sorted(set(names))]
    if not ranges:
        return {}
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id, '$or': ranges}},
        {'$match': {'path': _ZIP}},
        {
            '$group': {
                '_id': {
                    '$reduce': {
                        'input': {'$slice': [{'$split': ['$path', '/']}, 3]},
                        'initialValue': '',
                        'in': {'$cond': [{'$eq': ['$$value', '']}, '$$this', {'$concat': ['$$value', '/', '$$this']}]},
                    }
                },
                'count': {'$sum': 1},
                'size': {'$sum': '$size'},
            }
        },
    ]
    out: dict[str, ArchiveStats] = {}
    async for doc in await db[ASSETS].aggregate(pipeline):
        out[str(doc['_id'])] = ArchiveStats(count=int(doc['count']), size=int(doc['size']))
    return out
