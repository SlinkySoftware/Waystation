"""``apt_pool_index`` access (DATA_MODEL; FORMATS.md "apt"): ``Filename → (SHA256, Size)`` per proxy repository.

Entries come from two places:

- **Listings** (``verified: true``): every stanza of a ``Packages`` file the apt proxy cached, written in bulk when the
  index is parsed. The hash is the one the signed ``Release`` chain vouches for; pool files are validated against it.
- **Unverified fetches** (``verified: false``): a pool file served before any cached ``Packages`` listed it; the entry
  holds the hash Waystation computed. The next listing that names the file replaces it, and if the hashes differ the
  cached copy is dropped so the next request fetches (and validates) it again.

Lookups use ``repo_filename_unique``; listing writes are unordered bulk upserts in batches. Only written on cache fills,
never per cache hit (ARCHITECTURE §8).

``known_suites`` tells the apt proxy which suite directories (``dists/bookworm/mongodb-org/8.0``) have a cached
``Release`` (``assets``, ``repo_path_unique``), so an index path is split at the right suite.

``apt_packages`` (browse) holds one document per stanza of every cached ``Packages`` index: ``(suite, component,
arch)`` identify the index, ``name``/``version``/``architecture``/``section`` come from the stanza. Each index's
documents are replaced when a new generation of it is indexed (``gen`` = the index blob's sha256). Pages are read
through ``repo_suite_name``; "is it cached" joins ``assets`` (``repo_path_unique``) the other way round through
``filename_repo``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database
from waystation_common.db.collections import APT_PACKAGES, APT_POOL_INDEX, ASSETS
from waystation_common.db.retry import read_retry
from waystation_common.searchkeys import name_keys
from waystation_server.db import assets as assets_db

BATCH = 1000


@dataclass(frozen=True)
class PoolRecord:
    filename: str
    sha256: str
    size: int
    verified: bool


@dataclass(frozen=True)
class Listing:
    filename: str
    sha256: str
    size: int


def _record(doc: dict[str, object]) -> PoolRecord:
    return PoolRecord(
        filename=str(doc['filename']),
        sha256=str(doc['sha256']),
        size=int(str(doc['size'])),
        verified=bool(doc.get('verified')),
    )


async def lookup(db: Database, repo_id: str, filename: str) -> PoolRecord | None:
    doc = await db[APT_POOL_INDEX].find_one({'repo_id': repo_id, 'filename': filename})
    return _record(doc) if doc else None


@read_retry  # on the request path of every index request with a choice of suite
async def known_suites(db: Database, repo_id: str, suite_dirs: Sequence[str]) -> set[str]:
    """The directories among ``suite_dirs`` whose ``InRelease`` or ``Release`` is cached (``repo_path_unique``)."""
    paths = [f'{d}/{name}' for d in suite_dirs for name in ('InRelease', 'Release')]
    if not paths:
        return set()
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$in': paths}}, {'path': 1, '_id': 0})
    return {str(doc['path']).rpartition('/')[0] async for doc in cursor}


async def record_listing(
    db: Database,
    repo_id: str,
    entries: Sequence[Listing],
    *,
    suite: str,
    component: str,
    arch: str,
) -> list[str]:
    """Upsert the entries of one ``Packages`` file as verified. Returns the filenames whose earlier unverified hash
    differs from the listing (their cached copies have been dropped)."""
    if not entries:
        return []
    listed = {e.filename: e for e in entries}
    mismatched: list[str] = []
    async for doc in db[APT_POOL_INDEX].find(
        {'repo_id': repo_id, 'verified': False}, {'filename': 1, 'sha256': 1, '_id': 0}
    ):
        entry = listed.get(str(doc['filename']))
        if entry is not None and entry.sha256 != doc['sha256']:
            mismatched.append(entry.filename)
    now = datetime.now(UTC)
    ops = [
        UpdateOne(
            {'repo_id': repo_id, 'filename': e.filename},
            {
                '$set': {
                    'sha256': e.sha256,
                    'size': e.size,
                    'verified': True,
                    'suite': suite,
                    'component': component,
                    'arch': arch,
                    'updated_at': now,
                },
            },
            upsert=True,
        )
        for e in listed.values()
    ]
    for start in range(0, len(ops), BATCH):
        await db[APT_POOL_INDEX].bulk_write(ops[start : start + BATCH], ordered=False)
    for filename in mismatched:
        await drop_cached(db, repo_id, filename)
    return mismatched


async def record_unverified(db: Database, repo_id: str, filename: str, sha256: str, size: int) -> PoolRecord | None:
    """Remember the computed hash of a pool file no cached listing named. Returns the existing *verified* record
    when a listing landed in the meantime (the caller compares hashes), else None."""
    collection = db[APT_POOL_INDEX]
    now = datetime.now(UTC)
    fields = {'sha256': sha256, 'size': size, 'verified': False, 'updated_at': now}
    result = await collection.update_one(
        {'repo_id': repo_id, 'filename': filename, 'verified': False}, {'$set': fields}
    )
    if result.matched_count:
        return None
    try:
        await collection.insert_one(
            {'repo_id': repo_id, 'filename': filename, 'suite': None, 'component': None, 'arch': None, **fields}
        )
    except DuplicateKeyError:  # a verified listing exists
        doc = await collection.find_one({'repo_id': repo_id, 'filename': filename})
        return _record(doc) if doc else None
    return None


async def drop_cached(db: Database, repo_id: str, filename: str) -> None:
    """Forget the cached copy of a pool file (it failed validation against a listing); the blob is left to GC."""
    asset = await assets_db.get(db, repo_id, filename)
    if asset is not None:
        await assets_db.delete(db, asset.id)


# ---------------------------------------------------------------------------- apt_packages (browse)
PACKAGE_SORT: tuple[str, ...] = ('name', 'filename', 'arch', 'component')


@dataclass(frozen=True)
class Stanza:
    filename: str
    sha256: str
    size: int
    name: str
    version: str
    architecture: str
    section: str


async def record_packages(
    db: Database, repo_id: str, stanzas: Sequence[Stanza], *, suite: str, component: str, arch: str, gen: str
) -> None:
    """Make ``stanzas`` the listing of the index ``(suite, component, arch)``: upsert them, drop older entries."""
    listing = {'repo_id': repo_id, 'suite': suite, 'component': component, 'arch': arch}
    now = datetime.now(UTC)
    ops = [
        UpdateOne(
            {**listing, 'filename': s.filename},
            {
                '$set': {
                    'name': s.name,
                    **name_keys(s.name),
                    'version': s.version,
                    'architecture': s.architecture,
                    'section': s.section or None,
                    'sha256': s.sha256,
                    'size': s.size,
                    'gen': gen,
                    'updated_at': now,
                }
            },
            upsert=True,
        )
        for s in {s.filename: s for s in stanzas}.values()
    ]
    for start in range(0, len(ops), BATCH):
        await db[APT_PACKAGES].bulk_write(ops[start : start + BATCH], ordered=False)
    await db[APT_PACKAGES].delete_many({**listing, 'gen': {'$ne': gen}})


def _after(values: Sequence[Any]) -> dict[str, Any]:
    """Keyset condition: strictly after ``values`` in ``PACKAGE_SORT`` order."""
    branches = []
    for i, key in enumerate(PACKAGE_SORT):
        branch: dict[str, Any] = {PACKAGE_SORT[j]: values[j] for j in range(i)}
        branch[key] = {'$gt': values[i]}
        branches.append(branch)
    return {'$or': branches}


def _listing_filter(
    repo_id: str, suite: str, component: str | None, arch: str | None, name_regex: str | None
) -> dict[str, Any]:
    query: dict[str, Any] = {'repo_id': repo_id, 'suite': suite}
    if arch is not None:
        query['arch'] = arch
    if component is not None:
        query['component'] = component
    if name_regex is not None:
        query['name'] = {'$regex': name_regex, '$options': 'i'}
    return query


async def package_page(
    db: Database,
    repo_id: str,
    *,
    suite: str,
    component: str | None,
    arch: str | None,
    name_regex: str | None,
    after: Sequence[Any] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """A page of a suite's listing, by ``PACKAGE_SORT``."""
    query = _listing_filter(repo_id, suite, component, arch, name_regex)
    if after is not None:
        query = {'$and': [query, _after(after)]}
    return await db[APT_PACKAGES].find(query, {'_id': 0}).sort([(k, 1) for k in PACKAGE_SORT]).limit(limit).to_list()


