"""Ansible Galaxy records (DATA_MODEL §1): collection versions (``galaxy_collections``) and hosted upload imports
(``galaxy_imports``). Shared by the server (uploads, served lists, browse) and the worker (``import.galaxy``).

``galaxy_collections``, one document per (repository, namespace, name, version):

- ``repo_id``, ``namespace``, ``name``, ``version``, ``version_sort`` (``waystation_common.galaxy.version_sort``),
  ``path`` (the artifact's path in the repository), ``created`` (upload/import time, or the upstream's
  ``created_at``), ``requires_ansible``, ``description``;
- hosted (written by the import job): ``sha256``, ``size``, ``metadata`` (the ``MANIFEST.json`` ``collection_info``
  fields Galaxy serves: ``dependencies``, ``authors``, ``license``, ``tags``, ``repository``...), ``import_id``,
  ``uploaded_by``;
- proxy (written in the background when a version list or a version's details are cached): ``gen`` (sha256 of the
  cached version list blob the entry came from), and ``sha256``/``size`` once the version's details were cached.

``galaxy_imports``, one document per upload (``_id`` = the task id the client polls): ``repo_id``, ``namespace``,
``name``, ``version``, ``filename``, ``sha256``, ``size``, ``uploaded_by``, ``state`` (``waiting`` → ``running`` →
``completed`` | ``failed``), ``messages`` (``{level, message, time}``), ``error`` (``{code, description}``),
``created_at``, ``started_at``, ``finished_at``, ``expires_at`` (TTL: kept ``IMPORT_KEEP_DAYS``).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from waystation_common.db import usage as usage_db
from waystation_common.db.client import Database, Document, Session
from waystation_common.db.collections import ASSETS, GALAXY_COLLECTIONS, GALAXY_IMPORTS, REPOSITORIES
from waystation_common.ids import new_id

ARTIFACT_TYPE = 'application/gzip'
IMPORT_KEEP_DAYS = 30
ImportState = Literal['waiting', 'running', 'completed', 'failed']
UNFINISHED: tuple[ImportState, ...] = ('waiting', 'running')


async def repository(db: Database, repo_id: str) -> Document | None:
    """The repository's ``name``, ``format`` and ``type`` (None when it was deleted)."""
    return await db[REPOSITORIES].find_one({'_id': repo_id}, {'name': 1, 'format': 1, 'type': 1})


# ---------------------------------------------------------------------------- versions
async def get_version(db: Database, repo_id: str, namespace: str, name: str, version: str) -> Document | None:
    return await db[GALAXY_COLLECTIONS].find_one(
        {'repo_id': repo_id, 'namespace': namespace, 'name': name, 'version': version}, {'_id': 0}
    )


async def collection_versions(db: Database, repo_ids: Sequence[str], namespace: str, name: str) -> list[Document]:
    """Every version of a collection in ``repo_ids``, newest first (index ``repo_coll_sort``)."""
    cursor = (
        db[GALAXY_COLLECTIONS]
        .find({'repo_id': {'$in': list(repo_ids)}, 'namespace': namespace, 'name': name}, {'_id': 0})
        .sort('version_sort', DESCENDING)
    )
    return [doc async for doc in cursor]


async def insert_version(db: Database, record: Document) -> bool:
    """Insert a hosted collection version; False when the version exists already (versions are immutable)."""
    try:
        await db[GALAXY_COLLECTIONS].insert_one(dict(record))
    except DuplicateKeyError:
        return False
    return True


async def delete_version(
    db: Database, repo_id: str, namespace: str, name: str, version: str, *, session: Session | None = None
) -> Document | None:
    return await db[GALAXY_COLLECTIONS].find_one_and_delete(
        {'repo_id': repo_id, 'namespace': namespace, 'name': name, 'version': version},
        projection={'_id': 0},
        session=session,
    )


async def drop_missing(db: Database, repo_id: str) -> int:
    """Delete a hosted repository's version records whose artifact is gone (removed by a cleanup policy). Other
    repositories are left alone: a proxy lists every upstream version, cached or not."""
    repo = await repository(db, repo_id)
    if repo is None or repo.get('type') != 'hosted':
        return 0
    removed = 0
    cursor = db[GALAXY_COLLECTIONS].find({'repo_id': repo_id}, {'_id': 1, 'path': 1})
    batch: list[Document] = []
    async for doc in cursor:
        batch.append(doc)
        if len(batch) >= 1000:
            removed += await _drop_batch(db, repo_id, batch)
            batch = []
    if batch:
        removed += await _drop_batch(db, repo_id, batch)
    return removed


async def _drop_batch(db: Database, repo_id: str, docs: list[Document]) -> int:
    paths = [str(d['path']) for d in docs]
    stored = {
        str(a['path'])
        async for a in db[ASSETS].find({'repo_id': repo_id, 'path': {'$in': paths}}, {'path': 1, '_id': 0})
    }
    gone = [d['_id'] for d in docs if d['path'] not in stored]
    if not gone:
        return 0
    result = await db[GALAXY_COLLECTIONS].delete_many({'_id': {'$in': gone}})
    return int(result.deleted_count)


async def publish_artifact(db: Database, repo_id: str, path: str, *, sha256: str, size: int, now: datetime) -> None:
    """Make the committed blob ``sha256`` the repository's file ``path`` (an imported collection artifact); the
    repository's usage follows."""
    fields: dict[str, Any] = {
        'repo_id': repo_id,
        'path': path,
        'kind': 'content',
        'blob_sha256': sha256,
        'size': size,
        'content_type': ARTIFACT_TYPE,
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


# ---------------------------------------------------------------------------- imports
async def create_import(db: Database, doc: Document, *, now: datetime, session: Session | None = None) -> None:
    await db[GALAXY_IMPORTS].insert_one(
        {
            **doc,
            'state': 'waiting',
            'messages': [],
            'error': None,
            'created_at': now,
            'started_at': None,
            'finished_at': None,
            'expires_at': now + timedelta(days=IMPORT_KEEP_DAYS),
        },
        session=session,
    )


async def get_import(db: Database, task_id: str) -> Document | None:
    return await db[GALAXY_IMPORTS].find_one({'_id': task_id})


async def unfinished_import(db: Database, repo_id: str, namespace: str, name: str, version: str) -> Document | None:
    """An upload of this version that the import job has not finished yet (index ``repo_coll_version_state``)."""
    return await db[GALAXY_IMPORTS].find_one(
        {
            'repo_id': repo_id,
            'namespace': namespace,
            'name': name,
            'version': version,
            'state': {'$in': list(UNFINISHED)},
        },
        {'_id': 1},
    )


async def start_import(db: Database, task_id: str, *, now: datetime) -> Document | None:
    """Mark an unfinished import running (again, after a retry); None when it is finished or gone."""
    return await db[GALAXY_IMPORTS].find_one_and_update(
        {'_id': task_id, 'state': {'$in': list(UNFINISHED)}},
        {'$set': {'state': 'running', 'started_at': now}},
        return_document=ReturnDocument.AFTER,
    )


async def finish_import(
    db: Database,
    task_id: str,
    *,
    state: Literal['completed', 'failed'],
    messages: Sequence[Document],
    error: Document | None,
    now: datetime,
) -> None:
    await db[GALAXY_IMPORTS].update_one(
        {'_id': task_id},
        {
            '$set': {'state': state, 'error': error, 'finished_at': now},
            '$push': {'messages': {'$each': list(messages)}},
        },
    )
