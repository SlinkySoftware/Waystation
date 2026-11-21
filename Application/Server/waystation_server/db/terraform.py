"""Terraform records (DATA_MODEL):

- ``terraform_providers``: one summary per provider address a Terraform mirror has cached something of, written in
  the background (the latest version and version count from the registry's version list, and the ``h1:`` hash of
  every archive it cached); in the hosted repository one per ``<namespace>/<type>`` with its uploaded releases.
- ``terraform_modules`` (T-P5-TFM): one document per module of a repository: a proxy's cached version list and
  archives (sha256 pinned at the first fetch), the hosted repository's uploaded versions.
- Range reads of a repository's files in ``assets`` (``<hostname>/<namespace>/<type>/...``, ``v1/...``).

Queries use ``repo_name_unique`` (``terraform_providers``, ``terraform_modules``) and ranges on ``repo_path_unique``
(``assets``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, TERRAFORM_MODULES, TERRAFORM_PROVIDERS
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


async def page(
    db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int, releases: bool = False
) -> list[dict[str, Any]]:
    """Up to ``limit`` providers by address after ``after``; ``q``: a case-insensitive substring; ``releases``: with
    the hosted repository's releases."""
    name: dict[str, Any] = {}
    if after is not None:
        name['$gt'] = after
    if q:
        name['$regex'] = re.escape(q)
        name['$options'] = 'i'
    query: dict[str, Any] = {'repo_id': repo_id}
    if name:
        query['name'] = name
    projection: dict[str, int] = {'_id': 0, 'archives': 0} if releases else {'_id': 0, 'archives': 0, 'releases': 0}
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


async def files_under(db: Database, repo_id: str, prefix: str) -> list[dict[str, Any]]:
    """The repository's files below ``prefix`` (ending with ``/``), by path (``repo_path_unique`` range)."""
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': _range(prefix)}, FILE_FIELDS).sort('path', 1)
    return await cursor.limit(MAX_FILES).to_list()


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


# ---------------------------------------------------------------------------- modules (T-P5-TFM)
def _module_identity(name: str) -> dict[str, Any]:
    namespace, module, system = name.split('/')
    return {'namespace': namespace, 'module': module, 'system': system, **name_keys(name)}


async def record_module_versions(db: Database, repo_id: str, name: str, latest: str | None, count: int) -> None:
    """A proxy cached the origin's version list of the module ``name`` (``<namespace>/<name>/<system>``)."""
    now = datetime.now(UTC)
    await db[TERRAFORM_MODULES].update_one(
        {'repo_id': repo_id, 'name': name},
        {
            '$set': {'latest': latest, 'version_count': count, **_module_identity(name), 'updated_at': now},
            '$setOnInsert': {'created_at': now, 'versions': []},
        },
        upsert=True,
    )


async def record_module_archive(db: Database, repo_id: str, name: str, entry: dict[str, Any]) -> None:
    """A proxy cached a module version's archive (``entry``: version, filename, sha256, size, source), replacing an
    earlier entry for the version."""
    now = datetime.now(UTC)
    version = entry['version']
    identity = {key: {'$literal': value} for key, value in _module_identity(name).items()}
    await db[TERRAFORM_MODULES].update_one(
        {'repo_id': repo_id, 'name': name},
        [
            {
                '$set': {
                    **identity,
                    'updated_at': now,
                    'created_at': {'$ifNull': ['$created_at', now]},
                    'latest': {'$ifNull': ['$latest', {'$literal': version}]},
                    'version_count': {'$ifNull': ['$version_count', 0]},
                    'versions': {
                        '$concatArrays': [
                            {
                                '$filter': {
                                    'input': {'$ifNull': ['$versions', []]},
                                    'cond': {'$ne': ['$$this.version', version]},
                                }
                            },
                            [{'$literal': {**entry, 'created_at': now}}],
                        ]
                    },
                }
            }
        ],
        upsert=True,
    )


async def add_module_version(db: Database, repo_id: str, name: str, entry: dict[str, Any]) -> bool:
    """Add an uploaded version to a hosted module; False when the version exists (versions are immutable)."""
    now = datetime.now(UTC)
    try:
        await db[TERRAFORM_MODULES].update_one(
            {'repo_id': repo_id, 'name': name, 'versions.version': {'$ne': entry['version']}},
            {
                '$push': {'versions': entry},
                '$set': {**_module_identity(name), 'updated_at': now},
                '$setOnInsert': {'created_at': now},
            },
            upsert=True,
        )
    except DuplicateKeyError:
        return False
    return True


