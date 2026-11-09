"""The RBAC state of one server process: custom roles, groups and materialised grants (ARCHITECTURE §7).

``grant_version`` (MongoDB ``rbac_state``) is incremented by every RBAC change (``RbacState.changed`` after the write).
Each process keeps a snapshot of the custom roles and the groups (``GroupDirectory``) tagged with the version it was
loaded at, and learns about a new version

- at once from its own writes (``changed``),
- within moments from a MongoDB change stream on ``rbac_state`` (other replicas' writes), and
- at most ``POLL_S`` later by re-reading the counter (a change stream that is down, a standalone MongoDB).

Grants are materialised per principal from its effective roles (own plus group roles) and cached under
``(grant_version, roles)`` in-process and in Valkey (``grants:<principal>:<grant_version>``, an hour), so a new
version makes every cached table unreachable at once. Decisions stay in ``security.access``; this module only
computes what ``Principal.grants`` holds. ``on_change`` callbacks run when the version moves (the API token cache
drops resolved principals, whose grants are baked in).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Iterable

from redis.asyncio import Redis

from waystation_common.db.client import Database
from waystation_server.db import groups as groups_db
from waystation_server.db import roles as roles_db
from waystation_server.security.actions import Action, Grants, Permission
from waystation_server.security.groups import GroupDirectory
from waystation_server.security.identity import RoleRecord, UserRecord

log = logging.getLogger('waystation.rbac')

POLL_S = 5.0
GRANTS_TTL_S = 3600
MAX_CACHE = 10_000
WATCH_RETRY_S = 5.0


def grants_key(principal_id: str, version: int) -> str:
    return f'grants:{principal_id}:{version}'


def _encode(roles: tuple[str, ...], grants: Grants) -> str:
    return json.dumps(
        {'r': list(roles), 'a': grants.is_admin, 'p': [[p.action.value, p.scope] for p in grants.permissions]},
        separators=(',', ':'),
    )


def _decode(raw: str | bytes, roles: tuple[str, ...]) -> Grants | None:
    try:
        data = json.loads(raw)
        if tuple(data['r']) != roles:
            return None  # the principal's roles changed (group sync): materialise again
        return Grants(is_admin=bool(data['a']), permissions=tuple(Permission(Action(a), str(s)) for a, s in data['p']))
    except ValueError, KeyError, TypeError:
        return None


class RbacState:
    def __init__(self, db: Database, valkey: Redis, *, poll_s: float = POLL_S) -> None:
        self._db = db
        self._valkey = valkey
        self._poll_s = poll_s
        self.groups = GroupDirectory(lambda: groups_db.list_all(db))
        self.version = -1  # the version the snapshot belongs to (-1: not loaded)
        self._remote = -1  # the newest version seen (change stream, poll, own write)
        self._checked_at = -1e18
        self._roles: dict[str, RoleRecord] = {}
        self._table: dict[str, tuple[Permission, ...]] = {}
        self._cache: dict[tuple[int, tuple[str, ...]], Grants] = {}
        self._lock = asyncio.Lock()
        self._watcher: asyncio.Task[None] | None = None
        self.on_change: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._watcher is None:
            self._watcher = asyncio.create_task(self._watch(), name='rbac-watch')

    async def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watcher
            self._watcher = None

    async def _watch(self) -> None:
        while True:
            try:
                async for version in roles_db.watch(self._db):
                    self._seen(version)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # no replica set, MongoDB restarting: polling still bounds staleness
                log.warning('rbac change stream lost; retrying', extra={'error': type(exc).__name__})
            await asyncio.sleep(WATCH_RETRY_S)

    def _seen(self, version: int) -> None:
        if version != self._remote:
            self._remote = version
            self._checked_at = time.monotonic()

    # ------------------------------------------------------------------ snapshot
    async def changed(self) -> int:
        """Record an RBAC change made by this process (after its write): bump ``grant_version`` and reload."""
        version = await roles_db.bump(self._db)
        self._seen(version)
        self.groups.invalidate()
        return version

    async def ensure(self) -> None:
        now = time.monotonic()
        if now - self._checked_at >= self._poll_s:
            self._checked_at = now
            try:
                self._seen(await roles_db.version(self._db))
            except Exception as exc:  # keep deciding with the snapshot we have
                log.warning('grant_version check failed', extra={'error': type(exc).__name__})
        if self.version == self._remote and self.version >= 0:
            return
        async with self._lock:
            target = self._remote
            if self.version == target and self.version >= 0:
                return
            roles = await roles_db.list_all(self._db)
            self._roles = {r.id: r for r in roles}
            self._table = {r.id: _permissions(r) for r in roles}
            self._cache = {}
            self.groups.invalidate()
            moved = self.version >= 0
            self.version = max(target, 0)
            self._remote = max(self._remote, self.version)
        if moved:
            for callback in self.on_change:
                try:
                    callback()
                except Exception:
                    log.exception('rbac change callback failed')

    async def custom_roles(self) -> dict[str, RoleRecord]:
        await self.ensure()
        return dict(self._roles)

    async def table(self) -> dict[str, tuple[Permission, ...]]:
        """Custom role id → permissions (``access.RoleTable``)."""
        await self.ensure()
        return dict(self._table)

    # ------------------------------------------------------------------ principals
    async def roles_for(self, user: UserRecord) -> tuple[str, ...]:
        """The user's effective roles: its own plus its local and mapped groups' (``GroupDirectory``)."""
        await self.ensure()
        return await self.groups.roles_for(user)

    async def grants(self, principal_id: str, roles: Iterable[str]) -> Grants:
        """The materialised grants of ``roles`` for ``principal_id`` (cached per ``grant_version``)."""
        await self.ensure()
        key_roles = tuple(roles)
        version = self.version
        cached = self._cache.get((version, key_roles))
        if cached is not None:
            return cached
        grants: Grants | None = None
        key = grants_key(principal_id, version)
        try:
            raw = await self._valkey.get(key)
            grants = _decode(raw, key_roles) if raw is not None else None
        except Exception as exc:  # Valkey down: materialise here
            log.debug('grant cache read failed', extra={'error': type(exc).__name__})
        if grants is None:
            from waystation_server.security import access  # access → authn → services → rbac: import late

            grants = access.materialise(key_roles, self._table)
            try:
                await self._valkey.set(key, _encode(key_roles, grants), ex=GRANTS_TTL_S)
            except Exception as exc:
                log.debug('grant cache write failed', extra={'error': type(exc).__name__})
        if len(self._cache) >= MAX_CACHE:
            self._cache.clear()
        self._cache[(version, key_roles)] = grants
        return grants


def _permissions(record: RoleRecord) -> tuple[Permission, ...]:
    from waystation_server.security import access

    out: list[Permission] = []
    for perm in record.permissions:
        if access.permission_error(perm.action, perm.scope) is None:
            out.append(Permission(Action(perm.action), perm.scope))
    return tuple(out)
