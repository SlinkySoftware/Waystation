"""Quota thresholds and storage alerts shared by the server (after writes) and the worker (after a recount).

A repository with a quota is announced once when its usage reaches 80 % (``quota.warning``) and once when it reaches
100 % (``quota.exceeded``), in both modes; ``soft`` quotas only warn, ``hard`` quotas also refuse new writes (the
server's ``repos.quota``). Usage that falls back below a threshold re-arms it (``waystation_common.db.usage``).
``storage.low`` / ``storage.ok`` describe the low-disk guard.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from waystation_common.db import usage as usage_db
from waystation_common.db.client import Database
from waystation_common.events.schema import ActivityEvent, EventType

QuotaMode = Literal['soft', 'hard']


def human_bytes(n: float) -> str:
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(n) < 1024 or unit == 'TiB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TiB'  # pragma: no cover - loop always returns


def quota_event(
    *,
    level: int,
    repo_id: str,
    repo: str,
    format: str,
    used: int,
    quota: int,
    mode: QuotaMode,
    now: datetime,
) -> ActivityEvent:
    if level >= 100:
        consequence = 'new uploads and cache fills are refused' if mode == 'hard' else 'soft quota: nothing is refused'
        summary = f'{repo} reached its {human_bytes(quota)} quota ({human_bytes(used)} stored); {consequence}'
        kind = EventType.QUOTA_EXCEEDED
    else:
        summary = f'{repo} reached {level} % of its {human_bytes(quota)} quota ({human_bytes(used)} stored)'
        kind = EventType.QUOTA_WARNING
    return ActivityEvent(
        type=kind,
        ts=now,
        repo_id=repo_id,
        repo=repo,
        format=format,
        bytes=used,
        detail={'summary': summary, 'quota_bytes': quota, 'mode': mode, 'level': level},
    )


async def evaluate(
    db: Database,
    current: usage_db.Usage,
    *,
    repo: str,
    format: str,
    quota: int | None,
    mode: QuotaMode,
    now: datetime,
) -> ActivityEvent | None:
    """Compare ``current`` usage with the quota: the event to announce when a threshold was newly reached (exactly one
    caller gets it, across processes), None otherwise. Re-arms thresholds usage fell below."""
    level = usage_db.level_of(current.bytes, quota)
    if level > current.alert_level and quota:
        if await usage_db.raise_level(db, current.repo_id, level):
            return quota_event(
                level=level,
                repo_id=current.repo_id,
                repo=repo,
                format=format,
                used=current.bytes,
                quota=quota,
                mode=mode,
                now=now,
            )
    elif level < current.alert_level:
        await usage_db.lower_level(db, current.repo_id, level)
    return None


def storage_event(*, low: bool, free: int, floor: int, total: int, now: datetime) -> ActivityEvent:
    if low:
        summary = (
            f'Low disk: {human_bytes(free)} free, below the {human_bytes(floor)} guard; new cache fills and uploads '
            'are refused (cached content is still served)'
        )
    else:
        summary = f'Disk space recovered: {human_bytes(free)} free (guard {human_bytes(floor)}); writes resume'
    return ActivityEvent(
        type=EventType.STORAGE_LOW if low else EventType.STORAGE_OK,
        ts=now,
        bytes=free,
        detail={'summary': summary, 'free_bytes': free, 'floor_bytes': floor, 'total_bytes': total},
    )
