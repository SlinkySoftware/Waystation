"""The signed-in user's own account (SPECIFICATION §4.9): sessions, password, profile, effective permissions.

- ``GET /auth/sessions``: the caller's sessions (newest first) with last use, address, browser and which one is the
  current one. Session ids in the API are derived from the stored id (``SessionManager.public_id``), which is itself
  a hash of the cookie: neither reveals a usable cookie.
- ``DELETE /auth/sessions/{id}`` ends one (the current one too: its cookies are cleared); ``DELETE /auth/sessions``
  ends every other one. Audited as ``session.revoke``.
- ``POST /auth/me/password``: change a local password (needs an interactive session and the current password; rate
  limited like sign-in). It clears ``must_change_password`` and ends the user's other sessions. While a one-time
  password is pending, a session can only use this, ``GET /auth/me`` and sign-out (``access.enforce``).
- ``PATCH /auth/me``: display name and e-mail of a local account (a provider's sign-in overwrites them otherwise).
- ``GET /auth/me/permissions``: what the caller may do (narrowed by the API token used, when there is one).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from waystation_common.errors import Conflict, Forbidden, NotFound, Unauthorized
from waystation_server import audit
from waystation_server.api.auth import User
from waystation_server.api.deps import ServicesDep, client_ip
from waystation_server.api.rbac import (
    PROBLEM,
    EffectivePermissions,
    effective_permissions,
    require_manageable,
    user_grants,
)
from waystation_server.api.users import Email, UserId
from waystation_server.audit import AuditAction, Outcome
from waystation_server.db import sessions as sessions_db
from waystation_server.db import users as users_db
from waystation_server.problems import FieldError, FieldErrors
from waystation_server.security import access, passwords, ratelimit
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate, current_session
from waystation_server.security.identity import SessionRecord, UserRecord
from waystation_server.security.login import WINDOW_S
from waystation_server.security.principal import AuthMethod, PrincipalKind
from waystation_server.services import Services

router = APIRouter(tags=['auth'])

SessionId = Annotated[str, Path(min_length=1, max_length=64)]


class SessionInfo(BaseModel):
    id: str
    current: bool = Field(description='The session this request was made with')
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip: str | None
    user_agent: str | None
    provider_id: str | None = Field(description='The OIDC provider the session was signed in through, if any')


class SessionsRevoked(BaseModel):
    revoked: int


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra='forbid')

    current_password: str = Field(min_length=1, max_length=passwords.MAX_LENGTH, json_schema_extra={'writeOnly': True})
    new_password: str = Field(min_length=1, max_length=passwords.MAX_LENGTH, json_schema_extra={'writeOnly': True})


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    display_name: str | None = Field(None, min_length=1, max_length=256)
    email: Email = Field(None, max_length=256, description='null clears it')


async def _own_user(request: Request, svc: Services) -> UserRecord:
    principal = await authenticate(request)
    if principal.kind is not PrincipalKind.USER:
        raise Forbidden('only user accounts have sessions, passwords and profiles')
    user: UserRecord | None = getattr(request.state, 'user', None) or await users_db.get(svc.db, principal.id)
    if user is None:
        raise Unauthorized('not signed in')
    return user


def _session_target(public_id: str) -> audit.AuditTarget:
    return audit.target('session', public_id, None)


# ---------------------------------------------------------------------------- sessions
@router.get('/auth/sessions', response_model=list[SessionInfo], operation_id='listMySessions')
@access.authenticated
async def list_sessions(request: Request, svc: ServicesDep) -> list[SessionInfo]:
    return await _sessions_of(request, svc, await _own_user(request, svc))


async def _sessions_of(request: Request, svc: Services, user: UserRecord) -> list[SessionInfo]:
    current = current_session(request)
    records = await sessions_db.list_for_user(svc.db, user.id)
    seen = await svc.sessions.last_seen(records)
    return [
        SessionInfo(
            id=svc.sessions.public_id(r.id),
            current=current is not None and r.id == current.id,
            created_at=r.created_at,
            last_seen_at=seen[r.id],
            expires_at=r.expires_at,
            ip=r.ip,
            user_agent=r.user_agent,
            provider_id=r.provider_id,
        )
        for r in records
    ]


async def _find(svc: Services, user: UserRecord, public_id: str) -> SessionRecord:
    for record in await sessions_db.list_for_user(svc.db, user.id):
        if svc.sessions.public_id(record.id) == public_id:
            return record
    raise NotFound('session not found')


@router.delete('/auth/sessions/{session_id}', status_code=204, operation_id='revokeMySession', responses={404: PROBLEM})
@access.authenticated
async def revoke_session(session_id: SessionId, request: Request, svc: ServicesDep) -> Response:
    """End one of the caller's sessions (another user's is 404)."""
    user = await _own_user(request, svc)
    record = await _find(svc, user, session_id)
    await svc.sessions.destroy(record.id)
    current = current_session(request)
    is_current = current is not None and current.id == record.id
    audit.record(AuditAction.SESSION_REVOKE, target=_session_target(session_id), details={'current': is_current})
    out = Response(status_code=204)
    if is_current:
        svc.sessions.clear_cookies(out)
    return out


@router.delete('/auth/sessions', response_model=SessionsRevoked, operation_id='revokeMyOtherSessions')
@access.authenticated
async def revoke_other_sessions(request: Request, svc: ServicesDep) -> SessionsRevoked:
    """End every session of the caller except the one making this request."""
    user = await _own_user(request, svc)
    current = current_session(request)
    revoked = 0
    for record in await sessions_db.list_for_user(svc.db, user.id):
        if current is not None and record.id == current.id:
            continue
        await svc.sessions.destroy(record.id)
        revoked += 1
    if revoked:
        audit.record(
            AuditAction.SESSION_REVOKE, target=audit.target('user', user.id, user.username), details={'others': revoked}
        )
    return SessionsRevoked(revoked=revoked)


# ---------------------------------------------------------------------------- another user's sessions (user-admin)
async def _target_user(svc: Services, user_id: str) -> UserRecord:
    user = await users_db.get(svc.db, user_id)
    if user is None:
        raise NotFound('user not found')
    return user


@router.get(
    '/users/{user_id}/sessions',
    response_model=list[SessionInfo],
    operation_id='listUserSessions',
    tags=['admin'],
    responses={404: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def list_user_sessions(user_id: UserId, request: Request, svc: ServicesDep) -> list[SessionInfo]:
    """A user's sessions, newest first (``current`` marks the caller's own)."""
    return await _sessions_of(request, svc, await _target_user(svc, user_id))


@router.delete(
    '/users/{user_id}/sessions',
    response_model=SessionsRevoked,
    operation_id='revokeUserSessions',
    tags=['admin'],
    responses={403: PROBLEM, 404: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def revoke_user_sessions(user_id: UserId, request: Request, svc: ServicesDep) -> SessionsRevoked:
    """Sign a user out everywhere (API tokens are not affected). Not for a user holding more than the caller."""
    principal = await authenticate(request)
    user = await _target_user(svc, user_id)
    _, grants = await user_grants(svc, user)
    require_manageable(principal, grants, 'a user')
    revoked = await sessions_db.delete_for_user(svc.db, user.id)
    audit.record(
        AuditAction.SESSION_REVOKE,
        target=audit.target('user', user.id, user.username),
        details={'all': revoked, 'by_admin': True},
    )
    return SessionsRevoked(revoked=revoked)


# ---------------------------------------------------------------------------- password
@router.post(
    '/auth/me/password',
    status_code=204,
    operation_id='changeMyPassword',
    responses={403: PROBLEM, 409: PROBLEM, 422: PROBLEM, 429: PROBLEM},
)
@access.authenticated
@access.password_change_allowed
async def change_password(body: PasswordChange, request: Request, svc: ServicesDep) -> Response:
    principal = await authenticate(request)
    if principal.method is not AuthMethod.SESSION:
        raise Forbidden('sign in to change your password')
    user = await _own_user(request, svc)
    if user.source != 'local' or not user.password_hash:
        raise Conflict(f'the account signs in through {user.source}; its password is managed there')
    settings = svc.settings
    await ratelimit.check(svc.valkey, 'login', client_ip(request), limit=settings.login_rate_limit, window_s=WINDOW_S)
    target = audit.target('user', user.id, user.username)
    if not await passwords.verify_password(user.password_hash, body.current_password):
        audit.record(AuditAction.AUTH_PASSWORD_CHANGE, outcome=Outcome.FAILURE, reason='bad_credentials', target=target)
        raise FieldErrors([FieldError(field='current_password', message='not your current password', code='password')])
    problem = passwords.password_problem(body.new_password, username=user.username)
    if problem is None and body.new_password == body.current_password:
        problem = 'must differ from the current password'
    if problem:
        raise FieldErrors([FieldError(field='new_password', message=problem, code='password')])
    updated = await users_db.set_password(
        svc.db, user.id, await passwords.hash_password(body.new_password), must_change=False
    )
    if updated is None:
        raise Conflict('the account changed meanwhile; sign in again')
    current = current_session(request)
    ended = 0
    for record in await sessions_db.list_for_user(svc.db, user.id):
        if current is None or record.id != current.id:
            await svc.sessions.destroy(record.id)
            ended += 1
    audit.record(
        AuditAction.AUTH_PASSWORD_CHANGE,
        target=target,
        details={'by_admin': False, 'was_one_time': user.must_change_password, 'sessions_ended': ended},
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------- profile and permissions
@router.patch('/auth/me', response_model=User, operation_id='updateMyProfile', responses={409: PROBLEM, 422: PROBLEM})
@access.authenticated
async def update_profile(body: ProfileUpdate, request: Request, svc: ServicesDep) -> User:
    user = await _own_user(request, svc)
    changes: dict[str, object] = {}
    if body.display_name is not None:
        changes['display_name'] = body.display_name.strip() or user.username
    if 'email' in body.model_fields_set:
        changes['email'] = body.email
    if changes and user.source != 'local':
        raise Conflict(f'the profile comes from {user.source} and is updated at each sign-in')
    updated = await users_db.update(svc.db, user.id, changes) if changes else user
    if updated is None:
        raise NotFound('user not found')
    if changes:
        fields = {'display_name', 'email'}
        audit.record(
            AuditAction.USER_UPDATE,
            target=audit.target('user', user.id, user.username),
            details={'profile': True},
            changes=audit.diff(user.model_dump(include=fields), updated.model_dump(include=fields)),
        )
    return User(
        id=updated.id,
        username=updated.username,
        display_name=updated.display_name,
        email=updated.email,
        source=updated.source,
    )


@router.get('/auth/me/permissions', response_model=EffectivePermissions, operation_id='getMyPermissions')
@access.authenticated
async def my_permissions(request: Request, svc: ServicesDep) -> EffectivePermissions:
    principal = await authenticate(request)
    user = await users_db.get(svc.db, principal.id) if principal.kind is PrincipalKind.USER else None
    return await effective_permissions(svc, principal, user=user)