async def remove_module_version(db: Database, repo_id: str, name: str, version: str) -> dict[str, Any] | None:
    """Remove a hosted module version: the removed entry (None: there was none). A module left without versions goes."""
    doc = await db[TERRAFORM_MODULES].find_one_and_update(
        {'repo_id': repo_id, 'name': name, 'versions.version': version},
        {'$pull': {'versions': {'version': version}}, '$set': {'updated_at': datetime.now(UTC)}},
        projection={'versions': 1, '_id': 0},
    )
    if doc is None:
        return None
    await db[TERRAFORM_MODULES].delete_one({'repo_id': repo_id, 'name': name, 'versions': {'$size': 0}})
    return next((v for v in doc.get('versions') or [] if v.get('version') == version), None)


async def set_module_summary(db: Database, repo_id: str, name: str, *, latest: str | None, count: int) -> None:
    """Keep a hosted module's ``latest`` and ``version_count`` (browse, search) in step with its versions."""
    await db[TERRAFORM_MODULES].update_one(
        {'repo_id': repo_id, 'name': name}, {'$set': {'latest': latest, 'version_count': count}}
    )


async def module_archive_sha256(db: Database, repo_id: str, name: str, version: str) -> str | None:
    """The sha256 recorded when a proxy first cached a module version's archive (pinned), else None."""
    doc = await db[TERRAFORM_MODULES].find_one({'repo_id': repo_id, 'name': name}, {'versions': 1, '_id': 0})
    for entry in (doc or {}).get('versions') or []:
        if entry.get('version') == version and entry.get('sha256'):
            return str(entry['sha256'])
    return None


async def get_module(db: Database, repo_id: str, name: str) -> dict[str, Any] | None:
    return await db[TERRAFORM_MODULES].find_one({'repo_id': repo_id, 'name': name}, {'_id': 0})


async def module_page(
    db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int
) -> list[dict[str, Any]]:
    """Up to ``limit`` modules by address after ``after``; ``q``: a case-insensitive substring."""
    name: dict[str, Any] = {}
    if after is not None:
        name['$gt'] = after
    if q:
        name['$regex'] = re.escape(q)
        name['$options'] = 'i'
    query: dict[str, Any] = {'repo_id': repo_id}
    if name:
        query['name'] = name
    return await db[TERRAFORM_MODULES].find(query, {'_id': 0}).sort('name', 1).limit(limit).to_list()


# ---------------------------------------------------------------------------- hosted providers (T-P5-TFM)
def _hosted_identity(name: str) -> dict[str, Any]:
    namespace, kind = name.split('/')
    return {'hostname': '', 'namespace': namespace, 'type': kind, **name_keys(name)}


async def add_provider_release(db: Database, repo_id: str, name: str, release: dict[str, Any]) -> bool:
    """Add an uploaded release (one version, every platform) to a hosted provider (``<namespace>/<type>``); False
    when the version exists (releases are immutable)."""
    now = datetime.now(UTC)
    try:
        await db[TERRAFORM_PROVIDERS].update_one(
            {'repo_id': repo_id, 'name': name, 'releases.version': {'$ne': release['version']}},
            {
                '$push': {'releases': release},
                '$set': {**_hosted_identity(name), 'updated_at': now},
                '$setOnInsert': {'created_at': now, 'archives': []},
            },
            upsert=True,
        )
    except DuplicateKeyError:
        return False
    return True


async def remove_provider_release(db: Database, repo_id: str, name: str, version: str) -> dict[str, Any] | None:
    """Remove a hosted provider release: the removed entry (None: there was none). A provider left without releases
    goes."""
    doc = await db[TERRAFORM_PROVIDERS].find_one_and_update(
        {'repo_id': repo_id, 'name': name, 'releases.version': version},
        {'$pull': {'releases': {'version': version}}, '$set': {'updated_at': datetime.now(UTC)}},
        projection={'releases': 1, '_id': 0},
    )
    if doc is None:
        return None
    await db[TERRAFORM_PROVIDERS].delete_one({'repo_id': repo_id, 'name': name, 'releases': {'$size': 0}})
    return next((r for r in doc.get('releases') or [] if r.get('version') == version), None)


async def set_provider_summary(db: Database, repo_id: str, name: str, *, latest: str | None, count: int) -> None:
    await db[TERRAFORM_PROVIDERS].update_one(
        {'repo_id': repo_id, 'name': name}, {'$set': {'latest': latest, 'version_count': count}}
    )
