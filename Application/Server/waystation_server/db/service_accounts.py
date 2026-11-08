"""``service_accounts`` collection access (ARCHITECTURE §3)."""

from __future__ import annotations

from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database
from waystation_common.db.collections import SERVICE_ACCOUNTS
from waystation_common.errors import Conflict
from waystation_server.security.identity import ServiceAccountRecord

LIST_LIMIT = 1000


async def list_all(db: Database) -> list[ServiceAccountRecord]:
    cursor = db[SERVICE_ACCOUNTS].find({}).sort('name', 1).limit(LIST_LIMIT)
    return [ServiceAccountRecord.model_validate(doc) async for doc in cursor]


async def get(db: Database, account_id: str) -> ServiceAccountRecord | None:
    doc = await db[SERVICE_ACCOUNTS].find_one({'_id': account_id})
    return ServiceAccountRecord.model_validate(doc) if doc else None


async def insert(db: Database, record: ServiceAccountRecord) -> None:
    try:
        await db[SERVICE_ACCOUNTS].insert_one(record.model_dump(by_alias=True))
    except DuplicateKeyError:
        raise Conflict('a service account with this name already exists') from None


async def update(db: Database, account_id: str, changes: dict[str, Any]) -> ServiceAccountRecord | None:
    doc = await db[SERVICE_ACCOUNTS].find_one_and_update(
        {'_id': account_id}, {'$set': changes}, return_document=ReturnDocument.AFTER
    )
    return ServiceAccountRecord.model_validate(doc) if doc else None


async def names(db: Database, account_ids: list[str]) -> dict[str, str]:
    """``{account id: name}`` for listings."""
    if not account_ids:
        return {}
    cursor = db[SERVICE_ACCOUNTS].find({'_id': {'$in': account_ids}}, {'name': 1})
    return {doc['_id']: doc['name'] async for doc in cursor}


async def delete(db: Database, account_id: str) -> bool:
    result = await db[SERVICE_ACCOUNTS].delete_one({'_id': account_id})
    return result.deleted_count == 1
