"""``login_challenges`` collection access: sign-ins waiting for their second factor (TTL index on ``expires_at``).

``_id`` is the sha256 of the ``ws_mfa`` cookie value, so a database dump holds no usable challenge.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pymongo import ReturnDocument

from waystation_common.db.client import Database
from waystation_common.db.collections import LOGIN_CHALLENGES


class LoginChallenge(BaseModel):
    model_config = ConfigDict(extra='ignore', populate_by_name=True)

    id: str = Field(alias='_id')
    user_id: str
    created_at: datetime
    expires_at: datetime
    attempts: int = 0
    ip: str | None = None


async def insert(db: Database, challenge: LoginChallenge) -> None:
    await db[LOGIN_CHALLENGES].insert_one(challenge.model_dump(by_alias=True))


async def get(db: Database, challenge_id: str, now: datetime) -> LoginChallenge | None:
    """The live challenge (the TTL monitor runs only once a minute, so expiry is checked here too)."""
    doc = await db[LOGIN_CHALLENGES].find_one({'_id': challenge_id, 'expires_at': {'$gt': now}})
    return LoginChallenge.model_validate(doc) if doc else None


async def count_attempt(db: Database, challenge_id: str) -> int:
    """Count a wrong code; returns the attempts so far (0 when the challenge is gone)."""
    doc = await db[LOGIN_CHALLENGES].find_one_and_update(
        {'_id': challenge_id}, {'$inc': {'attempts': 1}}, return_document=ReturnDocument.AFTER
    )
    return int(doc['attempts']) if doc else 0


async def take(db: Database, challenge_id: str) -> bool:
    """Remove the challenge; True for the one caller that removed it (a challenge opens one session)."""
    result = await db[LOGIN_CHALLENGES].delete_one({'_id': challenge_id})
    return result.deleted_count == 1


async def delete_for_user(db: Database, user_id: str) -> int:
    result = await db[LOGIN_CHALLENGES].delete_many({'user_id': user_id})
    return result.deleted_count
