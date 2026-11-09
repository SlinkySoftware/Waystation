"""Instance settings stored in MongoDB (SPECIFICATION §4.12 "Settings"), over the environment defaults.

Read on hot paths (``GET /config`` on every page load), so the stored values are cached per process for ``TTL_S``;
the replica that saves applies its own change at once, the others within ``TTL_S``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from waystation_common.db.client import Database
from waystation_common.settings import Settings
from waystation_server.db import settings as settings_db

log = logging.getLogger('waystation.settings')

TTL_S = 5.0
READ_TIMEOUT_S = 1.0
GENERAL = 'general'


@dataclass(frozen=True)
class General:
    instance_name: str
    updated_at: datetime | None
    updated_by: str | None


class InstanceSettings:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._general: General | None = None
        self._at = 0.0

    def _from(self, doc: dict[str, object] | None) -> General:
        doc = doc or {}
        name = doc.get('instance_name')
        updated_at = doc.get('updated_at')
        updated_by = doc.get('updated_by')
        return General(
            instance_name=name if isinstance(name, str) and name else self._settings.instance_name,
            updated_at=updated_at if isinstance(updated_at, datetime) else None,
            updated_by=updated_by if isinstance(updated_by, str) else None,
        )

    async def general(self) -> General:
        """The general section. When MongoDB does not answer within ``READ_TIMEOUT_S`` the last known values (else
        the environment defaults) are used, so ``GET /config`` and the sign-in page keep working."""
        if self._general is None or time.monotonic() - self._at > TTL_S:
            try:
                async with asyncio.timeout(READ_TIMEOUT_S):
                    doc = await settings_db.get(self._db, GENERAL)
            except Exception as exc:
                log.warning('instance settings unavailable', extra={'error': type(exc).__name__})
                self._general = self._general or self._from(None)
            else:
                self._general = self._from(doc)
            self._at = time.monotonic()
        return self._general

    async def save_general(self, instance_name: str, by: str | None) -> General:
        self._general = self._from(await settings_db.put(self._db, GENERAL, {'instance_name': instance_name}, by))
        self._at = time.monotonic()
        return self._general