async def cached_package_page(
    db: Database,
    repo_id: str,
    *,
    suite: str,
    component: str | None,
    arch: str | None,
    name_regex: str | None,
    after: Sequence[Any] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Like ``package_page`` but only entries whose file is cached: walks the repository's assets (what is cached is
    usually far less than what is listed) and joins their listings."""
    listing = _listing_filter(repo_id, suite, component, arch, None)
    match: dict[str, Any] = {}
    if name_regex is not None:
        match['name'] = {'$regex': name_regex, '$options': 'i'}
    if after is not None:
        match = {'$and': [match, _after(after)]} if match else _after(after)
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id}},
        {'$project': {'path': 1, '_id': 0}},
        {
            '$lookup': {
                'from': APT_PACKAGES,
                'localField': 'path',
                'foreignField': 'filename',
                'pipeline': [{'$match': listing}, {'$project': {'_id': 0}}],
                'as': 'listed',
            }
        },
        {'$unwind': '$listed'},
        {'$replaceRoot': {'newRoot': '$listed'}},
    ]
    if match:
        pipeline.append({'$match': match})
    pipeline += [{'$sort': dict.fromkeys(PACKAGE_SORT, 1)}, {'$limit': limit}]
    return [doc async for doc in await db[ASSETS].aggregate(pipeline, allowDiskUse=True)]


async def suite_count(db: Database, repo_id: str, suite: str) -> int:
    return await db[APT_PACKAGES].count_documents({'repo_id': repo_id, 'suite': suite})


async def cached_counts(db: Database, repo_id: str) -> dict[str, int]:
    """Listing entries whose file is cached, per suite."""
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id}},
        {'$project': {'path': 1, '_id': 0}},
        {
            '$lookup': {
                'from': APT_PACKAGES,
                'localField': 'path',
                'foreignField': 'filename',
                'pipeline': [{'$match': {'repo_id': repo_id}}, {'$project': {'suite': 1, '_id': 0}}],
                'as': 'listed',
            }
        },
        {'$unwind': '$listed'},
        {'$group': {'_id': '$listed.suite', 'count': {'$sum': 1}}},
    ]
    return {str(doc['_id']): int(doc['count']) async for doc in await db[ASSETS].aggregate(pipeline)}
