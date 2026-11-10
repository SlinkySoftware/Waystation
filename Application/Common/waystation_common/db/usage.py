"""Stored bytes per repository (``repo_usage``; quotas, SPECIFICATION §4.8; DATA_MODEL §1).

Usage is the sum of the sizes of a repository's assets: a blob counts towards **every** repository that references
it (de-duplication saves disk space, not quota), and a repository's own duplicates (two paths, one blob) count
twice. One document per repository, ``_id`` = repository id:

- ``bytes``, ``assets``, ``artifacts``: kept up to date incrementally (``add``: one ``$inc`` per asset write or
  delete, by the code that writes ``assets``), so a quota check is one read by ``_id``; ``artifacts`` counts the
  content assets only (``kind: content``: the "artifacts" of the repository list and the storage figures, which read
  these documents instead of aggregating ``assets``);
- ``reconciled_at``: the last ``reconcile`` (worker job ``quota.reconcile``), which recomputes both figures from
  ``assets`` and so repairs any drift (a crash between an asset write and its ``$inc``, content that existed before
  usage was tracked, writes racing the recount);
- ``alert_level``: the highest quota threshold already announced (0, 80 or 100 percent), so a threshold crossing is
  announced once (``raise_level`` is a compare-and-set) and again only after usage went back below it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymongo import ReturnDocument

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, REPO_USAGE, REPOSITORIES

WARN_RATIO = 0.8  # soft and hard quotas warn at 80 %
LEVELS = (0, 80, 100)


@dataclass(frozen=True)
class Usage:
    repo_id: str
    bytes: int = 0
    assets: int = 0
    artifacts: int = 0
    alert_level: int = 0
    reconciled_at: datetime | None = None

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Usage:
        return cls(
            repo_id=str(doc['_id']),
            bytes=max(0, int(doc.get('bytes') or 0)),
            assets=max(0, int(doc.get('assets') or 0)),
            artifacts=max(0, int(doc.get('artifacts') or 0)),
            alert_level=int(doc.get('alert_level') or 0),
            reconciled_at=doc.get('reconciled_at'),
        )


def level_of(used: int, quota: int | None) -> int:
    """The quota threshold ``used`` has reached: 100 (at or over the quota), 80 (warning) or 0."""
    if not quota or quota <= 0:
        return 0
    if used >= quota:
        return 100
    if used >= quota * WARN_RATIO:
        return 80
    return 0


async def add(
    db: Database, repo_id: str, bytes_delta: int, assets_delta: int = 0, *, artifacts_delta: int = 0
) -> Usage | None:
    """Count an asset write or delete (deltas may be negative; ``artifacts_delta``: content assets). Returns the new
    figures (None when nothing changed)."""
    if not bytes_delta and not assets_delta and not artifacts_delta:
        return None
    inc = {'bytes': bytes_delta, 'assets': assets_delta, 'artifacts': artifacts_delta}
    doc = await db[REPO_USAGE].find_one_and_update(
        {'_id': repo_id},
        {'$inc': inc, '$set': {'updated_at': datetime.now(UTC)}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return Usage.from_doc(doc) if doc else None


async def get(db: Database, repo_id: str) -> Usage:
    doc = await db[REPO_USAGE].find_one({'_id': repo_id})
    return Usage.from_doc(doc) if doc else Usage(repo_id)


async def get_many(db: Database, repo_ids: Iterable[str]) -> dict[str, Usage]:
    ids = sorted(set(repo_ids))
    out = {repo_id: Usage(repo_id) for repo_id in ids}
    if ids:
        async for doc in db[REPO_USAGE].find({'_id': {'$in': ids}}):
            out[str(doc['_id'])] = Usage.from_doc(doc)
    return out


async def raise_level(db: Database, repo_id: str, level: int) -> bool:
    """Record that ``level`` was announced; True when this call moved it up (the caller announces it)."""
    result = await db[REPO_USAGE].update_one(
        {'_id': repo_id, 'alert_level': {'$not': {'$gte': level}}}, {'$set': {'alert_level': level}}
    )
    return bool(result.modified_count)


async def lower_level(db: Database, repo_id: str, level: int) -> None:
    """Usage went back below an announced threshold: the next crossing is announced again."""
    await db[REPO_USAGE].update_one({'_id': repo_id, 'alert_level': {'$gt': level}}, {'$set': {'alert_level': level}})


async def drop(db: Database, repo_id: str) -> None:
    """The repository was deleted."""
    await db[REPO_USAGE].delete_one({'_id': repo_id})


@dataclass(frozen=True)
class RepoQuota:
    """A repository as quota accounting sees it (``repositories.storage``)."""

    repo_id: str
    name: str
    format: str
    quota_bytes: int | None
    quota_mode: str


async def repositories(db: Database) -> list[RepoQuota]:
    out: list[RepoQuota] = []
    async for doc in db[REPOSITORIES].find({}, {'name': 1, 'format': 1, 'storage': 1}):
        storage = doc.get('storage') or {}
        quota = storage.get('quota_bytes')
        out.append(
            RepoQuota(
                repo_id=str(doc['_id']),
                name=str(doc.get('name') or ''),
                format=str(doc.get('format') or ''),
                quota_bytes=int(quota) if quota else None,
                quota_mode=str(storage.get('quota_mode') or 'soft'),
            )
        )
    return out


@dataclass(frozen=True)
class Reconciled:
    repo_id: str
    bytes: int
    assets: int
    drift_bytes: int  # recounted minus recorded
    artifacts: int = 0


async def totals(db: Database) -> dict[str, tuple[int, int, int]]:
    """Recount from ``assets``: repository id -> (bytes, assets, artifacts)."""
    pipeline: list[dict[str, Any]] = [
        {
            '$group': {
                '_id': '$repo_id',
                'bytes': {'$sum': '$size'},
                'assets': {'$sum': 1},
                'artifacts': {'$sum': {'$cond': [{'$eq': ['$kind', 'content']}, 1, 0]}},
            }
        }
    ]
    return {
        str(d['_id']): (int(d['bytes']), int(d['assets']), int(d['artifacts']))
        async for d in await db[ASSETS].aggregate(pipeline)
    }


async def reconcile(db: Database, repo_ids: Iterable[str], *, now: datetime | None = None) -> list[Reconciled]:
    """Recompute every repository's usage from ``assets`` and store it; documents of repositories that no longer exist
    are removed. Writes that land while the recount runs may be off until the next run (bounded drift)."""
    now = now or datetime.now(UTC)
    ids = set(repo_ids)
    counted = await totals(db)
    recorded = await get_many(db, ids)
    out: list[Reconciled] = []
    for repo_id in sorted(ids):
        size, assets, artifacts = counted.get(repo_id, (0, 0, 0))
        before = recorded[repo_id]
        await db[REPO_USAGE].update_one(
            {'_id': repo_id},
            {
                '$set': {
                    'bytes': size,
                    'assets': assets,
                    'artifacts': artifacts,
                    'reconciled_at': now,
                    'updated_at': now,
                }
            },
            upsert=True,
        )
        out.append(Reconciled(repo_id, size, assets, size - before.bytes, artifacts))
    await db[REPO_USAGE].delete_many({'_id': {'$nin': sorted(ids)}})
    return out
