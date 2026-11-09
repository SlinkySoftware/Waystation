"""MongoDB side of blob verify (``waystation_worker.verify``, SPECIFICATION §8.5): page through ``assets``, mark the
ones whose blob is missing or damaged, clear marks that no longer apply, and count the marks for ``/metrics``.

The mark is ``assets.blob_missing`` = ``{reason, hosted, detected_at}`` (DATA_MODEL): ``reason`` is ``missing`` (no
file) or ``damaged`` (wrong size or sha256; the file was moved out of the way), ``hosted`` is whether the asset
belongs to a hosted repository (a loss: only a re-upload or a restore brings it back). A proxy asset with a mark is
re-fetched on its next request (its blob file is gone, which the proxy engine treats as a miss) and the fill's
``save`` clears the mark; verify also clears marks whose blob is back.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pymongo import UpdateOne

from waystation_common.db.client import Database, Document
from waystation_common.db.collections import ASSETS, BLOBS, REPOSITORIES

MARK = 'blob_missing'
_FIELDS = {'_id': 1, 'repo_id': 1, 'path': 1, 'blob_sha256': 1, 'size': 1, MARK: 1}


async def repository_types(db: Database) -> dict[str, tuple[str, str]]:
    """Repository id -> (name, type) for every repository."""
    cursor = db[REPOSITORIES].find({}, {'_id': 1, 'name': 1, 'type': 1})
    return {str(d['_id']): (str(d.get('name') or d['_id']), str(d.get('type') or '')) async for d in cursor}


async def assets_page(db: Database, *, after: str | None, limit: int = 1000) -> list[Document]:
    """Assets in ``_id`` order (keyset pages), with the fields verify needs."""
    query: Document = {'_id': {'$gt': after}} if after is not None else {}
    return [doc async for doc in db[ASSETS].find(query, _FIELDS).sort('_id', 1).limit(limit)]


async def blob_sizes(db: Database, sha256s: Sequence[str]) -> dict[str, int]:
    """Recorded sizes (``blobs.size``) of the given blobs that have a document."""
    if not sha256s:
        return {}
    cursor = db[BLOBS].find({'_id': {'$in': list(sha256s)}}, {'_id': 1, 'size': 1})
    return {str(d['_id']): int(d['size']) async for d in cursor if isinstance(d.get('size'), int)}


async def apply_marks(
    db: Database, marks: Sequence[tuple[str, str, bool]], clear: Sequence[str], *, now: datetime
) -> tuple[int, int]:
    """Set ``blob_missing`` on ``(asset id, reason, hosted)`` (an asset already marked for the same reason keeps its
    first ``detected_at``) and remove it from the assets in ``clear``. Returns (marked or changed, cleared)."""
    marked = cleared = 0
    if marks:
        ops = [
            UpdateOne(
                {'_id': asset_id, f'{MARK}.reason': {'$ne': reason}},
                {'$set': {MARK: {'reason': reason, 'hosted': hosted, 'detected_at': now}}},
            )
            for asset_id, reason, hosted in marks
        ]
        marked = (await db[ASSETS].bulk_write(ops, ordered=False)).modified_count
    if clear:
        result = await db[ASSETS].update_many({'_id': {'$in': list(clear)}, MARK: {'$exists': True}}, {'$unset': {MARK: ''}})
        cleared = result.modified_count
    return marked, cleared


async def marked_counts(db: Database) -> dict[str, int]:
    """Assets currently marked, by kind of repository: ``{'hosted': n, 'proxy': m}`` (index ``blob_missing``)."""
    pipeline: list[dict[str, Any]] = [
        {'$match': {MARK: {'$exists': True}}},
        {'$group': {'_id': f'${MARK}.hosted', 'n': {'$sum': 1}}},
    ]
    counts = {'hosted': 0, 'proxy': 0}
    async for doc in await db[ASSETS].aggregate(pipeline):
        counts['hosted' if doc['_id'] is True else 'proxy'] += int(doc['n'])
    return counts
