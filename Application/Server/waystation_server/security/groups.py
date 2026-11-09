"""Groups and effective roles (SPECIFICATION §4.10: users hold roles directly or via groups).

A ``groups`` document holds roles. Local groups list their members on the user (``users.groups``); groups mapped from
an external provider (``source`` ``ldap``/``oidc``) match the groups that provider reported at the user's last sign-in
(``users.external_groups``) by ``(provider_id, external_id)``, where ``external_id`` is the mapping's group DN or name
in ``providers.group_key`` form. Mapping changes therefore apply on the next request, membership changes on the next
sign-in through the provider (group sync).

``GroupDirectory`` keeps every group in memory (refreshed every few seconds, invalidated on a local write) so a
principal's roles cost no database round trip per request. Role names are only resolved into permissions by
``security.access``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from waystation_server.security.identity import UserRecord
from waystation_server.security.providers import group_key

DIRECTORY_TTL_S = 5.0


class GroupRecord(BaseModel):
    """A ``groups`` document."""

    model_config = ConfigDict(extra='ignore', populate_by_name=True)

    id: str = Field(alias='_id')
    name: str
    source: str = 'local'  # ``local``, ``ldap``, ``oidc``
    provider_id: str | None = None
    external_id: str | None = None  # ``group_key`` of ``external_group``
    external_group: str | None = None  # as entered in the mapping
    position: int = 0  # order within the provider's mapping
    description: str = ''
    roles: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class GroupDirectory:
    def __init__(self, load: Callable[[], Awaitable[list[GroupRecord]]], ttl_s: float = DIRECTORY_TTL_S) -> None:
        self._load = load
        self._ttl_s = ttl_s
        self._by_id: dict[str, tuple[str, ...]] = {}
        self._by_external: dict[tuple[str, str], tuple[str, ...]] = {}
        self._loaded_at = -1e18
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._loaded_at = -1e18

    async def _refresh(self) -> None:
        if time.monotonic() - self._loaded_at < self._ttl_s:
            return
        async with self._lock:
            if time.monotonic() - self._loaded_at < self._ttl_s:
                return
            by_id: dict[str, tuple[str, ...]] = {}
            by_external: dict[tuple[str, str], tuple[str, ...]] = {}
            for group in await self._load():
                roles = tuple(group.roles)
                by_id[group.id] = roles
                if group.provider_id and group.external_id:
                    key = (f'{group.source}:{group.provider_id}', group.external_id)
                    by_external[key] = tuple(dict.fromkeys(by_external.get(key, ()) + roles))
            self._by_id, self._by_external = by_id, by_external
            self._loaded_at = time.monotonic()

    async def roles_for(self, user: UserRecord) -> tuple[str, ...]:
        """The user's own roles plus those of its local groups and of its mapped external groups."""
        await self._refresh()
        roles: list[str] = list(user.roles)
        for group_id in user.groups:
            roles.extend(self._by_id.get(group_id, ()))
        for ref in user.external_groups:
            for key in {group_key(ref.id), group_key(ref.name)}:
                roles.extend(self._by_external.get((ref.provider, key), ()))
        return tuple(dict.fromkeys(roles))


def mapped_roles(mapping: list[tuple[str, list[str]]], groups: list[tuple[str, str]]) -> list[str]:
    """Roles a mapping (``[(external_group, roles)]``) gives for reported groups (``[(id, name)]``): test-user."""
    keys = {group_key(value) for pair in groups for value in pair}
    roles: list[str] = []
    for external, granted in mapping:
        if group_key(external) in keys:
            roles.extend(granted)
    return list(dict.fromkeys(roles))


def role_sources(user: UserRecord, groups: list[GroupRecord]) -> list[tuple[str, str]]:
    """Where each of the user's effective roles comes from, as ``(role, via)`` with ``via`` = ``direct`` or the
    group's name (same matching as ``GroupDirectory.roles_for``; the effective-permissions view)."""
    out: list[tuple[str, str]] = [(role, 'direct') for role in user.roles]
    local = set(user.groups)
    refs: set[tuple[str, str]] = set()
    for ref in user.external_groups:
        refs.add((ref.provider, group_key(ref.id)))
        refs.add((ref.provider, group_key(ref.name)))
    for group in groups:
        if group.id in local or (
            group.provider_id
            and group.external_id
            and (f'{group.source}:{group.provider_id}', group.external_id) in refs
        ):
            out.extend((role, group.name) for role in group.roles)
    return list(dict.fromkeys(out))
