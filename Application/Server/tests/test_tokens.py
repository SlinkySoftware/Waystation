"""API tokens (T-S04): secrets and hashing, scope narrowing in the decision point, credential parsing."""

import base64
import dataclasses

import pytest
from ws_testing import make_repo

from waystation_common.crypto import parse_key
from waystation_common.errors import Unauthorized
from waystation_server.repos.model import RepoType
from waystation_server.security import access
from waystation_server.security.access import Action, TokenScope, check, token_actions
from waystation_server.security.authn import basic_credentials
from waystation_server.security.principal import AuthMethod, Principal, PrincipalKind, TokenLimits
from waystation_server.security.registry_token import Scope, TokenSigner
from waystation_server.security.tokens import TokenHasher, display_prefix, generate, looks_like_token

KEY_A = base64.b64encode(b'a' * 32).decode()
KEY_B = base64.b64encode(b'b' * 32).decode()

NPM_HOSTED = make_repo('npm-hosted', 'npm', type=RepoType.HOSTED, proxy=None)
NPM_PROXY = make_repo('npm-proxy', 'npm')
DOCKER = make_repo('docker-hub', 'docker')


def with_token(roles: tuple[str, ...], scopes: list[str], repos: tuple[str, ...] = ()) -> Principal:
    limits = TokenLimits('tok', token_actions(scopes), repos, tuple(scopes))
    return Principal(PrincipalKind.USER, 'u1', 'alice', roles, AuthMethod.API_TOKEN, token=limits)


def test_secret_format() -> None:
    token = generate()
    assert looks_like_token(token) and len(token) == 47
    assert display_prefix(token) == token[:12] and display_prefix(token).startswith('wst_')
    assert generate() != token


def test_hash_is_keyed_and_survives_key_rotation() -> None:
    token = generate()
    old = TokenHasher([parse_key(KEY_A)])
    rotated = TokenHasher([parse_key(KEY_B), parse_key(KEY_A)])
    stored = old.hash(token)
    assert stored not in (token, TokenHasher([]).hash(token))
    assert len(stored) == 64
    assert rotated.hash(token) != stored  # new tokens use the new key...
    assert stored in rotated.candidates(token)  # ...and old ones are still found
    unkeyed = TokenHasher([])
    assert not unkeyed.keyed and unkeyed.candidates(token) == [unkeyed.hash(token)]


@pytest.mark.parametrize(
    ('roles', 'scopes', 'repos', 'action', 'repo', 'allowed'),
    [
        (('deployer',), ['read'], (), Action.READ, NPM_HOSTED, True),
        (('deployer',), ['read'], (), Action.WRITE, NPM_HOSTED, False),  # the token narrows
        (('reader',), ['write'], (), Action.WRITE, NPM_HOSTED, False),  # the owner cannot write
        (('deployer',), ['write'], ('npm-*',), Action.WRITE, NPM_HOSTED, True),
        (('deployer',), ['write'], ('npm-*',), Action.READ, DOCKER, False),  # outside the patterns
        (('deployer',), ['read'], ('format:npm',), Action.READ, NPM_PROXY, True),
        (('deployer',), ['read'], ('type:proxy',), Action.READ, NPM_HOSTED, False),
        (('admin',), ['read'], (), Action.DELETE, NPM_HOSTED, False),
        (('admin',), ['read'], (), Action.ADMIN, None, False),
        (('admin',), ['admin'], (), Action.ADMIN, None, True),
        (('admin',), ['admin'], ('docker-*',), Action.DELETE, NPM_HOSTED, False),
        (('viewer',), ['stats-read'], (), Action.STATS_READ, None, True),
        (('viewer',), ['stats-read'], (), Action.BROWSE, NPM_HOSTED, False),
        (('admin',), ['metrics'], (), Action.READ, NPM_PROXY, False),
        (('admin',), ['no-such-scope'], (), Action.READ, NPM_PROXY, False),
    ],
)
def test_tokens_only_narrow(
    roles: tuple[str, ...], scopes: list[str], repos: tuple[str, ...], action: Action, repo: object, allowed: bool
) -> None:
    assert check(with_token(roles, scopes, repos), action, repo) is allowed  # type: ignore[arg-type]


def test_hidden_outside_patterns() -> None:
    principal = with_token(('admin',), ['read'], ('npm-*',))
    assert access.can_see(principal, NPM_HOSTED)
    assert not access.can_see(principal, DOCKER)
    assert access.filter_repos(principal, Action.BROWSE, [NPM_HOSTED, DOCKER]) == [NPM_HOSTED]


def test_available_scopes_follow_the_owner() -> None:
    def scopes(*roles: str) -> list[str]:
        return [s.value for s in access.token_scopes_for(Principal(PrincipalKind.USER, 'u', 'u', roles))]

    assert scopes('reader') == ['read']
    assert scopes('deployer') == ['read', 'write']
    assert scopes('viewer') == ['stats-read', 'metrics']
    assert scopes('admin') == [s.value for s in TokenScope]
    assert scopes() == []


def test_metrics_scope() -> None:
    assert access.may_scrape_metrics(with_token(('viewer',), ['metrics']))
    assert not access.may_scrape_metrics(with_token(('reader',), ['metrics']))  # owner lacks stats-read
    assert not access.may_scrape_metrics(with_token(('admin',), ['read']))
    assert not access.may_scrape_metrics(Principal(PrincipalKind.USER, 'u', 'u', ('admin',)))


def test_repository_patterns_and_roles() -> None:
    for good in ('npm-hosted', 'docker-*', 'format:npm', 'type:proxy', 'a?c'):
        assert access.valid_repository_pattern(good), good
    for bad in ('*', 'format:', 'type:mirror', 'a b', '../x', 'x' * 200, ''):
        assert not access.valid_repository_pattern(bad), bad
    assert access.is_role('reader') and not access.is_role('anonymous') and not access.is_role('root')
    deployer = Principal(PrincipalKind.USER, 'u', 'u', ('deployer',))
    admin = Principal(PrincipalKind.USER, 'a', 'a', ('admin',))
    assert access.may_assign_role(deployer, 'reader')
    assert not access.may_assign_role(deployer, 'admin')
    assert access.may_assign_role(admin, 'admin')


def test_basic_credentials() -> None:
    def header(text: str) -> str:
        return 'Basic ' + base64.b64encode(text.encode()).decode()

    assert basic_credentials(header('ci:wst_x')) == ('ci', 'wst_x')
    assert basic_credentials(header(':wst_x:y')) == ('', 'wst_x:y')
    assert basic_credentials('Bearer abc') is None
    assert basic_credentials('') is None
    with pytest.raises(Unauthorized):
        basic_credentials('Basic !!!')
    with pytest.raises(Unauthorized):
        basic_credentials(header('no-colon'))


def test_registry_token_carries_the_api_token_id() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signer = TokenSigner(Ed25519PrivateKey.generate())
    jwt, _ = signer.issue(subject='u1', audience='registry.example.com', access=[], token_id='tok-1')
    claims = signer.verify(jwt, audience='registry.example.com')
    assert claims is not None and claims.token_id == 'tok-1'
    plain, _ = signer.issue(subject='u1', audience='registry.example.com', access=[Scope('repository', 'a', ('pull',))])
    claims = signer.verify(plain, audience='registry.example.com')
    assert claims is not None and claims.token_id is None


def test_principal_without_token_keeps_rbac() -> None:
    principal = with_token(('deployer',), ['read'])
    assert not check(principal, Action.WRITE, NPM_HOSTED)
    assert check(dataclasses.replace(principal, token=None), Action.WRITE, NPM_HOSTED)
