"""First administrator (SPECIFICATION §4.9, run by ``init``).

When the database has no users at all, ``init`` creates the local user ``admin`` with the built-in ``admin`` role.
Its password comes from ``WAYSTATION_BOOTSTRAP_ADMIN_PASSWORD`` (prefer ``..._FILE``); when that is unset a random
one-time password is generated, printed once to the ``init`` container's stderr (never logged) and the account is
flagged ``must_change_password``. Idempotent: once any user exists nothing happens, and a set password is never
changed by a later run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from waystation_common.db.client import Database
from waystation_common.errors import Conflict
from waystation_common.ids import new_id
from waystation_common.settings import Settings
from waystation_server.db import users as users_db
from waystation_server.security import passwords
from waystation_server.security.identity import UserRecord

log = logging.getLogger('waystation.bootstrap')

ADMIN_USERNAME = 'admin'
ADMIN_ROLE = 'admin'


@dataclass(frozen=True)
class BootstrapResult:
    created: bool
    generated_password: str | None = None


async def ensure_bootstrap_admin(db: Database, settings: Settings) -> BootstrapResult:
    if await users_db.count(db) > 0:
        return BootstrapResult(created=False)
    configured = settings.bootstrap_admin_password
    password = configured.get_secret_value() if configured is not None else passwords.generate_password()
    user = UserRecord(
        id=new_id(),
        username=ADMIN_USERNAME,
        display_name='Administrator',
        source='local',
        password_hash=await passwords.hash_password(password),
        roles=[ADMIN_ROLE],
        must_change_password=configured is None,
        created_at=datetime.now(UTC),
    )
    try:
        await users_db.insert(db, user)
    except Conflict:  # a concurrent init created it first
        return BootstrapResult(created=False)
    log.info('bootstrap admin created', extra={'username': ADMIN_USERNAME, 'generated': configured is None})
    return BootstrapResult(created=True, generated_password=None if configured is not None else password)


def banner(password: str) -> str:
    line = '=' * 72
    return (
        f'{line}\n'
        f'Waystation: first administrator created.\n'
        f'  username: {ADMIN_USERNAME}\n'
        f'  one-time password: {password}\n'
        f'Sign in and change it. It is shown only this once.\n'
        f'{line}\n'
    )
