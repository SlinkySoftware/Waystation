"""Conda records (DATA_MODEL ``conda_packages``, ``conda_names``; FORMATS.md "Conda"), shared by the server (proxy
indexing, hosted uploads, browse) and the worker (``index.conda``: hosted repodata).

``conda_packages``: one document per package file of a subdir, ``(repo_id, subdir, filename)`` unique, with
``name``, ``version``, ``build``, ``build_number``, ``version_sort`` (conda VersionOrder), ``sha256``, ``md5``,
``size``, ``timestamp`` and ``verified``:

- **Listings** (proxy, ``verified: true``, ``source: 'repodata'``): every valid entry of a cached ``repodata.json``
  (any variant), written in unordered bulk upserts by a background scan after the file is cached. Upserts of an
  unchanged entry modify nothing, so re-indexing a refreshed file only writes what changed. Entries an upstream
  removes stay until the repository is deleted (package file names are never reused with other bytes).
- **Unverified fetches** (proxy, ``verified: false``, ``source: 'fetched'``): a package file served before any indexed
  listing named it; the hash is the one Waystation computed. The next listing that names the file confirms it, or
  drops the cached copy when the hashes differ (only that copy: compare-and-set on the blob).
- **Uploads** (hosted, ``source: 'hosted'``): plus ``entry`` (the repodata entry made from the package's
  ``info/index.json``), ``path``, ``uploaded_by``, ``created_at``.

``conda_names``: one summary per package name: ``latest`` / ``latest_sort`` (highest version listed), ``subdirs``,
``counts.<subdir>`` (files listed per subdir), ``summary``/``license`` (hosted), search keys.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymongo import DESCENDING, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

from waystation_common.conda import sort_string
from waystation_common.conda.repodata import Entry
from waystation_common.db import usage as usage_db
from waystation_common.db.apt import drop_cached
from waystation_common.db.client import Database, Document, Session
from waystation_common.db.collections import ASSETS, CONDA_NAMES, CONDA_PACKAGES, REPOSITORIES
from waystation_common.ids import new_id
from waystation_common.searchkeys import name_keys

BATCH = 1000


@dataclass(frozen=True)
class FileRecord:
    subdir: str
    filename: str
    sha256: str | None
    md5: str | None
    size: int | None
    verified: bool


def _record(doc: Mapping[str, Any]) -> FileRecord:
    size = doc.get('size')
    return FileRecord(
        subdir=str(doc['subdir']),
        filename=str(doc['filename']),
        sha256=doc.get('sha256'),
        md5=doc.get('md5'),
        size=int(size) if isinstance(size, int) else None,
        verified=bool(doc.get('verified')),
    )


async def lookup(db: Database, repo_id: str, subdir: str, filename: str) -> FileRecord | None:
    doc = await db[CONDA_PACKAGES].find_one({'repo_id': repo_id, 'subdir': subdir, 'filename': filename})
    return _record(doc) if doc else None


# ---------------------------------------------------------------------------- proxy listings
@dataclass
class NameStat:
    latest: str
    latest_sort: str
    files: int = 0


def _ts(millis: int | None) -> datetime | None:
    if millis is None:
        return None
    seconds = millis / 1000 if millis > 10**11 else millis  # old entries are in seconds
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except OverflowError, OSError, ValueError:
        return None


async def record_listing(
    db: Database, repo_id: str, subdir: str, entries: Sequence[Entry], *, sorts: Mapping[str, str] | None = None
) -> list[str]:
    """Upsert one batch of listed entries as verified (``sorts``: precomputed ``version_sort`` keys by version).
    Returns the file names whose earlier unverified hash differs from the listing (their cached copies have been
    dropped)."""
    if not entries:
        return []
    known = sorts or {}
    listed = {e.filename: e for e in entries}
    mismatched: list[tuple[str, str]] = []
    cursor = db[CONDA_PACKAGES].find(
        {'repo_id': repo_id, 'subdir': subdir, 'filename': {'$in': list(listed)}, 'verified': False},
        {'filename': 1, 'sha256': 1, 'size': 1, '_id': 0},
    )
    async for doc in cursor:
        entry = listed[str(doc['filename'])]
        if (entry.sha256 is not None and entry.sha256 != doc.get('sha256')) or (
            entry.size is not None and entry.size != doc.get('size')
        ):
            mismatched.append((entry.filename, str(doc.get('sha256'))))
    now = datetime.now(UTC)
    ops = [
        UpdateOne(
            {'repo_id': repo_id, 'subdir': subdir, 'filename': e.filename},
            {
                '$set': {
                    'name': e.name,
                    'version': e.version,
                    'build': e.build,
                    'build_number': e.build_number,
                    'version_sort': known.get(e.version) or sort_string(e.version),
                    'sha256': e.sha256,
                    'md5': e.md5,
                    'size': e.size,
                    'timestamp': _ts(e.timestamp),
                    'verified': True,
                    'source': 'repodata',
                },
                '$setOnInsert': {'created_at': now},
            },
            upsert=True,
        )
        for e in listed.values()
    ]
    await db[CONDA_PACKAGES].bulk_write(ops, ordered=False)
    for filename, unverified_sha in mismatched:
        await drop_cached(db, repo_id, f'{subdir}/{filename}', sha256=unverified_sha)
    return [filename for filename, _ in mismatched]


async def record_names(db: Database, repo_id: str, subdir: str, stats: Mapping[str, NameStat]) -> None:
    """Merge one subdir's per-name statistics into ``conda_names`` (the highest version wins ``latest``)."""
    now = datetime.now(UTC)
    ops = []
    for name, stat in stats.items():
        keys = name_keys(name)
        ops.append(
            UpdateOne(
                {'repo_id': repo_id, 'name': name},
                [
                    {
                        '$set': {
                            'latest': {
                                '$cond': [
                                    {'$gt': [stat.latest_sort, {'$ifNull': ['$latest_sort', '']}]},
                                    stat.latest,
                                    '$latest',
                                ]
                            },
                            'latest_sort': {'$max': [stat.latest_sort, {'$ifNull': ['$latest_sort', '']}]},
                            'subdirs': {'$setUnion': [{'$ifNull': ['$subdirs', []]}, [subdir]]},
                            f'counts.{subdir}': {'$literal': stat.files},
                            'name_lc': {'$literal': keys['name_lc']},
                            'name_sfx': {'$literal': keys['name_sfx']},
                            'created_at': {'$ifNull': ['$created_at', now]},
                            'updated_at': {'$literal': now},
                        }
                    }
                ],
                upsert=True,
            )
        )
    for start in range(0, len(ops), BATCH):
        await db[CONDA_NAMES].bulk_write(ops[start : start + BATCH], ordered=False)


