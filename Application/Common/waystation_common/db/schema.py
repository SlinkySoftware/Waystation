"""Declarative schema: create missing collections and indexes from ``collections.COLLECTIONS``.

``ensure_schema`` is idempotent: it only creates what is missing and reports what it did, so a second run reports
nothing. An existing index whose definition differs from the registry is **not** changed (dropping and rebuilding
an index on a large collection is a deliberate act): it is reported in ``conflicts`` and needs a migration.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pymongo import IndexModel

from waystation_common.db.client import Database
from waystation_common.db.collections import COLLECTIONS, CollectionSpec

log = logging.getLogger('waystation.db.schema')

# Index options compared between the registry and the server (others, like 'v' and 'ns', are server-side noise).
_COMPARED = ('unique', 'expireAfterSeconds', 'collation', 'sparse', 'partialFilterExpression')


@dataclass
class SchemaReport:
    created_collections: list[str] = field(default_factory=list)
    created_indexes: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created_collections or self.created_indexes)


def _wanted(model: IndexModel) -> dict[str, Any]:
    doc: dict[str, Any] = dict(model.document)
    doc['key'] = list(doc['key'].items())
    return doc


def _same(wanted: dict[str, Any], existing: Mapping[str, Any]) -> bool:
    if wanted['key'] != list(existing['key'].items()) and not _same_text_key(wanted, existing):
        return False
    for option in _COMPARED:
        mine, theirs = wanted.get(option), existing.get(option)
        if option == 'collation' and mine is not None and theirs is not None:
            if any(theirs.get(k) != v for k, v in mine.items()):
                return False
        elif option == 'unique':
            if bool(mine) != bool(theirs):
                return False
        elif mine != theirs:
            return False
    return True


def _same_text_key(wanted: dict[str, Any], existing: Mapping[str, Any]) -> bool:
    """Text indexes are stored as ``{_fts: 'text', _ftsx: 1}`` plus ``weights``."""
    if '_fts' not in existing['key']:
        return False
    fields = {name for name, kind in wanted['key'] if kind == 'text'}
    return set(existing.get('weights', {})) == fields


async def _ensure_collection(db: Database, spec: CollectionSpec, existing: set[str], report: SchemaReport) -> None:
    if spec.name in existing:
        return
    await db.create_collection(spec.name, **spec.options)
    report.created_collections.append(spec.name)
    log.info('collection created', extra={'collection': spec.name})


async def _ensure_indexes(db: Database, spec: CollectionSpec, report: SchemaReport) -> None:
    if not spec.indexes:
        return
    coll = db[spec.name]
    current = {ix['name']: ix async for ix in await coll.list_indexes()}
    missing: list[IndexModel] = []
    for model in spec.indexes:
        wanted = _wanted(model)
        name = wanted['name']
        if name not in current:
            missing.append(model)
        elif not _same(wanted, current[name]):
            report.conflicts.append(f'{spec.name}.{name}')
            log.warning('index differs from the registry', extra={'collection': spec.name, 'index': name})
    if missing:
        names = await coll.create_indexes(missing)
        report.created_indexes.extend(f'{spec.name}.{n}' for n in names)
        log.info('indexes created', extra={'collection': spec.name, 'indexes': names})


async def ensure_schema(db: Database) -> SchemaReport:
    report = SchemaReport()
    existing = set(await db.list_collection_names())
    for spec in COLLECTIONS.values():
        await _ensure_collection(db, spec, existing, report)
        await _ensure_indexes(db, spec, report)
    return report
