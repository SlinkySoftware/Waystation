"""Writes to the ``audit`` collection (DATA_MODEL ``audit``), shared by the server and the workers.

Records are append-only: nothing in Waystation updates or deletes them; the TTL index on ``expires_at`` retires them.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from pymongo.errors import BulkWriteError

from waystation_common.db.client import Database
from waystation_common.db.collections import AUDIT

DUPLICATE_KEY = 11000


def inserter(db: Database) -> Callable[[list[dict[str, Any]]], Coroutine[Any, Any, None]]:
    """``insert(batch)`` for ``AuditWriter``: unordered, so one bad document does not hold back the rest. A retried
    batch that was partly written before is fine: duplicate ids (the records already stored) are not errors."""

    async def insert(batch: list[dict[str, Any]]) -> None:
        try:
            await db[AUDIT].insert_many(batch, ordered=False)
        except BulkWriteError as exc:
            errors = exc.details.get('writeErrors', [])
            if not errors or any(e.get('code') != DUPLICATE_KEY for e in errors):
                raise

    return insert
