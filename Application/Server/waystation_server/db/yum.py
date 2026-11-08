"""``yum_packages`` access (DATA_MODEL; FORMATS.md "yum"): package path → checksum per proxy repository.

Entries come from two places:

- **Listings** (``verified: true``): every ``<package>`` of a ``primary.xml`` the yum proxy cached, written in bulk
  when it is parsed. ``repo_dir`` is the yum repository directory (the one holding ``repodata/``), ``path`` the
  package's repository path (``repo_dir`` + ``location href``), ``algorithm``/``digest``/``size`` what the listing
  says; ``name``/``epoch``/``version``/``release``/``arch`` describe the package. ``gen`` is the digest of the
  ``primary`` file the entry came from: indexing a newer ``primary`` of the same directory replaces the older
  generation's entries.
- **Unverified fetches** (``verified: false``): a package served before any cached ``primary`` listed it; the entry
  holds the SHA-256 Waystation computed. The next listing that names the file replaces it; when it disagrees (or uses
  another algorithm, so the two cannot be compared) the cached copy is dropped so the next request fetches and
  validates it.

Lookups use ``repo_path_unique``; listing writes are unordered bulk upserts in batches. Only written on cache fills,
never per cache hit (ARCHITECTURE §8).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database
from waystation_common.db.collections import YUM_PACKAGES
from waystation_server.db import assets as assets_db

BATCH = 1000


@dataclass(frozen=True)
class PackageRecord:
    path: str
    algorithm: str  # sha1 / sha256 / sha512
    digest: str
    size: int
    verified: bool


@dataclass(frozen=True)
class Listing:
    path: str
    algorithm: str
    digest: str
    size: int
    name: str = ''
    epoch: str = '0'
    version: str = ''
    release: str = ''
    arch: str = ''


def _record(doc: dict[str, object]) -> PackageRecord:
    return PackageRecord(
        path=str(doc['path']),
        algorithm=str(doc['algorithm']),
        digest=str(doc['digest']),
        size=int(str(doc['size'])),
        verified=bool(doc.get('verified')),
    )


async def lookup(db: Database, repo_id: str, path: str) -> PackageRecord | None:
    doc = await db[YUM_PACKAGES].find_one({'repo_id': repo_id, 'path': path})
    return _record(doc) if doc else None


async def indexed(db: Database, repo_id: str, repo_dir: str, gen: str) -> bool:
    """Whether the ``primary`` with digest ``gen`` of ``repo_dir`` has been indexed."""
    doc = await db[YUM_PACKAGES].find_one({'repo_id': repo_id, 'repo_dir': repo_dir, 'gen': gen}, {'_id': 1})
    return doc is not None


def _matches(listing: Listing, doc: dict[str, object]) -> bool:
    return listing.algorithm == 'sha256' and listing.digest == doc['digest'] and listing.size == doc['size']


async def record_primary(
    db: Database, repo_id: str, repo_dir: str, entries: Sequence[Listing], *, gen: str
) -> list[str]:
    """Make ``entries`` the listing of ``repo_dir`` (generation ``gen``): upsert them as verified, drop the entries of
    older generations. Returns the paths whose earlier unverified record disagreed with the listing (their cached
    copies have been dropped)."""
    listed = {e.path: e for e in entries}
    mismatched: list[str] = []
    if listed:
        async for doc in db[YUM_PACKAGES].find(
            {'repo_id': repo_id, 'verified': False}, {'path': 1, 'digest': 1, 'size': 1, '_id': 0}
        ):
            entry = listed.get(str(doc['path']))
            if entry is not None and not _matches(entry, doc):
                mismatched.append(entry.path)
    now = datetime.now(UTC)
    ops = [
        UpdateOne(
            {'repo_id': repo_id, 'path': e.path},
            {
                '$set': {
                    'algorithm': e.algorithm,
                    'digest': e.digest,
                    'size': e.size,
                    'verified': True,
                    'repo_dir': repo_dir,
                    'name': e.name,
                    'epoch': e.epoch,
                    'version': e.version,
                    'release': e.release,
                    'arch': e.arch,
                    'gen': gen,
                    'updated_at': now,
                },
            },
            upsert=True,
        )
        for e in listed.values()
    ]
    for start in range(0, len(ops), BATCH):
        await db[YUM_PACKAGES].bulk_write(ops[start : start + BATCH], ordered=False)
    await db[YUM_PACKAGES].delete_many({'repo_id': repo_id, 'repo_dir': repo_dir, 'gen': {'$ne': gen}})
    for path in mismatched:
        await drop_cached(db, repo_id, path)
    return mismatched


async def record_unverified(db: Database, repo_id: str, path: str, sha256: str, size: int) -> PackageRecord | None:
    """Remember the computed SHA-256 of a package no cached listing named. Returns the existing *verified* record
    when a listing landed in the meantime (the caller compares), else None."""
    collection = db[YUM_PACKAGES]
    now = datetime.now(UTC)
    fields = {'algorithm': 'sha256', 'digest': sha256, 'size': size, 'verified': False, 'updated_at': now}
    result = await collection.update_one({'repo_id': repo_id, 'path': path, 'verified': False}, {'$set': fields})
    if result.matched_count:
        return None
    try:
        await collection.insert_one({'repo_id': repo_id, 'path': path, 'repo_dir': None, 'gen': None, **fields})
    except DuplicateKeyError:  # a verified listing exists
        doc = await collection.find_one({'repo_id': repo_id, 'path': path})
        return _record(doc) if doc else None
    return None


async def drop_cached(db: Database, repo_id: str, path: str) -> None:
    """Forget the cached copy of a file (it failed validation against a listing); the blob is left to GC."""
    asset = await assets_db.get(db, repo_id, path)
    if asset is not None:
        await assets_db.delete(db, asset.id)
