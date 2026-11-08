"""``api_tokens`` collection access (ARCHITECTURE §3). Lookups go by the keyed hash (index ``hash_unique``) or by
owner (index ``principal_id``); nothing here sees a token secret."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import API_TOKENS
from waystation_server.security.identity import ApiTokenRecord, TokenOwner

LIST_LIMIT = 500


async def insert(db: Database, record: ApiTokenRecord) -> None:
    await db[API_TOKENS].insert_one(record.model_dump(by_alias=True, mode='python'))


async def find_by_hashes(db: Database, hashes: list[str]) -> ApiTokenRecord | None:
    """The token whose stored hash is one of ``hashes`` (one per configured secret key)."""
    doc = await db[API_TOKENS].find_one({'hash': {'$in': hashes}})
    return ApiTokenRecord.model_validate(doc) if doc else None


async def get(db: Database, token_id: str) -> ApiTokenRecord | None:
    doc = await db[API_TOKENS].find_one({'_id': token_id})
    return ApiTokenRecord.model_validate(doc) if doc else None


async def list_for(db: Database, owner: TokenOwner) -> list[ApiTokenRecord]:
    cursor = db[API_TOKENS].find({'principal.id': owner.id, 'principal.kind': owner.kind}).sort('_id', -1)
    return [ApiTokenRecord.model_validate(doc) async for doc in cursor.limit(LIST_LIMIT)]


async def list_all(db: Database, *, limit: int = LIST_LIMIT) -> list[ApiTokenRecord]:
    cursor = db[API_TOKENS].find({}).sort('_id', -1).limit(limit)
    return [ApiTokenRecord.model_validate(doc) async for doc in cursor]


async def count_active(db: Database, owner: TokenOwner, now: datetime) -> int:
    return await db[API_TOKENS].count_documents(
        {
            'principal.id': owner.id,
            'principal.kind': owner.kind,
            'revoked_at': None,
            '$or': [{'expires_at': None}, {'expires_at': {'$gt': now}}],
        }
    )


async def revoke(db: Database, token_id: str, *, now: datetime, by: str | None) -> bool:
    """Mark a token revoked; False when it does not exist or was already revoked."""
    result = await db[API_TOKENS].update_one(
        {'_id': token_id, 'revoked_at': None}, {'$set': {'revoked_at': now, 'revoked_by': by}}
    )
    return result.modified_count == 1


async def revoke_all_for(db: Database, owner: TokenOwner, *, now: datetime, by: str | None) -> list[str]:
    """Revoke every live token of ``owner``; returns their ids."""
    query: dict[str, Any] = {'principal.id': owner.id, 'principal.kind': owner.kind, 'revoked_at': None}
    ids = [doc['_id'] async for doc in db[API_TOKENS].find(query, {'_id': 1})]
    if ids:
        await db[API_TOKENS].update_many(
            {'_id': {'$in': ids}, 'revoked_at': None}, {'$set': {'revoked_at': now, 'revoked_by': by}}
        )
    return ids
