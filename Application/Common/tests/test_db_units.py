import pytest
from pymongo import TEXT

from waystation_common.db import collections as c
from waystation_common.db.client import create_client, database
from waystation_common.db.replset import _member
from waystation_common.db.schema import _same, _wanted
from waystation_common.errors import ConfigError
from waystation_common.settings import Settings


def test_registry_names_and_indexes_unique() -> None:
    assert len(c.COLLECTIONS) == len(c.SPECS)
    for spec in c.SPECS:
        names = [model.document['name'] for model in spec.indexes]
        assert len(names) == len(set(names)), spec.name
    assert c.COLLECTIONS[c.STATS_MINUTE].is_timeseries
    assert not c.COLLECTIONS[c.USERS].is_timeseries


def test_index_comparison() -> None:
    model = c._ix('u', [('a', 1)], unique=True, collation=c.CASE_INSENSITIVE)
    wanted = _wanted(model)
    server = {'name': 'u', 'key': {'a': 1}, 'unique': True, 'collation': {'locale': 'en', 'strength': 2, 'x': 1}}
    assert _same(wanted, server)
    assert not _same(wanted, {**server, 'unique': False})
    assert not _same(wanted, {**server, 'key': {'b': 1}})
    assert not _same(wanted, {**server, 'collation': {'locale': 'fr', 'strength': 2}})
    ttl = _wanted(c._ix('t', [('e', 1)], expireAfterSeconds=0))
    assert not _same(ttl, {'name': 't', 'key': {'e': 1}})


def test_text_index_comparison() -> None:
    wanted = _wanted(c._ix('text', [('name', TEXT), ('d', TEXT)]))
    server = {'name': 'text', 'key': {'_fts': 'text', '_ftsx': 1}, 'weights': {'name': 1, 'd': 1}}
    assert _same(wanted, server)
    assert not _same(wanted, {**server, 'weights': {'name': 1}})


def test_member_parsing() -> None:
    assert _member('mongodb://db.example.com:27018/?replicaSet=rs1') == ('rs1', 'db.example.com', 27018, {})
    rs, host, port, auth = _member('mongodb://u:p@db.example.com/?replicaSet=rs0&authSource=admin')
    assert (rs, host, port) == ('rs0', 'db.example.com', 27017)
    assert auth == {'username': 'u', 'password': 'p', 'authSource': 'admin'}
    assert _member('mongodb://u:p@db.example.com/?replicaSet=rs0')[3] == {'username': 'u', 'password': 'p'}
    with pytest.raises(ConfigError, match='replicaSet'):
        _member('mongodb://db.example.com/')
    with pytest.raises(ConfigError, match='srv'):
        _member('mongodb+srv://cluster.example.com/')


async def test_client_factory_does_not_connect() -> None:
    settings = Settings(mongo_uri='mongodb://db.example.com:27017/?replicaSet=rs0', mongo_db='x')  # type: ignore[arg-type]
    client = create_client(settings, serverSelectionTimeoutMS=50)
    try:
        assert database(client, settings).name == 'x'
        assert client.options.pool_options.max_pool_size > 0
    finally:
        await client.close()
