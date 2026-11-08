import os

import pytest

from waystation_common.db import collections as c
from waystation_common.db.client import Database
from waystation_common.db.migrations import (
    MIGRATIONS,
    Migration,
    SchemaMismatch,
    applied_ids,
    apply_migrations,
    check_schema,
)
from waystation_common.db.replset import ensure_replica_set
from waystation_common.db.schema import ensure_schema

pytestmark = pytest.mark.integration


async def test_replica_set_is_idempotent(mongo_db: Database) -> None:
    # the fixture already initiated it; a second call is a no-op that still waits for a primary
    assert await ensure_replica_set(os.environ['WAYSTATION_TEST_MONGO_URI']) is False
    hello = await mongo_db.client.admin.command('hello')
    assert hello['setName'] == 'rs0'
    assert hello['isWritablePrimary']


async def _index_names(db: Database, name: str) -> set[str]:
    return {ix['name'] async for ix in await db[name].list_indexes()}


async def test_schema_matches_registry_and_rerun_is_noop(mongo_db: Database) -> None:
    first = await ensure_schema(mongo_db)
    assert set(first.created_collections) == set(c.COLLECTIONS)
    assert not first.conflicts

    names = set(await mongo_db.list_collection_names())
    assert set(c.COLLECTIONS) <= names
    for spec in c.COLLECTIONS.values():
        if spec.is_timeseries:
            continue
        expected = {model.document['name'] for model in spec.indexes} | {'_id_'}
        assert await _index_names(mongo_db, spec.name) == expected, spec.name

    infos = [doc async for doc in await mongo_db.list_collections(filter={'name': c.STATS_MINUTE})]
    assert len(infos) == 1
    info = infos[0]
    assert info['type'] == 'timeseries'
    assert info['options']['timeseries']['timeField'] == 'ts'
    assert info['options']['timeseries']['metaField'] == 'm'
    assert info['options']['expireAfterSeconds'] == 7 * c.DAY_S

    second = await ensure_schema(mongo_db)
    assert not second.changed
    assert not second.conflicts


async def test_schema_reports_conflicting_index(mongo_db: Database) -> None:
    await mongo_db.create_collection(c.REPOSITORIES)
    await mongo_db[c.REPOSITORIES].create_index('name', name='name_unique')  # not unique: differs from registry
    report = await ensure_schema(mongo_db)
    assert report.conflicts == [f'{c.REPOSITORIES}.name_unique']
    assert f'{c.REPOSITORIES}.format_type' in report.created_indexes


async def test_unique_and_ttl_indexes_behave(mongo_db: Database) -> None:
    await ensure_schema(mongo_db)
    await mongo_db[c.USERS].insert_one({'username': 'alice'})
    with pytest.raises(Exception, match='duplicate key'):
        await mongo_db[c.USERS].insert_one({'username': 'alice'})
    indexes = {ix['name']: ix async for ix in await mongo_db[c.SESSIONS].list_indexes()}
    assert indexes['expires_at_ttl']['expireAfterSeconds'] == 0
    comps = {ix['name']: ix async for ix in await mongo_db[c.COMPONENTS].list_indexes()}
    assert comps['format_name_ci']['collation']['strength'] == 2


async def test_migrations_apply_once_in_order(mongo_db: Database) -> None:
    calls: list[str] = []

    def make(mid: str) -> Migration:
        async def apply(db: Database) -> None:
            calls.append(mid)

        return Migration(mid, f'test {mid}', apply)

    migrations = [make('0002_b'), make('0001_a')]
    assert await apply_migrations(mongo_db, migrations) == ['0001_a', '0002_b']
    assert calls == ['0001_a', '0002_b']
    assert await apply_migrations(mongo_db, migrations) == []
    assert calls == ['0001_a', '0002_b']
    await check_schema(mongo_db, migrations)

    with pytest.raises(SchemaMismatch, match='pending'):
        await check_schema(mongo_db, [*migrations, make('0003_c')])
    with pytest.raises(SchemaMismatch, match='newer'):
        await check_schema(mongo_db, migrations[:1])
    with pytest.raises(SchemaMismatch, match='does not know'):
        await apply_migrations(mongo_db, migrations[:1])


async def test_default_migrations(mongo_db: Database) -> None:
    assert await apply_migrations(mongo_db) == [m.id for m in MIGRATIONS]
    assert await applied_ids(mongo_db) == [m.id for m in MIGRATIONS]
    await check_schema(mongo_db)
