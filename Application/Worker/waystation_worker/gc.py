"""Blob garbage collection (SPECIFICATION §4.8, ARCHITECTURE §5): the ``blob.gc`` job.

One run, in order:

1. **Orphan uploads**: files in ``uploads/`` untouched for ``upload_age_s`` (crashed fills and uploads). Live fills
   write continuously (and their lease expires within seconds when they stop), so a day-old file has no owner.
2. **Adopt**: blob files without a ``blobs`` document (written before claims existed, or by a crashed writer) get
   one, so they are collected like any other.
3. **Mark and sweep** over ``blobs`` documents, in ``_id`` order:
   * referenced by an asset: clear any mark;
   * unreferenced and unmarked: mark (``gc_marked_at = now``);
   * unreferenced, marked *and* unclaimed for at least ``grace_s``: compare-and-set to ``deleting``
     (``waystation_common.db.blobs.begin_delete``), re-check references, unlink, drop the document.

Safety (property-tested in ``tests/test_gc_integration.py``, including a cache fill racing the sweep): a blob is never
deleted while an asset references it, while a fill is about to reference it (its commit's claim either makes the
compare-and-set miss or waits for the deletion to finish and writes the file again), or within the grace period of
its last reference or claim. In-progress fills and uploads live in ``uploads/``, never under ``blobs/``.

Data plane: nginx may be sending a blob (``X-Accel-Redirect``, ``sendfile``) while GC unlinks it. On POSIX the open
file stays readable until closed, so that response completes. A response is only ever started for a blob that an
asset referenced moments before, and such a blob is not deleted for the grace period (24 h by default).

Referenced blobs whose file is missing are counted (``missing``) and logged; proxy repositories re-fetch them on
the next request (the engine treats a missing blob as a miss).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from waystation_common.db import blobs as blobs_db
from waystation_common.db.client import Database
from waystation_common.storage import BlobStore

log = logging.getLogger('waystation.worker.gc')

PAGE = 1000
ABANDON_S = 60.0  # a deletion slower than this (since its compare-and-set) is abandoned, not completed

Progress = Callable[[int, int | None, str | None], Awaitable[None]]


@dataclass
class GcResult:
    scanned: int = 0
    adopted: int = 0
    marked: int = 0
    unmarked: int = 0
    freed_blobs: int = 0
    freed_bytes: int = 0
    dropped: int = 0  # documents of blobs whose file was already gone
    missing: int = 0  # referenced blobs without a file
    uploads_removed: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


async def adopt_files(db: Database, store: BlobStore) -> int:
    adopted = 0
    for prefix in await store.prefixes():
        files = await store.scan(prefix)
        for start in range(0, len(files), PAGE):
            batch = files[start : start + PAGE]
            known = await blobs_db.known(db, [f.sha256 for f in batch])
            for f in batch:
                if f.sha256 not in known and await blobs_db.adopt(
                    db, f.sha256, f.size, datetime.fromtimestamp(f.mtime, UTC)
                ):
                    adopted += 1
    return adopted


async def sweep_one(db: Database, store: BlobStore, sha256: str, *, cutoff: datetime) -> tuple[str, int]:
    """Delete one marked blob if it is still eligible: (``freed`` | ``dropped`` (file already gone) | ``kept``,
    bytes freed)."""
    started = time.monotonic()
    if not await blobs_db.begin_delete(db, sha256, cutoff=cutoff, now=datetime.now(UTC)):
        return 'kept', 0  # claimed or unmarked meanwhile
    try:
        if await blobs_db.is_referenced(db, sha256) or time.monotonic() - started > ABANDON_S:
            await blobs_db.abort_delete(db, sha256)
            return 'kept', 0
        stat = await store.stat(sha256)
        removed = await store.delete(sha256)
        await blobs_db.finish_delete(db, sha256)
    except BaseException:
        await blobs_db.abort_delete(db, sha256)
        raise
    if not removed:
        return 'dropped', 0
    return 'freed', stat.size if stat is not None else 0


async def collect(
    db: Database,
    store: BlobStore,
    *,
    grace_s: float,
    upload_age_s: float,
    now: datetime | None = None,
    progress: Progress | None = None,
) -> GcResult:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=grace_s)
    result = GcResult()
    result.uploads_removed = await store.sweep_uploads(upload_age_s)
    result.adopted = await adopt_files(db, store)
    total = await db['blobs'].estimated_document_count()
    after: str | None = None
    while True:
        docs = await blobs_db.page(db, after=after, limit=PAGE)
        if not docs:
            break
        after = str(docs[-1]['_id'])
        result.scanned += len(docs)
        live = [d for d in docs if d.get('gc_state') is None]
        refs = await blobs_db.referenced(db, [str(d['_id']) for d in live])
        result.unmarked += await blobs_db.unmark(db, [str(d['_id']) for d in live if d['_id'] in refs])
        present = await store.existing([str(d['_id']) for d in live if d['_id'] in refs])
        result.missing += sum(1 for d in live if d['_id'] in refs and d['_id'] not in present)
        for doc in live:
            sha = str(doc['_id'])
            if sha in refs:
                continue
            marked_at = doc.get('gc_marked_at')
            if marked_at is None:
                result.marked += int(await blobs_db.mark(db, sha, now))
                continue
            claimed_at = doc.get('claimed_at')
            if _aware(marked_at) > cutoff or (claimed_at is not None and _aware(claimed_at) > cutoff):
                continue
            outcome, freed = await sweep_one(db, store, sha, cutoff=cutoff)
            if outcome == 'freed':
                result.freed_blobs += 1
                result.freed_bytes += freed
            elif outcome == 'dropped':
                result.dropped += 1
        if progress is not None:
            await progress(result.scanned, max(total, result.scanned), 'mark and sweep')
    if result.missing:
        log.warning('referenced blobs are missing from the blob store', extra={'missing': result.missing})
    return result


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
