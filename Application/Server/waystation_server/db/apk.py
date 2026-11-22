"""``apk_packages`` access (DATA_MODEL; FORMATS.md "Alpine apk"): package path → index checksum per repository.

Entries come from three places:

- **Listings** (``verified: true``): every stanza of an ``APKINDEX.tar.gz`` the apk proxy cached, written in bulk when
  it is parsed. ``dir`` is the index's directory (``<branch>/<repository>/<arch>``), ``path`` the package's path
  (``dir`` + ``<name>-<version>.apk``), ``checksum`` the ``C:`` value (``Q1`` + base64 SHA-1 of the control segment)
  and ``size`` the ``S:`` value. ``gen`` is the digest of the index the entry came from: indexing a newer index of the
  same directory replaces the older generation's entries.
- **Unverified fetches** (``verified: false``): a package served before any cached index listed it; the entry holds
  the checksum and size Waystation computed from the file. The next listing that names the file replaces it; when it
  disagrees, the cached copy is dropped so the next request fetches and verifies it.
- **Hosted uploads** (``gen: 'hosted'``; ``waystation_common.db.apk``), written in the transaction that also puts the
  ``index.apk`` job in the outbox.

Only written on cache fills and uploads, never per cache hit (ARCHITECTURE §8).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pymongo import DESCENDING, UpdateOne
from pymongo.errors import DuplicateKeyError

from waystation_common.db.apk import HOSTED_GEN
from waystation_common.db.client import Database, Session
from waystation_common.db.collections import APK_PACKAGES
from waystation_common.searchkeys import name_keys
from waystation_server.db import assets as assets_db

BATCH = 1000


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class PackageRecord:
    path: str
    checksum: str
    size: int | None
    verified: bool


@dataclass(frozen=True)
class Listing:
    path: str
    checksum: str
    size: int | None
    name: str
    version: str
    arch: str
    version_sort: str
    details: dict[str, Any] = field(default_factory=dict)  # description, origin, license, url, build_time...


def _record(doc: dict[str, Any]) -> PackageRecord:
    size = doc.get('size')
    return PackageRecord(
        path=str(doc['path']),
        checksum=str(doc.get('checksum') or ''),
        size=int(size) if isinstance(size, int) else None,
        verified=bool(doc.get('verified')),
    )


async def lookup(db: Database, repo_id: str, path: str) -> PackageRecord | None:
    doc = await db[APK_PACKAGES].find_one({'repo_id': repo_id, 'path': path}, {'_id': 0, 'stanza': 0})
    return _record(doc) if doc else None


async def indexed(db: Database, repo_id: str, directory: str, gen: str) -> bool:
    """Whether the index with digest ``gen`` of ``directory`` has been indexed."""
    doc = await db[APK_PACKAGES].find_one({'repo_id': repo_id, 'dir': directory, 'gen': gen}, {'_id': 1})
    return doc is not None


def _matches(listing: Listing, doc: dict[str, Any]) -> bool:
    return listing.checksum == doc.get('checksum') and (listing.size is None or listing.size == doc.get('size'))


async def record_index(
    db: Database, repo_id: str, directory: str, entries: Sequence[Listing], *, gen: str
) -> list[str]:
    """Make ``entries`` the listing of ``directory`` (generation ``gen``): upsert them as verified, drop the entries
    of older generations. Returns the paths whose earlier unverified record disagreed with the listing (their cached
    copies have been dropped)."""
    listed = {e.path: e for e in entries}
    mismatched: list[str] = []
    if listed:
        prefix = f'^{re.escape(directory)}/'
        async for doc in db[APK_PACKAGES].find(
            {'repo_id': repo_id, 'verified': False, 'path': {'$regex': prefix}},
            {'path': 1, 'checksum': 1, 'size': 1, '_id': 0},
        ):
            entry = listed.get(str(doc['path']))
            if entry is not None and not _matches(entry, doc):
                mismatched.append(entry.path)
    stamp = now()
    ops = [
        UpdateOne(
            {'repo_id': repo_id, 'path': e.path},
            {
                '$set': {
                    'checksum': e.checksum,
                    'size': e.size,
                    'verified': True,
                    'dir': directory,
                    'name': e.name,
                    **name_keys(e.name),
                    'version': e.version,
                    'arch': e.arch,
                    'version_sort': e.version_sort,
                    **e.details,
                    'gen': gen,
                    'updated_at': stamp,
                },
            },
            upsert=True,
        )
        for e in listed.values()
    ]
    for start in range(0, len(ops), BATCH):
        await db[APK_PACKAGES].bulk_write(ops[start : start + BATCH], ordered=False)
    await db[APK_PACKAGES].delete_many(
        {'repo_id': repo_id, 'dir': directory, 'gen': {'$nin': [gen, None, HOSTED_GEN]}, 'verified': True}
    )
    for path in mismatched:
        await drop_cached(db, repo_id, path)
    return mismatched


async def record_unverified(db: Database, repo_id: str, path: str, checksum: str, size: int) -> PackageRecord | None:
    """Remember the computed checksum of a package no cached index listed. Returns the existing *verified* record
    when a listing landed in the meantime (the caller compares), else None."""
    collection = db[APK_PACKAGES]
    fields = {'checksum': checksum, 'size': size, 'verified': False, 'updated_at': now()}
    result = await collection.update_one({'repo_id': repo_id, 'path': path, 'verified': False}, {'$set': fields})
    if result.matched_count:
        return None
    try:
        await collection.insert_one({'repo_id': repo_id, 'path': path, 'dir': None, 'gen': None, **fields})
    except DuplicateKeyError:  # a verified listing exists
        doc = await collection.find_one({'repo_id': repo_id, 'path': path})
        return _record(doc) if doc else None
    return None


async def drop_cached(db: Database, repo_id: str, path: str) -> None:
    """Forget the cached copy of a file (it failed validation against a listing); the blob is left to GC."""
    asset = await assets_db.get(db, repo_id, path)
    if asset is not None:
        await assets_db.delete(db, asset.id)


async def listed_in(db: Database, repo_id: str, directory: str, name: str) -> list[dict[str, Any]]:
    """The entries of ``name`` in ``directory`` (prefetch), newest first."""
    cursor = (
        db[APK_PACKAGES]
        .find({'repo_id': repo_id, 'dir': directory, 'name': name, 'verified': True}, {'_id': 0, 'stanza': 0})
        .sort([('version_sort', DESCENDING)])
        .limit(50)
    )
    return [doc async for doc in cursor]


# ---------------------------------------------------------------------------- browse
async def package_names(
    db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int, name_is: str | None = None
) -> list[dict[str, Any]]:
    """Up to ``limit`` package names after ``after`` (``q``: case-insensitive substring; ``name_is``: one name),
    each with its newest entry (``latest``), its architectures and its number of distinct versions. Only listed
    packages (unverified fetches without a listing have no name)."""
    name: dict[str, Any] = {'$eq': name_is} if name_is is not None else {'$gt': ''}
    if after is not None:
        name['$gt'] = after
    if q:
        name['$regex'] = re.escape(q)
        name['$options'] = 'i'
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id, 'name': name}},
        {'$sort': {'name': 1, 'version_sort': DESCENDING}},
        {
            '$group': {
                '_id': '$name',
                'latest': {'$first': '$$ROOT'},
                'arches': {'$addToSet': '$arch'},
                'versions': {'$addToSet': '$version_sort'},
            }
        },
        {'$sort': {'_id': 1}},
        {'$limit': limit},
    ]
    out: list[dict[str, Any]] = []
    async for doc in await db[APK_PACKAGES].aggregate(pipeline):
        latest = doc['latest']
        latest.pop('_id', None)
        latest.pop('stanza', None)
        out.append(
            {
                'name': doc['_id'],
                'latest': latest,
                'arches': sorted(a for a in doc['arches'] if a),
                'version_count': len(doc['versions']),
            }
        )
    return out


async def package_versions(
    db: Database, repo_id: str, name: str, *, after: tuple[str, str] | None, limit: int
) -> list[dict[str, Any]]:
    """Every entry of one package name, newest first, then by path (``after``: the last ``(version_sort, path)`` of
    the previous page)."""
    query: dict[str, Any] = {'repo_id': repo_id, 'name': name}
    if after is not None:
        query['$or'] = [
            {'version_sort': {'$lt': after[0]}},
            {'version_sort': after[0], 'path': {'$gt': after[1]}},
        ]
    cursor = (
        db[APK_PACKAGES]
        .find(query, {'_id': 0, 'stanza': 0})
        .sort([('version_sort', DESCENDING), ('path', 1)])
        .limit(limit)
    )
    return [doc async for doc in cursor]


# ---------------------------------------------------------------------------- hosted repositories
async def hosted_by_path(db: Database, repo_id: str, path: str) -> dict[str, Any] | None:
    return await db[APK_PACKAGES].find_one({'repo_id': repo_id, 'path': path, 'gen': HOSTED_GEN}, {'_id': 0})


async def save_hosted(db: Database, repo_id: str, record: dict[str, Any], *, session: Session | None = None) -> None:
    """Insert or replace an uploaded package's record (``record`` holds every field but ``repo_id``; keyed by path)."""
    fields = {
        **record,
        **name_keys(str(record['name'])),
        'repo_id': repo_id,
        'gen': HOSTED_GEN,
        'verified': True,
        'updated_at': now(),
    }
    await db[APK_PACKAGES].update_one(
        {'repo_id': repo_id, 'path': record['path']}, {'$set': fields}, upsert=True, session=session
    )


async def delete_hosted(db: Database, repo_id: str, path: str, *, session: Session | None = None) -> None:
    await db[APK_PACKAGES].delete_one({'repo_id': repo_id, 'path': path, 'gen': HOSTED_GEN}, session=session)
