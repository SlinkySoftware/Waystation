"""OpenID Connect sign-in, browser side (T-A02; SPECIFICATION §4.9). Not REST API: the browser navigates here.

- ``GET /auth/oidc/{provider}/login?next=<SPA path>``: discovery (cached), then a redirect to the provider's
  authorization endpoint with a fresh ``state``, ``nonce`` and PKCE ``S256`` challenge. The flow record (provider,
  nonce, code verifier, where to go next, a hash of the browser binding) is kept in Valkey under
  ``oidc:state:<sha256(state)>`` for ``STATE_TTL_S`` and can be used once. The browser binding is a random value in
  the HttpOnly cookie ``ws_oidc`` (``SameSite=Lax``, path ``/auth/oidc/``): a callback is accepted only from the
  browser that started the flow (no login CSRF). Starts are rate limited per client address like password sign-ins.
- ``GET /auth/oidc/{provider}/callback``: one redirect URI per provider (mix-up defence), checked against the flow
  record; an ``iss`` parameter (RFC 9207) must name the provider's issuer. The code is exchanged at the token
  endpoint (client authentication + ``code_verifier``), the ID token is verified (``security.oidc``), claims become an
  ``ExternalIdentity`` and ``security.external.link_identity`` decides the Waystation user (``provision`` = the
  provider's auto-provision toggle; never a user with a local password). Success creates a new session (the old
  one is dropped) and redirects into the SPA; every failure redirects to the sign-in page with
  ``?error=oidc_<reason>`` (``failed``, ``cancelled``, ``expired``, ``unavailable``, ``too_many``) and the details
  go to the log only.

The session records the provider and, when RP-initiated logout is on and secret keys are set, the ID token encrypted
(``id_token_hint`` for ``POST /api/v1/auth/logout``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from starlette.responses import RedirectResponse, Response

from waystation_common.errors import CryptoError, RateLimited
from waystation_server.api.deps import ServicesDep
from waystation_server.security import access, ratelimit
from waystation_server.security.authn import client_address
from waystation_server.security.external import LinkRefused, link_identity
from waystation_server.security.oidc import FlowSecrets, OidcError, authorization_url, end_session_url
from waystation_server.security.providers import ProviderKind, ProviderRecord, decrypt_client_secret
from waystation_server.security.sessions import SESSION_COOKIE
from waystation_server.services import Services

log = logging.getLogger('waystation.auth.oidc')

router = APIRouter(include_in_schema=False)

STATE_TTL_S = 600
STATE_PREFIX = 'oidc:state:'
BINDING_COOKIE = 'ws_oidc'
COOKIE_PATH = '/auth/oidc/'
SPA_PREFIX = '/ui'
SIGN_IN = f'{SPA_PREFIX}/sign-in'
ID_TOKEN_AAD = b'session-oidc-id-token'
WINDOW_S = 300
_BINDING = re.compile(r'^[A-Za-z0-9_-]{43}$')
_UNAVAILABLE = frozenset({'unreachable', 'timeout', 'blocked', 'tls', 'too_large', 'discovery', 'bad_response'})


def callback_uri(base_url: str, provider_id: str) -> str:
    return f'{base_url.rstrip("/")}/auth/oidc/{provider_id}/callback'


def post_logout_uri(base_url: str) -> str:
    return f'{base_url.rstrip("/")}{SIGN_IN}'


def login_url(provider_id: str) -> str:
    return f'/auth/oidc/{provider_id}/login'


def safe_next(value: str | None) -> str:
    """An SPA path to return to: absolute path, same origin, printable, bounded; else ``/``."""
    if not value or len(value) > 1024 or not value.startswith('/') or value.startswith('//'):
        return '/'
    if '\\' in value or not value.isprintable():
        return '/'
    return value


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fail(reason: str) -> RedirectResponse:
    response = RedirectResponse(f'{SIGN_IN}?error=oidc_{reason}', status_code=303)
    response.headers['Cache-Control'] = 'no-store'
    return response


async def oidc_provider(svc: Services, provider_id: str) -> ProviderRecord | None:
    record = await svc.auth_providers.get(provider_id)
    if record is None or not record.enabled or record.kind is not ProviderKind.OIDC or record.oidc is None:
        return None
    return record


@router.get('/auth/oidc/{provider_id}/login')
@access.public
async def oidc_login(
    provider_id: str, request: Request, svc: ServicesDep, next: Annotated[str | None, Query(max_length=1024)] = None
) -> Response:
    record = await oidc_provider(svc, provider_id)
    if record is None or record.oidc is None:
        return _fail('failed')
    cfg = record.oidc
    try:
        limit = svc.settings.login_rate_limit
        await ratelimit.check(svc.valkey, 'oidc', client_address(request), limit=limit, window_s=WINDOW_S)
    except RateLimited:
        return _fail('too_many')
    try:
        meta = await svc.oidc.metadata(cfg)
    except OidcError as exc:
        log.warning('OIDC discovery failed', extra={'provider': record.id, 'reason': exc.reason, 'error': exc.message})
        return _fail('unavailable')
    flow = FlowSecrets.new()
    binding = request.cookies.get(BINDING_COOKIE) or ''
    if not _BINDING.match(binding):
        binding = secrets.token_urlsafe(32)
    flow_doc = {'p': record.id, 'n': flow.nonce, 'v': flow.verifier, 'next': safe_next(next), 'b': _sha(binding)}
    try:
        await svc.valkey.set(STATE_PREFIX + _sha(flow.state), json.dumps(flow_doc), ex=STATE_TTL_S)
    except Exception as exc:
        log.warning('OIDC state not stored', extra={'provider': record.id, 'error': type(exc).__name__})
        return _fail('unavailable')
    target = authorization_url(meta, cfg, flow, redirect_uri=callback_uri(svc.base_url, record.id))
    response = RedirectResponse(target, status_code=302)
    response.headers['Cache-Control'] = 'no-store'
    response.set_cookie(
        BINDING_COOKIE,
        binding,
        max_age=STATE_TTL_S,
        path=COOKIE_PATH,
        secure=svc.sessions.secure,
        httponly=True,
        samesite='lax',
    )
    return response


async def _take_flow(svc: Services, state: str) -> dict[str, Any] | None:
    raw = await svc.valkey.getdel(STATE_PREFIX + _sha(state))
    if raw is None:
        return None
    doc = json.loads(raw)
    return doc if isinstance(doc, dict) else None


@router.get('/auth/oidc/{provider_id}/callback')
@access.public
async def oidc_callback(provider_id: str, request: Request, svc: ServicesDep) -> Response:
    query = request.query_params
    state = query.get('state') or ''
    if not state or len(state) > 256:
        return _fail('failed')
    try:
        flow = await _take_flow(svc, state)
    except Exception as exc:
        log.warning('OIDC state lookup failed', extra={'provider': provider_id, 'error': type(exc).__name__})
        return _fail('unavailable')
    if flow is None:
        log.info('OIDC sign-in failed', extra={'provider': provider_id, 'reason': 'unknown_state'})
        return _fail('expired')
    binding = request.cookies.get(BINDING_COOKIE) or ''
    if flow.get('p') != provider_id or not hmac.compare_digest(_sha(binding), str(flow.get('b', ''))):
        log.warning('OIDC sign-in refused', extra={'provider': provider_id, 'reason': 'state_binding'})
        return _fail('failed')
    if query.get('error'):
        error = query.get('error', '')[:64]
        log.info('OIDC sign-in failed', extra={'provider': provider_id, 'reason': 'provider_error', 'error': error})
        return _fail('cancelled' if error == 'access_denied' else 'failed')
    record = await oidc_provider(svc, provider_id)
    code = query.get('code') or ''
    if record is None or record.oidc is None or not code or len(code) > 4096:
        log.info('OIDC sign-in failed', extra={'provider': provider_id, 'reason': 'no_code_or_provider'})
        return _fail('failed')
    cfg = record.oidc
    issuer = query.get('iss')
    if issuer is not None and issuer != cfg.issuer:  # RFC 9207 authorization server issuer identification
        log.warning('OIDC sign-in refused', extra={'provider': provider_id, 'reason': 'iss_parameter'})
        return _fail('failed')
    try:
        secret = decrypt_client_secret(svc.keyring, record)
    except CryptoError:
        log.error('OIDC client secret cannot be decrypted', extra={'provider': provider_id})
        return _fail('unavailable')
    try:
        result = await svc.oidc.sign_in(
            record.source,
            cfg,
            secret,
            code=code,
            redirect_uri=callback_uri(svc.base_url, record.id),
            flow_nonce=str(flow.get('n', '')),
            verifier=str(flow.get('v', '')),
        )
    except OidcError as exc:
        log.warning('OIDC sign-in failed', extra={'provider': provider_id, 'reason': exc.reason, 'error': exc.message})
        return _fail('unavailable' if exc.reason in _UNAVAILABLE else 'failed')
    try:
        user = await link_identity(svc.db, result.identity, provision=cfg.auto_provision)
    except LinkRefused as exc:
        log.info('OIDC sign-in refused', extra={'provider': provider_id, 'reason': exc.reason})
        return _fail('failed')
    old = request.cookies.get(SESSION_COOKIE)
    if old:  # never keep a pre-login session (fixation)
        previous = await svc.sessions.resolve(old)
        if previous is not None:
            await svc.sessions.destroy(previous.id)
    id_token_enc = None
    if cfg.rp_logout and svc.keyring is not None:
        id_token_enc = svc.keyring.encrypt_str(result.id_token, aad=ID_TOKEN_AAD)
    token, session = await svc.sessions.create(
        user.id,
        ip=client_address(request),
        user_agent=request.headers.get('user-agent'),
        provider_id=record.id,
        id_token_enc=id_token_enc,
    )
    log.info('signed in', extra={'user_id': user.id, 'provider': record.id})
    response = RedirectResponse(SPA_PREFIX + safe_next(str(flow.get('next', '/'))), status_code=303)
    response.headers['Cache-Control'] = 'no-store'
    svc.sessions.set_cookies(response, token, session)
    response.delete_cookie(BINDING_COOKIE, path=COOKIE_PATH, secure=svc.sessions.secure, httponly=True, samesite='lax')
    return response


async def logout_url(svc: Services, provider_id: str | None, id_token_enc: str | None) -> str | None:
    """Where the browser goes after signing out of a session that came from an OIDC provider (RP-initiated logout),
    or None (local sign-out only: no provider, logout off, or the provider offers no end-session endpoint)."""
    if not provider_id:
        return None
    record = await svc.auth_providers.get(provider_id)
    if record is None or record.oidc is None or not record.oidc.rp_logout:
        return None
    try:
        meta = await svc.oidc.metadata(record.oidc)
    except OidcError as exc:
        log.warning('OIDC logout skipped', extra={'provider': provider_id, 'reason': exc.reason})
        return None
    hint = None
    if id_token_enc and svc.keyring is not None:
        try:
            hint = svc.keyring.decrypt_str(id_token_enc, aad=ID_TOKEN_AAD)
        except CryptoError:
            hint = None
    return end_session_url(
        meta, record.oidc, id_token_hint=hint, post_logout_redirect_uri=post_logout_uri(svc.base_url)
    )
