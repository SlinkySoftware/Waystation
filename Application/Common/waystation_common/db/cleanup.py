"""Cleanup persistence (DATA_MODEL ``cleanup_policies``, ``repositories.cleanup_policy_ids``) and the removal of
planned content (SPECIFICATION §4.8). Shared by the server (policy API, preview) and the worker (runs).

* Policies are documents in ``cleanup_policies``; a policy with a schedule has a ``job_definitions`` entry
  ``cleanup:<policy id>`` (kind ``cleanup.run``) kept in step by ``sync_schedule``.
* ``load_repo_data`` reads what a plan needs (bounded by ``cap`` for previews); ``remove`` deletes the planned
  records only: asset documents, docker tags and manifests. Blobs stay for blob GC (mark-and-sweep with a grace
  period), so a client being served a removed file is never cut off.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from waystation_common.cleanup.model import Candidate
from waystation_common.cleanup.plan import AssetRow, ManifestRow, RepoData, TagRow
from waystation_common.db import usage as usage_db
from waystation_common.db.client import Database, Document
from waystation_common.db.collections import (
    ASSETS,
    CLEANUP_POLICIES,
    DOCKER_MANIFESTS,
    DOCKER_TAGS,
    JOB_DEFINITIONS,
    REPOSITORIES,
)
from waystation_common.errors import Conflict

CLEANUP_KIND = 'cleanup.run'
REMOVE_CONCURRENCY = 32
_ASSET_FIELDS = {'_id': 1, 'path': 1, 'kind': 1, 'size': 1, 'blob_sha256': 1, 'created_at': 1, 'last_downloaded_at': 1}


def definition_id(policy_id: str) -> str:
    return f'cleanup:{policy_id}'


def dedupe_key(policy_id: str) -> str:
    return f'cleanup:{policy_id}'


# ---------------------------------------------------------------- policies
async def list_policies(db: Database) -> list[Document]:
    return [doc async for doc in db[CLEANUP_POLICIES].find({}).sort('name', 1)]


async def get_policy(db: Database, policy_id: str) -> Document | None:
    return await db[CLEANUP_POLICIES].find_one({'_id': policy_id})


async def get_policies(db: Database, policy_ids: Sequence[str]) -> list[Document]:
    docs = {doc['_id']: doc async for doc in db[CLEANUP_POLICIES].find({'_id': {'$in': list(policy_ids)}})}
    return [docs[i] for i in policy_ids if i in docs]


async def insert_policy(db: Database, doc: Document) -> None:
    try:
        await db[CLEANUP_POLICIES].insert_one(doc)
    except DuplicateKeyError:
        raise Conflict('a cleanup policy with this name already exists') from None


async def update_policy(db: Database, policy_id: str, fields: Document) -> Document | None:
    try:
        return await db[CLEANUP_POLICIES].find_one_and_update(
            {'_id': policy_id}, {'$set': fields}, return_document=True
        )
    except DuplicateKeyError:
        raise Conflict('a cleanup policy with this name already exists') from None


async def delete_policy(db: Database, policy_id: str) -> bool:
    """Delete the policy, detach it from every repository and drop its schedule."""
    result = await db[CLEANUP_POLICIES].delete_one({'_id': policy_id})
    if not result.deleted_count:
        return False
    await db[REPOSITORIES].update_many(
        {'cleanup_policy_ids': policy_id},
        {'$pull': {'cleanup_policy_ids': policy_id}, '$inc': {'config_version': 1}},
    )
    await db[JOB_DEFINITIONS].delete_one({'_id': definition_id(policy_id)})
    return True


async def sync_schedule(db: Database, policy: Document) -> None:
    """Create, update or remove the policy's ``job_definitions`` entry. A changed schedule restarts its timer."""
    def_id = definition_id(str(policy['_id']))
    schedule = policy.get('schedule')
    if not schedule:
        await db[JOB_DEFINITIONS].delete_one({'_id': def_id})
        return
    existing = await db[JOB_DEFINITIONS].find_one({'_id': def_id})
    fields: dict[str, Any] = {
        'kind': CLEANUP_KIND,
        'params': {'policy_id': str(policy['_id'])},
        'schedule': schedule,
        'queue': None,
        'enabled': bool(policy.get('enabled', True)),
    }
    if existing is None or existing.get('schedule') != schedule:
        fields['next_run_at'] = None
    update: dict[str, Any] = {'$set': fields}
    if existing is None:
        update['$setOnInsert'] = {'last_fired_at': None}
    try:
        await db[JOB_DEFINITIONS].update_one({'_id': def_id}, update, upsert=True)
    except DuplicateKeyError:  # pragma: no cover - concurrent first save of the same policy
        await db[JOB_DEFINITIONS].update_one({'_id': def_id}, {'$set': fields})


