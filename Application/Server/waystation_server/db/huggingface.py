"""Hugging Face records (DATA_MODEL ``huggingface_repos``; FORMATS.md "Hugging Face").

``huggingface_repos``: one summary per Hub repository (model or dataset) a proxy has served, ``(repo_id, name)``
unique, ``name`` = ``<id>`` for models and ``datasets/<id>`` for datasets; ``kind``, ``hf_id``, ``latest`` (the
commit the default branch resolved to last), ``refs`` (``revision``, ``commit``, ``resolved_at``: the revisions
clients asked for and where they pointed when last resolved) and the search keys. Written when an info document is
cached and when a file is.

Cached files are the ``assets`` under ``[datasets/]<id>/resolve/<commit>/`` (ranges on ``repo_path_unique``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, HUGGINGFACE_REPOS
from waystation_common.huggingface import Kind, commit_prefix, display_name
from waystation_common.searchkeys import name_keys

MAX_REFS = 200
MAX_COMMITS = 1000
FILE_FIELDS: dict[str, int] = {
    'path': 1,
    'size': 1,
    'blob_sha256': 1,
    'created_at': 1,
    'last_downloaded_at': 1,
    'download_count': 1,
    '_id': 0,
}


def _range(prefix: str) -> dict[str, str]:
    return {'$gte': prefix, '$lt': prefix[:-1] + '0'}  # '/' + 1 = '0': the subtree of prefix


def _base(kind: Kind, hf_id: str, now: datetime) -> dict[str, Any]:
    name = display_name(kind, hf_id)
    return {'kind': kind, 'hf_id': hf_id, **name_keys(name), 'updated_at': now}


async def record_revision(
    db: Database, repo_id: str, kind: Kind, hf_id: str, revision: str | None, commit: str
) -> None:
    """A revision resolved to ``commit`` (None: the default branch, which also sets ``latest``)."""
    now = datetime.now(UTC)
    fields: dict[str, Any] = {**_base(kind, hf_id, now), 'created_at': {'$ifNull': ['$created_at', now]}}
    if revision is None:
        fields['latest'] = commit
    else:
        if revision == 'main':
            fields['latest'] = commit
        others = {'$filter': {'input': {'$ifNull': ['$refs', []]}, 'cond': {'$ne': ['$$this.revision', revision]}}}
        entry = {'revision': revision, 'commit': commit, 'resolved_at': now}
        fields['refs'] = {'$slice': [{'$concatArrays': [[{'$literal': entry}], others]}, MAX_REFS]}
    fields['name_sfx'] = {'$literal': fields['name_sfx']}
    await db[HUGGINGFACE_REPOS].update_one(
        {'repo_id': repo_id, 'name': display_name(kind, hf_id)}, [{'$set': fields}], upsert=True
    )


async def touch(db: Database, repo_id: str, kind: Kind, hf_id: str) -> None:
    """A file of the repository was cached."""
    now = datetime.now(UTC)
    await db[HUGGINGFACE_REPOS].update_one(
        {'repo_id': repo_id, 'name': display_name(kind, hf_id)},
        {'$set': _base(kind, hf_id, now), '$setOnInsert': {'created_at': now}},
        upsert=True,
    )


async def get(db: Database, repo_id: str, name: str) -> dict[str, Any] | None:
    return await db[HUGGINGFACE_REPOS].find_one({'repo_id': repo_id, 'name': name}, {'_id': 0, 'name_sfx': 0})


async def page(db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` summaries by name after ``after``; ``q``: a case-insensitive substring."""
    query: dict[str, Any] = {'repo_id': repo_id}
    if after is not None:
        query['name'] = {'$gt': after}
    if q:
        query['name_lc'] = {'$regex': re.escape(q.lower())}
    return await db[HUGGINGFACE_REPOS].find(query, {'_id': 0, 'name_sfx': 0}).sort('name', 1).limit(limit).to_list()


async def model_stats(db: Database, repo_id: str, models: list[tuple[Kind, str]]) -> dict[str, dict[str, Any]]:
    """Cached files per Hub repository (display name): ``files``, ``size``, ``downloads``, ``last_downloaded_at``."""
    out: dict[str, dict[str, Any]] = {}
    for kind, hf_id in models:
        prefix = commit_prefix(kind, hf_id)
        pipeline: list[dict[str, Any]] = [
            {'$match': {'repo_id': repo_id, 'path': _range(prefix)}},
            {
                '$group': {
                    '_id': None,
                    'files': {'$sum': 1},
                    'size': {'$sum': '$size'},
                    'downloads': {'$sum': {'$ifNull': ['$download_count', 0]}},
                    'last_downloaded_at': {'$max': '$last_downloaded_at'},
                }
            },
        ]
        async for doc in await db[ASSETS].aggregate(pipeline):
            out[display_name(kind, hf_id)] = doc
    return out


async def commits(db: Database, repo_id: str, kind: Kind, hf_id: str) -> list[dict[str, Any]]:
    """Commits with cached files, most recently cached first: ``commit``, ``files``, ``size``, ``cached_at``
    (first file), ``updated_at`` (last file), ``downloads``, ``last_downloaded_at``."""
    prefix = commit_prefix(kind, hf_id)
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id, 'path': _range(prefix)}},
        {
            '$group': {
                '_id': {'$substrCP': ['$path', len(prefix), 40]},
                'files': {'$sum': 1},
                'size': {'$sum': '$size'},
                'cached_at': {'$min': '$created_at'},
                'updated_at': {'$max': '$created_at'},
                'downloads': {'$sum': {'$ifNull': ['$download_count', 0]}},
                'last_downloaded_at': {'$max': '$last_downloaded_at'},
            }
        },
        {'$sort': {'updated_at': -1}},
        {'$limit': MAX_COMMITS},
    ]
    return [{**doc, 'commit': doc.pop('_id')} async for doc in await db[ASSETS].aggregate(pipeline)]


async def files(
    db: Database, repo_id: str, kind: Kind, hf_id: str, commit: str, *, offset: int, limit: int
) -> list[dict[str, Any]]:
    """A commit's cached files by path."""
    prefix = commit_prefix(kind, hf_id, commit)
    cursor = (
        db[ASSETS]
        .find({'repo_id': repo_id, 'path': _range(prefix)}, FILE_FIELDS)
        .sort('path', 1)
        .skip(offset)
        .limit(limit)
    )
    return await cursor.to_list()
