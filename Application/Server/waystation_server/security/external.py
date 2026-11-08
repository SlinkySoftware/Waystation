"""Sign-in through an external provider: identity linking and group sync (SPECIFICATION §4.9, provider-neutral).

A provider (LDAP now, OIDC with T-A02) that has verified someone hands an ``ExternalIdentity`` to
``link_identity``, which returns the Waystation user to sign in:

- One Waystation user per username across providers. No user yet: one is created (``source`` = the provider) when
  ``provision`` is set (LDAP always; OIDC per its auto-provision toggle).
- Never link to an account that has a usable local password: the local password stays the only way in for it.
  A local account *without* a password (created for linking) is linked and takes the provider as its source.
- Users of another provider with the same username are linked too (the same person by the same-username rule);
  ``identities`` records every linked external account and ``source`` keeps the provider that created the user.
- Disabled users are refused.
- Group sync: the groups the provider reported replace that provider's entries in ``external_groups``; the user's
  roles via mapped groups follow from them (``security.groups``). Other providers' entries are kept.

Refusals raise ``LinkRefused`` with a reason for the logs; callers answer with the generic sign-in failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from waystation_common.db.client import Database
from waystation_common.errors import Conflict
from waystation_common.ids import new_id
from waystation_server.db import users as users_db
from waystation_server.security.identity import ExternalGroupRef, LinkedIdentity, UserRecord

log = logging.getLogger('waystation.auth')

MAX_EXTERNAL_GROUPS = 500
MAX_IDENTITIES = 20


@dataclass(frozen=True)
class ExternalGroup:
    id: str  # LDAP group DN, OIDC claim value
    name: str


@dataclass(frozen=True)
class ExternalIdentity:
    provider: str  # provider source, ``ldap:<id>`` / ``oidc:<id>``
    subject: str  # LDAP entry DN, OIDC ``sub``
    username: str  # lower-case
    display_name: str
    email: str | None
    groups: tuple[ExternalGroup, ...] = ()


class LinkRefused(Exception):  # noqa: N818 - reads as an outcome
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _identities(user: UserRecord, identity: ExternalIdentity, now: datetime) -> list[LinkedIdentity]:
    kept = [i for i in user.identities if not (i.provider == identity.provider and i.subject == identity.subject)]
    current = next(
        (i for i in user.identities if i.provider == identity.provider and i.subject == identity.subject), None
    )
    linked_at = current.linked_at if current else now
    entry = LinkedIdentity(provider=identity.provider, subject=identity.subject, linked_at=linked_at, last_login=now)
    return [entry, *kept][:MAX_IDENTITIES]


def _groups(user: UserRecord, identity: ExternalIdentity) -> list[ExternalGroupRef]:
    others = [g for g in user.external_groups if g.provider != identity.provider]
    mine = [ExternalGroupRef(provider=identity.provider, id=g.id, name=g.name) for g in identity.groups]
    return (mine + others)[:MAX_EXTERNAL_GROUPS]


async def link_identity(
    db: Database, identity: ExternalIdentity, *, provision: bool, now: datetime | None = None
) -> UserRecord:
    now = now or datetime.now(UTC)
    user = await users_db.get_by_username(db, identity.username)
    if user is None:
        if not provision:
            raise LinkRefused('not_provisioned')
        created = UserRecord(
            id=new_id(),
            username=identity.username,
            display_name=identity.display_name or identity.username,
            email=identity.email,
            source=identity.provider,
            created_at=now,
        )
        try:
            await users_db.insert(db, created)
            log.info('user provisioned', extra={'user_id': created.id, 'provider': identity.provider})
            user = created
        except Conflict:  # created concurrently (another replica, the same login twice): link to that one
            user = await users_db.get_by_username(db, identity.username)
            if user is None:  # pragma: no cover - deleted in between
                raise LinkRefused('race') from None
    if user.disabled:
        raise LinkRefused('disabled')
    if user.password_hash:
        raise LinkRefused('local_password')
    synced = await users_db.sync_external(
        db,
        user.id,
        display_name=identity.display_name,
        email=identity.email,
        external_groups=_groups(user, identity),
        identities=_identities(user, identity, now),
        now=now,
        source=identity.provider if user.source == 'local' else None,
    )
    if synced is None:
        raise LinkRefused('changed')
    return synced
