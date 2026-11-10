"""RBAC decision point (ARCHITECTURE §7): decisions, the route-enumeration test and an HTTP matrix."""

import dataclasses
import re
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from ws_testing import asgi_client, iter_routes, make_repo, static_catalog

from waystation_server.app import create_app
from waystation_server.formats.base import FormatDescriptor, FormatRequest, UiHints
from waystation_server.repos.model import RepoType
from waystation_server.security import access
from waystation_server.security.access import Action, can_see, check, filter_repos, scope_matches
from waystation_server.security.principal import ANONYMOUS, AuthMethod, Principal, PrincipalKind, TokenLimits
from waystation_server.services import Services

PROXY = make_repo('npm-proxy', 'npm')
HOSTED = make_repo('npm-hosted', 'npm', type=RepoType.HOSTED, proxy=None)
DOCKER = make_repo('docker-hub', 'docker')


def user(*roles: str) -> Principal:
    return Principal(PrincipalKind.USER, f'id-{"-".join(roles)}', '-'.join(roles) or 'nobody', roles, AuthMethod.BASIC)


NO_GRANTS = access.Grants(is_admin=False, permissions=())
ANONYMOUS_GRANTS = access.grants_for(('anonymous',))  # the default anonymous access: browse + read on proxies


def signed_in(principal: Principal, anonymous: access.Grants = ANONYMOUS_GRANTS) -> Principal:
    """``principal`` as authentication resolves it: also holding the anonymous grants (SPEC §4.10)."""
    return dataclasses.replace(principal, anonymous_grants=anonymous)


READER, DEPLOYER, ADMIN, VIEWER = user('reader'), user('deployer'), user('admin'), user('viewer')
NOBODY = signed_in(user(), NO_GRANTS)  # signed in, no role, anonymous access turned off: nothing at all
NEWCOMER = signed_in(user())  # signed in, no role, the default anonymous access: exactly what anonymous may do
GUEST = user('anonymous')  # signed in, holding only what anonymous access grants (browse + read on proxies)


def test_scopes() -> None:
    assert scope_matches('*', PROXY)
    assert scope_matches('NPM-proxy', PROXY)
    assert scope_matches('npm-*', HOSTED)
    assert not scope_matches('docker-*', HOSTED)
    assert scope_matches('format:docker', DOCKER)
    assert scope_matches('type:hosted', HOSTED)
    assert not scope_matches('type:hosted', PROXY)


@pytest.mark.parametrize(
    ('principal', 'action', 'repo', 'allowed'),
    [
        (ANONYMOUS, Action.READ, PROXY, True),
        (ANONYMOUS, Action.BROWSE, PROXY, True),
        (ANONYMOUS, Action.READ, HOSTED, False),
        (ANONYMOUS, Action.WRITE, PROXY, False),
        (ANONYMOUS, Action.STATS_READ, None, False),
        (READER, Action.READ, HOSTED, True),
        (READER, Action.WRITE, HOSTED, False),
        (DEPLOYER, Action.WRITE, HOSTED, True),
        (DEPLOYER, Action.DELETE, HOSTED, False),
        (DEPLOYER, Action.MANAGE, HOSTED, False),
        (VIEWER, Action.STATS_READ, None, True),
        (VIEWER, Action.READ, HOSTED, False),
        (ADMIN, Action.MANAGE, HOSTED, True),
        (ADMIN, Action.SETTINGS_ADMIN, None, True),
        (NOBODY, Action.READ, PROXY, False),
        (NEWCOMER, Action.READ, PROXY, True),
        (NEWCOMER, Action.BROWSE, PROXY, True),
        (NEWCOMER, Action.READ, HOSTED, False),
        (NEWCOMER, Action.WRITE, PROXY, False),
        (NEWCOMER, Action.STATS_READ, None, False),
    ],
)
def test_check(principal: Principal, action: Action, repo: object, allowed: bool) -> None:
    assert check(principal, action, repo) is allowed  # type: ignore[arg-type]


