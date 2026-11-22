"""Generic folder listing of a repository's cached assets (``GET /repositories/{name}/browse``), for every format.

The tree is the asset keys (``assets.path``) split on ``/``. A page lists folders first, then files; each group in
path byte order (that is name order, except that a folder sorts as ``<name>/``). Folders carry no size; their
``child_count`` (direct children: files and sub-folders) is counted up to ``CHILD_COUNT_CAP`` and null beyond: one
read of at most ``CHILD_COUNT_SPAN`` paths covers the small folders of a page, the others are walked one by one (one
index seek per sub-folder at most, ``CHILD_COUNT_CONCURRENCY`` at a time), all of them together within
``CHILD_COUNT_READS`` index reads; a folder whose count did not fit is null too (T-P2-PERF: a root of big folders
with many sub-folders each cost one read per sub-folder, about a thousand per page).
The cursor is ``(phase, last key)``.

A path that has no assets under it answers 404 (a file path is not a folder).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from waystation_common.errors import NotFound
from waystation_common.paths import normalise
from waystation_server.browse.models import FolderEntry, FolderListing, decode_cursor, encode_cursor
from waystation_server.db import browse as browse_db

if TYPE_CHECKING:
    from waystation_common.db.client import Database

CHILD_COUNT_CAP = 100
CHILD_COUNT_CONCURRENCY = 8
CHILD_COUNT_SPAN = 2048  # paths read at once to count the children of small folders
CHILD_COUNT_READS = 64  # index reads the per-folder walks of one page may spend together
FILE_FIELDS: dict[str, int] = {
    'path': 1,
    'size': 1,
    'content_type': 1,
    'blob_sha256': 1,
    'upstream.last_modified': 1,
    'updated_at': 1,
    'last_downloaded_at': 1,
    'download_count': 1,
    '_id': 0,
}


def folder_prefix(path: str) -> str:
    """'' for the root, else the normalised path with a trailing slash (raises ``PathRejected``)."""
    clean = normalise(path.strip('/'), allow_empty=True)
    return f'{clean}/' if clean else ''


def _last_modified(doc: dict[str, Any]) -> datetime | None:
    raw = (doc.get('upstream') or {}).get('last_modified')
    if isinstance(raw, str):
        try:
            return parsedate_to_datetime(raw)
        except TypeError, ValueError:
            pass
    updated = doc.get('updated_at')
    return updated if isinstance(updated, datetime) else None


def _file(prefix: str, name: str, doc: dict[str, Any]) -> FolderEntry:
    return FolderEntry(
        name=name,
        path=prefix + name,
        kind='file',
        size=doc.get('size'),
        content_type=doc.get('content_type'),
        sha256=doc.get('blob_sha256'),
        last_modified=_last_modified(doc),
        last_downloaded_at=doc.get('last_downloaded_at'),
        download_count=doc.get('download_count'),
    )


async def list_folder(db: Database, repo_id: str, path: str, *, cursor: str | None, limit: int) -> FolderListing:
    prefix = folder_prefix(path)
    phase, after = decode_cursor(cursor, str, str) if cursor else ('d', None)
    if phase not in ('d', 'f'):
        phase = 'd'
    entries: list[FolderEntry] = []
    more = False
    if phase == 'd' and after is None:
        # First page: one walk collects the folders and the first files (the walk passes them anyway).
        files: list[FolderEntry] = []
        async for child in browse_db.children(db, repo_id, prefix, projection=FILE_FIELDS):
            if child.kind == 'folder':
                if len(entries) == limit:
                    more = True
                    break
                entries.append(FolderEntry(name=child.name, path=prefix + child.name, kind='folder'))
            elif len(files) <= limit:
                files.append(_file(prefix, child.name, dict(child.doc)))
        if not more:
            room = limit - len(entries)
            more = len(files) > room
            entries.extend(files[:room])
    else:
        more = await _later_page(db, repo_id, prefix, phase, after, limit, entries)
    if prefix and not entries and cursor is None:
        raise NotFound('folder not found')
    await _child_counts(db, repo_id, prefix, entries)
    next_cursor = None
    if more:
        last = entries[-1]
        next_cursor = encode_cursor('d' if last.kind == 'folder' else 'f', last.name)
    return FolderListing(path=prefix.rstrip('/'), entries=entries, next_cursor=next_cursor)


async def _child_counts(db: Database, repo_id: str, prefix: str, entries: list[FolderEntry]) -> None:
    """``child_count`` of the page's folders: one bounded read over the range they span counts the small ones; each
    folder that did not fit is walked on its own, up to ``CHILD_COUNT_CAP`` children, the walks sharing
    ``CHILD_COUNT_READS`` reads."""
    folders = [e for e in entries if e.kind == 'folder']
    if not folders:
        return
    small = await browse_db.span_children(db, repo_id, prefix, [e.name for e in folders], limit=CHILD_COUNT_SPAN)
    gate = asyncio.Semaphore(CHILD_COUNT_CONCURRENCY)
    budget = browse_db.ReadBudget(CHILD_COUNT_READS)

    async def count(entry: FolderEntry) -> None:
        async with gate:
            entry.child_count = await browse_db.count_children(
                db, repo_id, entry.path + '/', cap=CHILD_COUNT_CAP, budget=budget
            )

    rest = []
    for entry in folders:
        found = small.get(entry.name)
        if found is None:
            rest.append(count(entry))
        else:
            entry.child_count = len(found) if len(found) <= CHILD_COUNT_CAP else None
    await asyncio.gather(*rest)


async def _later_page(
    db: Database, repo_id: str, prefix: str, phase: str, after: str | None, limit: int, entries: list[FolderEntry]
) -> bool:
    """A page after the first: the folders after ``after`` (phase 'd'), then the files from the start of the folder
    or after the file ``after`` (phase 'f'). Appends to ``entries``; True when there is more."""
    if phase == 'd':
        start = browse_db.subtree_end(f'{prefix}{after}/') if after is not None else None
        async for child in browse_db.children(db, repo_id, prefix, start=start):
            if child.kind != 'folder':
                continue
            if len(entries) == limit:
                return True
            entries.append(FolderEntry(name=child.name, path=prefix + child.name, kind='folder'))
        after = None
    start = prefix + after if (phase == 'f' and after is not None) else None
    async for child in browse_db.children(
        db, repo_id, prefix, start=start, inclusive=start is None, projection=FILE_FIELDS
    ):
        if child.kind != 'file':
            continue
        if len(entries) == limit:
            return True
        entries.append(_file(prefix, child.name, dict(child.doc)))
    return False
