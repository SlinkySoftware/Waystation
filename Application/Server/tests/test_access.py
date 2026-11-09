"""RBAC decision point (ARCHITECTURE §7): decisions, the route-enumeration test and an HTTP matrix."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from ws_testing import asgi_client, make_repo, static_catalog

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
        'stats.read',
        'audit.read',
        'users.admin',
        'settings.admin',
    ]
    assert access.capabilities(VIEWER) == ['stats.read']
    assert access.capabilities(ANONYMOUS) == []


def test_requires_rejects_repo_actions() -> None:
    with pytest.raises(ValueError):
        access.requires(Action.READ)


def test_every_route_declares_its_access_rule() -> None:
    """ARCHITECTURE §7: a route without a declared action is a bug."""
    app = create_app()
    missing = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            if access.route_rule(route.endpoint) is None:
                missing.append(f'{sorted(route.methods)} {route.path}')
        elif isinstance(route, Route):
            # Framework routes outside the dependency system: only the public OpenAPI document is allowed.
            assert route.path == '/api/v1/openapi.json', route.path
    assert not missing, f'routes without an access rule: {missing}'


# ---------------------------------------------------------------------------- HTTP matrix
class EchoPlugin:
    def __init__(self) -> None:
        self.descriptor = FormatDescriptor(
            name='npm', label='npm', types=frozenset(RepoType), options_model=BaseModel, ui=UiHints('npm')
        )

    async def handle(self, req: FormatRequest) -> Response:
        return PlainTextResponse(f'{req.method} {req.repo.name}/{req.path} as {req.principal.display}')


TOKENS = {'reader': READER, 'deployer': DEPLOYER, 'admin': ADMIN}


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
        (None, 'GET', 'npm-hosted', 404),  # hidden: 404, never 403
        (None, 'PUT', 'npm-proxy', 401),
        (None, 'PUT', 'npm-hosted', 404),
        ('reader', 'GET', 'npm-hosted', 200),
        ('reader', 'PUT', 'npm-hosted', 403),
        ('deployer', 'PUT', 'npm-hosted', 200),
        ('deployer', 'DELETE', 'npm-hosted', 403),
        ('admin', 'DELETE', 'npm-hosted', 200),
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
