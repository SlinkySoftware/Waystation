"""``roles`` (custom roles) and ``rbac_state`` (the ``grant_version`` counter) collection access.

Every RBAC change (roles, groups, a user's or service account's roles/groups/status, a provider's group mapping)
calls ``bump`` after its write: ``grant_version`` is what replicas watch (change stream) and what materialised grants
are keyed by (ARCHITECTURE §7).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database
from waystation_common.db.collections import GROUPS, RBAC_STATE, ROLES, SERVICE_ACCOUNTS, USERS
from waystation_common.errors import Conflict
from waystation_server.security.identity import RoleRecord

STATE_ID = 'grant_version'
MAX_ROLES = 1000


async def list_all(db: Database) -> list[RoleRecord]:
    cursor = db[ROLES].find({}).sort('_id', 1).limit(MAX_ROLES)
    return [RoleRecord.model_validate(doc) async for doc in cursor]


async def get(db: Database, role_id: str) -> RoleRecord | None:
    doc = await db[ROLES].find_one({'_id': role_id})
    return RoleRecord.model_validate(doc) if doc else None


async def count(db: Database) -> int:
    return await db[ROLES].count_documents({})


async def insert(db: Database, record: RoleRecord) -> None:
    try:
        await db[ROLES].insert_one(record.model_dump(by_alias=True))
    except DuplicateKeyError:
        raise Conflict('a role with this id already exists', field='id') from None


async def update(db: Database, role_id: str, changes: dict[str, Any]) -> RoleRecord | None:
    doc = await db[ROLES].find_one_and_update({'_id': role_id}, {'$set': changes}, return_document=ReturnDocument.AFTER)
    return RoleRecord.model_validate(doc) if doc else None


async def delete(db: Database, role_id: str) -> bool:
    result = await db[ROLES].delete_one({'_id': role_id})
    return result.deleted_count == 1


async def usage(db: Database, role_id: str) -> dict[str, int]:
    """How many users, groups and service accounts hold ``role_id`` directly."""
    return {
        'users': await db[USERS].count_documents({'roles': role_id}),
        'groups': await db[GROUPS].count_documents({'roles': role_id}),
        'service_accounts': await db[SERVICE_ACCOUNTS].count_documents({'roles': role_id}),
    }


# ---------------------------------------------------------------------------- grant_version
async def version(db: Database) -> int:
    doc = await db[RBAC_STATE].find_one({'_id': STATE_ID})
    return int(doc.get('grant_version', 0)) if doc else 0


async def bump(db: Database) -> int:
    """Increment ``grant_version`` (creating the singleton on first use); returns the new value."""
    doc = await db[RBAC_STATE].find_one_and_update(
        {'_id': STATE_ID}, {'$inc': {'grant_version': 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return int(doc['grant_version']) if doc else 0


async def watch(db: Database) -> AsyncIterator[int]:
    """Yield the new ``grant_version`` whenever another process bumps it (MongoDB change stream on ``rbac_state``;
    needs a replica set, which every deployment runs)."""
    pipeline = [{'$match': {'documentKey._id': STATE_ID}}]
    async with await db[RBAC_STATE].watch(pipeline, full_document='updateLookup') as stream:
        async for change in stream:
            doc = change.get('fullDocument') or {}
            yield int(doc.get('grant_version', 0))
