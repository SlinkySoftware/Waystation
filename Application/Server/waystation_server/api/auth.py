"""``/api/v1/config`` and ``/api/v1/auth/*``: instance config, sign-in, sign-out, the current session.

OpenAPI contract: ``UiConfig``, ``AuthProvider``, ``LoginRequest``, ``Session``, ``Preferences``.
"""

from __future__ import annotations

from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from waystation_common.errors import Forbidden, Unauthorized
from waystation_server.api.deps import ServicesDep, client_ip
from waystation_server.db import users as users_db
from waystation_server.security import access
from waystation_server.security.authn import authenticate, current_session, principal_for
from waystation_server.security.identity import Preferences, SessionRecord, UserRecord
from waystation_server.security.login import verify_login
from waystation_server.security.principal import AuthMethod
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
    )


@config_router.get('/config', response_model=UiConfig, operation_id='getUiConfig')
@access.public
async def get_config(svc: ServicesDep) -> UiConfig:
    default = await svc.catalog.default_docker()
    return UiConfig(
        instance_name=svc.settings.instance_name,
        base_url=svc.base_url,
        version=VERSION,
        docker=DockerConfig(registry_host=registry_host(svc.base_url), default_repo=default.name if default else None),
    )


@router.get('/auth/providers', response_model=list[AuthProvider], operation_id='listAuthProviders')
@access.public
async def list_providers(svc: ServicesDep) -> list[AuthProvider]:
    """Local accounts, then the enabled external providers in sign-in order (LDAP shares the password form)."""
    external = [AuthProvider(id=p.id, kind=p.kind.value, name=p.name) for p in await svc.auth_providers.enabled()]
    return [AuthProvider(id='local', kind='local', name='Local account'), *external]


@router.post(
    '/auth/login',
    response_model=Session,
    operation_id='login',
    responses={401: {'description': 'Bad credentials'}, 429: {'description': 'Too many attempts'}},
)
@access.public
async def login(body: LoginRequest, request: Request, response: Response, svc: ServicesDep) -> Session:
    user = await verify_login(
        svc,
        username=body.username,
        password=body.password,
        client_ip=client_ip(request),
        provider=body.provider or None,
        interactive=True,
    )
    old = request.cookies.get('ws_session')
    if old:  # never keep a pre-login session (fixation)
        record = await svc.sessions.resolve(old)
        if record is not None:
            await svc.sessions.destroy(record.id)
    token, record = await svc.sessions.create(
        user.id, ip=client_ip(request), user_agent=request.headers.get('user-agent')
    )
    svc.sessions.set_cookies(response, token, record)
    return await _session_body(svc, user, record)


def _require_csrf(request: Request, record: SessionRecord) -> None:
    if not csrf_matches(record, request.headers.get(CSRF_HEADER)):
        raise Forbidden('CSRF token missing or invalid')


@router.post('/auth/logout', status_code=204, operation_id='logout')
@access.public  # CSRF checked in the handler when a session exists
async def logout(request: Request, svc: ServicesDep) -> Response:
    await authenticate(request)
    record = current_session(request)
    response = Response(status_code=204)
    if record is not None:
        _require_csrf(request, record)
        await svc.sessions.destroy(record.id)
    svc.sessions.clear_cookies(response)
    return response


async def _signed_in(request: Request, svc: Services) -> tuple[UserRecord, SessionRecord]:
    await authenticate(request)
    record = current_session(request)
    user: UserRecord | None = getattr(request.state, 'user', None)
    if record is None or user is None:
        raise Unauthorized('not signed in')
    return user, record


@router.get('/auth/me', response_model=Session, operation_id='getMe')
@access.authenticated
async def me(request: Request, svc: ServicesDep) -> Session:
    user, record = await _signed_in(request, svc)
    return await _session_body(svc, user, record)


@router.put('/auth/me/preferences', response_model=Preferences, operation_id='updatePreferences')
@access.authenticated
async def update_preferences(body: Preferences, request: Request, svc: ServicesDep) -> Preferences:
    user, _ = await _signed_in(request, svc)
    await users_db.set_preferences(svc.db, user.id, body)
    return body