async def record_unverified(
    db: Database, repo_id: str, subdir: str, filename: str, fields: Mapping[str, Any], sha256: str, size: int
) -> FileRecord | None:
    """Remember the computed hash of a package file no indexed listing named. Returns the existing *verified* record
    when a listing landed in the meantime (the caller compares hashes), else None."""
    collection = db[CONDA_PACKAGES]
    now = datetime.now(UTC)
    values = {'sha256': sha256, 'md5': None, 'size': size, 'verified': False, 'source': 'fetched'}
    query = {'repo_id': repo_id, 'subdir': subdir, 'filename': filename}
    result = await collection.update_one({**query, 'verified': False}, {'$set': values})
    if result.matched_count:
        return None
    try:
        await collection.insert_one({**query, **fields, **values, 'created_at': now})
    except DuplicateKeyError:  # a verified listing exists
        doc = await collection.find_one(query)
        return _record(doc) if doc else None
    return None


async def unverified_for(db: Database, repo_id: str, subdir: str, filename: str, sha256: str) -> None:
    """Forget an unverified record whose cached copy was dropped (only if it is still that copy's)."""
    await db[CONDA_PACKAGES].delete_one(
        {'repo_id': repo_id, 'subdir': subdir, 'filename': filename, 'verified': False, 'sha256': sha256}
    )


