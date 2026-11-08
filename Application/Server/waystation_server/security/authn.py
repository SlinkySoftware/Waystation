"""Authentication: who is making this request (ARCHITECTURE §4 step 2).

Order: session cookie (UI), Docker registry token (``Authorization: Bearer <jwt>``, ``/v2/`` only), else
``anonymous``. API tokens and basic auth for format clients arrive with T-S04. The result is cached on the request
(``request.state.principal``); the session record, when there is one, on ``request.state.session``.
"""

from __future__ import annotations

from starlette.requests import Request

from waystation_common.logs import bind_context
from waystation_server.db import users as users_db
from waystation_server.security.identity import SessionRecord, UserRecord
from waystation_server.security.principal import ANONYMOUS, AuthMethod, Principal, PrincipalKind
from waystation_server.security.sessions import SESSION_COOKIE
from waystation_server.services import Services


def user_principal(user: UserRecord, method: AuthMethod, *, session_id: str | None = None) -> Principal:
    return Principal(PrincipalKind.USER, user.id, user.username, tuple(user.roles), method, session_id=session_id)


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
    return user_principal(user, AuthMethod.SESSION, session_id=record.id)


async def authenticate(request: Request) -> Principal:
    cached: Principal | None = getattr(request.state, 'principal', None)
    if cached is not None:
        return cached
    services: Services = request.app.state.services
    principal = await _from_session(services, request)
    if principal is None:
        header = request.headers.get('authorization', '')
        scheme, _, credentials = header.partition(' ')
        if scheme.lower() == 'bearer' and credentials:
            for authenticator in services.bearer_authenticators:
                principal = await authenticator(request, credentials.strip())
                if principal is not None:
                    break
    principal = principal or ANONYMOUS
    request.state.principal = principal
    request.state.log['principal'] = principal.display
    bind_context(principal=principal.display)
    return principal


def current_session(request: Request) -> SessionRecord | None:
    session: SessionRecord | None = getattr(request.state, 'session', None)
    return session