async def schedules(db: Database, policy_ids: Sequence[str]) -> dict[str, Document]:
    """The ``job_definitions`` entry of each scheduled policy, by policy id."""
    ids = [definition_id(p) for p in policy_ids]
    return {str(doc['params']['policy_id']): doc async for doc in db[JOB_DEFINITIONS].find({'_id': {'$in': ids}})}


# ---------------------------------------------------------------- attachment
async def attached_repositories(db: Database, policy_id: str | None = None) -> list[Document]:
    """Repositories with policies attached (``_id``, ``name``, ``format``, ``cleanup_policy_ids``), by name."""
    query: Document = {'cleanup_policy_ids': policy_id} if policy_id else {'cleanup_policy_ids.0': {'$exists': True}}
    projection = {'_id': 1, 'name': 1, 'format': 1, 'cleanup_policy_ids': 1}
    return [doc async for doc in db[REPOSITORIES].find(query, projection).sort('name', 1)]


async def set_repository_policies(db: Database, repo_id: str, policy_ids: Sequence[str]) -> bool:
    result = await db[REPOSITORIES].update_one(
        {'_id': repo_id}, {'$set': {'cleanup_policy_ids': list(policy_ids)}, '$inc': {'config_version': 1}}
    )
    return result.matched_count == 1


# ---------------------------------------------------------------- planning input
def _asset_row(doc: Document) -> AssetRow:
    return AssetRow(
        id=str(doc['_id']),
        path=str(doc['path']),
        kind=str(doc.get('kind', 'content')),
        size=int(doc.get('size') or 0),
        blob_sha256=str(doc.get('blob_sha256', '')),
        created_at=doc['created_at'],
        last_downloaded_at=doc.get('last_downloaded_at'),
    )


def _manifest_row(doc: Document) -> ManifestRow:
    blobs = [str(layer['digest']) for layer in doc.get('layers') or [] if layer.get('digest')]
    if doc.get('config_digest'):
        blobs.append(str(doc['config_digest']))
    return ManifestRow(
        image=str(doc['image']),
        digest=str(doc['digest']),
        updated_at=doc.get('updated_at') or doc['created_at'],
        children=tuple(str(m['digest']) for m in doc.get('manifests') or [] if m.get('digest')),
        subject=doc.get('subject_digest'),
        blobs=tuple(blobs),
    )


