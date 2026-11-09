"""The low-disk guard settings (SPECIFICATION §4.8): the ``settings`` document ``storage``.

``min_free_bytes`` and ``min_free_percent`` (of the data filesystem's size): new cache fills and uploads are refused
while free space is below the larger of the two (``BlobStore.check_free_space``); cached content is still served.
Without the document the guard uses ``WAYSTATION_MIN_FREE_GB`` and no percentage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from waystation_common.db.client import Database
from waystation_common.db.collections import SETTINGS

DOC_ID = 'storage'


@dataclass(frozen=True)
class GuardSettings:
    min_free_bytes: int
    min_free_percent: float = 0.0
    updated_at: datetime | None = None
    updated_by: str | None = None

    def floor(self, total_bytes: int) -> int:
        """Free bytes below which writes are refused, for a filesystem of ``total_bytes``."""
        return max(self.min_free_bytes, int(total_bytes * self.min_free_percent / 100))


async def load(db: Database, *, default_min_free_bytes: int) -> GuardSettings:
    doc = await db[SETTINGS].find_one({'_id': DOC_ID})
    if not doc:
        return GuardSettings(min_free_bytes=default_min_free_bytes)
    return GuardSettings(
        min_free_bytes=max(0, int(doc.get('min_free_bytes', default_min_free_bytes))),
        min_free_percent=min(99.0, max(0.0, float(doc.get('min_free_percent') or 0.0))),
        updated_at=doc.get('updated_at'),
        updated_by=doc.get('updated_by'),
    )


async def save(db: Database, settings: GuardSettings, *, by: str | None) -> GuardSettings:
    now = datetime.now(UTC)
    await db[SETTINGS].update_one(
        {'_id': DOC_ID},
        {
            '$set': {
                'min_free_bytes': settings.min_free_bytes,
                'min_free_percent': settings.min_free_percent,
                'updated_at': now,
                'updated_by': by,
            }
        },
        upsert=True,
    )
    return GuardSettings(settings.min_free_bytes, settings.min_free_percent, now, by)
