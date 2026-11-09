"""Custom roles, delegation rules, filter_query and the grant cache codec (SPECIFICATION §4.10, ARCHITECTURE §7)."""

import pytest
from ws_testing import NOW, make_repo

from waystation_server.repos.model import RepoType
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.actions import Grants, Permission
from waystation_server.security.groups import GroupRecord, role_sources
from waystation_server.security.identity import ExternalGroupRef, UserRecord
from waystation_server.security.passwords import password_problem
from waystation_server.security.principal import AuthMethod, Principal, PrincipalKind, TokenLimits
from waystation_server.security.rbac import _decode, _encode, fingerprint

NPM = make_repo('npm-proxy', 'npm')
NPM_HOSTED = make_repo('npm-hosted', 'npm', type=RepoType.HOSTED, proxy=None)
DOCKER = make_repo('docker-hub', 'docker')

CUSTOM: dict[str, tuple[Permission, ...]] = {
    'npm-publisher': (Permission(Action.WRITE, 'format:npm'),),
    'auditor': (Permission(Action.AUDIT_READ, '*'), Permission(Action.BROWSE, '*')),
    'user-admins': (Permission(Action.USER_ADMIN, '*'),),
    'root': (Permission(Action.ADMIN, '*'),),
}


def principal(*roles: str, token: TokenLimits | None = None) -> Principal:
    return Principal(
        PrincipalKind.USER,
        f'id-{"-".join(roles)}',
        '-'.join(roles) or 'nobody',
        roles,
        AuthMethod.SESSION,
        grants=access.materialise(roles, CUSTOM),
        token=token,
    )


def test_custom_roles_materialise_with_implied_actions() -> None:
    publisher = principal('npm-publisher')
    assert access.check(publisher, Action.WRITE, NPM_HOSTED)
    assert access.check(publisher, Action.READ, NPM)  # write implies read and browse on the same scope
    assert not access.check(publisher, Action.READ, DOCKER)
    assert not access.check(publisher, Action.AUDIT_READ)
    assert access.check(principal('auditor'), Action.AUDIT_READ)
    assert access.materialise(('root',), CUSTOM).is_admin
    assert access.materialise(('gone', 'reader'), CUSTOM) == access.grants_for(('reader',))  # unknown: nothing
    # without materialised grants, custom roles grant nothing (only built-ins are known)
    bare = Principal(PrincipalKind.USER, 'u', 'u', ('npm-publisher',), AuthMethod.SESSION)
    assert not access.check(bare, Action.WRITE, NPM_HOSTED)


def test_is_role_and_permission_errors() -> None:
    assert access.is_role('npm-publisher', CUSTOM) and access.is_role('reader')
    assert not access.is_role('npm-publisher') and not access.is_role('anonymous', CUSTOM)
    assert access.permission_error('write', 'npm-*') is None
    assert access.permission_error('read', 'type:hosted') is None
    assert access.permission_error('read', '*') is None
    assert access.permission_error('admin', '*') is None
    assert access.permission_error('user-admin', 'npm-*') == 'global actions are granted only on scope *'
    assert access.permission_error('fly', '*') == "unknown action 'fly'"
    assert access.permission_error('read', 'type:weird') is not None
    assert access.permission_error('read', '../etc') is not None


@pytest.mark.parametrize(
    ('giver', 'role', 'allowed'),
    [
        (('user-admins',), 'reader', True),  # repository powers are what user-admin hands out
        (('user-admins',), 'npm-publisher', True),
        (('user-admins',), 'user-admins', True),  # holds it
        (('user-admins',), 'auditor', False),  # audit-read not held
        (('user-admins',), 'admin', False),
        (('user-admins',), 'root', False),  # a custom role holding admin
        (('admin',), 'root', True),
        (('root',), 'admin', True),
    ],
)
def test_delegation(giver: tuple[str, ...], role: str, allowed: bool) -> None:
    assert access.may_assign_role(principal(*giver), role, CUSTOM) is allowed


def test_manage_needs_everything_the_target_holds() -> None:
    user_admin = principal('user-admins')
    assert access.may_manage(user_admin, access.materialise(('deployer',), CUSTOM))
    assert not access.may_manage(user_admin, access.materialise(('admin',), CUSTOM))
    assert access.may_manage(principal('admin'), access.materialise(('admin',), CUSTOM))


def test_delegation_respects_the_credential() -> None:
    """An admin acting through a token without the admin scope cannot hand out admin."""
    limits = TokenLimits(
        token_id='t1', actions=access.token_actions(['read', 'stats-read']), scopes=('read', 'stats-read')
    )
    narrowed = principal('admin', token=limits)
    assert not access.may_assign_role(narrowed, 'admin', CUSTOM)
    full = TokenLimits(token_id='t2', actions=access.token_actions(['admin']), scopes=('admin',))
    assert access.may_assign_role(principal('admin', token=full), 'admin', CUSTOM)


def test_filter_query() -> None:
    repos = [NPM, NPM_HOSTED, DOCKER]
    assert access.filter_query(principal('admin'), Action.BROWSE, repos) == {}
    assert access.filter_query(principal('npm-publisher'), Action.WRITE, repos) == {
        'repo_id': {'$in': ['id-npm-hosted', 'id-npm-proxy']}
    }
    assert access.filter_query(principal(), Action.READ, repos, field='m.repo_id') == {'m.repo_id': {'$in': []}}


def test_grant_cache_codec() -> None:
    grants = access.materialise(('npm-publisher', 'viewer'), CUSTOM)
    table = fingerprint(CUSTOM)
    raw = _encode(('npm-publisher', 'viewer'), table, grants)
    assert _decode(raw, ('npm-publisher', 'viewer'), table) == grants
    assert _decode(raw, ('viewer',), table) is None  # other roles
    assert _decode(raw, ('npm-publisher', 'viewer'), fingerprint({})) is None  # other role definitions
    assert _decode('not json', ('viewer',), table) is None
    assert _decode('{"r": [], "t": "x"}', (), 'x') is None
    assert fingerprint(CUSTOM) == fingerprint(dict(reversed(list(CUSTOM.items()))))
    assert Grants(is_admin=False, permissions=()) == access.materialise((), CUSTOM)


def test_role_sources() -> None:
    def group(gid: str, roles: list[str], **fields: str) -> GroupRecord:
        return GroupRecord(_id=gid, name=f'G {gid}', roles=roles, created_at=NOW, updated_at=NOW, **fields)

    groups = [
        group('g1', ['reader']),
        group('g2', ['deployer'], source='ldap', provider_id='p1', external_id='cn=dev,dc=example,dc=com'),
        group('g3', ['admin'], source='ldap', provider_id='p2', external_id='cn=dev,dc=example,dc=com'),
    ]
    user = UserRecord(
        _id='u1',
        username='alice',
        display_name='Alice',
        roles=['viewer', 'reader'],
        groups=['g1'],
        external_groups=[ExternalGroupRef(provider='ldap:p1', id='CN=Dev, DC=example,DC=com', name='dev')],
        created_at=NOW,
    )
    assert role_sources(user, groups) == [
        ('viewer', 'direct'),
        ('reader', 'direct'),
        ('reader', 'G g1'),
        ('deployer', 'G g2'),
    ]


def test_password_policy() -> None:
    assert password_problem('short', username='bob') == 'at least 12 characters'
    assert password_problem('alice-the-admin', username='Alice-the-admin') == 'must not be the username'
    assert password_problem(' ' * 14, username='bob') == 'not only spaces'
    assert password_problem('x' * 1025, username='bob') == 'at most 1024 characters'
    assert password_problem('a perfectly fine one', username='bob') is None