def test_visibility_and_filtering() -> None:
    assert can_see(ANONYMOUS, PROXY)
    assert not can_see(ANONYMOUS, HOSTED)
    assert can_see(VIEWER, HOSTED)  # browse only
    assert filter_repos(ANONYMOUS, Action.BROWSE, [PROXY, HOSTED, DOCKER]) == [PROXY, DOCKER]
    assert filter_repos(NOBODY, Action.BROWSE, [PROXY]) == []


def test_capabilities() -> None:
    assert access.capabilities(ADMIN) == [
        'admin',
        'repositories.create',
        'repositories.manage',
        'repositories.browse',
        'stats.read',
        'audit.read',
        'users.admin',
        'settings.admin',
    ]
    assert access.capabilities(VIEWER) == ['repositories.browse', 'stats.read']
    assert access.capabilities(READER) == ['repositories.browse']
    assert access.capabilities(GUEST) == ['repositories.browse']
    assert access.capabilities(ANONYMOUS) == ['repositories.browse']
    assert access.capabilities(NOBODY) == []  # no role, no anonymous access: the "no access yet" landing
    assert access.capabilities(NEWCOMER) == ['repositories.browse']  # no role: what anonymous access allows
    custom = Principal(
        PrincipalKind.USER,
        'id-custom',
        'custom',
        ('docker-readers',),
        AuthMethod.BASIC,
        grants=access.Grants(is_admin=False, permissions=(access.Permission(Action.READ, 'docker-*'),)),
    )
    assert access.capabilities(custom) == ['repositories.browse']  # read on a glob: some repository may be visible


# ---------------------------------------------------------------------------- own + anonymous grants (SPEC §4.10)
ACTIONS = [Action.BROWSE, Action.READ, Action.WRITE, Action.DELETE, Action.MANAGE, *access.GLOBAL_ACTIONS]


def test_a_signed_in_principal_without_roles_may_do_what_anonymous_may() -> None:
    for action in ACTIONS:
        for repo in (PROXY, HOSTED, DOCKER, None):
            assert check(NEWCOMER, action, repo) is check(ANONYMOUS, action, repo), (action, repo)
            assert check(NOBODY, action, repo) is False, (action, repo)
    assert filter_repos(NEWCOMER, Action.BROWSE, [PROXY, HOSTED, DOCKER]) == [PROXY, DOCKER]


def test_own_and_anonymous_grants_add_up() -> None:
    publish = access.Grants(is_admin=False, permissions=(access.Permission(Action.WRITE, 'npm-hosted'),))
    viewer = signed_in(VIEWER, publish)  # anonymous may publish to npm-hosted (a custom anonymous role)
    assert check(viewer, Action.STATS_READ)  # own
    assert check(viewer, Action.WRITE, HOSTED)  # anonymous
    assert not check(viewer, Action.WRITE, PROXY)
    reader = signed_in(READER)
    assert check(reader, Action.READ, HOSTED) and check(reader, Action.READ, PROXY)
    effective = access.effective_grants(viewer)
    assert access.Permission(Action.WRITE, 'npm-hosted') in effective.permissions
    assert access.Permission(Action.STATS_READ, '*') in effective.permissions
    assert access.grants_of(viewer) == access.grants_for(('viewer',))  # own grants stay own
    assert access.effective_grants(ANONYMOUS) == access.grants_of(ANONYMOUS)


def test_anonymous_grants_never_carry_a_global_action() -> None:
    """Anonymous access holds repository permissions only (SPEC §4.9), also if a role it uses was later edited to
    hold a global action: a signed-in principal never gains one from it."""
    bogus = access.Grants(
        is_admin=True,
        permissions=(access.Permission(Action.ADMIN, '*'), access.Permission(Action.USER_ADMIN, '*')),
    )
    principal = signed_in(user(), bogus)
    for action in access.GLOBAL_ACTIONS:
        assert not check(principal, action)
    assert not check(principal, Action.MANAGE, HOSTED)
    assert access.capabilities(principal) == []
    assert access.repository_only(bogus) == NO_GRANTS


