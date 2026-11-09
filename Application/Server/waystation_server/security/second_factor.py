"""Two-factor sign-in for local accounts (SPECIFICATION §4.9): TOTP checks, replay protection, recovery codes, the
pending sign-in between the password and the code, and the "require 2FA for local administrators" policy.

- **Enrolled** = a local account with a password and ``users.totp_enc``. LDAP/OIDC accounts are never asked: their
  provider owns their sign-in.
- **Replay**: a TOTP code is accepted once per step per user. The step is claimed in Valkey
  (``totp:used:<user id>:<step>``, ``SET NX`` for ``REPLAY_TTL_S``) and in MongoDB (``users.totp_last_step``: only a
  later step is accepted). Either refuses a replay; with Valkey down MongoDB alone still does.
- **Recovery codes** are single use: consuming one is an atomic ``$pull`` of its keyed hash.
- **Pending sign-in**: after the password, enrolled users get a challenge (``login_challenges``, ``CHALLENGE_TTL_S``,
  at most ``MAX_ATTEMPTS`` wrong codes) bound to the browser by the HttpOnly ``ws_mfa`` cookie (``SameSite=Strict``,
  path ``/api/v1/auth/login``). The challenge is not a session: it opens nothing but the second step.
- **Policy**: with ``security.require_totp_for_admins`` on, a local administrator (``admin``) without a second factor
  gets sessions that may only enrol (``access.enforce``), and ``docker login`` with its password is refused.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from waystation_common.crypto import parse_key
from waystation_common.errors import Conflict
from waystation_server.db import login_challenges as challenges_db
from waystation_server.db import users as users_db
from waystation_server.security import totp
from waystation_server.security.actions import Grants
from waystation_server.security.identity import UserRecord

if TYPE_CHECKING:
    from waystation_server.services import Services

log = logging.getLogger('waystation.auth.totp')

CHALLENGE_COOKIE = 'ws_mfa'
CHALLENGE_PATH = '/api/v1/auth/login'
CHALLENGE_TTL_S = 300
MAX_ATTEMPTS = 5
ENROL_TTL_S = 900
REPLAY_TTL_S = (2 * totp.DRIFT_STEPS + 2) * totp.STEP_S
ISSUER_MAX = 64

Method = Literal['totp', 'recovery_code']


def clock() -> float:
    """The current time for TOTP checks (tests replace it)."""
    return time.time()


def secret_aad(user_id: str) -> bytes:
    return f'user-totp:{user_id}'.encode()


def enrolled(user: UserRecord) -> bool:
    return user.source == 'local' and bool(user.password_hash) and bool(user.totp_enc)


def can_enrol(user: UserRecord) -> bool:
    return user.source == 'local' and bool(user.password_hash)


def recovery_hasher(svc: Services) -> totp.RecoveryHasher:
    keys = [parse_key(value.get_secret_value()) for value in svc.settings.secret_keys]
    if not keys or svc.keyring is None:
        raise Conflict('two-factor sign-in needs WAYSTATION_SECRET_KEYS to be configured')
    return totp.RecoveryHasher(keys)


def decrypt_secret(svc: Services, user_id: str, enc: str) -> str:
    if svc.keyring is None:
        raise Conflict('two-factor sign-in needs WAYSTATION_SECRET_KEYS to be configured')
    return svc.keyring.decrypt_str(enc, aad=secret_aad(user_id))


def encrypt_secret(svc: Services, user_id: str, secret: str) -> str:
    if svc.keyring is None:
        raise Conflict('two-factor sign-in needs WAYSTATION_SECRET_KEYS to be configured')
    return svc.keyring.encrypt_str(secret, aad=secret_aad(user_id))


def new_recovery(svc: Services, user_id: str) -> tuple[list[str], list[str]]:
    """Fresh recovery codes and their stored hashes."""
    hasher = recovery_hasher(svc)
    codes = totp.new_recovery_codes()
    return codes, [hasher.hash(user_id, code) for code in codes]


async def _claim_step(svc: Services, user_id: str, step: int) -> bool:
    try:
        fresh = await svc.valkey.set(f'totp:used:{user_id}:{step}', '1', nx=True, ex=REPLAY_TTL_S)
    except Exception as exc:  # Valkey down: MongoDB alone decides
        log.warning('TOTP replay check in Valkey failed', extra={'error': type(exc).__name__})
        fresh = True
    if not fresh:
        return False
    return await users_db.claim_totp_step(svc.db, user_id, step)


def check_code(secret: str, code: str, now: float | None = None) -> int | None:
    """The step ``code`` belongs to (no replay check): enrolment confirmation and tests."""
    return totp.match_step(secret, code, clock() if now is None else now)


async def verify(svc: Services, user: UserRecord, code: str, *, allow_recovery: bool = True) -> Method | None:
    """Check a TOTP code (once per step) or, when allowed, consume a recovery code. None when neither matches."""
    if not enrolled(user) or user.totp_enc is None:
        return None
    if totp.looks_like_code(code):
        step = check_code(decrypt_secret(svc, user.id, user.totp_enc), code)
        if step is None:
            log.info('second factor refused', extra={'user_id': user.id, 'reason': 'wrong_code'})
            return None
        if not await _claim_step(svc, user.id, step):
            log.info('second factor refused', extra={'user_id': user.id, 'reason': 'replay'})
            return None
        return 'totp'
    if not allow_recovery or totp.normalise_recovery(code) is None:
        log.info('second factor refused', extra={'user_id': user.id, 'reason': 'malformed'})
        return None
    left = await users_db.use_recovery_code(svc.db, user.id, recovery_hasher(svc).candidates(user.id, code))
    if left is None:
        log.info('second factor refused', extra={'user_id': user.id, 'reason': 'wrong_recovery_code'})
        return None
    log.info('recovery code used', extra={'user_id': user.id, 'left': left})
    return 'recovery_code'


async def setup_required(svc: Services, user: UserRecord, grants: Grants | None = None) -> bool:
    """Must this user enrol before doing anything else (policy on, local administrator, no second factor)?
    ``grants``: the user's materialised grants when the caller has them already."""
    if not can_enrol(user) or user.totp_enc:
        return False
    if not (await svc.instance.security()).require_totp_for_admins:
        return False
    if grants is None:
        grants = await svc.rbac.grants(user.id, await svc.rbac.roles_for(user))
    return grants.is_admin


# ---------------------------------------------------------------------------- the pending sign-in
@dataclass(frozen=True)
class Challenge:
    token: str
    expires_at: datetime


def challenge_id(token: str) -> str:
    return hashlib.sha256(f'login-challenge:{token}'.encode()).hexdigest()


async def start(svc: Services, user: UserRecord, *, ip: str | None) -> Challenge:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=CHALLENGE_TTL_S)
    await challenges_db.insert(
        svc.db,
        challenges_db.LoginChallenge(
            id=challenge_id(token), user_id=user.id, created_at=now, expires_at=expires_at, ip=ip
        ),
    )
    return Challenge(token=token, expires_at=expires_at)


async def pending(svc: Services, token: str | None) -> challenges_db.LoginChallenge | None:
    if not token or len(token) > 128:
        return None
    return await challenges_db.get(svc.db, challenge_id(token), datetime.now(UTC))
