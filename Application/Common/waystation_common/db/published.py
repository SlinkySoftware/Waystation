"""Generated files of hosted repositories published as metadata assets (yum ``repodata/``, apt ``dists/``): the
worker commits each file to the blob store, then makes it the asset at its path, several in one transaction when
clients must see a consistent set (ARCHITECTURE §10, "atomic publish")."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pymongo import ReturnDocument

from waystation_common.db import usage as usage_db
from waystation_common.db.client import Database, Document, Session
from waystation_common.db.collections import ASSETS
from waystation_common.ids import new_id


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
