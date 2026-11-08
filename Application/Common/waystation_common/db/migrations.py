"""Forward-only data migrations (SPECIFICATION §8.6), run by ``init`` after ``ensure_schema``.

A migration is an idempotent async function with a sortable id (``NNNN_slug``). Applied ids are recorded in the
``migrations`` collection (``{_id, description, applied_at}``); each runs once, in id order. There is no "down".
Collections and indexes are not migrations: they are declarative (``schema.ensure_schema``). Use a migration for
data transforms and for changing an existing index.

``check_schema`` is for processes that only *use* the database (server, worker): it refuses to run against a
database migrated by a newer release (an applied id this code does not know) or one with pending migrations.

Concurrency: migrations are run by the single ``init`` container; running two ``init`` jobs at once is unsupported.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymongo import UpdateOne

from waystation_common.db.client import Database
from waystation_common.db.collections import (
    APT_PACKAGES,
    ASSETS,
    DOCKER_TAGS,
    NPM_PACKAGES,
    YUM_PACKAGES,
)
from waystation_common.db.collections import MIGRATIONS as MIGRATIONS_COLLECTION
from waystation_common.errors import ConfigError
from waystation_common.searchkeys import file_keys, name_keys

log = logging.getLogger('waystation.db.migrations')


@dataclass(frozen=True)
class Migration:
    id: str
    description: str
    apply: Callable[[Database], Awaitable[None]]


class SchemaMismatch(ConfigError):  # noqa: N818 - reads as a state, like the other config errors
    code = 'schema_mismatch'


async def _baseline(db: Database) -> None:
    """Marks the first schema; collections and indexes come from ``ensure_schema``."""


BACKFILL_BATCH = 1000


async def _backfill(
    db: Database, collection: str, query: dict[str, Any], field: str, keys: Callable[[str], dict[str, str]]
) -> None:
    """Set the search keys on every document matching ``query`` that has none yet, in batches. Idempotent and
    resumable: a document is only touched while it lacks ``name_lc``."""
    ops: list[UpdateOne] = []
    async for doc in db[collection].find({**query, 'name_lc': {'$exists': False}}, {field: 1}):
        value = doc.get(field)
        if isinstance(value, str) and value:
            ops.append(UpdateOne({'_id': doc['_id'], 'name_lc': {'$exists': False}}, {'$set': keys(value)}))
        if len(ops) >= BACKFILL_BATCH:
            await db[collection].bulk_write(ops, ordered=False)
            ops = []
    if ops:
        await db[collection].bulk_write(ops, ordered=False)


async def _search_keys(db: Database) -> None:
    """Search keys (``name_lc``/``base_lc``, ``waystation_common.searchkeys``) on content written before T-U06."""
    await _backfill(db, NPM_PACKAGES, {}, 'name', name_keys)
    await _backfill(db, DOCKER_TAGS, {}, 'image', name_keys)
    await _backfill(db, APT_PACKAGES, {}, 'name', name_keys)
    await _backfill(db, YUM_PACKAGES, {'verified': True}, 'name', name_keys)
    await _backfill(db, ASSETS, {'kind': 'content'}, 'path', file_keys)


MIGRATIONS: tuple[Migration, ...] = (
    Migration('0001_baseline', 'baseline schema', _baseline),
    Migration('0002_search_keys', 'search keys on searchable content (T-U06)', _search_keys),
)


async def applied_ids(db: Database) -> list[str]:
    return sorted([doc['_id'] async for doc in db[MIGRATIONS_COLLECTION].find({}, {'_id': 1})])


async def apply_migrations(db: Database, migrations: Sequence[Migration] = MIGRATIONS) -> list[str]:
    """Apply pending migrations in id order. Returns the ids applied by this call (empty on a re-run)."""
    known = {m.id for m in migrations}
    done = set(await applied_ids(db))
    unknown = sorted(done - known)
    if unknown:
        raise SchemaMismatch(f'database has migrations this release does not know: {", ".join(unknown)}')
    applied: list[str] = []
    for migration in sorted(migrations, key=lambda m: m.id):
        if migration.id in done:
            continue
        log.info('applying migration', extra={'migration': migration.id})
        await migration.apply(db)
        await db[MIGRATIONS_COLLECTION].insert_one(
            {'_id': migration.id, 'description': migration.description, 'applied_at': datetime.now(UTC)}
        )
        applied.append(migration.id)
    return applied


async def check_schema(db: Database, migrations: Sequence[Migration] = MIGRATIONS) -> None:
    """Raise ``SchemaMismatch`` unless the database is exactly at this release's migration level."""
    done = set(await applied_ids(db))
    known = {m.id for m in migrations}
    if done - known:
        raise SchemaMismatch(f'database schema is newer than this release ({", ".join(sorted(done - known))})')
    if known - done:
        raise SchemaMismatch(f'database has pending migrations ({", ".join(sorted(known - done))}); run init')
