"""Batched "last used" / "last seen" (ARCHITECTURE §8): the server records use in Valkey hashes, never in MongoDB on
the request path; the worker's rollup flushes them here in bulk (≤ 60 s staleness).

- ``seen:tokens``: API token id → ``"<epoch seconds> <client address>"`` → ``api_tokens.last_used_at/_ip``;
- ``seen:sessions``: session id → ``"<epoch seconds>"`` → ``sessions.last_seen_at``;
- ``seen:assets`` (asset id → ``"<epoch seconds>"``) and ``dl:assets`` (asset id → count) →
  ``assets.last_downloaded_at`` (``$max``) and ``download_count`` (``$inc``), which cleanup's "last downloaded"
  criterion reads.

Updates only move timestamps forward, so a late or repeated flush is harmless.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pymongo import UpdateOne

from waystation_common.db.client import Database
from waystation_common.db.collections import API_TOKENS, ASSETS, SESSIONS
from waystation_common.events.keys import DL_ASSETS, SEEN_ASSETS

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


async def flush_assets(db: Database, seen_entries: dict[str, str], counts: dict[str, str]) -> int:
    """Last download times (``$max``) and download counts (``$inc``) per asset; deleted assets are no-ops."""
    ops = []
    for asset_id in sorted(seen_entries.keys() | counts.keys()):
        update: dict[str, dict[str, object]] = {}
        parsed = parse(seen_entries[asset_id]) if asset_id in seen_entries else None
        if parsed is not None:
            update['$max'] = {'last_downloaded_at': parsed[0]}
        raw = counts.get(asset_id) or '0'
        n = int(raw) if raw.isdigit() else 0
        if n > 0:
            update['$inc'] = {'download_count': n}
        if update:
            ops.append(UpdateOne({'_id': asset_id}, update))
    if not ops:
        return 0
    result = await db[ASSETS].bulk_write(ops, ordered=False)
    return result.modified_count


__all__ = ['DL_ASSETS', 'SEEN_ASSETS', 'SEEN_SESSIONS', 'SEEN_TOKENS', 'flush_assets', 'flush_sessions', 'flush_tokens']
