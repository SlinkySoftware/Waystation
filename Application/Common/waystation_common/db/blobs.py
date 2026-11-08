"""``blobs`` collection (DATA_MODEL): one document per blob file, the state blob GC works on (SPECIFICATION §4.8).

Fields: ``_id`` = sha256 hex, ``size``, ``first_seen``, ``claimed_at`` (last time a commit put or found the file in
place for a new reference), ``gc_marked_at`` (when GC first found it unreferenced; cleared when referenced or
claimed again), ``gc_state`` = ``deleting`` + ``gc_deleting_at`` (GC is removing the file right now).

Protocol between writers and GC (no blob that is or is about to be referenced is ever deleted):

* **Claim** (``claim``; ``BlobStore.commit`` through ``MongoBlobGuard``, before the rename): sets ``claimed_at``,
  clears the mark, and fails while ``gc_state`` is ``deleting`` (the upsert collides on ``_id``), in which case the
  writer waits until GC is done (the file is then gone and the writer puts its own copy in place).
* **Delete** (``begin_delete``): a compare-and-set to ``deleting`` that only matches when the mark *and* the last
  claim are older than the grace period. GC then re-checks that no asset references the blob, unlinks the file and
  removes the document (``finish_delete``), or reverts (``abort_delete``).

A claim that happened after GC's reference check therefore always wins: either its ``claimed_at`` makes the
compare-and-set miss, or it waits for the deletion to finish and writes the file again.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database, Document
from waystation_common.db.collections import ASSETS, BLOBS
from waystation_common.errors import Conflict

DELETING = 'deleting'
STALE_DELETING_S = 600.0  # a deletion that started this long ago was abandoned (GC died): claims may take over
CLAIM_WAIT_S = 30.0
CLAIM_POLL_S = 0.05


async def claim(db: Database, sha256: str, size: int, *, wait_s: float = CLAIM_WAIT_S) -> None:
    deadline = time.monotonic() + wait_s
    while True:
        now = datetime.now(UTC)
        try:
            await db[BLOBS].update_one(
                {'_id': sha256, 'gc_state': {'$ne': DELETING}},
                {
                    '$set': {'claimed_at': now},
                    '$unset': {'gc_marked_at': ''},
                    '$setOnInsert': {'size': size, 'first_seen': now},
                },
                upsert=True,
            )
            return
        except DuplicateKeyError:
            pass  # GC is deleting this blob (or a concurrent first claim inserted it: retry at once)
        stale = now - timedelta(seconds=STALE_DELETING_S)
        await db[BLOBS].update_one(
            {'_id': sha256, 'gc_state': DELETING, 'gc_deleting_at': {'$lt': stale}},
            {'$unset': {'gc_state': '', 'gc_deleting_at': ''}},
        )
        if time.monotonic() > deadline:
            raise Conflict('the blob is being garbage-collected; try again')
        await asyncio.sleep(CLAIM_POLL_S)


class MongoBlobGuard:
    """``waystation_common.storage.BlobGuard`` backed by the ``blobs`` collection."""

    def __init__(self, db: Database, *, wait_s: float = CLAIM_WAIT_S) -> None:
        self.db = db
        self.wait_s = wait_s

    async def claim(self, sha256: str, size: int) -> None:
        await claim(self.db, sha256, size, wait_s=self.wait_s)


# ---------------------------------------------------------------- GC
async def page(db: Database, *, after: str | None, limit: int = 1000) -> list[Document]:
    query: Document = {'_id': {'$gt': after}} if after is not None else {}
    return [doc async for doc in db[BLOBS].find(query).sort('_id', 1).limit(limit)]


async def referenced(db: Database, sha256s: Sequence[str]) -> set[str]:
    """The given blobs that at least one asset references (index ``blob_sha256``)."""
    if not sha256s:
        return set()
    values = await db[ASSETS].distinct('blob_sha256', {'blob_sha256': {'$in': list(sha256s)}})
    return {str(v) for v in values}


async def is_referenced(db: Database, sha256: str) -> bool:
    return await db[ASSETS].find_one({'blob_sha256': sha256}, {'_id': 1}) is not None


async def known(db: Database, sha256s: Sequence[str]) -> set[str]:
    if not sha256s:
        return set()
    return {str(doc['_id']) async for doc in db[BLOBS].find({'_id': {'$in': list(sha256s)}}, {'_id': 1})}


async def adopt(db: Database, sha256: str, size: int, first_seen: datetime) -> bool:
    """A blob file without a document (written before claims existed, or by a crashed writer): track it."""
    try:
        result = await db[BLOBS].update_one(
            {'_id': sha256}, {'$setOnInsert': {'size': size, 'first_seen': first_seen}}, upsert=True
        )
    except DuplicateKeyError:  # pragma: no cover - a claim inserted it concurrently
        return False
    return result.upserted_id is not None


async def mark(db: Database, sha256: str, now: datetime) -> bool:
    result = await db[BLOBS].update_one(
        {'_id': sha256, 'gc_marked_at': {'$exists': False}, 'gc_state': {'$exists': False}},
        {'$set': {'gc_marked_at': now}},
    )
    return result.modified_count == 1


async def unmark(db: Database, sha256s: Sequence[str]) -> int:
    if not sha256s:
        return 0
    result = await db[BLOBS].update_many(
        {'_id': {'$in': list(sha256s)}, 'gc_marked_at': {'$exists': True}, 'gc_state': {'$exists': False}},
        {'$unset': {'gc_marked_at': ''}},
    )
    return result.modified_count


async def begin_delete(db: Database, sha256: str, *, cutoff: datetime, now: datetime) -> bool:
    """Compare-and-set to ``deleting``: only when marked and unclaimed since ``cutoff`` (= now - grace)."""
    result = await db[BLOBS].update_one(
        {
            '_id': sha256,
            'gc_state': {'$exists': False},
            'gc_marked_at': {'$lte': cutoff},
            '$or': [{'claimed_at': {'$exists': False}}, {'claimed_at': {'$lte': cutoff}}],
        },
        {'$set': {'gc_state': DELETING, 'gc_deleting_at': now}},
    )
    return result.modified_count == 1


async def abort_delete(db: Database, sha256: str) -> None:
    await db[BLOBS].update_one(
        {'_id': sha256, 'gc_state': DELETING},
        {'$unset': {'gc_state': '', 'gc_deleting_at': '', 'gc_marked_at': ''}},
    )


async def finish_delete(db: Database, sha256: str) -> None:
    await db[BLOBS].delete_one({'_id': sha256, 'gc_state': DELETING})


async def pending(db: Database) -> tuple[int, int]:
    """Blobs marked unreferenced (waiting out the grace period): count and bytes (index ``gc_marked_at``)."""
    pipeline: list[Document] = [
        {'$match': {'gc_marked_at': {'$exists': True}}},
        {'$group': {'_id': None, 'n': {'$sum': 1}, 'bytes': {'$sum': '$size'}}},
    ]
    async for doc in await db[BLOBS].aggregate(pipeline):
        return int(doc['n']), int(doc['bytes'])
    return 0, 0
