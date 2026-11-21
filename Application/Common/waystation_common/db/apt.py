"""apt proxy records shared by the server's apt proxy and the worker's ``apt.refresh`` job (DATA_MODEL
``apt_pool_index``, ``apt_packages``; FORMATS.md "apt").

``apt_pool_index`` maps ``Filename → (SHA256, Size)`` per proxy repository. Entries come from two places:

- **Listings** (``verified: true``): every stanza of a ``Packages`` file the apt proxy cached, written in bulk when the
  index is parsed. The hash is the one the signed ``Release`` chain vouches for; pool files are validated against it.
- **Unverified fetches** (``verified: false``): a pool file served before any cached ``Packages`` listed it; the entry
  holds the hash Waystation computed. The next listing that names the file replaces it, and if the hashes differ the
  cached copy is dropped so the next request fetches (and validates) it again. A drop only removes the cached copy
  that was checked (same blob), never a copy a later fill stored.

``apt_packages`` (browse) holds one document per stanza of every cached ``Packages`` index: ``(suite, component,
arch)`` identify the index, ``gen`` the index blob it came from; a new generation replaces the listing.

The worker's refresh also reads and writes the repository's ``Release`` and index assets here (the same document
shape as the proxy engine's cache fills; ``assets`` access is through ``repo_path_unique``) and counts them in the
repository's usage. Lookups use ``repo_filename_unique``; listing writes are unordered bulk upserts in batches. Only
written on cache fills and by the refresh job, never per cache hit (ARCHITECTURE §8).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymongo import ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

from waystation_common.apt import deb822
from waystation_common.apt.indexes import PackagesTarget, browse_suite
from waystation_common.db import usage as usage_db
from waystation_common.db.client import Database, Document
from waystation_common.db.collections import APT_PACKAGES, APT_POOL_INDEX, ASSETS, REPOSITORIES
from waystation_common.db.verify import MARK
from waystation_common.ids import new_id
from waystation_common.searchkeys import name_keys

BATCH = 1000
RELEASE_PATHS = r'(^|/)(InRelease|Release|Release\.gpg)$'


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


@dataclass(frozen=True)
class Stanza:
    filename: str
    sha256: str
    size: int
    name: str
    version: str
    architecture: str
    section: str


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
    mismatched: list[tuple[str, str]] = []
    async for doc in db[APT_POOL_INDEX].find(
        {'repo_id': repo_id, 'verified': False}, {'filename': 1, 'sha256': 1, '_id': 0}
    ):
        entry = listed.get(str(doc['filename']))
        if entry is not None and entry.sha256 != doc['sha256']:
            mismatched.append((entry.filename, str(doc['sha256'])))
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
    for filename, unverified_sha in mismatched:
        await drop_cached(db, repo_id, filename, sha256=unverified_sha)
    return [filename for filename, _ in mismatched]


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


async def drop_cached(db: Database, repo_id: str, filename: str, *, sha256: str | None = None) -> bool:
    """Forget the cached copy of a pool file (it failed validation against a listing); the blob is left to GC.
    With ``sha256`` only the copy with that content goes (a fill that stored a verified copy meanwhile stays).
    Returns whether a copy was removed."""
    query: dict[str, Any] = {'repo_id': repo_id, 'path': filename}
    if sha256 is not None:
        query['blob_sha256'] = sha256
    doc = await db[ASSETS].find_one_and_delete(query, projection={'size': 1, 'kind': 1})
    if doc is None:
        return False
    artifact = int(doc.get('kind') == 'content')
    await usage_db.add(db, repo_id, -int(doc.get('size') or 0), -1, artifacts_delta=-artifact)
    return True


async def unverified(db: Database, repo_id: str) -> AsyncIterator[list[PoolRecord]]:
    """The repository's unverified pool entries, in batches (``repo_verified``)."""
    batch: list[PoolRecord] = []
    async for doc in db[APT_POOL_INDEX].find({'repo_id': repo_id, 'verified': False}, {'_id': 0}).batch_size(BATCH):
        batch.append(_record(doc))
        if len(batch) >= BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