# ---------------------------------------------------------------------------- browse
def _prefix(q: str) -> dict[str, str]:
    return {'$regex': '^' + re.escape(q.lower())}


async def names_page(db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int) -> list[Document]:
    """Package summaries by name (``repo_name_unique``); ``q``: a name prefix."""
    name: dict[str, Any] = {}
    if after is not None:
        name['$gt'] = after
    if q:
        name.update(_prefix(q))
    query: dict[str, Any] = {'repo_id': repo_id}
    if name:
        query['name'] = name
    projection = {'_id': 0, 'name_sfx': 0}
    return await db[CONDA_NAMES].find(query, projection).sort('name', 1).limit(limit).to_list()


async def get_name(db: Database, repo_id: str, name: str) -> Document | None:
    return await db[CONDA_NAMES].find_one({'repo_id': repo_id, 'name': name}, {'_id': 0, 'name_sfx': 0})


async def files_page(
    db: Database, repo_id: str, name: str, *, subdir: str | None, offset: int, limit: int
) -> list[Document]:
    """A package's files, newest version first (``repo_name_sort``)."""
    query: dict[str, Any] = {'repo_id': repo_id, 'name': name}
    if subdir:
        query['subdir'] = subdir
    cursor = (
        db[CONDA_PACKAGES]
        .find(query, {'_id': 0})
        .sort([('version_sort', DESCENDING), ('build_number', DESCENDING), ('filename', 1)])
        .skip(offset)
        .limit(limit)
    )
    return await cursor.to_list()


async def held(db: Database, repo_id: str, paths: Sequence[str]) -> dict[str, Document]:
    """The stored files among ``paths`` (``repo_path_unique``): size, cached time, downloads."""
    if not paths:
        return {}
    projection = {'path': 1, 'size': 1, 'created_at': 1, 'download_count': 1, 'last_downloaded_at': 1, '_id': 0}
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$in': list(paths)}}, projection)
    return {str(doc['path']): doc async for doc in cursor}


async def count_files(db: Database, repo_id: str, name: str, subdir: str | None) -> int:
    query: dict[str, Any] = {'repo_id': repo_id, 'name': name}
    if subdir:
        query['subdir'] = subdir
    return int(await db[CONDA_PACKAGES].count_documents(query))


async def matching(
    db: Database, repo_id: str, subdir: str, name: str, version: str | None, build: str | None
) -> list[Document]:
    """Files of ``name`` in ``subdir`` (prefetch): the given version (and build), else the newest version's."""
    query: dict[str, Any] = {'repo_id': repo_id, 'name': name, 'subdir': subdir}
    if version is not None:
        query['version'] = version
    if build is not None:
        query['build'] = build
    docs = await db[CONDA_PACKAGES].find(query, {'_id': 0}).sort('version_sort', DESCENDING).limit(500).to_list()
    if version is None and docs:
        top = docs[0]['version_sort']
        docs = [d for d in docs if d['version_sort'] == top]
    return docs


# ---------------------------------------------------------------------------- hosted
async def repository(db: Database, repo_id: str) -> Document | None:
    return await db[REPOSITORIES].find_one({'_id': repo_id}, {'name': 1, 'format': 1, 'type': 1})


async def save_hosted(db: Database, repo_id: str, record: Mapping[str, Any], *, session: Session | None = None) -> None:
    """Record an uploaded package file (replacing an earlier upload of the same file)."""
    await db[CONDA_PACKAGES].replace_one(
        {'repo_id': repo_id, 'subdir': record['subdir'], 'filename': record['filename']},
        {'repo_id': repo_id, **record},
        upsert=True,
        session=session,
    )


async def get_hosted(db: Database, repo_id: str, subdir: str, filename: str) -> Document | None:
    return await db[CONDA_PACKAGES].find_one({'repo_id': repo_id, 'subdir': subdir, 'filename': filename})


async def delete_hosted(
    db: Database, repo_id: str, subdir: str, filename: str, *, session: Session | None = None
) -> None:
    await db[CONDA_PACKAGES].delete_one({'repo_id': repo_id, 'subdir': subdir, 'filename': filename}, session=session)


