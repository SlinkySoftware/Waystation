"""``auth_providers`` collection access."""

from __future__ import annotations

from typing import Any

from pymongo import ReturnDocument

from waystation_common.db.client import Database
from waystation_common.db.collections import AUTH_PROVIDERS
from waystation_server.security.providers import ProviderRecord


async def list_all(db: Database) -> list[ProviderRecord]:
    return [ProviderRecord.model_validate(doc) async for doc in db[AUTH_PROVIDERS].find({}).sort('order', 1)]


async def get(db: Database, provider_id: str) -> ProviderRecord | None:
    doc = await db[AUTH_PROVIDERS].find_one({'_id': provider_id})
    return ProviderRecord.model_validate(doc) if doc else None


async def insert(db: Database, doc: dict[str, Any]) -> ProviderRecord:
    await db[AUTH_PROVIDERS].insert_one(doc)
    return ProviderRecord.model_validate(doc)


async def replace(db: Database, provider_id: str, fields: dict[str, Any]) -> ProviderRecord | None:
    """Set ``fields`` and bump ``config_version`` (cached password checks of the old configuration stop matching)."""
    doc = await db[AUTH_PROVIDERS].find_one_and_update(
        {'_id': provider_id},
        {'$set': fields, '$inc': {'config_version': 1}},
        return_document=ReturnDocument.AFTER,
    )
    return ProviderRecord.model_validate(doc) if doc else None


async def delete(db: Database, provider_id: str) -> bool:
    result = await db[AUTH_PROVIDERS].delete_one({'_id': provider_id})
    return result.deleted_count == 1
