"""``upload_sessions`` access (DATA_MODEL): resumable hosted uploads (OCI chunked blob uploads).

A session names a resumable upload file in the blob store (``uploads/<id>``, the session id) and records how many
bytes it holds. One request at a time may append: ``claim`` takes a short lease (``busy_until``) only when the stored
offset is the one the request starts from, ``release`` stores the new offset and drops the lease. Sessions expire
``SESSION_IDLE_S`` after their last activity (TTL index); blob GC removes upload files untouched for a day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ReturnDocument

from waystation_common.db.client import Database
from waystation_common.db.collections import UPLOAD_SESSIONS

SESSION_IDLE_S = 12 * 3600
BUSY_LEASE_S = 15 * 60  # a stuck request (killed replica) blocks its session at most this long


@dataclass(frozen=True)
class UploadSession:
    id: str
    repo_id: str
    image: str
    principal: str
    bytes_received: int
    started_at: datetime

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> UploadSession:
        return cls(
            id=str(doc['_id']),
            repo_id=str(doc['repo_id']),
            image=str(doc['image']),
            principal=str(doc.get('principal') or ''),
            bytes_received=int(doc.get('bytes_received') or 0),
            started_at=doc['started_at'],
        )


async def create(db: Database, session_id: str, *, repo_id: str, image: str, principal: str) -> UploadSession:
    now = datetime.now(UTC)
    doc = {
        '_id': session_id,
        'repo_id': repo_id,
        'image': image,
        'kind': 'oci-chunked',
        'principal': principal,
        'bytes_received': 0,
        'busy_until': None,
        'started_at': now,
        'updated_at': now,
        'expires_at': now + timedelta(seconds=SESSION_IDLE_S),
    }
    await db[UPLOAD_SESSIONS].insert_one(doc)
    return UploadSession.from_doc(doc)


async def get(db: Database, session_id: str, repo_id: str, image: str) -> UploadSession | None:
    doc = await db[UPLOAD_SESSIONS].find_one({'_id': session_id, 'repo_id': repo_id, 'image': image})
    if doc is None or doc['expires_at'].replace(tzinfo=UTC) <= datetime.now(UTC):
        return None
    return UploadSession.from_doc(doc)


async def claim(db: Database, session_id: str, *, offset: int | None) -> UploadSession | None:
    """Take the append lease when nobody holds it (and the session holds ``offset`` bytes, unless None)."""
    now = datetime.now(UTC)
    query: dict[str, Any] = {
        '_id': session_id,
        'expires_at': {'$gt': now},
        '$or': [{'busy_until': None}, {'busy_until': {'$lt': now}}],
    }
    if offset is not None:
        query['bytes_received'] = offset
    doc = await db[UPLOAD_SESSIONS].find_one_and_update(
        query,
        {'$set': {'busy_until': now + timedelta(seconds=BUSY_LEASE_S), 'updated_at': now}},
        return_document=ReturnDocument.AFTER,
    )
    return UploadSession.from_doc(doc) if doc else None


async def release(db: Database, session_id: str, *, bytes_received: int) -> None:
    now = datetime.now(UTC)
    await db[UPLOAD_SESSIONS].update_one(
        {'_id': session_id},
        {
            '$set': {
                'bytes_received': bytes_received,
                'busy_until': None,
                'updated_at': now,
                'expires_at': now + timedelta(seconds=SESSION_IDLE_S),
            }
        },
    )


async def delete(db: Database, session_id: str) -> None:
    await db[UPLOAD_SESSIONS].delete_one({'_id': session_id})


async def delete_repo(db: Database, repo_id: str) -> list[str]:
    """Drop a deleted repository's sessions; returns their ids (the caller discards the upload files)."""
    ids = [str(doc['_id']) async for doc in db[UPLOAD_SESSIONS].find({'repo_id': repo_id}, {'_id': 1})]
    if ids:
        await db[UPLOAD_SESSIONS].delete_many({'_id': {'$in': ids}})
    return ids