async def hosted_entries(db: Database, repo_id: str) -> list[Document]:
    """Every uploaded file of a hosted repository, by subdir and file name."""
    cursor = db[CONDA_PACKAGES].find({'repo_id': repo_id}, {'_id': 0}).sort([('subdir', 1), ('filename', 1)])
    return [doc async for doc in cursor]


async def drop_missing(db: Database, repo_id: str, docs: list[Document]) -> list[Document]:
    """``docs`` whose package file is still stored; records of removed files (cleanup policies) are deleted."""
    paths = [f'{d["subdir"]}/{d["filename"]}' for d in docs]
    stored: set[str] = set()
    for start in range(0, len(paths), BATCH):
        cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$in': paths[start : start + BATCH]}}, {'path': 1})
        stored.update([str(a['path']) async for a in cursor])
    kept = []
    for doc, path in zip(docs, paths, strict=True):
        if path in stored:
            kept.append(doc)
        else:
            await delete_hosted(db, repo_id, str(doc['subdir']), str(doc['filename']))
    return kept


async def replace_names(db: Database, repo_id: str, summaries: Mapping[str, Mapping[str, Any]]) -> None:
    """Make ``summaries`` (by name) the repository's ``conda_names`` (hosted: rebuilt with the repodata)."""
    now = datetime.now(UTC)
    ops = [
        UpdateOne(
            {'repo_id': repo_id, 'name': name},
            {'$set': {**fields, **name_keys(name), 'updated_at': now}, '$setOnInsert': {'created_at': now}},
            upsert=True,
        )
        for name, fields in summaries.items()
    ]
    for start in range(0, len(ops), BATCH):
        await db[CONDA_NAMES].bulk_write(ops[start : start + BATCH], ordered=False)
    await db[CONDA_NAMES].delete_many({'repo_id': repo_id, 'name': {'$nin': list(summaries)}})


async def publish(
    db: Database, repo_id: str, path: str, *, sha256: str, size: int, content_type: str, now: datetime
) -> None:
    """Make the committed blob ``sha256`` the metadata asset at ``path`` (one atomic document write; usage kept)."""
    fields: dict[str, Any] = {
        'repo_id': repo_id,
        'path': path,
        'kind': 'metadata',
        'blob_sha256': sha256,
        'size': size,
        'content_type': content_type,
        'checksums': {'sha256': sha256},
        'headers': {},
        'upstream': {'url': '', 'etag': None, 'last_modified': None, 'fetched_at': now, 'status': 201},
        'updated_at': now,
    }
    before = await db[ASSETS].find_one_and_update(
        {'repo_id': repo_id, 'path': path},
        {'$set': fields, '$setOnInsert': {'_id': new_id(), 'created_at': now}, '$unset': {'blob_missing': ''}},
        upsert=True,
        return_document=ReturnDocument.BEFORE,
        projection={'size': 1},
    )
    if before is None:
        await usage_db.add(db, repo_id, size, 1)
    else:
        await usage_db.add(db, repo_id, size - int(before.get('size') or 0))


async def published(db: Database, repo_id: str, names: Iterable[str]) -> list[str]:
    """Paths of published repodata files (``<subdir>/<name>``) of the repository."""
    pattern = '^[a-z0-9-]+/(' + '|'.join(re.escape(n) for n in names) + ')$'
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$regex': pattern}, 'kind': 'metadata'}, {'path': 1})
    return [str(doc['path']) async for doc in cursor]


async def unpublish(db: Database, repo_id: str, path: str) -> None:
    doc = await db[ASSETS].find_one_and_delete({'repo_id': repo_id, 'path': path}, projection={'size': 1})
    if doc is not None:
        await usage_db.add(db, repo_id, -int(doc.get('size') or 0), -1)


__all__ = [
    'FileRecord',
    'NameStat',
    'drop_cached',
    'lookup',
    'record_listing',
    'record_names',
    'record_unverified',
]
