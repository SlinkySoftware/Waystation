"""Username/password verification for local users (SPECIFICATION §4.9), shared by the UI sign-in and the Docker
token service (``docker login``).

- Rate limited per client address (``WAYSTATION_LOGIN_RATE_LIMIT`` attempts per 5 minutes, then 429).
- Lockout: ``LOGIN_LOCKOUT_FAILURES`` consecutive failures lock a local account for ``LOGIN_LOCKOUT_MINUTES``.
- Failures are indistinguishable: unknown user, wrong password, disabled or locked account all raise the same
  ``Unauthorized`` after the same argon2 work.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from waystation_common.db.client import Database
from waystation_common.errors import Unauthorized
from waystation_common.settings import Settings
from waystation_server.db import users as users_db
from waystation_server.security import passwords, ratelimit
from waystation_server.security.identity import UserRecord

log = logging.getLogger('waystation.auth')

WINDOW_S = 300
GENERIC = 'invalid username or password'


async def verify_login(
    db: Database, valkey: Redis, settings: Settings, *, username: str, password: str, client_ip: str
) -> UserRecord:
    await ratelimit.check(valkey, 'login', client_ip, limit=settings.login_rate_limit, window_s=WINDOW_S)
    user = await users_db.get_by_username(db, username.strip())
    local = user is not None and user.source == 'local'
    ok = await passwords.verify_password(user.password_hash if local and user else None, password)
    now = datetime.now(UTC)
    if user is None or not local:
        log.info('sign-in failed', extra={'reason': 'unknown_user'})
        raise Unauthorized(GENERIC)
    if user.disabled or (user.locked_until is not None and user.locked_until > now):
        log.info('sign-in refused', extra={'user_id': user.id, 'reason': 'disabled' if user.disabled else 'locked'})
        raise Unauthorized(GENERIC)
    if not ok:
        failures = await users_db.record_failure(
            db,
            user.id,
            threshold=settings.login_lockout_failures,
            locked_until=now + timedelta(minutes=settings.login_lockout_minutes),
        )
        log.info('sign-in failed', extra={'user_id': user.id, 'reason': 'password', 'failures': failures})
        raise Unauthorized(GENERIC)
    rehash = None
    if user.password_hash and passwords.needs_rehash(user.password_hash):
        rehash = await passwords.hash_password(password)
    await users_db.record_success(db, user.id, now, password_hash=rehash)
    log.info('signed in', extra={'user_id': user.id})
    return user
