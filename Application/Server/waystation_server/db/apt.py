"""``apt_pool_index`` access (DATA_MODEL; FORMATS.md "apt"): ``Filename → (SHA256, Size)`` per proxy repository.

Entries come from two places:

- **Listings** (``verified: true``): every stanza of a ``Packages`` file the apt proxy cached, written in bulk when the
  index is parsed. The hash is the one the signed ``Release`` chain vouches for; pool files are validated against it.
- **Unverified fetches** (``verified: false``): a pool file served before any cached ``Packages`` listed it; the entry
  holds the hash Waystation computed. The next listing that names the file replaces it, and if the hashes differ the
  cached copy is dropped so the next request fetches (and validates) it again.

Lookups use ``repo_filename_unique``; listing writes are unordered bulk upserts in batches. Only written on cache fills,
never per cache hit (ARCHITECTURE §8).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database
from waystation_common.db.collections import APT_POOL_INDEX
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