async def load_repo_data(db: Database, repo_id: str, fmt: str, *, cap: int | None = None) -> RepoData:
    """Rows for ``plan_repository``. With ``cap``, at most that many assets (and tags, manifests) are read and
    ``truncated`` says whether there were more (previews of very large repositories)."""
    data = RepoData(format=fmt)
    query: Document = {'repo_id': repo_id}
    if fmt != 'docker':
        query['kind'] = 'content'
    cursor = db[ASSETS].find(query, _ASSET_FIELDS)
    if cap is not None:
        cursor = cursor.limit(cap + 1)
    data.assets = [_asset_row(doc) async for doc in cursor]
    if cap is not None and len(data.assets) > cap:
        data.assets, data.truncated = data.assets[:cap], True
    if fmt == 'docker':
        tags = db[DOCKER_TAGS].find({'repo_id': repo_id}, {'image': 1, 'tag': 1, 'manifest_digest': 1, 'updated_at': 1})
        if cap is not None:
            tags = tags.limit(cap + 1)
        data.tags = [
            TagRow(
                image=str(d['image']), tag=str(d['tag']), digest=str(d['manifest_digest']), updated_at=d['updated_at']
            )
            async for d in tags
        ]
        projection = {
            'image': 1,
            'digest': 1,
            'updated_at': 1,
            'created_at': 1,
            'manifests.digest': 1,
            'subject_digest': 1,
            'layers.digest': 1,
            'config_digest': 1,
        }
        manifests = db[DOCKER_MANIFESTS].find({'repo_id': repo_id}, projection)
        if cap is not None:
            manifests = manifests.limit(cap + 1)
        data.manifests = [_manifest_row(d) async for d in manifests]
        if cap is not None and (len(data.tags) > cap or len(data.manifests) > cap):
            data.tags, data.manifests, data.truncated = data.tags[:cap], data.manifests[:cap], True
    return data


# ---------------------------------------------------------------- removal
@dataclass
class Removed:
    items: int = 0
    bytes: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    def add(self, other: Removed) -> None:
        self.items += other.items
        self.bytes += other.bytes
        for kind, n in other.by_kind.items():
            self.by_kind[kind] = self.by_kind.get(kind, 0) + n


async def _delete_asset(db: Database, repo_id: str, c: Candidate) -> int:
    """Delete the planned asset unless it changed since (re-fetched with other content): bytes removed (also taken
    off the repository's usage, ``waystation_common.db.usage``)."""
    if c.asset_id is None:
        return -1
    doc = await db[ASSETS].find_one_and_delete(
        {'_id': c.asset_id, 'blob_sha256': c.blob_sha256}, projection={'size': 1, 'kind': 1}
    )
    if doc is None:
        return -1
    artifact = int(doc.get('kind') == 'content')
    await usage_db.add(db, repo_id, -int(doc.get('size') or 0), -1, artifacts_delta=-artifact)
    return c.size


async def _remove_one(db: Database, repo_id: str, c: Candidate) -> tuple[bool, int]:
    removed = False
    if c.kind == 'tag':
        result = await db[DOCKER_TAGS].delete_one(
            {'repo_id': repo_id, 'image': c.image, 'tag': c.tag, 'manifest_digest': c.digest}
        )
        removed = bool(result.deleted_count)
    elif c.kind == 'manifest':
        result = await db[DOCKER_MANIFESTS].delete_one({'repo_id': repo_id, 'image': c.image, 'digest': c.digest})
        removed = bool(result.deleted_count)
    freed = await _delete_asset(db, repo_id, c)
    return removed or freed >= 0, max(freed, 0)


async def remove(
    db: Database,
    repo_id: str,
    plan: Sequence[Candidate],
    *,
    progress: Callable[[int], Awaitable[None]] | None = None,
    batch: int = 500,
) -> Removed:
    """Remove the planned records (idempotent: what is already gone is skipped). ``progress(done)`` per batch."""
    total = Removed()
    limiter = asyncio.Semaphore(REMOVE_CONCURRENCY)

    async def one(c: Candidate) -> tuple[bool, int]:
        async with limiter:
            return await _remove_one(db, repo_id, c)

    for start in range(0, len(plan), batch):
        chunk = plan[start : start + batch]
        results = await asyncio.gather(*(one(c) for c in chunk))
        for c, (removed, freed) in zip(chunk, results, strict=True):
            if removed:
                total.items += 1
                total.bytes += freed
                total.by_kind[c.kind] = total.by_kind.get(c.kind, 0) + 1
        if progress is not None:
            await progress(start + len(chunk))
    return total


async def repository_name(db: Database, repo_id: str) -> str | None:
    doc = await db[REPOSITORIES].find_one({'_id': repo_id}, {'name': 1})
    return str(doc['name']) if doc else None


def as_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None
