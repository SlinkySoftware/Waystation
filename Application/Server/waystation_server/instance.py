"""Instance settings stored in MongoDB (SPECIFICATION §4.12 "Settings"), over the environment defaults.

Read on hot paths (``GET /config`` on every page load, every session request for the 2FA policy), so the stored
values are cached per process for ``TTL_S``; the replica that saves applies its own change at once, the others
within ``TTL_S``. Sections: ``general`` (instance name) and ``security`` (require 2FA for local administrators).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.settings import Settings
from waystation_server.db import settings as settings_db

log = logging.getLogger('waystation.settings')

TTL_S = 5.0
READ_TIMEOUT_S = 1.0
GENERAL = 'general'
SECURITY = 'security'


@dataclass(frozen=True)
class General:
    instance_name: str
    updated_at: datetime | None
    updated_by: str | None


@dataclass(frozen=True)
class Security:
    require_totp_for_admins: bool  # local administrators must enrol a second factor before anything else
    updated_at: datetime | None
    updated_by: str | None


def _stamp(doc: dict[str, Any]) -> tuple[datetime | None, str | None]:
    updated_at = doc.get('updated_at')
    updated_by = doc.get('updated_by')
    return (
        updated_at if isinstance(updated_at, datetime) else None,
        updated_by if isinstance(updated_by, str) else None,
    )


class InstanceSettings:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._docs: dict[str, dict[str, Any] | None] = {}
        self._at: dict[str, float] = {}

    async def _section(self, name: str) -> dict[str, Any]:
        """A stored section. When MongoDB does not answer within ``READ_TIMEOUT_S`` the last known values (else the
        environment defaults) are used, so ``GET /config``, the sign-in page and sessions keep working."""
        if name not in self._docs or time.monotonic() - self._at.get(name, 0.0) > TTL_S:
            try:
                async with asyncio.timeout(READ_TIMEOUT_S):
                    self._docs[name] = await settings_db.get(self._db, name)
            except Exception as exc:
                log.warning('instance settings unavailable', extra={'error': type(exc).__name__, 'section': name})
                self._docs.setdefault(name, None)
            self._at[name] = time.monotonic()
        return self._docs[name] or {}

    def _general(self, doc: dict[str, Any]) -> General:
        name = doc.get('instance_name')
        return General(
            name if isinstance(name, str) and name else self._settings.instance_name,
            *_stamp(doc),
        )

    @staticmethod
    def _security(doc: dict[str, Any]) -> Security:
        return Security(doc.get('require_totp_for_admins') is True, *_stamp(doc))

    async def general(self) -> General:
        return self._general(await self._section(GENERAL))

    async def security(self) -> Security:
        return self._security(await self._section(SECURITY))

    async def _save(self, name: str, values: dict[str, Any], by: str | None) -> dict[str, Any]:
        doc = await settings_db.put(self._db, name, values, by)
        self._docs[name] = doc
        self._at[name] = time.monotonic()
        return doc

    async def save_general(self, instance_name: str, by: str | None) -> General:
        return self._general(await self._save(GENERAL, {'instance_name': instance_name}, by))

    async def save_security(self, require_totp_for_admins: bool, by: str | None) -> Security:
        return self._security(await self._save(SECURITY, {'require_totp_for_admins': require_totp_for_admins}, by))
