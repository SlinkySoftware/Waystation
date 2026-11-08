"""Authentication: who is making this request (ARCHITECTURE §4 step 2).

Order:

1. the UI session cookie;
2. ``Authorization``:
   - ``Bearer <api-token>`` / ``Token <api-token>`` (scripts, npm ``_authToken``, Terraform, Galaxy);
   - ``Basic`` with an API token as the password, any username (curl ``-u``, apt ``auth.conf``, Maven, pip...);
   - ``Bearer <jwt>`` on ``/v2/``: a Docker registry token (``registry_auth``; ``/v2/`` accepts nothing else, so API
     tokens there are ignored and the client gets the Bearer challenge);
3. else ``anonymous``.

Credentials that are presented but not valid are refused (401) rather than downgraded to anonymous, so a revoked
or expired token never silently keeps working through anonymous access. Basic credentials whose password is not
an API token are refused too: format clients use API tokens (only ``docker login`` takes a password, at
``/auth/token``). The result is cached on the request (``request.state.principal``); the session record, when there
is one, on ``request.state.session``.
"""

from __future__ import annotations

import base64
import binascii

from starlette.requests import Request

from waystation_common.errors import Unauthorized
from waystation_common.logs import bind_context
from waystation_server.db import users as users_db
from waystation_server.security.identity import SessionRecord, UserRecord
from waystation_server.security.principal import ANONYMOUS, AuthMethod, Principal, PrincipalKind
from waystation_server.security.sessions import SESSION_COOKIE
from waystation_server.security.tokens import looks_like_token
from waystation_server.services import Services

TOKEN_ONLY = 'use an API token as the password'  # noqa: S105 - a message


def user_principal(
    user: UserRecord, method: AuthMethod, *, session_id: str | None = None, roles: tuple[str, ...] | None = None
) -> Principal:
    """``roles`` = the effective roles (``principal_for``); default: the user's own roles only."""
    granted = tuple(user.roles) if roles is None else roles
    return Principal(PrincipalKind.USER, user.id, user.username, granted, method, session_id=session_id)


async def principal_for(
    services: Services, user: UserRecord, method: AuthMethod, *, session_id: str | None = None
) -> Principal:
    """The user's principal with its effective roles: its own plus those of its groups (``security.groups``)."""
    roles = await services.groups.roles_for(user)
    return user_principal(user, method, session_id=session_id, roles=roles)


def basic_credentials(header: str) -> tuple[str, str] | None:
    """``Authorization: Basic`` → (username, password); None for other schemes; ``Unauthorized`` when malformed."""
    scheme, _, credentials = header.partition(' ')
    if scheme.lower() != 'basic' or not credentials:
        return None
    try:
        decoded = base64.b64decode(credentials.strip(), validate=True).decode('utf-8')
    except binascii.Error, UnicodeDecodeError:
        raise Unauthorized('invalid credentials') from None
    username, sep, password = decoded.partition(':')
    if not sep:
        raise Unauthorized('invalid credentials')
    return username, password


def client_address(request: Request) -> str:
    return str(getattr(request.state, 'client_ip', '') or 'unknown')


async def _from_session(services: Services, request: Request) -> Principal | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    record = await services.sessions.resolve(token)
    if record is None:
        return None
    user = await users_db.get(services.db, record.user_id)
    if user is None or user.disabled:
        await services.sessions.destroy(record.id)
        return None
    request.state.session = record
    request.state.user = user
    return await principal_for(services, user, AuthMethod.SESSION, session_id=record.id)


async def _from_header(services: Services, request: Request) -> Principal | None:
    header = request.headers.get('authorization', '')
    if not header:
        return None
    registry = request.url.path == '/v2' or request.url.path.startswith('/v2/')
    scheme, _, credentials = header.partition(' ')
    credentials = credentials.strip()
    if scheme.lower() in ('bearer', 'token') and credentials:
        if looks_like_token(credentials) and not registry:
            return await services.tokens.authenticate(credentials, client_ip=client_address(request))
        if scheme.lower() == 'bearer':
            for authenticator in services.bearer_authenticators:
                principal = await authenticator(request, credentials)
                if principal is not None:
                    return principal
        return None
    if registry:
        return None
    basic = basic_credentials(header)
    if basic is None:
        return None
    if not looks_like_token(basic[1]):
        raise Unauthorized(TOKEN_ONLY)
    return await services.tokens.authenticate(basic[1], client_ip=client_address(request))


async def authenticate(request: Request) -> Principal:
    cached: Principal | None = getattr(request.state, 'principal', None)
    if cached is not None:
        return cached
    services: Services = request.app.state.services
    principal = await _from_session(services, request)
    if principal is None:
        principal = await _from_header(services, request)
    principal = principal or ANONYMOUS
    request.state.principal = principal
    request.state.log['principal'] = principal.display
    bind_context(principal=principal.display)
    return principal


def current_session(request: Request) -> SessionRecord | None:
    session: SessionRecord | None = getattr(request.state, 'session', None)
    return session
