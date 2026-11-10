"""``assets`` queries for browsing (generic folder tree and per-format "is it cached" lookups).

Every query is a range or ``$in`` on ``repo_path_unique`` (``repo_id``, ``path``).

The folder walk (``children``) lists the direct children of a folder without scanning its subtrees: it reads asset
paths in index order in batches, emits each sub-folder at its first path (a subtree is one contiguous range:
``<folder>/`` < ``<folder>0`` in byte order) and, when a batch ends inside a sub-folder, seeks past the rest of its
subtree. Batches grow while they hold several children (small subtrees: many sub-folders per query) and shrink back
when most of a batch was inside sub-folders already emitted (big subtrees: one seek per sub-folder). Cost: at most
one index seek per sub-folder plus the files themselves, whatever the subtree sizes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS

FIRST_BATCH = 16
MAX_BATCH = 1024
PATH_ONLY: dict[str, int] = {'path': 1, '_id': 0}


@dataclass(frozen=True)
class Child:
    kind: Literal['folder', 'file']
    name: str
    doc: Mapping[str, Any]  # the asset (files) or the first asset found inside the folder (folders)


def subtree_end(prefix: str) -> str:
    """The smallest path after every path starting with ``prefix`` (which ends with ``/``)."""
    return prefix[:-1] + '0'


async def children(
    db: Database,
    repo_id: str,
    prefix: str,
    *,
    start: str | None = None,
    inclusive: bool = True,
    projection: Mapping[str, Any] = PATH_ONLY,
) -> AsyncIterator[Child]:
    """Direct children of the folder ``prefix`` ('' = root, else ending with ``/``) in path order, from ``start``
    (default: the beginning)."""
    lo = start if start is not None else prefix
    op = '$gte' if inclusive else '$gt'
    hi = subtree_end(prefix) if prefix else None
    batch = FIRST_BATCH
    while True:
        bounds: dict[str, str] = {op: lo}
        if hi is not None:
            bounds['$lt'] = hi
        docs = (
            await db[ASSETS]
            .find({'repo_id': repo_id, 'path': bounds}, dict(projection))
            .sort('path', 1)
            .limit(batch)
            .to_list()
        )
        folder: str | None = None  # the sub-folder the current document is in (its subtree is contiguous)
        inside = 0  # documents of the batch inside an already emitted sub-folder (read for nothing)
        for doc in docs:
            name, sep, _ = str(doc['path'])[len(prefix) :].partition('/')
            if sep:
                if name == folder:
                    inside += 1
                    continue
                folder = name
                yield Child('folder', name, doc)
                continue
            folder = None
            yield Child('file', name, doc)
            lo, op = str(doc['path']), '$gt'
        if len(docs) < batch:
            return
        if folder is not None:  # the batch ended inside a sub-folder: seek past the rest of its subtree
            lo, op = subtree_end(f'{prefix}{folder}/'), '$gte'
        # Big subtrees: back to small batches (one seek per sub-folder); small ones: several children per batch.
        batch = FIRST_BATCH if inside * 2 > len(docs) else min(batch * 2, MAX_BATCH)


async def by_paths(
    db: Database, repo_id: str, paths: Iterable[str], projection: Mapping[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """The assets at ``paths`` (a page's worth), by path."""
    wanted = sorted(set(paths))
    if not wanted:
        return {}
    fields = dict(projection) if projection is not None else {'path': 1, 'size': 1, 'blob_sha256': 1, '_id': 0}
    fields['path'] = 1
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$in': wanted}}, fields)
    return {str(doc['path']): doc async for doc in cursor}


async def in_folder(
    db: Database, repo_id: str, prefix: str, projection: Mapping[str, Any] | None = None, *, limit: int = 100_000
) -> list[dict[str, Any]]:
    """Every asset under ``prefix`` (ending with ``/``), bounded."""
    fields = dict(projection) if projection is not None else {'path': 1, 'size': 1, 'blob_sha256': 1, '_id': 0}
    cursor = (
        db[ASSETS]
        .find({'repo_id': repo_id, 'path': {'$gte': prefix, '$lt': subtree_end(prefix)}}, fields)
        .sort('path', 1)
        .limit(limit)
    )
    return await cursor.to_list()


async def matching(
    db: Database, repo_id: str, prefix: str, pattern: str, projection: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Assets under ``prefix`` ('' = everywhere) whose path matches ``pattern`` (a regular expression anchored by
    the caller; evaluated on index keys, so only the matches are fetched)."""
    bounds: dict[str, str] = {'$regex': pattern}
    if prefix:
        bounds |= {'$gte': prefix, '$lt': subtree_end(prefix)}
    fields = dict(projection) if projection is not None else {'_id': 0}
    return await db[ASSETS].find({'repo_id': repo_id, 'path': bounds}, fields).sort('path', 1).to_list()