async def listings_of(db: Database, repo_id: str, filenames: Sequence[str]) -> dict[str, Listing]:
    """For each of ``filenames`` one listing of it in a cached ``Packages`` index (``apt_packages``,
    ``filename_repo``), if any."""
    if not filenames:
        return {}
    cursor = db[APT_PACKAGES].find(
        {'filename': {'$in': list(filenames)}, 'repo_id': repo_id},
        {'filename': 1, 'sha256': 1, 'size': 1, 'suite': 1, 'component': 1, 'arch': 1, '_id': 0},
    )
    out: dict[str, Listing] = {}
    async for doc in cursor:
        out.setdefault(str(doc['filename']), Listing(str(doc['filename']), str(doc['sha256']), int(doc['size'])))
    return out


async def mark_verified(db: Database, repo_id: str, record: PoolRecord, listing: Listing) -> bool:
    """An unverified entry whose computed hash a listing confirms becomes verified (only if it is still the
    unverified entry that was checked)."""
    result = await db[APT_POOL_INDEX].update_one(
        {'repo_id': repo_id, 'filename': record.filename, 'verified': False, 'sha256': record.sha256},
        {'$set': {'sha256': listing.sha256, 'size': listing.size, 'verified': True, 'updated_at': datetime.now(UTC)}},
    )
    return result.modified_count == 1


async def replace_unverified(db: Database, repo_id: str, record: PoolRecord, listing: Listing) -> bool:
    """An unverified entry a listing contradicts: the listing's hash replaces it and the cached copy (the one that
    was checked) is dropped. Returns whether the entry was still the one checked."""
    result = await db[APT_POOL_INDEX].update_one(
        {'repo_id': repo_id, 'filename': record.filename, 'verified': False, 'sha256': record.sha256},
        {'$set': {'sha256': listing.sha256, 'size': listing.size, 'verified': True, 'updated_at': datetime.now(UTC)}},
    )
    if result.modified_count != 1:
        return False
    await drop_cached(db, repo_id, record.filename, sha256=record.sha256)
    return True


# ---------------------------------------------------------------------------- apt_packages (browse)
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


@dataclass(frozen=True)
class Indexed:
    entries: int
    dropped: list[str]  # unverified pool files the listing contradicted (cached copies removed)


async def index_packages(db: Database, repo_id: str, raw: bytes, what: PackagesTarget, *, gen: str) -> Indexed:
    """Parse one cached ``Packages`` file (in a thread, bounded: ``deb822``) and record it: ``apt_pool_index``
    (verifying unverified pool files it names) and the browse listing ``apt_packages`` (generation ``gen``)."""
    entries = await asyncio.to_thread(deb822.parse_packages_file, raw, what.suffix)
    listings = [Listing(e.filename, e.sha256, e.size) for e in entries]
    dropped = await record_listing(db, repo_id, listings, suite=what.suite, component=what.component, arch=what.arch)
    stanzas = [Stanza(e.filename, e.sha256, e.size, e.package, e.version, e.architecture, e.section) for e in entries]
    await record_packages(
        db,
        repo_id,
        stanzas,
        suite=browse_suite(what.suite),
        component=what.component,
        arch=what.arch,
        gen=gen,
    )
    return Indexed(entries=len(listings), dropped=dropped)


async def is_indexed(db: Database, repo_id: str, what: PackagesTarget, *, gen: str) -> bool:
    """Whether the generation ``gen`` of the index is the browse listing (``repo_listing_filename_unique``)."""
    doc = await db[APT_PACKAGES].find_one(
        {
            'repo_id': repo_id,
            'suite': browse_suite(what.suite),
            'component': what.component,
            'arch': what.arch,
            'gen': gen,
        },
        {'_id': 1},
    )
    return doc is not None


