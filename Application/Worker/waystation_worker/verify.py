"""Blob verify (SPECIFICATION §8.5): the ``blob.verify`` job, run after a restore and weekly.

Pages through ``assets`` and checks each referenced blob file once per run:

* **missing**: no file under ``blobs/``;
* **damaged**: the file's size differs from the asset's and from the ``blobs`` record (or, with ``deep``, its sha256
  differs from its name). With ``repair`` (the default) the damaged file is moved into ``uploads/`` (where blob GC
  removes it as an orphan after a day), so the next request for a proxied asset re-fetches it instead of serving
  bad bytes.

Assets whose blob is missing or damaged get ``assets.blob_missing`` (``waystation_common.db.verify``): proxy assets
are re-fetched from the upstream on their next request (a missing blob file is a cache miss for the proxy engine,
whose fill clears the mark); hosted assets are **lost** and are reported (job log lines, the run's result, a
``storage.blobs_missing`` activity event and the ``waystation_assets_blob_missing{repository_type}`` gauge) until
they are uploaded again or restored. Marks whose blob is back are cleared.

A file whose size matches the ``blobs`` record but not the asset is an inconsistent asset record, not a damaged
blob: it is counted (``inconsistent``) and logged, never moved.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from waystation_common.db import verify as verify_db
from waystation_common.db.client import Database, Document
from waystation_common.errors import ValidationFailed
from waystation_common.storage import BlobStore

log = logging.getLogger('waystation.worker.verify')

PAGE = 1000
LOGGED_LOSSES = 50  # hosted losses listed in the job log one by one; the rest are counted
CHUNK = 1024 * 1024

Progress = Callable[[int, int | None, str | None], Awaitable[None]]
LogLine = Callable[[str], Awaitable[None]]

MISSING = 'missing'
DAMAGED = 'damaged'


@dataclass
class VerifyResult:
    assets: int = 0  # assets checked
    blobs: int = 0  # distinct blob files checked
    missing_blobs: int = 0
    damaged_blobs: int = 0
    moved_blobs: int = 0  # damaged files moved out of the way (repair)
    inconsistent: int = 0  # asset size differs from a file that matches its blobs record
    proxy_marked: int = 0  # proxy assets newly marked for re-fetch
    hosted_lost: int = 0  # hosted assets whose blob is missing or damaged (all of them, not only new ones)
    cleared: int = 0  # marks removed because the blob is back
    lost_repositories: dict[str, int] = field(default_factory=dict)  # hosted repository name -> lost assets

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def summary(self) -> str:
        return (
            f'checked {self.assets} assets ({self.blobs} blobs): {self.missing_blobs} blobs missing, '
            f'{self.damaged_blobs} damaged; {self.proxy_marked} proxied assets marked for re-fetch, '
            f'{self.hosted_lost} hosted assets lost, {self.cleared} marks cleared'
        )


def _hash_sync(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open('rb') as fh:
            while chunk := fh.read(CHUNK):
                digest.update(chunk)
    except FileNotFoundError:
        return None
    return digest.hexdigest()


def _sizes_sync(store: BlobStore, sha256s: list[str]) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for sha in sha256s:
        try:
            out[sha] = store.blob_path(sha).stat().st_size
        except FileNotFoundError, ValidationFailed:  # no file, or not a sha256 at all
            out[sha] = None
    return out


def _move_sync(store: BlobStore, sha256: str, expected: set[int]) -> bool:
    """Move a damaged blob file into ``uploads/`` if it is still damaged (a fill may have replaced it meanwhile)."""
    path = store.blob_path(sha256)
    try:
        if path.stat().st_size in expected:
            return False
    except FileNotFoundError:
        return False
    store.uploads_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(path, store.uploads_dir / f'{uuid.uuid4().hex}.damaged')
    except FileNotFoundError:
        return False
    return True


class _Checker:
    """Checks each blob once per run (a blob several assets share is stat'ed, and hashed, once)."""

    def __init__(self, db: Database, store: BlobStore, *, deep: bool, repair: bool, result: VerifyResult) -> None:
        self.db = db
        self.store = store
        self.deep = deep
        self.repair = repair
        self.result = result
        self.state: dict[str, str | None] = {}  # sha256 -> None (fine) / MISSING / DAMAGED

    async def check(self, docs: list[Document]) -> None:
        sizes: dict[str, set[int]] = {}
        for doc in docs:
            sha = str(doc.get('blob_sha256') or '')
            if sha and sha not in self.state:
                sizes.setdefault(sha, set()).add(int(doc.get('size') or 0))
        if not sizes:
            return
        stats = await asyncio.to_thread(_sizes_sync, self.store, list(sizes))
        recorded = await verify_db.blob_sizes(self.db, list(sizes))
        for sha, asset_sizes in sizes.items():
            self.result.blobs += 1
            size = stats[sha]
            if size is None:
                self.state[sha] = MISSING
                self.result.missing_blobs += 1
                continue
            expected = {recorded[sha]} if sha in recorded else asset_sizes
            damaged = size not in expected
            if not damaged and size not in asset_sizes:
                self.result.inconsistent += 1
                log.warning('asset size differs from its blob', extra={'sha256': sha, 'size': size})
            if not damaged and self.deep:
                damaged = await asyncio.to_thread(_hash_sync, self.store.blob_path(sha)) not in (sha, None)
            if not damaged:
                self.state[sha] = None
                continue
            self.state[sha] = DAMAGED
            self.result.damaged_blobs += 1
            if self.repair and await asyncio.to_thread(_move_sync, self.store, sha, set() if self.deep else expected):
                self.result.moved_blobs += 1
            log.warning('damaged blob', extra={'sha256': sha, 'size': size, 'moved': self.repair})


async def verify(
    db: Database,
    store: BlobStore,
    *,
    deep: bool = False,
    repair: bool = True,
    now: datetime | None = None,
    progress: Progress | None = None,
    log_line: LogLine | None = None,
) -> VerifyResult:
    now = now or datetime.now(UTC)
    result = VerifyResult()
    repos = await verify_db.repository_types(db)
    checker = _Checker(db, store, deep=deep, repair=repair, result=result)
    total = await db['assets'].estimated_document_count()
    logged = 0
    after: str | None = None
    while True:
        docs = await verify_db.assets_page(db, after=after, limit=PAGE)
        if not docs:
            break
        after = str(docs[-1]['_id'])
        result.assets += len(docs)
        await checker.check(docs)
        marks: list[tuple[str, str, bool]] = []
        clear: list[str] = []
        for doc in docs:
            state = checker.state.get(str(doc.get('blob_sha256') or ''))
            if state is None:
                if doc.get(verify_db.MARK) is not None:
                    clear.append(str(doc['_id']))
                continue
            name, kind = repos.get(str(doc.get('repo_id')), (str(doc.get('repo_id')), ''))
            hosted = kind == 'hosted'
            marks.append((str(doc['_id']), state, hosted))
            if hosted:
                result.hosted_lost += 1
                result.lost_repositories[name] = result.lost_repositories.get(name, 0) + 1
                if log_line is not None and logged < LOGGED_LOSSES:
                    logged += 1
                    await log_line(f'lost: {name}/{doc.get("path")} (blob {state})')
        marked_before = {str(d['_id']) for d in docs if d.get(verify_db.MARK) is not None}
        new_marks = [m for m in marks if not m[2] and m[0] not in marked_before]
        _, cleared = await verify_db.apply_marks(db, marks, clear, now=now)
        result.proxy_marked += len(new_marks)
        result.cleared += cleared
        if progress is not None:
            await progress(result.assets, max(total, result.assets), 'checking blobs')
    if log_line is not None and result.hosted_lost > logged:
        await log_line(f'... and {result.hosted_lost - logged} more lost hosted assets')
    if result.missing_blobs or result.damaged_blobs:
        log.warning('blob verify found missing or damaged blobs', extra=result.as_dict())
    return result
