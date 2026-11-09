"""``users`` collection access."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database
from waystation_common.db.collections import USERS
from waystation_common.errors import Conflict
from waystation_server.security.identity import ExternalGroupRef, LinkedIdentity, Preferences, UserRecord


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


async def sync_external(
    db: Database,
    user_id: str,
    *,
    display_name: str | None,
    email: str | None,
    external_groups: list[ExternalGroupRef],
    identities: list[LinkedIdentity],
    now: datetime,
    source: str | None = None,
) -> UserRecord | None:
    """Record a sign-in through an external provider (profile, group sync, linked identity). ``None`` when the user
    vanished, was disabled or got a local password meanwhile (never link to an account with a local password)."""
    update: dict[str, Any] = {
        'external_groups': [g.model_dump() for g in external_groups],
        'identities': [i.model_dump() for i in identities],
        'last_login': now,
    }
    if display_name:
        update['display_name'] = display_name
    if email is not None:
        update['email'] = email
    if source is not None:
        update['source'] = source
    doc = await db[USERS].find_one_and_update(
        {'_id': user_id, 'disabled': {'$ne': True}, 'password_hash': None},
        {'$set': update},
        return_document=ReturnDocument.AFTER,
    )
    return UserRecord.model_validate(doc) if doc else None


# ---------------------------------------------------------------------------- administration (SPEC §4.12)
LIST_MAX = 200


async def list_page(
    db: Database,
    *,
    prefix: str | None = None,
    source: str | None = None,
    disabled: bool | None = None,
    group_id: str | None = None,
    role: str | None = None,
    after: str | None = None,
    limit: int = 50,
) -> list[UserRecord]:
    """Users by username (index ``username_unique``), after the username ``after`` (keyset paging). ``prefix``
    matches the start of the (lower-case) username; ``source`` is ``local``, a provider source (``ldap:<id>``) or a
    kind (``ldap``/``oidc``: every provider of that kind)."""
    query: dict[str, Any] = {}
    names: dict[str, Any] = {}
    if prefix:
        names['$regex'] = '^' + re.escape(prefix.lower())
    if after:
        names['$gt'] = after
    if names:
        query['username'] = names
    if source:
        query['source'] = source if ':' in source or source == 'local' else {'$regex': '^' + re.escape(source) + ':'}
    if disabled is not None:
        query['disabled'] = True if disabled else {'$ne': True}
    if group_id:
        query['groups'] = group_id
    if role:
        query['roles'] = role
    cursor = db[USERS].find(query).sort('username', 1).limit(max(1, min(limit, LIST_MAX)))
    return [UserRecord.model_validate(doc) async for doc in cursor]


async def update(db: Database, user_id: str, changes: dict[str, Any]) -> UserRecord | None:
    doc = await db[USERS].find_one_and_update({'_id': user_id}, {'$set': changes}, return_document=ReturnDocument.AFTER)
    return UserRecord.model_validate(doc) if doc else None


async def set_password(db: Database, user_id: str, password_hash: str, *, must_change: bool) -> UserRecord | None:
    """A new local password; clears the lockout and the failure count. Local accounts only."""
    doc = await db[USERS].find_one_and_update(
        {'_id': user_id, 'source': 'local'},
        {
            '$set': {
                'password_hash': password_hash,
                'must_change_password': must_change,
                'failed_logins': 0,
                'locked_until': None,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    return UserRecord.model_validate(doc) if doc else None


async def unlock(db: Database, user_id: str) -> UserRecord | None:
    return await update(db, user_id, {'failed_logins': 0, 'locked_until': None})


async def delete(db: Database, user_id: str) -> bool:
    result = await db[USERS].delete_one({'_id': user_id})
    return result.deleted_count == 1


async def add_group(db: Database, user_ids: list[str], group_id: str) -> int:
    result = await db[USERS].update_many({'_id': {'$in': user_ids}}, {'$addToSet': {'groups': group_id}})
    return result.modified_count


async def remove_group(db: Database, group_id: str, user_ids: list[str] | None = None) -> int:
    """Remove a local group from ``user_ids`` (None: from every member, when the group is deleted)."""
    query: dict[str, Any] = {'groups': group_id}
    if user_ids is not None:
        query['_id'] = {'$in': user_ids}
    result = await db[USERS].update_many(query, {'$pull': {'groups': group_id}})
    return result.modified_count


async def count_members(db: Database, group_ids: list[str]) -> dict[str, int]:
    """``{local group id: members}`` (index ``groups``)."""
    if not group_ids:
        return {}
    pipeline: list[dict[str, Any]] = [
        {'$match': {'groups': {'$in': group_ids}}},
        {'$unwind': '$groups'},
        {'$match': {'groups': {'$in': group_ids}}},
        {'$group': {'_id': '$groups', 'n': {'$sum': 1}}},
    ]
    cursor = await db[USERS].aggregate(pipeline)
    return {doc['_id']: int(doc['n']) async for doc in cursor}


async def with_roles(db: Database, roles: list[str]) -> list[UserRecord]:
    """Enabled users holding any of ``roles`` directly (last-administrator checks)."""
    cursor = db[USERS].find({'roles': {'$in': roles}, 'disabled': {'$ne': True}}).limit(1000)
    return [UserRecord.model_validate(doc) async for doc in cursor]


async def in_groups(db: Database, group_ids: list[str]) -> list[UserRecord]:
    """Enabled users in any of the local groups ``group_ids``."""
    if not group_ids:
        return []
    cursor = db[USERS].find({'groups': {'$in': group_ids}, 'disabled': {'$ne': True}}).limit(1000)
    return [UserRecord.model_validate(doc) async for doc in cursor]


async def get_many(db: Database, user_ids: list[str]) -> list[UserRecord]:
    if not user_ids:
        return []
    return [UserRecord.model_validate(doc) async for doc in db[USERS].find({'_id': {'$in': user_ids}})]
