"""Unit tests for the LDAP provider helpers, the bind cache, group → role resolution and provider validation (T-A01).
The directory conversations themselves are covered against real servers in ``test_ldap_integration.py``."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from waystation_server.api.auth_providers import _check_ldap, _check_mapping
from waystation_server.security.external import ExternalGroup, ExternalIdentity
from waystation_server.security.groups import GroupDirectory, GroupRecord, mapped_roles
from waystation_server.security.identity import ExternalGroupRef, UserRecord
from waystation_server.security.ldap import (
    BindCache,
    LdapDirectory,
    ServerAddress,
    check_ca,
    check_dn,
    check_filter,
    parse_server,
    valid_username,
)
from waystation_server.security.providers import (
    GroupMapping,
    LdapConfigWrite,
    LdapSecurity,
    ProviderCatalog,
    ProviderRecord,
    group_key,
)

NOW = datetime(2026, 11, 9, tzinfo=UTC)


@pytest.mark.parametrize(
    ('value', 'security', 'expected'),
    [
        ('ldap1.example.com', LdapSecurity.STARTTLS, ServerAddress('ldap1.example.com', 389, False)),
        ('ldap1.example.com:3389', LdapSecurity.NONE, ServerAddress('ldap1.example.com', 3389, False)),
        ('ldap1.example.com', LdapSecurity.LDAPS, ServerAddress('ldap1.example.com', 636, True)),
        ('ldaps://ldap1.example.com:1636/', LdapSecurity.LDAPS, ServerAddress('ldap1.example.com', 1636, True)),
        ('ldap://192.0.2.10', LdapSecurity.STARTTLS, ServerAddress('192.0.2.10', 389, False)),
        ('[2001:db8::1]:389', LdapSecurity.STARTTLS, ServerAddress('2001:db8::1', 389, False)),
    ],
)
def test_parse_server(value: str, security: LdapSecurity, expected: ServerAddress) -> None:
    assert parse_server(value, security) == expected


def test_server_url() -> None:
    assert ServerAddress('2001:db8::1', 636, True).url == 'ldaps://[2001:db8::1]:636'
    assert ServerAddress('ldap1.example.com', 389, False).url == 'ldap://ldap1.example.com:389'


@pytest.mark.parametrize(
    ('value', 'security'),
    [
        ('ldaps://ldap1.example.com', LdapSecurity.STARTTLS),
        ('ldap://ldap1.example.com', LdapSecurity.LDAPS),
        ('http://ldap1.example.com', LdapSecurity.NONE),
        ('ldap://user:pw@ldap1.example.com', LdapSecurity.NONE),
        ('ldap://ldap1.example.com/dc=example', LdapSecurity.NONE),
        ('ldap://ldap1.example.com:99999', LdapSecurity.NONE),
        ('ldap://:389', LdapSecurity.NONE),
    ],
)
def test_parse_server_rejects(value: str, security: LdapSecurity) -> None:
    with pytest.raises(ValueError, match=r'.'):
        parse_server(value, security)


def test_filter_dn_ca_and_username_checks() -> None:
    check_filter('(objectClass=person)')
    check_filter('(&(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))')
    for bad in ('objectClass=person', '(objectClass=person', '((x)'):
        with pytest.raises(ValueError, match='filter'):
            check_filter(bad)
    check_dn('cn=users,cn=accounts,dc=example,dc=com')
    with pytest.raises(ValueError, match='DN'):
        check_dn('not a dn')
    with pytest.raises(ValueError, match='PEM'):
        check_ca('-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n')
    assert valid_username('alice')
    assert not valid_username('')
    assert not valid_username(' alice')
    assert not valid_username('al\x00ice')
    assert not valid_username('a' * 257)


def test_group_key() -> None:
    assert group_key(' CN=Ops , OU=Groups,DC=example, DC=com ') == 'cn=ops,ou=groups,dc=example,dc=com'
    assert group_key('Platform') == 'platform'


def identity(username: str = 'alice') -> ExternalIdentity:
    return ExternalIdentity(
        provider='ldap:p1', subject=f'uid={username}', username=username, display_name='A', email=None
    )


def test_bind_cache_is_keyed_and_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = BindCache(max_entries=2)
    key = cache.key('p1', 1, 'Alice', 'secret-1')
    assert key == cache.key('p1', 1, 'alice', 'secret-1')
    assert 'secret-1' not in key and len(key) == 64
    assert key != cache.key('p1', 2, 'alice', 'secret-1')  # configuration changed
    assert key != cache.key('p1', 1, 'alice', 'secret-2')  # other password
    assert key != BindCache().key('p1', 1, 'alice', 'secret-1')  # per-process secret
    cache.put(key, identity(), 0)
    assert cache.get(key) is None  # ttl 0: never cached
    cache.put(key, identity(), 60)
    assert cache.get(key) == identity()
    cache.put(cache.key('p1', 1, 'bob', 'x'), identity('bob'), 60)
    cache.put(cache.key('p1', 1, 'carol', 'x'), identity('carol'), 60)
    assert cache.get(key) is None  # evicted (oldest first)
    later = time.monotonic() + 61
    monkeypatch.setattr(time, 'monotonic', lambda: later)
    assert cache.get(cache.key('p1', 1, 'bob', 'x')) is None
    cache.clear()


def group(gid: str, roles: list[str], *, provider: str | None = None, external: str | None = None) -> GroupRecord:
    return GroupRecord(
        _id=gid,
        name=gid,
        source='ldap' if provider else 'local',
        provider_id=provider,
        external_id=group_key(external) if external else None,
        roles=roles,
        created_at=NOW,
        updated_at=NOW,
    )


async def test_group_directory_roles() -> None:
    calls = 0

    async def load() -> list[GroupRecord]:
        nonlocal calls
        calls += 1
        return [
            group('g-local', ['viewer']),
            group('g-dev', ['deployer'], provider='p1', external='cn=developers,dc=example,dc=com'),
            group('g-ops', ['reader', 'viewer'], provider='p1', external='ops'),
            group('g-other', ['admin'], provider='p2', external='ops'),
        ]

    directory = GroupDirectory(load, ttl_s=60)
    user = UserRecord(
        _id='u1',
        username='alice',
        display_name='Alice',
        roles=['reader'],
        groups=['g-local', 'g-missing'],
        external_groups=[
            ExternalGroupRef(provider='ldap:p1', id='CN=Developers, DC=example,DC=com', name='developers'),
            ExternalGroupRef(provider='ldap:p1', id='cn=ops,dc=example,dc=com', name='Ops'),
        ],
        created_at=NOW,
    )
    assert await directory.roles_for(user) == ('reader', 'viewer', 'deployer')
    assert await directory.roles_for(user.model_copy(update={'external_groups': []})) == ('reader', 'viewer')
    assert calls == 1
    directory.invalidate()
    await directory.roles_for(user)
    assert calls == 2


def test_mapped_roles() -> None:
    mapping = [('cn=developers,dc=example,dc=com', ['deployer']), ('platform', ['viewer', 'deployer'])]
    groups = [('CN=Developers,DC=example,DC=com', 'developers'), ('cn=platform,dc=example,dc=com', 'platform')]
    assert mapped_roles(mapping, groups) == ['deployer', 'viewer']
    assert mapped_roles(mapping, []) == []


def ldap_config(**connection: Any) -> LdapConfigWrite:
    data: dict[str, Any] = {
        'connection': {'servers': ['ldap1.example.com'], 'security': 'starttls', **connection},
        'users': {'base_dn': 'cn=users,cn=accounts,dc=example,dc=com'},
        'groups': {'mode': 'member_of'},
    }
    return LdapConfigWrite.model_validate(data)


def test_check_ldap() -> None:
    assert _check_ldap(ldap_config(), stored_secret=False, keyring_set=True) == []
    errors = _check_ldap(ldap_config(bind_dn='uid=svc,dc=example,dc=com'), stored_secret=False, keyring_set=True)
    assert [(e.field, e.code) for e in errors] == [('ldap.connection.bind_password', 'required')]
    assert _check_ldap(ldap_config(bind_dn='uid=svc,dc=example,dc=com'), stored_secret=True, keyring_set=True) == []
    errors = _check_ldap(
        ldap_config(bind_dn='uid=svc,dc=example,dc=com', bind_password='pw', ca_pem='junk'),
        stored_secret=False,
        keyring_set=False,
    )
    assert {(e.field, e.code) for e in errors} == {
        ('ldap.connection.bind_password', 'secret_keys'),
        ('ldap.connection.ca_pem', 'format'),
    }
    errors = _check_ldap(
        ldap_config(servers=['ldap1.example.com', 'LDAP1.example.com']), stored_secret=False, keyring_set=True
    )
    assert [e.field for e in errors] == ['ldap.connection.servers']


def test_check_mapping() -> None:
    rows = [
        GroupMapping(external_group='cn=ops,dc=example,dc=com', roles=['reader']),
        GroupMapping(external_group='CN=Ops, DC=example,DC=com', roles=['superuser']),
    ]
    assert [(e.field, e.code) for e in _check_mapping(rows)] == [
        ('group_mapping.1.external_group', 'duplicate'),
        ('group_mapping.1.roles', 'unknown'),
    ]


def test_directory_refuses_empty_passwords_without_network() -> None:
    directory = LdapDirectory('ldap:p1', ldap_config(servers=['192.0.2.1']), None)
    assert directory.authenticate('alice', '').outcome == 'bad_password'
    assert directory.authenticate('al\nice', 'pw').outcome == 'not_found'


async def test_provider_catalog_orders_and_filters() -> None:
    def record(pid: str, name: str, order: int, enabled: bool = True) -> ProviderRecord:
        return ProviderRecord.model_validate(
            {
                '_id': pid,
                'kind': 'ldap',
                'name': name,
                'order': order,
                'enabled': enabled,
                'ldap': ldap_config().model_dump(mode='json'),
                'created_at': NOW,
                'updated_at': NOW,
            }
        )

    async def load() -> list[ProviderRecord]:
        return [record('b', 'Beta', 20), record('a', 'Alpha', 20), record('c', 'Gamma', 5, enabled=False)]

    catalog = ProviderCatalog(load)
    assert [r.id for r in await catalog.all()] == ['c', 'a', 'b']
    assert [r.id for r in await catalog.enabled('ldap')] == ['a', 'b']
    assert (await catalog.get('b')) is not None and (await catalog.get('zz')) is None
    assert (await catalog.get('a')).source == 'ldap:a'  # type: ignore[union-attr]


def test_external_group_is_hashable() -> None:
    assert len({ExternalGroup(id='a', name='a'), ExternalGroup(id='a', name='a')}) == 1
