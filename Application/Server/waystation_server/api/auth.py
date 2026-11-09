"""``/api/v1/config`` and ``/api/v1/auth/*``: instance config, sign-in, sign-out, the current session.

OpenAPI contract: ``UiConfig``, ``AuthProvider``, ``LoginRequest``, ``Session``, ``Preferences``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from waystation_common.errors import Forbidden, Unauthorized
from waystation_server import audit
from waystation_server.api.deps import ServicesDep, client_ip
from waystation_server.audit import AuditAction, Outcome
from waystation_server.db import login_challenges as login_challenges_db
from waystation_server.db import users as users_db
from waystation_server.oidc_auth import login_url, logout_url
from waystation_server.security import access, ratelimit, second_factor
from waystation_server.security.authn import authenticate, current_session, principal_for, user_principal
from waystation_server.security.identity import Preferences, SessionRecord, UserRecord
from waystation_server.security.login import WINDOW_S as LOGIN_WINDOW_S
from waystation_server.security.login import count_failure, is_locked, verify_login
from waystation_server.security.principal import AuthMethod
from waystation_server.security.providers import ProviderKind
from waystation_server.security.sessions import CSRF_HEADER, csrf_matches
from waystation_server.services import Services

router = APIRouter(tags=['auth'])
config_router = APIRouter(tags=['config'])


def _version() -> str:
    try:
        return version('waystation-server')
    except PackageNotFoundError:  # pragma: no cover - source checkout without metadata
        return '0.0.0'


VERSION = _version()


class DockerConfig(BaseModel):
    registry_host: str = Field(description='Host (and port) docker clients use, e.g. `registry.example.com`')
    default_repo: str | None = Field(description='Repository receiving unprefixed names and mirror-mode pulls')


class UiConfig(BaseModel):
    instance_name: str
    base_url: str
    version: str
    docker: DockerConfig


class AuthProvider(BaseModel):
    id: str
    kind: Literal['local', 'ldap', 'oidc']
    name: str
    login_url: str | None = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024, json_schema_extra={'writeOnly': True})
    provider: str | None = Field(None, max_length=64)


class User(BaseModel):
    id: str
    username: str
    display_name: str
    email: str | None = None
    source: str


class Session(BaseModel):
    user: User
    csrf_token: str
    capabilities: list[str]
    preferences: Preferences
    expires_at: datetime
    must_change_password: bool = Field(
        description='The account holds a one-time password: change it (`POST /auth/me/password`) before anything else'
    )
    totp_setup_required: bool = Field(
        description='Two-factor sign-in is required for this account and not set up yet: enrol '
        '(`POST /auth/me/totp/setup`, then `/confirm`) before anything else'
    )


class SecondFactorRequired(BaseModel):
    second_factor: Literal['totp'] = Field(
        description='The password was accepted; send a code from the authenticator app (or a recovery code) to '
        '`POST /auth/login/totp`'
    )
    expires_at: datetime = Field(description='Until when the second step can be completed')


class SecondFactorLogin(BaseModel):
    model_config = ConfigDict(extra='forbid')

    code: str = Field(
        min_length=6,
        max_length=32,
        json_schema_extra={'writeOnly': True},
        description='The 6-digit code from the authenticator app, or a recovery code (`xxxxx-xxxxx`)',
    )


def registry_host(base_url: str) -> str:
    return urlsplit(base_url).netloc


async def _session_body(svc: Services, user: UserRecord, record: SessionRecord) -> Session:
    principal = await principal_for(svc, user, AuthMethod.SESSION)
    return Session(
        user=User(
            id=user.id, username=user.username, display_name=user.display_name, email=user.email, source=user.source
        ),
        csrf_token=record.csrf,
        capabilities=access.capabilities(principal),
        preferences=user.preferences,
        expires_at=record.expires_at,
        must_change_password=user.must_change_password,
        totp_setup_required=await second_factor.setup_required(svc, user, principal.grants),
    )


@config_router.get('/config', response_model=UiConfig, operation_id='getUiConfig')
@access.public
async def get_config(svc: ServicesDep) -> UiConfig:
    default = await svc.catalog.default_docker()
    return UiConfig(
        instance_name=(await svc.instance.general()).instance_name,
        base_url=svc.base_url,
        version=VERSION,
        docker=DockerConfig(registry_host=registry_host(svc.base_url), default_repo=default.name if default else None),
    )


@router.get('/auth/providers', response_model=list[AuthProvider], operation_id='listAuthProviders')
@access.public
async def list_providers(svc: ServicesDep) -> list[AuthProvider]:
    """Local accounts, then the enabled external providers in sign-in order (LDAP shares the password form; OIDC
    providers carry the URL the browser navigates to)."""
    external = [
        AuthProvider(
            id=p.id, kind=p.kind.value, name=p.name, login_url=login_url(p.id) if p.kind is ProviderKind.OIDC else None
        )
        for p in await svc.auth_providers.enabled()
    ]
    return [AuthProvider(id='local', kind='local', name='Local account'), *external]


async def _open_session(
    request: Request, response: Response, svc: Services, user: UserRecord, extra: dict[str, object] | None = None
) -> Session:
    old = request.cookies.get('ws_session')
    if old:  # never keep a pre-login session (fixation)
        record = await svc.sessions.resolve(old)
        if record is not None:
            await svc.sessions.destroy(record.id)
    token, record = await svc.sessions.create(
        user.id, ip=client_ip(request), user_agent=request.headers.get('user-agent')
    )
    svc.sessions.set_cookies(response, token, record)
    session = await _session_body(svc, user, record)
    audit.record(
        AuditAction.AUTH_LOGIN,
        actor=user_principal(user, AuthMethod.SESSION),
        target=audit.target('user', user.id, user.username),
        details={'username': user.username, 'via': 'ui', 'provider': user.source, **(extra or {})},
    )
    return session


@router.post(
    '/auth/login',
    response_model=Session,
    operation_id='login',
    responses={
        202: {'model': SecondFactorRequired, 'description': 'Password accepted; the second factor is needed'},
        401: {'description': 'Bad credentials'},
        429: {'description': 'Too many attempts'},
    },
)
@access.public
async def login(body: LoginRequest, request: Request, response: Response, svc: ServicesDep) -> Session | JSONResponse:
    user = await verify_login(
        svc,
        username=body.username,
        password=body.password,
        client_ip=client_ip(request),
        provider=body.provider or None,
        interactive=True,
    )
    if second_factor.enrolled(user):
        challenge = await second_factor.start(svc, user, ip=client_ip(request))
        pending = JSONResponse(
            SecondFactorRequired(second_factor='totp', expires_at=challenge.expires_at).model_dump(mode='json'),
            status_code=202,
        )
        pending.set_cookie(
            second_factor.CHALLENGE_COOKIE,
            challenge.token,
            max_age=second_factor.CHALLENGE_TTL_S,
            path=second_factor.CHALLENGE_PATH,
            secure=svc.sessions.secure,
            httponly=True,
            samesite='strict',
        )
        return pending
    return await _open_session(request, response, svc, user)


class SecondFactorExpired(Unauthorized):
    code = 'second_factor_expired'


SECOND_FACTOR_FAILED = 'invalid code'


@router.post(
    '/auth/login/totp',
    response_model=Session,
    operation_id='loginSecondFactor',
    responses={
        401: {'description': 'Wrong code, or the pending sign-in expired (`code` `second_factor_expired`)'},
        429: {'description': 'Too many attempts'},
    },
)
@access.public  # the pending sign-in (HttpOnly, SameSite=Strict cookie) is checked here
async def login_second_factor(
    body: SecondFactorLogin, request: Request, response: Response, svc: ServicesDep
) -> Session:
    """Second sign-in step for accounts with two-factor sign-in: a TOTP code (each one works once) or a recovery
    code (single use). Wrong codes count towards the account lockout; after a few the pending sign-in ends."""
    ip = client_ip(request)
    await ratelimit.check(svc.valkey, 'login', ip, limit=svc.settings.login_rate_limit, window_s=LOGIN_WINDOW_S)
    challenge = await second_factor.pending(svc, request.cookies.get(second_factor.CHALLENGE_COOKIE))
    if challenge is None:
        raise SecondFactorExpired('the sign-in expired; enter your password again')
    user = await users_db.get(svc.db, challenge.user_id)
    now = datetime.now(UTC)
    if user is None or user.disabled or is_locked(user, now) or not second_factor.enrolled(user):
        await login_challenges_db.take(svc.db, challenge.id)
        reason = 'disabled' if user is not None and user.disabled else 'locked' if user is not None else 'unknown_user'
        _second_factor_failed(user, reason)
        raise SecondFactorExpired('the sign-in expired; enter your password again')
    method = await second_factor.verify(svc, user, body.code)
    if method is None:
        await count_failure(svc, user, 'second_factor')
        attempts = await login_challenges_db.count_attempt(svc.db, challenge.id)
        if attempts >= second_factor.MAX_ATTEMPTS:
            await login_challenges_db.take(svc.db, challenge.id)
        _second_factor_failed(user, 'bad_second_factor')
        raise Unauthorized(SECOND_FACTOR_FAILED)
    if not await login_challenges_db.take(svc.db, challenge.id):  # a concurrent request completed it
        raise SecondFactorExpired('the sign-in expired; enter your password again')
    await users_db.record_success(svc.db, user.id, now)
    out = await _open_session(request, response, svc, user, {'second_factor': method})
    response.delete_cookie(
        second_factor.CHALLENGE_COOKIE, path=second_factor.CHALLENGE_PATH, secure=svc.sessions.secure, samesite='strict'
    )
    return out


def _second_factor_failed(user: UserRecord | None, reason: str) -> None:
    audit.record(
        AuditAction.AUTH_LOGIN,
        outcome=Outcome.FAILURE,
        reason=reason,
        target=audit.target('user', user.id if user else None, user.username if user else None),
        details={'username': user.username if user else None, 'via': 'ui', 'step': 'second_factor'},
    )


def _require_csrf(request: Request, record: SessionRecord) -> None:
    if not csrf_matches(record, request.headers.get(CSRF_HEADER)):
        raise Forbidden('CSRF token missing or invalid')


class LogoutResult(BaseModel):
    end_session_url: str | None = Field(
        description="The OIDC provider's sign-out page (RP-initiated logout) to send the browser to, if any"
    )


@router.post('/auth/logout', response_model=LogoutResult, operation_id='logout')
@access.public  # CSRF checked in the handler when a session exists
async def logout(request: Request, response: Response, svc: ServicesDep) -> LogoutResult:
    principal = await authenticate(request)
    record = current_session(request)
    end_session: str | None = None
    if record is not None:
        _require_csrf(request, record)
        await svc.sessions.destroy(record.id)
        audit.record(AuditAction.AUTH_LOGOUT, target=audit.target('user', record.user_id, principal.name))
        end_session = await logout_url(svc, record.provider_id, record.id_token_enc)
    svc.sessions.clear_cookies(response)
    return LogoutResult(end_session_url=end_session)


async def _signed_in(request: Request, svc: Services) -> tuple[UserRecord, SessionRecord]:
    await authenticate(request)
    record = current_session(request)
    user: UserRecord | None = getattr(request.state, 'user', None)
    if record is None or user is None:
        raise Unauthorized('not signed in')
    return user, record


@router.get('/auth/me', response_model=Session, operation_id='getMe')
@access.authenticated
@access.password_change_allowed
@access.totp_setup_allowed
async def me(request: Request, svc: ServicesDep) -> Session:
    user, record = await _signed_in(request, svc)
    return await _session_body(svc, user, record)


@router.put('/auth/me/preferences', response_model=Preferences, operation_id='updatePreferences')
@access.authenticated
async def update_preferences(body: Preferences, request: Request, svc: ServicesDep) -> Preferences:
    user, _ = await _signed_in(request, svc)
    await users_db.set_preferences(svc.db, user.id, body)
    return body
