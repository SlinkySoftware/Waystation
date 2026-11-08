"""``assets`` and ``blobs`` collection access for the proxy engine. Every query uses ``repo_path_unique`` or
``_id``. Writes happen on cache fills and revalidations only, never per cache hit (ARCHITECTURE §8)."""

from __future__ import annotations

from datetime import datetime

from pymongo import ReturnDocument

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS
from waystation_common.ids import new_id
from waystation_server.proxy.records import StoredAsset, UpstreamMeta


async def get(db: Database, repo_id: str, path: str) -> StoredAsset | None:
    doc = await db[ASSETS].find_one({'repo_id': repo_id, 'path': path})
    return StoredAsset.model_validate(doc) if doc else None


async def save(db: Database, asset: StoredAsset) -> StoredAsset:
    """Insert or replace the asset at ``(repo_id, path)``; keeps the original ``_id`` and ``created_at``."""
    fields = asset.model_dump(by_alias=True, exclude={'id', 'created_at'})
    doc = await db[ASSETS].find_one_and_update(
        {'repo_id': asset.repo_id, 'path': asset.path},
        {'$set': fields, '$setOnInsert': {'_id': new_id(), 'created_at': asset.created_at}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return StoredAsset.model_validate(doc)


async def mark_revalidated(db: Database, asset_id: str, upstream: UpstreamMeta, updated_at: datetime) -> None:
    await db[ASSETS].update_one(
        {'_id': asset_id}, {'$set': {'upstream': upstream.model_dump(), 'updated_at': updated_at}}
    )


async def delete(db: Database, asset_id: str) -> None:
    await db[ASSETS].delete_one({'_id': asset_id})
