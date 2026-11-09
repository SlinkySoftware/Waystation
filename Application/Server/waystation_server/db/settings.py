"""``settings`` collection access: singleton documents by ``_id`` (``general``, ...), each holding one section of the
instance settings plus ``updated_at`` / ``updated_by``. A missing document means "the environment defaults"."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import SETTINGS


async def get(db: Database, section: str) -> dict[str, Any] | None:
    doc = await db[SETTINGS].find_one({'_id': section})
    return dict(doc) if doc else None


async def put(db: Database, section: str, values: dict[str, Any], by: str | None) -> dict[str, Any]:
    """Replace a section; returns the stored document."""
    doc = {'_id': section, **values, 'updated_at': datetime.now(UTC), 'updated_by': by}
    await db[SETTINGS].replace_one({'_id': section}, doc, upsert=True)
    return doc
