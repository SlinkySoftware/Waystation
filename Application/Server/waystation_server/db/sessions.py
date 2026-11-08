"""``sessions`` collection access (TTL index on ``expires_at`` removes expired sessions)."""

from __future__ import annotations

from waystation_common.db.client import Database
from waystation_common.db.collections import SESSIONS
from waystation_server.security.identity import SessionRecord


async def insert(db: Database, session: SessionRecord) -> None:
    await db[SESSIONS].insert_one(session.model_dump(by_alias=True))


async def get(db: Database, session_id: str) -> SessionRecord | None:
    doc = await db[SESSIONS].find_one({'_id': session_id})
    return SessionRecord.model_validate(doc) if doc else None


async def delete(db: Database, session_id: str) -> None:
    await db[SESSIONS].delete_one({'_id': session_id})


async def delete_for_user(db: Database, user_id: str) -> int:
    result = await db[SESSIONS].delete_many({'user_id': user_id})
    return result.deleted_count