def test_delegation_ignores_the_anonymous_grants() -> None:
    """What a caller may give away is its own: the anonymous grants (and a bogus global action in them) do not
    count for ``may_assign`` / ``may_manage``."""
    bogus = access.Grants(is_admin=False, permissions=(access.Permission(Action.USER_ADMIN, '*'),))
    caller = signed_in(user(), bogus)
    user_admin = access.materialise(('_',), {'_': (access.Permission(Action.USER_ADMIN, '*'),)})
    assert not access.may_assign(caller, user_admin)
    assert not access.may_manage(caller, user_admin)
    assert access.may_assign(caller, access.grants_for(('reader',)))  # repository actions are not limited
    assert not access.may_assign_role(signed_in(user('viewer')), 'admin')
    assert access.may_assign_role(signed_in(user('admin')), 'admin')


def test_credential_narrowing_bounds_the_anonymous_grants_too() -> None:
    """An API token's repository patterns and scopes, and a Docker registry token's grants, are an upper bound on
    everything the principal may do, the anonymous grants included."""
    hosted_only = TokenLimits('t1', access.token_actions(['read']), repositories=('npm-hosted',), scopes=('read',))
    reader = signed_in(dataclasses.replace(READER, token=hosted_only))
    assert check(reader, Action.READ, HOSTED)
    assert not check(reader, Action.READ, PROXY)  # anonymous could, the token may not
    assert not can_see(reader, PROXY)
    stats_only = TokenLimits('t2', access.token_actions(['stats-read']), scopes=('stats-read',))
    newcomer = dataclasses.replace(NEWCOMER, token=stats_only)
    assert not check(newcomer, Action.READ, PROXY)
    registry = dataclasses.replace(NEWCOMER, token_access=(('npm-hosted', frozenset({Action.READ})),))
    assert not check(registry, Action.READ, PROXY)  # the registry token grants nothing on the proxy
    pulled = dataclasses.replace(NEWCOMER, token_access=(('npm-proxy', frozenset({Action.READ, Action.WRITE})),))
    assert check(pulled, Action.READ, PROXY)
    assert not check(pulled, Action.WRITE, PROXY)  # the token asks for more than the principal holds


def test_token_scopes_follow_what_the_owner_may_do() -> None:
    assert access.token_scopes_for(NEWCOMER) == [access.TokenScope.READ]  # the anonymous read: a token adds nothing
    assert access.token_scopes_for(NOBODY) == []


def test_requires_rejects_repo_actions() -> None:
    with pytest.raises(ValueError):
        access.requires(Action.READ)


def test_every_route_declares_its_access_rule() -> None:
    """ARCHITECTURE §7: a route without a declared action is a bug."""
    routes = iter_routes(create_app())
    assert len(routes) > 80, 'the enumeration must see the included routers'
    missing = []
    for path, methods, endpoint in routes:
        if path == '/api/v1/openapi.json':
            continue  # framework route outside the dependency system: the public OpenAPI document
        if access.route_rule(endpoint) is None:
            missing.append(f'{sorted(methods)} {path}')
    assert not missing, f'routes without an access rule: {missing}'


# ---------------------------------------------------------------------------- HTTP matrix
class EchoPlugin:
    def __init__(self) -> None:
        self.descriptor = FormatDescriptor(
            name='npm', label='npm', types=frozenset(RepoType), options_model=BaseModel, ui=UiHints('npm')
        )

    async def handle(self, req: FormatRequest) -> Response:
        return PlainTextResponse(f'{req.method} {req.repo.name}/{req.path} as {req.principal.display}')