async def indexed_lists(db: Database, repo_id: str, suite: str) -> set[tuple[str, str]]:
    """The ``(component, arch)`` pairs of the suite (browse id) with a recorded listing: the indexes clients have
    used through this repository (a covered scan of ``repo_listing_filename_unique``)."""
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id, 'suite': suite}},
        {'$group': {'_id': {'c': '$component', 'a': '$arch'}}},
    ]
    return {(str(d['_id']['c']), str(d['_id']['a'])) async for d in await db[APT_PACKAGES].aggregate(pipeline)}


# ---------------------------------------------------------------------------- refresh job: repositories, assets
async def apt_proxies(db: Database) -> list[Document]:
    """Every apt proxy repository (``format,type``)."""
    return await db[REPOSITORIES].find({'format': 'apt', 'type': 'proxy'}).to_list()


async def repository(db: Database, repo_id: str) -> Document | None:
    return await db[REPOSITORIES].find_one({'_id': repo_id})


async def release_assets(db: Database, repo_id: str) -> list[Document]:
    """The repository's cached ``InRelease``/``Release``/``Release.gpg`` files (a key-only scan of the repository's
    range of ``repo_path_unique``; the documents are fetched for matches only)."""
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$regex': RELEASE_PATHS}})
    return await cursor.to_list()


async def get_asset(db: Database, repo_id: str, path: str) -> Document | None:
    return await db[ASSETS].find_one({'repo_id': repo_id, 'path': path})


async def existing_paths(db: Database, repo_id: str, paths: Sequence[str]) -> set[str]:
    """Which of ``paths`` the repository holds (``repo_path_unique``)."""
    if not paths:
        return set()
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$in': sorted(set(paths))}}, {'path': 1, '_id': 0})
    return {str(doc['path']) async for doc in cursor}


@dataclass(frozen=True)
class Fetched:
    """Upstream facts about a stored file (``assets.upstream``)."""

    url: str
    etag: str | None
    last_modified: str | None
    status: int = 200


async def save_asset(
    db: Database,
    repo_id: str,
    path: str,
    *,
    sha256: str,
    size: int,
    content_type: str,
    fetched: Fetched,
    now: datetime,
) -> None:
    """Make a committed blob the metadata asset at ``path`` (as a proxy cache fill does: same fields, the original
    ``_id``/``created_at`` kept) and count it in the repository's usage."""
    fields = {
        'repo_id': repo_id,
        'path': path,
        'kind': 'metadata',
        'blob_sha256': sha256,
        'size': size,
        'content_type': content_type,
        'checksums': {'sha256': sha256},
        'headers': {},
        'upstream': {
            'url': fetched.url,
            'etag': fetched.etag,
            'last_modified': fetched.last_modified,
            'fetched_at': now,
            'status': fetched.status,
        },
        'updated_at': now,
    }
    before = await db[ASSETS].find_one_and_update(
        {'repo_id': repo_id, 'path': path},
        {'$set': fields, '$setOnInsert': {'_id': new_id(), 'created_at': now}, '$unset': {MARK: ''}},
        upsert=True,
        return_document=ReturnDocument.BEFORE,
        projection={'size': 1, 'kind': 1},
    )
    if before is None:
        await usage_db.add(db, repo_id, size, 1)
        return
    was_artifact = int(before.get('kind') == 'content')
    await usage_db.add(db, repo_id, size - int(before.get('size') or 0), artifacts_delta=-was_artifact)


async def mark_confirmed(db: Database, asset_id: str, fetched: Fetched, now: datetime) -> None:
    """The upstream confirmed a cached file (``304``): only the timestamps change."""
    await db[ASSETS].update_one(
        {'_id': asset_id},
        {'$set': {'upstream.fetched_at': now, 'upstream.url': fetched.url, 'updated_at': now}},
    )
