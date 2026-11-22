import re
from pathlib import Path

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


DATA_MODEL = Path(__file__).resolve().parents[3] / 'Documents' / 'Architecture' / 'DATA_MODEL.md'


def _documented_collections() -> dict[str, str]:
    """Collection name -> its "Indexes" cell, from the tables in DATA_MODEL §1 (MongoDB). A row may name several
    collections of the same shape (`stats_hour` / `stats_day`)."""
    text = DATA_MODEL.read_text(encoding='utf-8')
    section = text.split('\n## 1. ', 1)[1].split('\n## 2. ', 1)[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in re.split(r'(?<!\\)\|', line)[1:-1]]
        if len(cells) == 3 and re.fullmatch(r'`[a-z_]+`( / `[a-z_]+`)*', cells[0]):
            rows.update(dict.fromkeys(re.findall(r'`([a-z_]+)`', cells[0]), cells[2]))
    return rows


def test_registry_matches_data_model() -> None:
    """Every collection in DATA_MODEL §1 is in the registry and the reverse (T-F06), and the unique and TTL indexes
    the document names agree with the registry. Collections documented as not used have no registry entry."""
    documented = _documented_collections()
    unused = {name for name, cell in documented.items() if cell == '\N{EN DASH}' and name not in c.COLLECTIONS}
    assert unused == {'signing_keys'}
    assert set(documented) - unused == set(c.COLLECTIONS)
    mismatches = []
    for name, spec in c.COLLECTIONS.items():
        cell = documented[name].lower()
        unique = any(model.document.get('unique') for model in spec.indexes)
        ttl = 'expireAfterSeconds' in spec.options or any(
            'expireAfterSeconds' in model.document for model in spec.indexes
        )
        if unique != ('unique' in cell):
            mismatches.append(f'{name}: unique index in the registry: {unique}; DATA_MODEL: {documented[name]}')
        if ttl != ('ttl' in cell or 'expireafterseconds' in cell):
            mismatches.append(f'{name}: TTL in the registry: {ttl}; DATA_MODEL: {documented[name]}')
    assert not mismatches, '\n'.join(mismatches)
