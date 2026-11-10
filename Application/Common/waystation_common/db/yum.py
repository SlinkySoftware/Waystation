"""Hosted yum repositories: their package records (``yum_packages`` with ``gen: 'hosted'``) and the published
repodata assets (DATA_MODEL; FORMATS.md "yum / dnf").

Shared by the server (records an uploaded package and, in the same transaction, the ``index.yum`` outbox entry;
``waystation_server.db.yum``) and the worker (``index.yum``: builds the repository's repodata from the records and
publishes it). Hosted record fields, besides the listing fields of every ``yum_packages`` entry (``path``,
``algorithm`` = ``sha256``, ``digest``, ``size``, ``verified`` = true, ``repo_dir`` = ``''``, ``name``, ``epoch``,
``version``, ``release``, ``arch``, ``version_sort``): ``summary``, ``uploaded_at``, ``uploaded_by`` and ``meta``
(the package's ``primary``/``filelists``/``other`` XML entries, JSON, gzip-compressed, BSON binary).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pymongo import ASCENDING, ReturnDocument

from waystation_common.db import usage as usage_db
from waystation_common.db.client import Database, Document, Session
from waystation_common.db.collections import ASSETS, REPOSITORIES, YUM_PACKAGES
from waystation_common.ids import new_id

HOSTED_GEN = 'hosted'
REPODATA_PREFIX = 'repodata/'
BATCH = 500


async def repository(db: Database, repo_id: str) -> Document | None:
    """The repository's ``name``, ``format`` and ``type`` (None when it was deleted)."""
    return await db[REPOSITORIES].find_one({'_id': repo_id}, {'name': 1, 'format': 1, 'type': 1})


async def hosted_packages(db: Database, repo_id: str) -> AsyncIterator[list[Document]]:
    """A hosted repository's package records in batches, by name, EVR, architecture and path."""
    cursor = (
        db[YUM_PACKAGES]
        .find({'repo_id': repo_id, 'gen': HOSTED_GEN}, {'_id': 0})
        .sort([('name', ASCENDING), ('version_sort', ASCENDING), ('arch', ASCENDING), ('path', ASCENDING)])
        .batch_size(BATCH)
    )
    batch: list[Document] = []
    async for doc in cursor:
        batch.append(doc)
        if len(batch) >= BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


async def drop_missing(db: Database, repo_id: str, docs: list[Document]) -> list[Document]:
    """``docs`` whose package file is still stored; records of removed files (cleanup policies) are deleted."""
    paths = [str(d['path']) for d in docs]
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$in': paths}}, {'path': 1})
    stored = {str(a['path']) async for a in cursor}
    gone = [d['path'] for d in docs if d['path'] not in stored]
    if gone:
        await db[YUM_PACKAGES].delete_many({'repo_id': repo_id, 'gen': HOSTED_GEN, 'path': {'$in': gone}})
    return [d for d in docs if d['path'] in stored]


@dataclass(frozen=True)
class Published:
    """One generated file to publish as a metadata asset of the repository."""

    path: str
    sha256: str
    size: int
    content_type: str


def _fields(repo_id: str, file: Published, now: datetime) -> dict[str, Any]:
    return {
        'repo_id': repo_id,
        'path': file.path,
        'kind': 'metadata',
        'blob_sha256': file.sha256,
        'size': file.size,
        'content_type': file.content_type,
        'checksums': {'sha256': file.sha256},
        'headers': {},
        'upstream': {'url': '', 'etag': None, 'last_modified': None, 'fetched_at': now, 'status': 201},
        'updated_at': now,
    }


async def publish(
    db: Database, repo_id: str, files: Sequence[Published], *, now: datetime, session: Session | None = None
) -> list[tuple[int, int]]:
    """Make each committed blob the asset at its path (one document replace each; with ``session`` inside the
    caller's transaction, so clients see the old or the new set). Returns the (bytes, assets) usage deltas for
    ``count_usage`` (outside the transaction)."""
    deltas: list[tuple[int, int]] = []
    for file in files:
        before = await db[ASSETS].find_one_and_update(
            {'repo_id': repo_id, 'path': file.path},
            {
                '$set': _fields(repo_id, file, now),
                '$setOnInsert': {'_id': new_id(), 'created_at': now},
                '$unset': {'blob_missing': ''},
            },
            upsert=True,
            return_document=ReturnDocument.BEFORE,
            projection={'size': 1},
            session=session,
        )
        deltas.append((file.size, 1) if before is None else (file.size - int(before.get('size') or 0), 0))
    return deltas


async def count_usage(db: Database, repo_id: str, deltas: Sequence[tuple[int, int]]) -> None:
    size, count = sum(d[0] for d in deltas), sum(d[1] for d in deltas)
    if size or count:
        await usage_db.add(db, repo_id, size, count)


async def get_asset(db: Database, repo_id: str, path: str) -> Document | None:
    return await db[ASSETS].find_one({'repo_id': repo_id, 'path': path}, {'blob_sha256': 1, 'size': 1})


async def repodata_paths(db: Database, repo_id: str) -> list[str]:
    """Every asset path under ``repodata/``."""
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$regex': '^repodata/'}}, {'path': 1})
    return [str(doc['path']) async for doc in cursor]


async def remove_assets(db: Database, repo_id: str, paths: Sequence[str]) -> None:
    """Delete generated repodata files that are no longer referenced (blob GC frees them)."""
    if not paths:
        return
    size = 0
    removed = 0
    for path in paths:
        doc = await db[ASSETS].find_one_and_delete({'repo_id': repo_id, 'path': path}, projection={'size': 1})
        if doc is not None:
            size += int(doc.get('size') or 0)
            removed += 1
    if removed:
        await usage_db.add(db, repo_id, -size, -removed)
