"""Generic folder listing of a repository's cached assets (``GET /repositories/{name}/browse``), for every format.

The tree is the asset keys (``assets.path``) split on ``/``. A page lists folders first, then files; each group in
path byte order (that is name order, except that a folder sorts as ``<name>/``). Folders carry no size or child count
(``child_count`` is null: counting would mean scanning the subtree). The cursor is ``(phase, last key)``.

A path that has no assets under it answers 404 (a file path is not a folder).
"""

from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from waystation_common.errors import NotFound
from waystation_common.paths import normalise
from waystation_server.browse.models import FolderEntry, FolderListing, decode_cursor, encode_cursor
from waystation_server.db import browse as browse_db

if TYPE_CHECKING:
    from waystation_common.db.client import Database

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
    if phase == 'd':
        start = browse_db.subtree_end(f'{prefix}{after}/') if after is not None else None
        async for child in browse_db.children(db, repo_id, prefix, start=start):
            if child.kind != 'folder':
                continue
            if len(entries) == limit:
                more = True
                break
            entries.append(FolderEntry(name=child.name, path=prefix + child.name, kind='folder'))
        after = None
    if not more:
        start = prefix + after if (phase == 'f' and after is not None) else None
        async for child in browse_db.children(
            db, repo_id, prefix, start=start, inclusive=start is None, projection=FILE_FIELDS
        ):
            if child.kind != 'file':
                continue
            if len(entries) == limit:
                more = True
                break
            entries.append(_file(prefix, child.name, dict(child.doc)))
    if prefix and not entries and cursor is None:
        raise NotFound('folder not found')
    next_cursor = None
    if more:
        last = entries[-1]
        next_cursor = encode_cursor('d' if last.kind == 'folder' else 'f', last.name)
    return FolderListing(path=prefix.rstrip('/'), entries=entries, next_cursor=next_cursor)