TOKENS = {
    'reader': READER,
    'deployer': DEPLOYER,
    'admin': ADMIN,
    'nobody': NOBODY,
    'guest': GUEST,
    'viewer': VIEWER,
    # no anonymous grants attached: authentication attaches the current ones (the default anonymous access)
    'newcomer': user(),
    'narrowed': dataclasses.replace(
        READER, token=TokenLimits('t', access.token_actions(['read']), repositories=('npm-hosted',), scopes=('read',))
    ),
}


async def _token_auth(request: Request, token: str) -> Principal | None:
    return TOKENS.get(token)


@pytest.fixture
async def matrix_client(unit_services: Services) -> AsyncIterator[httpx.AsyncClient]:
    unit_services.catalog = static_catalog([PROXY, HOSTED])
    unit_services.formats.set_plugin(EchoPlugin())  # type: ignore[arg-type]
    unit_services.bearer_authenticators.append(_token_auth)
    async with asgi_client(unit_services) as client:
        yield client


@pytest.mark.parametrize(
    ('who', 'method', 'repo', 'status'),
    [
        (None, 'GET', 'npm-proxy', 200),
        (None, 'HEAD', 'npm-proxy', 200),
        (None, 'GET', 'npm-hosted', 401),  # hidden, no credentials: the Basic challenge (ADR 0005), never 403
        (None, 'PUT', 'npm-proxy', 401),
        (None, 'PUT', 'npm-hosted', 401),
        ('reader', 'GET', 'npm-hosted', 200),
        ('reader', 'PUT', 'npm-hosted', 403),
        ('deployer', 'PUT', 'npm-hosted', 200),
        ('deployer', 'DELETE', 'npm-hosted', 403),
        ('admin', 'DELETE', 'npm-hosted', 200),
        # signed in, no role, anonymous access off: nothing is visible, so everything is 404 (never 403, never 401)
        ('nobody', 'GET', 'npm-proxy', 404),
        ('nobody', 'GET', 'npm-hosted', 404),
        ('nobody', 'PUT', 'npm-proxy', 404),
        # signed in with only the anonymous grants: proxies readable, hosted hidden, no writes
        ('guest', 'GET', 'npm-proxy', 200),
        ('guest', 'GET', 'npm-hosted', 404),
        ('guest', 'PUT', 'npm-proxy', 403),
        # signed in, no role, the default anonymous access: exactly what anonymous may do (403 instead of 401)
        ('newcomer', 'GET', 'npm-proxy', 200),
        ('newcomer', 'HEAD', 'npm-proxy', 200),
        ('newcomer', 'GET', 'npm-hosted', 404),
        ('newcomer', 'PUT', 'npm-proxy', 403),
        ('newcomer', 'PUT', 'npm-hosted', 404),
        # an API token limited to npm-hosted: the anonymous grants on the proxy do not get past it
        ('narrowed', 'GET', 'npm-hosted', 200),
        ('narrowed', 'GET', 'npm-proxy', 404),
        ('narrowed', 'PUT', 'npm-hosted', 403),
    ],
)
async def test_format_route_matrix(
    matrix_client: httpx.AsyncClient, who: str | None, method: str, repo: str, status: int
) -> None:
    headers = {'Authorization': f'Bearer {who}'} if who else {}
    response = await matrix_client.request(method, f'/repository/{repo}/pkg', headers=headers)
    assert response.status_code == status, response.text


async def test_api_route_rules(matrix_client: httpx.AsyncClient) -> None:
    assert (await matrix_client.get('/api/v1/auth/me')).status_code == 401
    assert (await matrix_client.get('/api/v1/config')).status_code == 200


def _api_routes(rule_kinds: set[str], at_least: int = 40) -> list[tuple[str, str, access.RouteAccess]]:
    out = []
    for path, methods, endpoint in iter_routes(create_app()):
        rule = access.route_rule(endpoint)
        if path.startswith('/api/v1/') and rule is not None and rule.kind in rule_kinds:
            out.extend((method, re.sub(r'\{[^}]+\}', 'x', path), rule) for method in sorted(methods))
    assert len(out) > at_least, 'the enumeration must see the included routers'
    return out


