"""RBAC decision point (ARCHITECTURE §7): decisions, the route-enumeration test and an HTTP matrix."""

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
from waystation_server.security.principal import ANONYMOUS, AuthMethod, Principal, PrincipalKind
from waystation_server.services import Services

PROXY = make_repo('npm-proxy', 'npm')
HOSTED = make_repo('npm-hosted', 'npm', type=RepoType.HOSTED, proxy=None)
DOCKER = make_repo('docker-hub', 'docker')


def user(*roles: str) -> Principal:
    return Principal(PrincipalKind.USER, f'id-{"-".join(roles)}', '-'.join(roles) or 'nobody', roles, AuthMethod.BASIC)


READER, DEPLOYER, ADMIN, VIEWER, NOBODY = user('reader'), user('deployer'), user('admin'), user('viewer'), user()
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
    assert access.capabilities(NOBODY) == []  # signed in, no role: the UI shows the "no access yet" landing
    custom = Principal(
        PrincipalKind.USER,
        'id-custom',
        'custom',
        ('docker-readers',),
        AuthMethod.BASIC,
        grants=access.Grants(is_admin=False, permissions=(access.Permission(Action.READ, 'docker-*'),)),
    )
    assert access.capabilities(custom) == ['repositories.browse']  # read on a glob: some repository may be visible


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


TOKENS = {'reader': READER, 'deployer': DEPLOYER, 'admin': ADMIN, 'nobody': NOBODY, 'guest': GUEST, 'viewer': VIEWER}


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
        # signed in, no role: nothing is visible, so everything is 404 (never 403, never 401)
        ('nobody', 'GET', 'npm-proxy', 404),
        ('nobody', 'GET', 'npm-hosted', 404),
        ('nobody', 'PUT', 'npm-proxy', 404),
        # signed in with only the anonymous grants: proxies readable, hosted hidden, no writes
        ('guest', 'GET', 'npm-proxy', 200),
        ('guest', 'GET', 'npm-hosted', 404),
        ('guest', 'PUT', 'npm-proxy', 403),
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
        for who in ('reader', 'deployer', 'nobody', 'guest'):
            denied = await matrix_client.request(method, path, json=body, headers={'Authorization': f'Bearer {who}'})
            assert denied.status_code == 403, f'{who} {method} {path}: {denied.status_code}'
            assert denied.json()['code'] == 'permission_required', f'{who} {method} {path}: {denied.text}'


@pytest.mark.parametrize(('method', 'path', 'rule'), _api_routes({'repo'}, at_least=20))
async def test_every_repository_api_route_hides_repositories_from_a_principal_without_roles(
    matrix_client: httpx.AsyncClient, method: str, path: str, rule: access.RouteAccess
) -> None:
    """The "signed in, no roles" column for repository routes: every repository is hidden (404, never 403), the
    anonymous-only column sees proxies but nothing hosted. Decided by ``enforce`` before the handler runs."""
    assert rule.param is not None
    body = {} if method in ('POST', 'PUT', 'PATCH') else None
    for repo in ('npm-proxy', 'npm-hosted'):
        url = path.replace('/x', f'/{repo}', 1) if '/repositories/x' in path else path
        hidden = await matrix_client.request(method, url, json=body, headers={'Authorization': 'Bearer nobody'})
        assert hidden.status_code == 404, f'nobody {method} {url}: {hidden.status_code}'
        if repo == 'npm-proxy' and rule.action in (Action.BROWSE, Action.READ):
            continue  # allowed: the handler would run (needs MongoDB; the integration tests cover it)
        as_guest = await matrix_client.request(method, url, json=body, headers={'Authorization': 'Bearer guest'})
        expected = 404 if repo == 'npm-hosted' else 403
        assert as_guest.status_code == expected, f'guest {method} {url}: {as_guest.status_code}'


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
