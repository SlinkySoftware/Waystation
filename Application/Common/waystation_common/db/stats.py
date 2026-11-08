"""``stats_minute`` persistence (DATA_MODEL §1). Time-series collections cannot upsert, so the rollup checks for an
existing document before inserting; the query uses the collection's automatic meta+time index."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import STATS_MINUTE


async def minute_exists(db: Database, repo_id: str, ts: datetime) -> bool:
    return await db[STATS_MINUTE].find_one({'m.repo_id': repo_id, 'ts': ts}, {'_id': 1}) is not None


async def insert_minute(db: Database, doc: dict[str, Any]) -> None:
    await db[STATS_MINUTE].insert_one(doc)


async def minutes(db: Database, repo_id: str, since: datetime) -> list[dict[str, Any]]:
    """All minute documents of a repo since ``since`` (tests, diagnostics; the stats API is T-S11)."""
    return [doc async for doc in db[STATS_MINUTE].find({'m.repo_id': repo_id, 'ts': {'$gte': since}}).sort('ts', 1)]