@pytest.mark.parametrize(('method', 'path', 'rule'), _api_routes({'global', 'authenticated'}))
async def test_every_protected_api_route_refuses_anonymous_and_unprivileged(
    matrix_client: httpx.AsyncClient, method: str, path: str, rule: access.RouteAccess
) -> None:
    """Route x principal matrix for the REST API: anonymous gets 401 wherever a principal is needed, and a reader and
    a deployer (no global actions) get 403 on every route that needs one (decided by ``enforce`` before the handler
    runs). The administrator column is ``test_identity_integration.test_admin_passes_every_access_rule``."""
    body = {} if method in ('POST', 'PUT', 'PATCH') else None
    anonymous = await matrix_client.request(method, path, json=body)
    assert anonymous.status_code == 401, f'{method} {path}: {anonymous.status_code}'
    if rule.kind == 'global':
        for who in ('reader', 'deployer', 'nobody', 'guest', 'newcomer'):
            denied = await matrix_client.request(method, path, json=body, headers={'Authorization': f'Bearer {who}'})
            assert denied.status_code == 403, f'{who} {method} {path}: {denied.status_code}'
            assert denied.json()['code'] == 'permission_required', f'{who} {method} {path}: {denied.text}'


@pytest.mark.parametrize(('method', 'path', 'rule'), _api_routes({'repo'}, at_least=20))
async def test_every_repository_api_route_hides_repositories_from_a_principal_without_roles(
    matrix_client: httpx.AsyncClient, method: str, path: str, rule: access.RouteAccess
) -> None:
    """The "signed in, no roles, no anonymous access" column for repository routes: every repository is hidden
    (404, never 403); the anonymous-grants columns (a role, or the grants every signed-in principal receives) see
    proxies but nothing hosted. Decided by ``enforce`` before the handler runs."""
    assert rule.param is not None
    body = {} if method in ('POST', 'PUT', 'PATCH') else None
    for repo in ('npm-proxy', 'npm-hosted'):
        url = path.replace('/x', f'/{repo}', 1) if '/repositories/x' in path else path
        hidden = await matrix_client.request(method, url, json=body, headers={'Authorization': 'Bearer nobody'})
        assert hidden.status_code == 404, f'nobody {method} {url}: {hidden.status_code}'
        if repo == 'npm-proxy' and rule.action in (Action.BROWSE, Action.READ):
            continue  # allowed: the handler would run (needs MongoDB; the integration tests cover it)
        expected = 404 if repo == 'npm-hosted' else 403
        for who in ('guest', 'newcomer'):  # the anonymous grants, held as a role or received on signing in
            got = await matrix_client.request(method, url, json=body, headers={'Authorization': f'Bearer {who}'})
            assert got.status_code == expected, f'{who} {method} {url}: {got.status_code}'


async def test_signed_in_principal_without_roles_gets_calm_answers(matrix_client: httpx.AsyncClient) -> None:
    """What the UI shell asks for a user with no grants: no 401 (that would sign the user out), the stats and the
    event stream are 403 ``permission_required`` (the UI does not ask without ``stats.read``)."""
    nobody = {'Authorization': 'Bearer nobody'}
    for path in ('/api/v1/stats/summary', '/api/v1/activity', '/api/v1/events/stream?topics=transfers'):
        response = await matrix_client.get(path, headers=nobody)
        assert response.status_code == 403, f'{path}: {response.status_code}'
        assert response.json()['code'] == 'permission_required'
    viewer = await matrix_client.get('/api/v1/events/stream?topics=tasks', headers={'Authorization': 'Bearer viewer'})
    assert viewer.status_code == 403  # the tasks topic is admin-only
    assert viewer.json()['code'] == 'permission_required'


