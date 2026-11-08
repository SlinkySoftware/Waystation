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

from waystation_common.db.client import Database
from waystation_common.db.collections import MIGRATIONS as MIGRATIONS_COLLECTION
from waystation_common.errors import ConfigError

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


MIGRATIONS: tuple[Migration, ...] = (Migration('0001_baseline', 'baseline schema', _baseline),)


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
