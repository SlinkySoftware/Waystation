"""``groups`` collection access."""

from __future__ import annotations

from datetime import datetime

from pymongo import DeleteMany, UpdateOne

from waystation_common.db.client import Database
from waystation_common.db.collections import GROUPS
from waystation_common.ids import new_id
from waystation_server.security.groups import GroupRecord
from waystation_server.security.providers import GroupMapping, group_key

MAX_GROUPS = 10_000


async def list_all(db: Database) -> list[GroupRecord]:
    return [GroupRecord.model_validate(doc) async for doc in db[GROUPS].find({}).limit(MAX_GROUPS)]


async def for_provider(db: Database, provider_id: str) -> list[GroupRecord]:
    cursor = db[GROUPS].find({'provider_id': provider_id}).sort('position', 1)
    return [GroupRecord.model_validate(doc) async for doc in cursor]


def mapped_group_name(kind: str, provider_name: str, external_group: str) -> str:
    return f'{kind}:{provider_name}:{external_group}'[:1200]


async def replace_mapping(
    db: Database, *, kind: str, provider_id: str, provider_name: str, mapping: list[GroupMapping], now: datetime
) -> None:
    """Make the provider's mapped groups exactly ``mapping`` (one group per row, in order)."""
    keep = [group_key(row.external_group) for row in mapping]
    ops: list[UpdateOne | DeleteMany] = [
        DeleteMany({'provider_id': provider_id, 'external_id': {'$nin': keep}}),
    ]
    for position, row in enumerate(mapping):
        ops.append(
            UpdateOne(
                {'provider_id': provider_id, 'external_id': group_key(row.external_group)},
                {
                    '$set': {
                        'name': mapped_group_name(kind, provider_name, row.external_group),
                        'source': kind,
                        'external_group': row.external_group,
                        'position': position,
                        'roles': list(dict.fromkeys(row.roles)),
                        'updated_at': now,
                    },
                    '$setOnInsert': {'_id': new_id(), 'created_at': now},
                },
                upsert=True,
            )
        )
    await db[GROUPS].bulk_write(ops, ordered=True)


async def delete_for_provider(db: Database, provider_id: str) -> int:
    result = await db[GROUPS].delete_many({'provider_id': provider_id})
    return result.deleted_count
