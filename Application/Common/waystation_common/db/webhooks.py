"""Webhook persistence (DATA_MODEL ``webhooks``, ``webhook_deliveries``; SPECIFICATION §4.12, T-O05).

Shared by the server (definitions API, test events, redelivery, the delivery log) and the worker (fan-out of an
event into deliveries, attempts, the retry sweep).

A delivery is attempted by whoever **claims** it: a compare-and-set on a pending delivery that is due and not leased
(``lease_until`` in the past). The lease covers one attempt (the webhook's timeout plus a margin), so a worker lost
mid-attempt only delays the delivery until the sweep finds it again: at-least-once, never two attempts at once.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database, Document
from waystation_common.db.collections import WEBHOOK_DELIVERIES, WEBHOOKS
from waystation_common.errors import Conflict

DeliveryStatus = Literal['pending', 'succeeded', 'dead']
DELIVERY_RETENTION_DAYS = 30
ATTEMPT_LOG_MAX = 20  # attempts kept per delivery (the newest)
LEASE_MARGIN_S = 30

EPOCH = datetime.fromtimestamp(0, UTC)  # lease_until of an unleased delivery


# ---------------------------------------------------------------- definitions
async def list_webhooks(db: Database) -> list[Document]:
    return [doc async for doc in db[WEBHOOKS].find({}).sort('name', 1)]


async def list_enabled(db: Database) -> list[Document]:
    """What matching needs (no secrets)."""
    projection = {'_id': 1, 'events': 1, 'repositories': 1}
    return [doc async for doc in db[WEBHOOKS].find({'enabled': True}, projection)]


async def get_webhook(db: Database, webhook_id: str) -> Document | None:
    return await db[WEBHOOKS].find_one({'_id': webhook_id})


async def get_webhooks(db: Database, webhook_ids: Sequence[str]) -> list[Document]:
    return [doc async for doc in db[WEBHOOKS].find({'_id': {'$in': list(webhook_ids)}})]


async def insert_webhook(db: Database, doc: Document) -> None:
    try:
        await db[WEBHOOKS].insert_one(doc)
    except DuplicateKeyError:
        raise Conflict('a webhook with this name already exists') from None


async def update_webhook(db: Database, webhook_id: str, fields: Document) -> Document | None:
    try:
        return await db[WEBHOOKS].find_one_and_update(
            {'_id': webhook_id}, {'$set': fields}, return_document=ReturnDocument.AFTER
        )
    except DuplicateKeyError:
        raise Conflict('a webhook with this name already exists') from None


async def delete_webhook(db: Database, webhook_id: str) -> bool:
    """Delete the definition and its delivery log (pending deliveries go with it)."""
    result = await db[WEBHOOKS].delete_one({'_id': webhook_id})
    if not result.deleted_count:
        return False
    await db[WEBHOOK_DELIVERIES].delete_many({'webhook_id': webhook_id})
    return True


# ---------------------------------------------------------------- deliveries
def new_delivery(
    *,
    delivery_id: str,
    webhook_id: str,
    event_id: str,
    event: str,
    repository: str | None,
    body: str,
    now: datetime,
    lease_until: datetime | None = None,
    test: bool = False,
    redelivery_of: str | None = None,
    trigger: Literal['event', 'test', 'redeliver'] = 'event',
) -> Document:
    return {
        '_id': delivery_id,
        'webhook_id': webhook_id,
        'event_id': event_id,
        'event': event,
        'repository': repository,
        'trigger': 'test' if test else trigger,
        'redelivery_of': redelivery_of,
        'body': body,
        'status': 'pending',
        'attempts': 0,
        'next_attempt_at': now,
        'lease_until': lease_until or EPOCH,
        'created_at': now,
        'updated_at': now,
        'finished_at': None,
        'last': None,
        'log': [],
        'expires_at': now + timedelta(days=DELIVERY_RETENTION_DAYS),
    }


async def insert_delivery(db: Database, doc: Document) -> bool:
    """Insert unless a delivery with that id exists (an event job that runs again creates nothing new)."""
    try:
        await db[WEBHOOK_DELIVERIES].insert_one(doc)
    except DuplicateKeyError:
        return False
    return True


async def get_delivery(db: Database, webhook_id: str, delivery_id: str) -> Document | None:
    return await db[WEBHOOK_DELIVERIES].find_one({'_id': delivery_id, 'webhook_id': webhook_id})


async def delivery_webhook_id(db: Database, delivery_id: str) -> str | None:
    doc = await db[WEBHOOK_DELIVERIES].find_one({'_id': delivery_id}, {'webhook_id': 1})
    return str(doc['webhook_id']) if doc is not None else None


async def list_deliveries(
    db: Database,
    webhook_id: str,
    *,
    status: DeliveryStatus | None = None,
    before: tuple[datetime, str] | None = None,
    limit: int = 50,
) -> list[Document]:
    """Newest first, without request bodies; ``before`` = (created_at, _id) of the last row of the previous page."""
    query: dict[str, Any] = {'webhook_id': webhook_id}
    if status is not None:
        query['status'] = status
    if before is not None:
        at, last_id = before
        query['$or'] = [{'created_at': {'$lt': at}}, {'created_at': at, '_id': {'$lt': last_id}}]
    cursor = (
        db[WEBHOOK_DELIVERIES]
        .find(query, {'body': 0, 'log': 0})
        .sort([('created_at', DESCENDING), ('_id', DESCENDING)])
        .limit(limit)
    )
    return [doc async for doc in cursor]


async def last_deliveries(db: Database, webhook_ids: Sequence[str]) -> dict[str, Document]:
    """The newest delivery of each webhook."""
    out: dict[str, Document] = {}
    for webhook_id in webhook_ids:
        doc = await db[WEBHOOK_DELIVERIES].find_one(
            {'webhook_id': webhook_id},
            {'body': 0, 'log': 0},
            sort=[('created_at', DESCENDING), ('_id', DESCENDING)],
        )
        if doc is not None:
            out[webhook_id] = doc
    return out


async def claim(db: Database, delivery_id: str, *, now: datetime, lease_s: float) -> Document | None:
    """Take a pending, due, unleased delivery for one attempt; None when someone else has it or it is not due."""
    return await db[WEBHOOK_DELIVERIES].find_one_and_update(
        {'_id': delivery_id, 'status': 'pending', 'next_attempt_at': {'$lte': now}, 'lease_until': {'$lte': now}},
        {'$set': {'lease_until': now + timedelta(seconds=lease_s + LEASE_MARGIN_S), 'updated_at': now}},
        return_document=ReturnDocument.AFTER,
    )


async def due(db: Database, *, now: datetime, limit: int = 100) -> list[str]:
    """Ids of pending deliveries whose next attempt is due (the sweep claims them one by one)."""
    cursor = (
        db[WEBHOOK_DELIVERIES]
        .find({'status': 'pending', 'next_attempt_at': {'$lte': now}, 'lease_until': {'$lte': now}}, {'_id': 1})
        .sort('next_attempt_at', 1)
        .limit(limit)
    )
    return [str(doc['_id']) async for doc in cursor]


async def record_attempt(
    db: Database,
    delivery_id: str,
    *,
    attempt: Document,
    status: DeliveryStatus,
    next_attempt_at: datetime | None,
    now: datetime,
) -> Document | None:
    """Store one attempt's outcome, release the lease and set what happens next."""
    update: dict[str, Any] = {
        '$set': {
            'status': status,
            'last': attempt,
            'updated_at': now,
            'lease_until': EPOCH,
            'finished_at': None if status == 'pending' else now,
        },
        '$inc': {'attempts': 1},
        '$push': {'log': {'$each': [attempt], '$slice': -ATTEMPT_LOG_MAX}},
    }
    if next_attempt_at is not None:
        update['$set']['next_attempt_at'] = next_attempt_at
    return await db[WEBHOOK_DELIVERIES].find_one_and_update(
        {'_id': delivery_id}, update, return_document=ReturnDocument.AFTER
    )


async def give_up(db: Database, delivery_id: str, *, reason: str, now: datetime) -> None:
    """End a pending delivery without an attempt (its webhook was turned off)."""
    await db[WEBHOOK_DELIVERIES].update_one(
        {'_id': delivery_id, 'status': 'pending'},
        {
            '$set': {
                'status': 'dead',
                'updated_at': now,
                'finished_at': now,
                'lease_until': EPOCH,
                'last': {'at': now, 'error': reason, 'status_code': None, 'latency_ms': None, 'response': None},
            }
        },
    )


async def pending_count(db: Database) -> int:
    # The range on next_attempt_at lets the partial ``pending_due`` index answer it.
    return await db[WEBHOOK_DELIVERIES].count_documents({'status': 'pending', 'next_attempt_at': {'$gte': EPOCH}})
