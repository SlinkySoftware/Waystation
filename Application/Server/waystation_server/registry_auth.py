"""Docker registry authentication plumbing (T-S12; FORMATS.md "Docker / OCI"; Docker token authentication spec).

- ``GET /auth/token``: the token service (``security.registry_token``): anonymous, or HTTP basic credentials of a
  local user (``docker login``). Grants per scope = what RBAC allows on the repository the name resolves to.
- ``/v2/`` only accepts registry tokens. Without one (or with one that does not cover the repository) the answer is
  ``401`` with ``WWW-Authenticate: Bearer realm="<base>/auth/token",service="<host>",scope="repository:<name>:pull"``
  (``error="insufficient_scope"`` when a token was sent), which makes clients fetch a token and retry; ``GET /v2/``
  without a token is the version check that starts this dance.
- The bearer authenticator turns a valid token into the principal it was issued to (re-resolved every request)
  limited to the token's grants, mapped from Docker actions to repository actions (pull → read, push → write,
  delete → delete).
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import logging
import re
import time
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from waystation_common.errors import Unauthorized
from waystation_server.db import users as users_db
from waystation_server.problems import oci_error
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import user_principal
from waystation_server.security.identity import UserRecord
from waystation_server.security.login import verify_login
from waystation_server.security.principal import ANONYMOUS, AuthMethod, Principal
from waystation_server.security.registry_token import Scope, parse_scopes
from waystation_server.services import Services

log = logging.getLogger('waystation.registry_auth')

router = APIRouter(include_in_schema=False)

DOCKER_API_VERSION = {'Docker-Distribution-API-Version': 'registry/2.0'}
ACTION_MAP: dict[str, tuple[Action, ...]] = {
    'pull': (Action.READ, Action.BROWSE),
    'push': (Action.WRITE,),
    'delete': (Action.DELETE,),
}
_OPERATION = re.compile(r'/(manifests|blobs|tags|referrers)(/|$)')
USER_CACHE_S = 30.0


def registry_host(services: Services) -> str:
    return urlsplit(services.base_url).netloc


def image_name(name_path: str) -> str:
    """``library/nginx/manifests/latest`` → ``library/nginx`` (the name in the request URL)."""
    matches = list(_OPERATION.finditer(name_path))
    return name_path[: matches[-1].start()] if matches else name_path


def request_scope(name_path: str, method: str) -> str | None:
    if name_path in ('', '_catalog'):
        return 'registry:catalog:*' if name_path == '_catalog' else None
    actions = 'delete' if method == 'DELETE' else ('pull,push' if method in ('PUT', 'POST', 'PATCH') else 'pull')
    return f'repository:{image_name(name_path)}:{actions}'


def challenge(services: Services, *, scope: str | None, error: str | None = None) -> Response:
    parts = [f'realm="{services.base_url}/auth/token"', f'service="{registry_host(services)}"']
    if scope:
        parts.append(f'scope="{scope}"')
    if error:
        parts.append(f'error="{error}"')
    return oci_error(
        401,
        'authentication required',
        code='UNAUTHORIZED',
        headers={'WWW-Authenticate': 'Bearer ' + ','.join(parts), **DOCKER_API_VERSION},
    )


def token_allows(principal: Principal, repo_name: str, action: Action) -> bool:
    """Does the registry token grant ``action`` on the repository (before RBAC is consulted)?"""
    if principal.token_access is None:
        return False
    return any(name == repo_name and action in actions for name, actions in principal.token_access)


# ---------------------------------------------------------------------------- grants
async def _grant(services: Services, principal: Principal, scope: Scope) -> Scope:
    from waystation_server.routing import resolve_docker

    if scope.type == 'registry':
        allowed = scope.name == 'catalog' and access.check(principal, Action.ADMIN)
        return dataclasses.replace(scope, actions=('*',) if allowed else ())
    route = await resolve_docker(services, scope.name)
    if route is None or not access.can_see(principal, route.repo):
        return dataclasses.replace(scope, actions=())
    wanted = DOCKER_ACTIONS_ALL if '*' in scope.actions else scope.actions
    granted = tuple(a for a in wanted if a in ACTION_MAP and access.check(principal, ACTION_MAP[a][0], route.repo))
    return dataclasses.replace(scope, actions=granted)


DOCKER_ACTIONS_ALL = ('pull', 'push', 'delete')


def _basic(request: Request) -> tuple[str, str] | None:
    scheme, _, credentials = request.headers.get('authorization', '').partition(' ')
    if scheme.lower() != 'basic' or not credentials:
        return None
    try:
        username, sep, password = base64.b64decode(credentials.strip(), validate=True).decode('utf-8').partition(':')
    except binascii.Error, UnicodeDecodeError:
        raise Unauthorized('invalid credentials') from None
    if not sep:
        raise Unauthorized('invalid credentials')
    return username, password


@router.get('/auth/token')
@access.public  # the token service authenticates the caller itself (anonymous or basic credentials)
async def token(
    request: Request,
    service: str | None = None,
    scope: Annotated[list[str] | None, Query(max_length=4096)] = None,
) -> Response:
    services: Services = request.app.state.services
    host = registry_host(services)
    if service and service != host:
        return oci_error(400, f'unknown service {service!r}', code='UNSUPPORTED')
    principal: Principal = ANONYMOUS
    credentials = _basic(request)
    if credentials is not None:
        user = await verify_login(
            services.db,
            services.valkey,
            services.settings,
            username=credentials[0],
            password=credentials[1],
            client_ip=str(getattr(request.state, 'client_ip', '') or 'unknown'),
        )
        principal = user_principal(user, AuthMethod.BASIC)
    scopes = parse_scopes(scope or [])[:20]
    granted = [await _grant(services, principal, s) for s in scopes]
    signer = await services.registry_signer.get()
    subject = 'anonymous' if principal.is_anonymous else principal.id
    jwt, ttl = signer.issue(subject=subject, audience=host, access=granted)
    request.state.log['principal'] = principal.display
    return JSONResponse(
        {
            'token': jwt,
            'access_token': jwt,
            'expires_in': ttl,
            'issued_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        }
    )


# ---------------------------------------------------------------------------- bearer authentication on /v2/
class _UserCache:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, UserRecord | None]] = {}

    async def get(self, services: Services, user_id: str) -> UserRecord | None:
        hit = self._entries.get(user_id)
        if hit is not None and time.monotonic() - hit[0] < USER_CACHE_S:
            return hit[1]
        user = await users_db.get(services.db, user_id)
        if len(self._entries) > 10_000:
            self._entries.clear()
        self._entries[user_id] = (time.monotonic(), user)
        return user


_users = _UserCache()


async def authenticate_bearer(request: Request, token: str) -> Principal | None:
    if not request.url.path.startswith('/v2'):
        return None
    services: Services = request.app.state.services
    signer = await services.registry_signer.get()
    claims = signer.verify(token, audience=registry_host(services))
    if claims is None:
        return None
    if claims.subject == 'anonymous':
        base = dataclasses.replace(ANONYMOUS, method=AuthMethod.REGISTRY_TOKEN)
    else:
        user = await _users.get(services, claims.subject)
        if user is None or user.disabled:
            return None
        base = user_principal(user, AuthMethod.REGISTRY_TOKEN)
    from waystation_server.routing import resolve_docker

    grants: dict[str, set[str]] = {}
    for scope in claims.access:
        if scope.type != 'repository' or not scope.actions:
            continue
        route = await resolve_docker(services, scope.name)
        if route is None:
            continue
        actions = grants.setdefault(route.repo.name, set())
        for docker_action in scope.actions:
            actions.update(a.value for a in ACTION_MAP.get(docker_action, ()))
    token_access = tuple((name, frozenset(Action(a) for a in acts)) for name, acts in grants.items())
    return dataclasses.replace(base, token_access=token_access)


def install(services: Services) -> None:
    if authenticate_bearer not in services.bearer_authenticators:
        services.bearer_authenticators.append(authenticate_bearer)
