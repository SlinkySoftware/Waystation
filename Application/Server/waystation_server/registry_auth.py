"""Docker registry authentication plumbing (T-S12; FORMATS.md "Docker / OCI"; Docker token authentication spec).

- ``GET /auth/token``: the token service (``security.registry_token``): anonymous, or HTTP basic credentials
  (``docker login``): a local user's password, or an API token as the password (any username; the registry token
  then carries the API token's id and its scopes/repository patterns narrow the grants). Grants per scope = what
  RBAC allows on the repository the name resolves to. ``offline_token=true`` with credentials (``docker login``)
  also returns a ``refresh_token`` (90 days), which the Docker CLI stores instead of the password.
- ``POST /auth/token``: the OAuth2 endpoint of the token spec (form body): ``grant_type=password`` (as above;
  ``access_type=offline`` adds a refresh token; containerd tries this first) and ``grant_type=refresh_token``
  (the subject is re-checked: enabled user, registry tokens not revoked since, or the API token still live).
- Revocation before expiry (``security.registry_revocation``): disabling or deleting a user, a password change and
  revoking sessions move the user's cut-off forward; ``/v2/`` and the refresh grant refuse tokens issued before it.
  Registry tokens issued for an API token live exactly as long as that API token.
- ``/v2/`` only accepts registry tokens. Without one (or with one that does not cover the repository) the answer is
  ``401`` with ``WWW-Authenticate: Bearer realm="<base>/auth/token",service="<host>",scope="repository:<name>:pull"``
  (``error="insufficient_scope"`` when a token was sent), which makes clients fetch a token and retry; ``GET /v2/``
  without a token is the version check that starts this dance.
- containerd sends ``?ns=<registry>`` to a mirror's ``/v2/`` but not to the token service, so the challenge to such a
  request carries it in the realm (``/auth/token?ns=...``; clients keep the realm's query). Scopes are then resolved
  with it (``routing.resolve_docker``) and a granted scope is renamed ``<repository>/<image>``, so the token names the
  repository it was granted on and does not depend on ``ns`` afterwards.
- ``registry:catalog:*`` is granted to every caller: ``/v2/_catalog`` lists only the images of repositories RBAC lets
  the principal see.
- The bearer authenticator turns a valid token into the principal it was issued to (re-resolved every request;
  for an API token: the token itself, so revocation is immediate) limited to the token's grants, mapped from Docker
  actions to repository actions (pull → read, push → write, delete → delete).
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
import time
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.db import users as users_db
from waystation_server.problems import oci_error
from waystation_server.security import access, registry_revocation
from waystation_server.security.access import Action
from waystation_server.security.authn import basic_credentials, client_address, principal_for
from waystation_server.security.identity import UserRecord
from waystation_server.security.login import verify_login
from waystation_server.security.principal import AuthMethod, Principal
from waystation_server.security.registry_token import Scope, parse_scopes
from waystation_server.security.tokens import looks_like_token
from waystation_server.services import Services

log = logging.getLogger('waystation.registry_auth')

router = APIRouter(include_in_schema=False)

DOCKER_API_VERSION = {'Docker-Distribution-API-Version': 'registry/2.0'}
# Docker action → repository actions a registry token carries (the first is the one RBAC must allow). ``push`` and
# ``delete`` also carry ``browse`` (RBAC: write/delete imply it), so a delete-only token sees the repository instead of
# getting the hidden-repository 404.
ACTION_MAP: dict[str, tuple[Action, ...]] = {
    'pull': (Action.READ, Action.BROWSE),
    'push': (Action.WRITE, Action.BROWSE),
    'delete': (Action.DELETE, Action.BROWSE),
}
_OPERATION = re.compile(r'/(manifests|blobs|tags|referrers)(/|$)')
USER_CACHE_S = 30.0
# The ``token_access`` entry of a token that may list the catalog (repository names never contain ``:``).
CATALOG = 'registry:catalog'


def registry_host(services: Services) -> str:
    return urlsplit(services.base_url).netloc


def image_name(name_path: str) -> str:
    """``library/nginx/manifests/latest`` → ``library/nginx`` (the name in the request URL)."""
    matches = list(_OPERATION.finditer(name_path))
    return name_path[: matches[-1].start()] if matches else name_path


_UPLOAD = re.compile(r'.+/blobs/uploads(/[^/]+)?')


def is_upload(name_path: str) -> bool:
    """``<name>/blobs/uploads[/<id>]``: an upload session (cancelling one with DELETE is part of a push)."""
    return _UPLOAD.fullmatch(name_path) is not None


def request_action(name_path: str, method: str) -> Action:
    """The repository action a ``/v2/`` request needs: pull = read, push = write, delete = delete (an upload's
    ``DELETE`` cancels a push: write)."""
    if method == 'DELETE' and is_upload(name_path):
        return Action.WRITE
    return access.method_action(method)


def request_scope(name_path: str, method: str) -> str | None:
    if name_path in ('', '_catalog'):
        return 'registry:catalog:*' if name_path == '_catalog' else None
    action = request_action(name_path, method)
    actions = {Action.DELETE: 'delete', Action.WRITE: 'pull,push'}.get(action, 'pull')
    return f'repository:{image_name(name_path)}:{actions}'


def challenge(services: Services, *, scope: str | None, error: str | None = None, ns: str | None = None) -> Response:
    realm = f'{services.base_url}/auth/token' + (f'?{urlencode({"ns": ns})}' if ns else '')
    parts = [f'realm="{realm}"', f'service="{registry_host(services)}"']
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
async def _grant(services: Services, principal: Principal, scope: Scope, ns: str | None = None) -> Scope:
    from waystation_server.routing import resolve_docker

    if scope.type == 'registry':
        # ``/v2/_catalog`` lists only what RBAC lets the principal see, so anyone may ask for it.
        allowed = scope.name == 'catalog' and '*' in scope.actions
        return dataclasses.replace(scope, actions=('*',) if allowed else ())
    route = await resolve_docker(services, scope.name, ns)
    if route is None or not access.can_see(principal, route.repo):
        return dataclasses.replace(scope, actions=())
    wanted = DOCKER_ACTIONS_ALL if '*' in scope.actions else scope.actions
    granted = tuple(a for a in wanted if a in ACTION_MAP and access.check(principal, ACTION_MAP[a][0], route.repo))
    if ns is not None:
        # ``<repository>/<image>`` resolves to the same repository without ``ns`` (``authenticate_bearer``).
        return dataclasses.replace(scope, name=f'{route.repo.name}/{route.path}', actions=granted)
    return dataclasses.replace(scope, actions=granted)


DOCKER_ACTIONS_ALL = ('pull', 'push', 'delete')


DOCKER_LOGIN_AUDIT_S = 3600


async def _audit_password_login(services: Services, principal: Principal, client_ip: str) -> None:
    """Docker clients send the password with every token request (each pull or push), so a successful one is
    audited at most once an hour per user and client address (failures always are, in ``verify_login``)."""
    digest = hashlib.sha256(f'{principal.id} {client_ip}'.encode()).hexdigest()[:32]
    try:
        first = bool(await services.valkey.set(f'audit:docker-login:{digest}', '1', nx=True, ex=DOCKER_LOGIN_AUDIT_S))
    except Exception:  # Valkey down: record it rather than lose it
        first = True
    if first:
        audit.record(
            AuditAction.AUTH_LOGIN,
            actor=principal,
            target=audit.target('user', principal.id, principal.name),
            details={'username': principal.name, 'via': 'docker', 'provider': principal.provider},
        )


async def _password_principal(services: Services, request: Request, username: str, password: str) -> Principal:
    """``docker login``: a local password, or an API token as the password (any username). Raises ``Unauthorized``
    (rate-limited and audited like every sign-in)."""
    if looks_like_token(password):
        return await services.tokens.authenticate(password, client_ip=client_address(request))
    user = await verify_login(services, username=username, password=password, client_ip=client_address(request))
    principal = await principal_for(services, user, AuthMethod.BASIC)
    await _audit_password_login(services, principal, client_address(request))
    return principal


async def _issue(
    request: Request,
    services: Services,
    principal: Principal,
    *,
    scope_values: list[str],
    ns: str | None,
    offline: bool,
) -> Response:
    """The token response: an access token for what the scopes grant, plus a refresh token when ``offline`` was
    asked for by a signed-in caller."""
    from waystation_server.routing import mirror_namespace

    host = registry_host(services)
    scopes = parse_scopes(scope_values)[:20]
    namespace = mirror_namespace(services, ns)
    granted = [await _grant(services, principal, s, namespace) for s in scopes]
    signer = await services.registry_signer.get()
    subject = 'anonymous' if principal.is_anonymous else principal.id
    token_id = principal.token.token_id if principal.token is not None else None
    now = time.time()
    jwt, ttl = signer.issue(subject=subject, audience=host, access=granted, token_id=token_id, now=now)
    request.state.log['principal'] = principal.display
    body: dict[str, object] = {
        'token': jwt,
        'access_token': jwt,
        'expires_in': ttl,
        'issued_at': datetime.fromtimestamp(now, UTC).isoformat().replace('+00:00', 'Z'),
        'scope': ' '.join(f'{s.type}:{s.name}:{",".join(s.actions)}' for s in granted if s.actions),
    }
    if offline and not principal.is_anonymous:
        body['refresh_token'] = signer.issue_refresh(subject=subject, audience=host, token_id=token_id, now=now)
    return JSONResponse(body, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})


def _service_error(services: Services, service: str | None) -> Response | None:
    if service and service != registry_host(services):
        return oci_error(400, f'unknown service {service!r}', code='UNSUPPORTED')
    return None


@router.get('/auth/token')
@access.public  # the token service authenticates the caller itself (anonymous or basic credentials)
async def token(
    request: Request,
    service: str | None = None,
    scope: Annotated[list[str] | None, Query(max_length=4096)] = None,
    ns: Annotated[str | None, Query(max_length=255)] = None,
    offline_token: Annotated[str | None, Query(max_length=8)] = None,
) -> Response:
    """Docker token authentication: anonymous, or HTTP basic credentials. ``offline_token=true`` (``docker login``)
    with credentials also returns a ``refresh_token``, which clients keep instead of the password."""
    services: Services = request.app.state.services
    if (problem := _service_error(services, service)) is not None:
        return problem
    principal: Principal = await services.rbac.anonymous()
    credentials = basic_credentials(request.headers.get('authorization', ''))
    if credentials is not None:
        principal = await _password_principal(services, request, *credentials)
    return await _issue(
        request,
        services,
        principal,
        scope_values=scope or [],
        ns=ns,
        offline=(offline_token or '').lower() == 'true',
    )


FORM_MAX_BYTES = 16 * 1024


async def _form(request: Request) -> dict[str, str] | None:
    """The ``application/x-www-form-urlencoded`` body (bounded); None when it is not one."""
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/x-www-form-urlencoded':
        return None
    body = b''
    async for chunk in request.stream():
        body += chunk
        if len(body) > FORM_MAX_BYTES:
            return None
    try:
        pairs = parse_qsl(body.decode('utf-8'), keep_blank_values=True, max_num_fields=64)
    except UnicodeDecodeError, ValueError:
        return None
    return dict(pairs)


def _invalid_grant(message: str) -> Response:
    return oci_error(401, message, code='UNAUTHORIZED', headers={'Cache-Control': 'no-store'})


async def _refresh_principal(services: Services, refresh_token: str) -> Principal | None:
    """The principal a refresh token stands for, re-checked now: the user exists, is enabled and has not had their
    registry tokens revoked since (``security.registry_revocation``); a token issued for an API token needs that
    token to be live."""
    signer = await services.registry_signer.get()
    claims = signer.verify_refresh(refresh_token, audience=registry_host(services))
    if claims is None or claims.subject == 'anonymous':
        return None
    if claims.token_id is not None:
        # Issued for an API token: it lives exactly as long as that token (revoked, expired, owner disabled).
        owner = await services.tokens.by_id(claims.token_id)
        return owner if owner is not None and owner.id == claims.subject else None
    user = await users_db.get(services.db, claims.subject)
    if user is None or user.disabled:
        return None
    cut_offs = (
        registry_revocation.epoch_ms(user.tokens_not_before),
        await registry_revocation.not_before_ms(services.valkey, claims.subject),
    )
    if registry_revocation.revoked(claims.issued_at_ms, *cut_offs):
        return None
    return await principal_for(services, user, AuthMethod.REGISTRY_TOKEN)


@router.post('/auth/token')
@access.public  # OAuth2 token endpoint of the Docker token specification; authenticates the grant itself
async def oauth_token(request: Request, ns: Annotated[str | None, Query(max_length=255)] = None) -> Response:
    """``grant_type=password`` (``username``, ``password``; an API token works as the password) or
    ``grant_type=refresh_token`` (``refresh_token``), with ``service``, ``scope`` (space-separated) and
    ``client_id``. ``access_type=offline`` with a password grant also returns a ``refresh_token``. containerd posts a
    password grant first; the Docker daemon uses refresh tokens once ``docker login`` received one."""
    services: Services = request.app.state.services
    form = await _form(request)
    if form is None:
        return oci_error(400, 'expected an application/x-www-form-urlencoded body', code='UNSUPPORTED')
    if (problem := _service_error(services, form.get('service'))) is not None:
        return problem
    namespace = ns if ns is not None else form.get('ns')
    if namespace is not None and len(namespace) > 255:
        return oci_error(400, 'ns is too long', code='UNSUPPORTED')
    scope_values = [form.get('scope', '')]
    grant = form.get('grant_type', '')
    if grant == 'password':
        username, password = form.get('username', ''), form.get('password', '')
        if not username or not password:
            return oci_error(400, 'username and password are required', code='UNSUPPORTED')
        principal = await _password_principal(services, request, username, password)
        offline = form.get('access_type', '') == 'offline'
        return await _issue(request, services, principal, scope_values=scope_values, ns=namespace, offline=offline)
    if grant == 'refresh_token':
        presented = form.get('refresh_token', '')
        refreshed = await _refresh_principal(services, presented) if presented else None
        if refreshed is None:
            return _invalid_grant('invalid, expired or revoked refresh token')
        # The refresh token is not rotated: the Docker daemon keeps the one ``docker login`` stored.
        return await _issue(request, services, refreshed, scope_values=scope_values, ns=namespace, offline=False)
    return oci_error(400, 'unsupported grant_type (password or refresh_token)', code='UNSUPPORTED')


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
    if claims.token_id is not None:
        owner = await services.tokens.by_id(claims.token_id)
        if owner is None or owner.id != claims.subject:
            return None
        base = dataclasses.replace(owner, method=AuthMethod.REGISTRY_TOKEN)
    elif claims.subject == 'anonymous':
        base = await services.rbac.anonymous(AuthMethod.REGISTRY_TOKEN)
    else:
        user = await _users.get(services, claims.subject)
        if user is None or user.disabled:
            return None
        cut_offs = (
            registry_revocation.epoch_ms(user.tokens_not_before),
            await registry_revocation.not_before_ms(services.valkey, claims.subject),
        )
        if registry_revocation.revoked(claims.issued_at_ms, *cut_offs):
            return None
        base = await principal_for(services, user, AuthMethod.REGISTRY_TOKEN)
    from waystation_server.routing import resolve_docker

    grants: dict[str, set[str]] = {}
    for scope in claims.access:
        if scope.type == 'registry' and scope.name == 'catalog' and '*' in scope.actions:
            grants[CATALOG] = {Action.BROWSE.value}
            continue
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
