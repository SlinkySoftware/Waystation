"""Two-factor sign-in (TOTP) for local accounts (SPECIFICATION §4.9): the caller's own enrolment and an
administrator's reset.

- ``GET /auth/me/totp``: whether it is available (local account with a password), enabled, required by policy, and
  how many recovery codes are left.
- ``POST /auth/me/totp/setup`` (current password): a new secret, kept pending (encrypted) for ``ENROL_TTL_S``; the
  answer carries it once, as base32 and as an ``otpauth://`` URI the UI turns into a QR code locally.
- ``POST /auth/me/totp/confirm`` (a code from the app): turns it on and returns 10 recovery codes, once.
- ``POST /auth/me/totp/recovery-codes`` (a TOTP code): replaces the recovery codes, returned once.
- ``POST /auth/me/totp/disable`` (current password and a TOTP or recovery code): turns it off.
- ``DELETE /users/{id}/totp`` (``user-admin``, not for an account holding more than the caller, not one's own):
  removes a user's second factor (lost device); the user signs in with the password and can enrol again.

All of them need an interactive session (not an API token), are rate limited like sign-in and audited
(``auth.totp_enable``, ``auth.totp_disable``, ``auth.totp_recovery_codes``); codes and secrets never are.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from waystation_common.errors import Conflict, Forbidden, NotFound, Unauthorized
from waystation_server import audit
from waystation_server.api.deps import ServicesDep, client_ip
from waystation_server.api.rbac import PROBLEM, require_manageable, user_grants
from waystation_server.api.users import UserId
from waystation_server.audit import AuditAction, Outcome
from waystation_server.db import login_challenges as login_challenges_db
from waystation_server.db import users as users_db
from waystation_server.problems import FieldError, FieldErrors
from waystation_server.security import access, passwords, ratelimit, second_factor, totp
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate
from waystation_server.security.identity import UserRecord
from waystation_server.security.login import WINDOW_S, count_failure
from waystation_server.security.principal import AuthMethod, PrincipalKind
from waystation_server.services import Services

router = APIRouter(tags=['auth'])


class TotpStatus(BaseModel):
    available: bool = Field(description='The account can use two-factor sign-in (a local account with a password)')
    enabled: bool
    enabled_at: datetime | None
    required: bool = Field(description='Policy requires it for this account and it is not set up yet')
    recovery_codes_left: int


class TotpSetupRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    password: str = Field(min_length=1, max_length=passwords.MAX_LENGTH, json_schema_extra={'writeOnly': True})


class TotpSetup(BaseModel):
    secret: str = Field(description='The shared secret (base32), for typing into an app by hand. Shown once.')
    otpauth_uri: str = Field(description='`otpauth://totp/...` for a QR code (render it locally, never via a service)')
    issuer: str
    account: str
    expires_at: datetime = Field(description='Confirm with a code before this, or start again')


class TotpCode(BaseModel):
    model_config = ConfigDict(extra='forbid')

    code: str = Field(min_length=6, max_length=32, json_schema_extra={'writeOnly': True})


class TotpDisable(BaseModel):
    model_config = ConfigDict(extra='forbid')

    password: str = Field(min_length=1, max_length=passwords.MAX_LENGTH, json_schema_extra={'writeOnly': True})
    code: str = Field(
        min_length=6,
        max_length=32,
        json_schema_extra={'writeOnly': True},
        description='A code from the app or a recovery code',
    )


class TotpRecoveryCodes(BaseModel):
    recovery_codes: list[str] = Field(description='Single-use codes for when the app is not at hand. Shown once.')


# ---------------------------------------------------------------------------- helpers
async def _me(request: Request, svc: Services, *, interactive: bool = True) -> UserRecord:
    principal = await authenticate(request)
    if principal.kind is not PrincipalKind.USER:
        raise Forbidden('only user accounts have two-factor sign-in')
    if interactive and principal.method is not AuthMethod.SESSION:
        raise Forbidden('sign in to change two-factor sign-in')
    user: UserRecord | None = getattr(request.state, 'user', None) or await users_db.get(svc.db, principal.id)
    if user is None:
        raise Unauthorized('not signed in')
    return user


def _local(user: UserRecord) -> None:
    if not second_factor.can_enrol(user):
        raise Conflict(f'the account signs in through {user.source}; two-factor sign-in is managed there')


async def _limit(request: Request, svc: Services) -> None:
    settings = svc.settings
    await ratelimit.check(svc.valkey, 'login', client_ip(request), limit=settings.login_rate_limit, window_s=WINDOW_S)


async def _check_password(user: UserRecord, password: str, action: AuditAction) -> None:
    if not await passwords.verify_password(user.password_hash, password):
        audit.record(action, outcome=Outcome.FAILURE, reason='bad_credentials', target=_target(user))
        raise FieldErrors([FieldError(field='password', message='not your current password', code='password')])


def _bad_code(field: str = 'code') -> FieldErrors:
    return FieldErrors([FieldError(field=field, message='the code is not valid (or was already used)', code='code')])


def _target(user: UserRecord) -> audit.AuditTarget:
    return audit.target('user', user.id, user.username)


def _issuer(name: str) -> str:
    return (name.replace(':', ' ').strip() or 'Waystation')[: second_factor.ISSUER_MAX]


# ---------------------------------------------------------------------------- own account
@router.get('/auth/me/totp', response_model=TotpStatus, operation_id='getMyTotp')
@access.authenticated
@access.totp_setup_allowed
async def my_totp(request: Request, svc: ServicesDep) -> TotpStatus:
    user = await _me(request, svc, interactive=False)
    return TotpStatus(
        available=second_factor.can_enrol(user),
        enabled=second_factor.enrolled(user),
        enabled_at=user.totp_enabled_at if second_factor.enrolled(user) else None,
        required=await second_factor.setup_required(svc, user),
        recovery_codes_left=len(user.totp_recovery) if second_factor.enrolled(user) else 0,
    )


@router.post(
    '/auth/me/totp/setup',
    response_model=TotpSetup,
    operation_id='startMyTotpSetup',
    responses={403: PROBLEM, 409: PROBLEM, 422: PROBLEM, 429: PROBLEM},
)
@access.authenticated
@access.totp_setup_allowed
async def start_setup(body: TotpSetupRequest, request: Request, svc: ServicesDep) -> TotpSetup:
    """Start enrolment: a fresh secret, pending until confirmed with a code (starting again replaces it)."""
    user = await _me(request, svc)
    _local(user)
    if second_factor.enrolled(user):
        raise Conflict('two-factor sign-in is already on; turn it off first to move it to another device')
    await _limit(request, svc)
    await _check_password(user, body.password, AuditAction.AUTH_TOTP_ENABLE)
    secret = totp.new_secret()
    expires_at = datetime.now(UTC) + timedelta(seconds=second_factor.ENROL_TTL_S)
    if not await users_db.set_totp_pending(
        svc.db, user.id, second_factor.encrypt_secret(svc, user.id, secret), expires_at
    ):
        raise Conflict('the account changed meanwhile; reload and try again')
    issuer = _issuer((await svc.instance.general()).instance_name)
    return TotpSetup(
        secret=secret,
        otpauth_uri=totp.otpauth_uri(secret, account=user.username, issuer=issuer),
        issuer=issuer,
        account=user.username,
        expires_at=expires_at,
    )


@router.post(
    '/auth/me/totp/confirm',
    response_model=TotpRecoveryCodes,
    operation_id='confirmMyTotp',
    responses={403: PROBLEM, 409: PROBLEM, 422: PROBLEM, 429: PROBLEM},
)
@access.authenticated
@access.totp_setup_allowed
async def confirm_setup(body: TotpCode, request: Request, svc: ServicesDep) -> TotpRecoveryCodes:
    """Turn two-factor sign-in on with a code from the app; returns the recovery codes (once)."""
    user = await _me(request, svc)
    _local(user)
    if second_factor.enrolled(user):
        raise Conflict('two-factor sign-in is already on')
    await _limit(request, svc)
    now = datetime.now(UTC)
    pending_enc, expires = user.totp_pending_enc, user.totp_pending_expires_at
    if pending_enc is None or expires is None or expires <= now:
        raise Conflict('set-up expired or was not started; start again')
    step = second_factor.check_code(second_factor.decrypt_secret(svc, user.id, pending_enc), body.code)
    if step is None:
        raise _bad_code()
    codes, hashes = second_factor.new_recovery(svc, user.id)
    updated = await users_db.enable_totp(svc.db, user.id, pending_enc=pending_enc, recovery=hashes, step=step, now=now)
    if updated is None:
        raise Conflict('the account changed meanwhile; start again')
    audit.record(AuditAction.AUTH_TOTP_ENABLE, target=_target(user), details={'recovery_codes': len(codes)})
    return TotpRecoveryCodes(recovery_codes=codes)


@router.post(
    '/auth/me/totp/recovery-codes',
    response_model=TotpRecoveryCodes,
    operation_id='regenerateMyRecoveryCodes',
    responses={403: PROBLEM, 409: PROBLEM, 422: PROBLEM, 429: PROBLEM},
)
@access.authenticated
async def regenerate_recovery_codes(body: TotpCode, request: Request, svc: ServicesDep) -> TotpRecoveryCodes:
    """New recovery codes (the old ones stop working); needs a code from the app."""
    user = await _me(request, svc)
    if not second_factor.enrolled(user):
        raise Conflict('two-factor sign-in is off')
    await _limit(request, svc)
    if await second_factor.verify(svc, user, body.code, allow_recovery=False) is None:
        await count_failure(svc, user, 'second_factor')
        audit.record(
            AuditAction.AUTH_TOTP_RECOVERY_CODES,
            outcome=Outcome.FAILURE,
            reason='bad_second_factor',
            target=_target(user),
        )
        raise _bad_code()
    codes, hashes = second_factor.new_recovery(svc, user.id)
    if not await users_db.set_recovery_codes(svc.db, user.id, hashes):
        raise Conflict('two-factor sign-in is off')
    audit.record(AuditAction.AUTH_TOTP_RECOVERY_CODES, target=_target(user), details={'recovery_codes': len(codes)})
    return TotpRecoveryCodes(recovery_codes=codes)


@router.post(
    '/auth/me/totp/disable',
    status_code=204,
    operation_id='disableMyTotp',
    responses={403: PROBLEM, 409: PROBLEM, 422: PROBLEM, 429: PROBLEM},
)
@access.authenticated
async def disable(body: TotpDisable, request: Request, svc: ServicesDep) -> Response:
    """Turn two-factor sign-in off (current password and a code from the app or a recovery code)."""
    user = await _me(request, svc)
    if not second_factor.enrolled(user):
        raise Conflict('two-factor sign-in is off')
    await _limit(request, svc)
    await _check_password(user, body.password, AuditAction.AUTH_TOTP_DISABLE)
    method = await second_factor.verify(svc, user, body.code)
    if method is None:
        await count_failure(svc, user, 'second_factor')
        audit.record(
            AuditAction.AUTH_TOTP_DISABLE, outcome=Outcome.FAILURE, reason='bad_second_factor', target=_target(user)
        )
        raise _bad_code()
    await users_db.clear_totp(svc.db, user.id)
    audit.record(AuditAction.AUTH_TOTP_DISABLE, target=_target(user), details={'by_admin': False, 'with': method})
    return Response(status_code=204)


# ---------------------------------------------------------------------------- administration
@router.delete(
    '/users/{user_id}/totp',
    status_code=204,
    operation_id='resetUserTotp',
    tags=['admin'],
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def reset_user_totp(user_id: UserId, request: Request, svc: ServicesDep) -> Response:
    """Remove a user's second factor (a lost device). Not one's own (turn it off on the profile page, which needs
    the password and a code) and not for an account holding more than the caller."""
    principal = await authenticate(request)
    user = await users_db.get(svc.db, user_id)
    if user is None:
        raise NotFound('user not found')
    _, grants = await user_grants(svc, user)
    require_manageable(principal, grants, 'a user')
    if user.id == principal.id:
        raise Conflict('turn off your own two-factor sign-in on your profile page')
    if not await users_db.clear_totp(svc.db, user.id):
        raise Conflict('two-factor sign-in is not on for this user')
    await login_challenges_db.delete_for_user(svc.db, user.id)
    audit.record(AuditAction.AUTH_TOTP_DISABLE, target=_target(user), details={'by_admin': True})
    return Response(status_code=204)
