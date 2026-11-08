"""``users`` collection access."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database
from waystation_common.db.collections import USERS
from waystation_common.errors import Conflict
from waystation_server.security.identity import Preferences, UserRecord


async def count(db: Database) -> int:
    return await db[USERS].count_documents({})


async def get_by_username(db: Database, username: str) -> UserRecord | None:
    doc = await db[USERS].find_one({'username': username.lower()})
    return UserRecord.model_validate(doc) if doc else None


async def get(db: Database, user_id: str) -> UserRecord | None:
    doc = await db[USERS].find_one({'_id': user_id})
    return UserRecord.model_validate(doc) if doc else None


async def insert(db: Database, user: UserRecord) -> None:
    try:
        await db[USERS].insert_one(user.model_dump(by_alias=True))
    except DuplicateKeyError:
        raise Conflict('a user with this username already exists') from None


async def record_success(db: Database, user_id: str, now: datetime, *, password_hash: str | None = None) -> None:
    update: dict[str, Any] = {'last_login': now, 'failed_logins': 0, 'locked_until': None}
    if password_hash is not None:
        update['password_hash'] = password_hash
    await db[USERS].update_one({'_id': user_id}, {'$set': update})


async def record_failure(db: Database, user_id: str, *, threshold: int, locked_until: datetime) -> int:
    """Count a failed sign-in; lock the account when the count reaches ``threshold``. Returns the new count."""
    doc = await db[USERS].find_one_and_update(
        {'_id': user_id}, {'$inc': {'failed_logins': 1}}, return_document=ReturnDocument.AFTER
    )
    failures = int(doc['failed_logins']) if doc else 0
    if failures >= threshold:
        await db[USERS].update_one({'_id': user_id}, {'$set': {'locked_until': locked_until, 'failed_logins': 0}})
    return failures


async def usernames(db: Database, user_ids: list[str]) -> dict[str, str]:
    """``{user id: username}`` for listings."""
    if not user_ids:
        return {}
    cursor = db[USERS].find({'_id': {'$in': user_ids}}, {'username': 1})
    return {doc['_id']: doc['username'] async for doc in cursor}


async def set_preferences(db: Database, user_id: str, preferences: Preferences) -> None:
    await db[USERS].update_one({'_id': user_id}, {'$set': {'preferences': preferences.model_dump()}})
