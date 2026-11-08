"""Batched "last used" / "last seen" (ARCHITECTURE §8): the server records use in Valkey hashes, never in MongoDB on
the request path; the worker's rollup flushes them here in bulk (≤ 60 s staleness).

- ``seen:tokens``: API token id → ``"<epoch seconds> <client address>"`` → ``api_tokens.last_used_at/_ip``;
- ``seen:sessions``: session id → ``"<epoch seconds>"`` → ``sessions.last_seen_at``.

Updates only move timestamps forward, so a late or repeated flush is harmless.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pymongo import UpdateOne

from waystation_common.db.client import Database
from waystation_common.db.collections import API_TOKENS, SESSIONS

SEEN_TOKENS = 'seen:tokens'
SEEN_SESSIONS = 'seen:sessions'


def parse(raw: str) -> tuple[datetime, str | None] | None:
    stamp, _, extra = raw.partition(' ')
    try:
        return datetime.fromtimestamp(int(stamp), UTC), extra or None
    except ValueError, OverflowError:
        return None


async def flush_tokens(db: Database, entries: dict[str, str]) -> int:
    ops = []
    for token_id, raw in entries.items():
        parsed = parse(raw)
        if parsed is None:
            continue
        when, ip = parsed
        ops.append(
            UpdateOne(
                {'_id': token_id, '$or': [{'last_used_at': None}, {'last_used_at': {'$lt': when}}]},
                {'$set': {'last_used_at': when, 'last_used_ip': ip}},
            )
        )
    if not ops:
        return 0
    result = await db[API_TOKENS].bulk_write(ops, ordered=False)
    return result.modified_count


async def flush_sessions(db: Database, entries: dict[str, str]) -> int:
    ops = []
    for session_id, raw in entries.items():
        parsed = parse(raw)
        if parsed is not None:
            ops.append(UpdateOne({'_id': session_id}, {'$max': {'last_seen_at': parsed[0]}}))
    if not ops:
        return 0
    result = await db[SESSIONS].bulk_write(ops, ordered=False)
    return result.modified_count
