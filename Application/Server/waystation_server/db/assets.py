"""``assets`` and ``blobs`` collection access for the proxy engine. Every query uses ``repo_path_unique`` or
``_id``. Writes happen on cache fills and revalidations only, never per cache hit (ARCHITECTURE §8). Reads retry
once on ``AutoReconnect`` (``waystation_common.db.retry``); writes never do.

Every write and delete here also counts the bytes in the repository's usage (``waystation_common.db.usage``, one
``$inc``; quotas): a new asset adds its size, a replaced one the difference, a deleted one subtracts it."""

from __future__ import annotations

from datetime import datetime

from pymongo import ReturnDocument

from waystation_common.db import usage as usage_db
from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS
from waystation_common.db.retry import read_retry
from waystation_common.ids import new_id
from waystation_common.searchkeys import file_keys
from waystation_server.proxy.records import StoredAsset, UpstreamMeta


@read_retry  # the cache-hit lookup: a transient AutoReconnect must not answer 500
async def get(db: Database, repo_id: str, path: str) -> StoredAsset | None:
    doc = await db[ASSETS].find_one({'repo_id': repo_id, 'path': path})
    return StoredAsset.model_validate(doc) if doc else None


async def save(db: Database, asset: StoredAsset) -> StoredAsset:
    """Insert or replace the asset at ``(repo_id, path)``; keeps the original ``_id`` and ``created_at``."""
    fields = asset.model_dump(by_alias=True, exclude={'id', 'created_at'})
    if asset.kind == 'content':
        fields |= file_keys(asset.path)  # global search (waystation_common.searchkeys)
    asset_id = new_id()
    before = await db[ASSETS].find_one_and_update(
        {'repo_id': asset.repo_id, 'path': asset.path},
        {'$set': fields, '$setOnInsert': {'_id': asset_id, 'created_at': asset.created_at}},
        upsert=True,
        return_document=ReturnDocument.BEFORE,
        projection={'_id': 1, 'created_at': 1, 'size': 1, 'kind': 1},
    )
    artifact = int(asset.kind == 'content')
    if before is None:
        await usage_db.add(db, asset.repo_id, asset.size, 1, artifacts_delta=artifact)
        return asset.model_copy(update={'id': asset_id})
    was_artifact = int(before.get('kind') == 'content')
    await usage_db.add(
        db, asset.repo_id, asset.size - int(before.get('size') or 0), artifacts_delta=artifact - was_artifact
    )
    return asset.model_copy(update={'id': str(before['_id']), 'created_at': before['created_at']})


async def mark_revalidated(db: Database, asset_id: str, upstream: UpstreamMeta, updated_at: datetime) -> None:
    await db[ASSETS].update_one(
        {'_id': asset_id}, {'$set': {'upstream': upstream.model_dump(), 'updated_at': updated_at}}
    )


async def delete(db: Database, asset_id: str) -> None:
    doc = await db[ASSETS].find_one_and_delete({'_id': asset_id}, projection={'repo_id': 1, 'size': 1, 'kind': 1})
    if doc is not None:
        artifact = int(doc.get('kind') == 'content')
        await usage_db.add(db, str(doc['repo_id']), -int(doc.get('size') or 0), -1, artifacts_delta=-artifact)


async def delete_path(db: Database, repo_id: str, path: str) -> StoredAsset | None:
    """Remove the asset at ``(repo_id, path)`` (hosted deletes); returns what was removed."""
    doc = await db[ASSETS].find_one_and_delete({'repo_id': repo_id, 'path': path})
    if doc is None:
        return None
    asset = StoredAsset.model_validate(doc)
    await usage_db.add(db, repo_id, -asset.size, -1, artifacts_delta=-int(asset.kind == 'content'))
    return asset


@read_retry
async def sizes(db: Database, repo_id: str, paths: list[str]) -> dict[str, int]:
    """Which of ``paths`` the repository holds, with their sizes (index ``repo_path_unique``)."""
    if not paths:
        return {}
    cursor = db[ASSETS].find(
        {'repo_id': repo_id, 'path': {'$in': sorted(set(paths))}}, {'path': 1, 'size': 1, '_id': 0}
    )
    return {str(doc['path']): int(doc['size']) async for doc in cursor}
