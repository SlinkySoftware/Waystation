"""Reads of the ``audit`` collection (``GET /audit``, ``GET /audit/export``). Writes: ``waystation_common.db.audit``.

Every filter is served by an index (DATA_MODEL ``audit``): ``ts`` alone, ``action,ts``, ``principal.id|name,ts``,
``target.id|name,ts``; newest first by ``(ts, _id)`` with a keyset cursor.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pymongo import DESCENDING

from waystation_common.db.client import Database
from waystation_common.db.collections import AUDIT

SORT = [('ts', DESCENDING), ('_id', DESCENDING)]


@dataclass(frozen=True)
class AuditQuery:
    start: datetime | None = None
    end: datetime | None = None
    actor: str | None = None
    actions: tuple[str, ...] = ()  # exact names or ``<category>.*``
    target: str | None = None
    outcome: str | None = None

    def filter(self) -> dict[str, Any]:
        clauses: list[dict[str, Any]] = []
        ts: dict[str, Any] = {}
        if self.start is not None:
            ts['$gte'] = self.start
        if self.end is not None:
            ts['$lt'] = self.end
        if ts:
            clauses.append({'ts': ts})
        if self.actions:
            wanted: list[Any] = [
                re.compile('^' + re.escape(a[:-2]) + r'\.') if a.endswith('.*') else a for a in self.actions
            ]
            clauses.append({'action': {'$in': wanted}})
        if self.actor:
            clauses.append({'$or': [{'principal.name': self.actor}, {'principal.id': self.actor}]})
        if self.target:
            clauses.append({'$or': [{'target.name': self.target}, {'target.id': self.target}]})
        if self.outcome:
            clauses.append({'outcome': self.outcome})
        if not clauses:
            return {}
        return clauses[0] if len(clauses) == 1 else {'$and': clauses}


def _after(cursor: tuple[datetime, str] | None) -> dict[str, Any]:
    if cursor is None:
        return {}
    ts, record_id = cursor
    return {'$or': [{'ts': {'$lt': ts}}, {'ts': ts, '_id': {'$lt': record_id}}]}


def _combine(query: AuditQuery, cursor: tuple[datetime, str] | None) -> dict[str, Any]:
    base, after = query.filter(), _after(cursor)
    if base and after:
        return {'$and': [base, after]}
    return base or after


async def page(
    db: Database, query: AuditQuery, *, cursor: tuple[datetime, str] | None, limit: int
) -> list[dict[str, Any]]:
    """Up to ``limit + 1`` records (the extra one tells the caller there is a next page)."""
    found = db[AUDIT].find(_combine(query, cursor), sort=SORT, limit=limit + 1)
    return [doc async for doc in found]


async def stream(
    db: Database, query: AuditQuery, *, limit: int, batch_size: int = 500
) -> AsyncIterator[dict[str, Any]]:
    """Up to ``limit`` records, newest first, fetched in batches (exports)."""
    found = db[AUDIT].find(query.filter(), sort=SORT, limit=limit, batch_size=batch_size)
    try:
        async for doc in found:
            yield doc
    finally:
        await found.close()
