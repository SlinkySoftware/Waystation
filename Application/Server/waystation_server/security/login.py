"""Username/password verification (SPECIFICATION §4.9), shared by the UI sign-in and the Docker token service
(``docker login``): local accounts and LDAP providers.

- Rate limited per client address (``WAYSTATION_LOGIN_RATE_LIMIT`` attempts per 5 minutes, then 429).
- Local accounts (``source`` ``local`` with a password) are checked with argon2id only, whatever provider the client
  picked: a local password is never bypassed by a directory account of the same name (identity linking rule).
  Lockout: ``LOGIN_LOCKOUT_FAILURES`` consecutive failures lock a local account for ``LOGIN_LOCKOUT_MINUTES``.
- Everyone else goes to the enabled LDAP providers in ``order`` (or only the one the client picked): the user's own
  provider first, then the others; the first directory that knows the username decides. A success links and syncs
  the Waystation user (``security.external``). Directories enforce their own lockout.
- Failures are indistinguishable: unknown user, wrong password, disabled, locked, not in the required group or
  directory unavailable, or a one-time password used outside the UI (``docker login``), or the password of an
  account with two-factor sign-in (or one that must enrol first) used outside the UI all raise the same
  ``Unauthorized`` (the reason is logged and goes to the audit record
  ``auth.login`` / failure; the attempt that locks an account also records ``auth.lockout``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from waystation_common.errors import Unauthorized
from waystation_server import audit
from waystation_server.audit import AuditAction, Outcome
from waystation_server.db import users as users_db
from waystation_server.security import passwords, ratelimit, second_factor
from waystation_server.security.external import LinkRefused, link_identity
from waystation_server.security.identity import UserRecord
from waystation_server.security.ldap import LdapDirectory
from waystation_server.security.providers import ProviderRecord, decrypt_bind_password

if TYPE_CHECKING:
    from waystation_server.services import Services

log = logging.getLogger('waystation.auth')

WINDOW_S = 300
GENERIC = 'invalid username or password'
LOCAL = 'local'


_LDAP_REASONS = {'bad_password': 'bad_credentials'}


class LoginFailed(Unauthorized):
    """The generic sign-in failure, carrying the real reason for the audit record (never for the client)."""

    def __init__(self, reason: str, user: UserRecord | None = None, provider: str | None = None) -> None:
        super().__init__(GENERIC)
        self.reason = reason
        self.user = user
        self.provider = provider


async def verify_login(
    svc: Services,
    *,
    username: str,
    password: str,
    client_ip: str,
    provider: str | None = None,
    interactive: bool = False,
) -> UserRecord:
    """The user these credentials belong to, or ``Unauthorized``. ``interactive`` (UI sign-in) skips the LDAP
    bind cache; format clients and ``docker login`` use it."""
    settings = svc.settings
    await ratelimit.check(svc.valkey, 'login', client_ip, limit=settings.login_rate_limit, window_s=WINDOW_S)
    name = username.strip()
    try:
        return await _verify(svc, name, password, provider=provider, interactive=interactive)
    except LoginFailed as exc:
        user = exc.user
        audit.record(
            AuditAction.AUTH_LOGIN,
            outcome=Outcome.FAILURE,
            reason=exc.reason,
            target=audit.target('user', user.id if user else None, user.username if user else name[:256]),
            details={'username': name[:256], 'via': 'ui' if interactive else 'docker', 'provider': exc.provider},
        )
        raise


async def _verify(svc: Services, name: str, password: str, *, provider: str | None, interactive: bool) -> UserRecord:
    user = await users_db.get_by_username(svc.db, name)
    if user is not None and user.source == LOCAL and user.password_hash:
        return await _verify_local(svc, user, password, interactive=interactive)
    candidates = [] if provider == LOCAL else await _ldap_candidates(svc, user, provider)
    if not candidates:
        await passwords.verify_password(None, password)  # same work as a wrong password
        log.info('sign-in failed', extra={'reason': 'unknown_user'})
        raise LoginFailed('unknown_user', user)
    if user is not None and user.disabled:
        log.info('sign-in refused', extra={'user_id': user.id, 'reason': 'disabled'})
        raise LoginFailed('disabled', user)
    return await _verify_ldap(svc, candidates, name, password, interactive=interactive)


async def count_failure(svc: Services, user: UserRecord, what: str) -> None:
    """Count a failed sign-in step (wrong password or wrong second factor) towards the account lockout."""
    settings = svc.settings
    locked_until = datetime.now(UTC) + timedelta(minutes=settings.login_lockout_minutes)
    failures = await users_db.record_failure(
        svc.db, user.id, threshold=settings.login_lockout_failures, locked_until=locked_until
    )
    log.info('sign-in failed', extra={'user_id': user.id, 'reason': what, 'failures': failures})
    if failures >= settings.login_lockout_failures:
        audit.record(
            AuditAction.AUTH_LOCKOUT,
            outcome=Outcome.FAILURE,
            reason='too_many_failures',
            target=audit.target('user', user.id, user.username),
            details={'failures': failures, 'locked_until': locked_until},
        )


def is_locked(user: UserRecord, now: datetime) -> bool:
    return user.locked_until is not None and user.locked_until > now


async def _verify_local(svc: Services, user: UserRecord, password: str, *, interactive: bool) -> UserRecord:
    ok = await passwords.verify_password(user.password_hash, password)
    now = datetime.now(UTC)
    if user.disabled or is_locked(user, now):
        reason = 'disabled' if user.disabled else 'locked'
        log.info('sign-in refused', extra={'user_id': user.id, 'reason': reason})
        raise LoginFailed(reason, user, LOCAL)
    if not ok:
        await count_failure(svc, user, 'password')
        raise LoginFailed('bad_credentials', user, LOCAL)
    if user.must_change_password and not interactive:
        # A one-time password only opens the UI, where it must be changed first (SPECIFICATION §4.9).
        log.info('sign-in refused', extra={'user_id': user.id, 'reason': 'password_change_required'})
        raise LoginFailed('password_change_required', user, LOCAL)
    if second_factor.enrolled(user):
        if not interactive:
            # The password alone is not enough: package clients use API tokens (SPECIFICATION §4.9).
            log.info('sign-in refused', extra={'user_id': user.id, 'reason': 'totp_required'})
            raise LoginFailed('totp_required', user, LOCAL)
        # Half way: success (and the reset of the failure count) is recorded after the second factor, so a known
        # password does not buy unlimited code guesses.
        log.info('password accepted, second factor pending', extra={'user_id': user.id})
        return user
    if not interactive and await second_factor.setup_required(svc, user):
        log.info('sign-in refused', extra={'user_id': user.id, 'reason': 'totp_setup_required'})
        raise LoginFailed('totp_setup_required', user, LOCAL)
    rehash = None
    if user.password_hash and passwords.needs_rehash(user.password_hash):
        rehash = await passwords.hash_password(password)
    await users_db.record_success(svc.db, user.id, now, password_hash=rehash)
    log.info('signed in', extra={'user_id': user.id})
    return user


async def _ldap_candidates(svc: Services, user: UserRecord | None, provider: str | None) -> list[ProviderRecord]:
    enabled = [p for p in await svc.auth_providers.enabled('ldap') if p.ldap is not None]
    if provider is not None:
        return [p for p in enabled if p.id == provider]
    if user is None:
        return enabled
    own = {user.source, *(i.provider for i in user.identities)}
    return sorted(enabled, key=lambda p: p.source not in own)  # stable: the user's providers first, then by order


async def _verify_ldap(
    svc: Services, candidates: list[ProviderRecord], username: str, password: str, *, interactive: bool
) -> UserRecord:
    unavailable = False
    for record in candidates:
        try:
            assert record.ldap is not None  # _ldap_candidates
            directory = LdapDirectory(record.source, record.ldap, decrypt_bind_password(svc.keyring, record))
        except ValueError as exc:  # a stored configuration this release cannot use
            log.error('LDAP provider unusable', extra={'provider': record.id, 'error': str(exc)})
            continue
        result = await svc.ldap.authenticate(
            provider_id=record.id,
            version=record.config_version,
            directory=directory,
            username=username,
            password=password,
            use_cache=not interactive,
        )
        if result.outcome in ('not_found', 'unavailable', 'error'):
            log.info(
                'LDAP sign-in not decided',
                extra={
                    'provider': record.id,
                    'reason': result.outcome,
                    'server': result.server,
                    'error': result.detail,
                },
            )
            unavailable = unavailable or result.outcome != 'not_found'
            continue
        if result.outcome != 'ok' or result.identity is None:
            log.info('sign-in failed', extra={'provider': record.id, 'reason': result.outcome, 'server': result.server})
            raise LoginFailed(_LDAP_REASONS.get(result.outcome, result.outcome), provider=record.source)
        if result.detail == 'cached':
            cached = await users_db.get_by_username(svc.db, result.identity.username)
            if cached is not None and not cached.disabled and not cached.password_hash:
                return cached
        try:
            user = await link_identity(svc.db, result.identity, provision=True)
        except LinkRefused as exc:
            log.info('sign-in refused', extra={'provider': record.id, 'reason': exc.reason})
            raise LoginFailed('link_refused', provider=record.source) from None
        log.info('signed in', extra={'user_id': user.id, 'provider': record.id, 'server': result.server})
        return user
    log.info('sign-in failed', extra={'reason': 'unknown_user'})
    raise LoginFailed('directory_unavailable' if unavailable else 'unknown_user')