# ---------------------------------------------------------------------------- ADR 0005: the anonymous challenge
@pytest.mark.parametrize('method', ['GET', 'HEAD', 'PUT', 'DELETE'])
@pytest.mark.parametrize('path', ['pkg', '', 'a/%2e%2e/%2e%2e/etc/passwd', 'a%00b'])
async def test_anonymous_gets_the_same_challenge_for_hidden_and_missing_repositories(
    matrix_client: httpx.AsyncClient, method: str, path: str
) -> None:
    """ADR 0005: no credentials on ``/repository/<repo>/...`` → 401 + Basic challenge for a hidden repository, and
    the very same answer (status, headers, body) for a repository that does not exist, so names are not
    enumerable."""
    hidden = await matrix_client.request(method, f'/repository/npm-hosted/{path}')
    missing = await matrix_client.request(method, f'/repository/no-such-repo/{path}')
    assert hidden.status_code == 401, hidden.text
    assert hidden.headers['www-authenticate'].startswith('Basic realm="Waystation"')
    assert missing.status_code == hidden.status_code
    assert missing.content == hidden.content
    assert {k: v for k, v in missing.headers.items() if k != 'x-request-id'} == {
        k: v for k, v in hidden.headers.items() if k != 'x-request-id'
    }


@pytest.mark.parametrize('who', ['nobody', 'not-a-known-token'])
@pytest.mark.parametrize('repo', ['npm-hosted', 'no-such-repo'])
async def test_requests_with_credentials_keep_the_404(matrix_client: httpx.AsyncClient, who: str, repo: str) -> None:
    """An authenticated principal that cannot see the repository, or a request whose credentials did not resolve to a
    principal, keeps the 404 (ADR 0005 only changes requests without credentials)."""
    response = await matrix_client.get(f'/repository/{repo}/pkg', headers={'Authorization': f'Bearer {who}'})
    assert response.status_code == 404, response.text
    assert 'www-authenticate' not in response.headers


async def test_visible_repository_without_permission_still_challenges_and_others_are_unchanged(
    matrix_client: httpx.AsyncClient,
) -> None:
    assert (await matrix_client.get('/repository/npm-proxy/pkg')).status_code == 200
    denied = await matrix_client.put('/repository/npm-proxy/pkg')
    assert denied.status_code == 401
    assert denied.headers['www-authenticate'].startswith('Basic realm="Waystation"')
    hidden = await matrix_client.put('/repository/npm-hosted/pkg')
    assert hidden.content == denied.content  # visible-but-denied and hidden look the same to anonymous
    # REST API and /v2/ are unchanged: the API hides with 404, /v2/ challenges with Bearer.
    api = await matrix_client.get('/api/v1/repositories/npm-hosted')
    assert api.status_code == 404
    assert 'www-authenticate' not in api.headers
    assert (await matrix_client.get('/api/v1/repositories/no-such-repo')).status_code == 404
    v2 = await matrix_client.get('/v2/npm-hosted/tags/list')
    assert v2.status_code == 401
    assert v2.headers['www-authenticate'].startswith('Bearer ')


def test_hidden_repo_decision() -> None:
    def request(path: str, headers: dict[str, str] | None = None) -> Request:
        raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        return Request({'type': 'http', 'method': 'GET', 'path': path, 'headers': raw, 'query_string': b''})

    assert access.hidden_repo(request('/repository/x/a'), ANONYMOUS).status == 401
    assert access.hidden_repo(request('/repository/x/a', {'Authorization': 'Bearer junk'}), ANONYMOUS).status == 404
    assert access.hidden_repo(request('/repository/x/a'), READER).status == 404
    assert access.hidden_repo(request('/api/v1/repositories/x'), ANONYMOUS).status == 404
    assert access.hidden_repo(request('/v2/x/tags/list'), ANONYMOUS).status == 404
