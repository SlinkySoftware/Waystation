"""Prefetch persistence (T-O12; DATA_MODEL ``repositories.proxy.prefetch``, ``job_definitions`` ``prefetch:<id>``).

Shared by the server (repository writes keep the schedule in step, the repository page reads the last run) and the
worker (the jobs select what to refresh and read the warm list).

* A proxy repository with a warm list and a ``proxy.prefetch.schedule`` has the ``job_definitions`` entry
  ``prefetch:<repo id>`` (kind ``prefetch.warm``), kept in step by ``sync_schedule``; its runs (scheduled and "run
  now") share the dedupe key ``prefetch:<repo id>``.
* ``popular_metadata``: a repository's most downloaded metadata assets that were downloaded recently (index
  ``prefetch_popular``, partial on metadata assets with downloads).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database, Document
from waystation_common.db.collections import ASSETS, JOB_DEFINITIONS, JOB_RUNS, REPOSITORIES

WARM_KIND = 'prefetch.warm'
REFRESH_KIND = 'prefetch.refresh'
MAX_TOP_N = 1000


def definition_id(repo_id: str) -> str:
    return f'prefetch:{repo_id}'


def dedupe_key(repo_id: str) -> str:
    return f'prefetch:{repo_id}'


def _settings(repo: Document) -> dict[str, Any]:
    if repo.get('type') != 'proxy':
        return {}
    proxy = repo.get('proxy') or {}
    prefetch = proxy.get('prefetch') or {}
    return prefetch if isinstance(prefetch, dict) else {}


async def sync_schedule(db: Database, repo: Document) -> None:
    """Create, update or remove the repository's warm schedule (``prefetch:<repo id>``). Present only for a proxy
    repository with warm items and a schedule; a changed schedule restarts its timer."""
    repo_id = str(repo['_id'])
    def_id = definition_id(repo_id)
    prefetch = _settings(repo)
    schedule = prefetch.get('schedule')
    if not schedule or not prefetch.get('warm'):
        await db[JOB_DEFINITIONS].delete_one({'_id': def_id})
        return
    existing = await db[JOB_DEFINITIONS].find_one({'_id': def_id})
    fields: dict[str, Any] = {
        'kind': WARM_KIND,
        'params': {'repo_id': repo_id},
        'schedule': schedule,
        'queue': None,
        'enabled': True,
    }
    if existing is None or existing.get('schedule') != schedule:
        fields['next_run_at'] = None
    update: dict[str, Any] = {'$set': fields}
    if existing is None:
        update['$setOnInsert'] = {'last_fired_at': None}
    try:
        await db[JOB_DEFINITIONS].update_one({'_id': def_id}, update, upsert=True)
    except DuplicateKeyError:  # pragma: no cover - concurrent first save of the same repository
        await db[JOB_DEFINITIONS].update_one({'_id': def_id}, {'$set': fields})


async def refresh_repositories(db: Database, repo_id: str | None = None) -> list[Document]:
    """Proxy repositories with ``proxy.prefetch.refresh_popular`` (one by id, or all; a small collection)."""
    query: dict[str, Any] = {'type': 'proxy', 'proxy.prefetch.refresh_popular': True}
    if repo_id is not None:
        query['_id'] = repo_id
    return [doc async for doc in db[REPOSITORIES].find(query).sort('name', 1)]


async def repository(db: Database, repo_id: str) -> Document | None:
    return await db[REPOSITORIES].find_one({'_id': repo_id})


async def drop_schedule(db: Database, repo_id: str) -> None:
    await db[JOB_DEFINITIONS].delete_one({'_id': definition_id(repo_id)})


async def schedule(db: Database, repo_id: str) -> Document | None:
    return await db[JOB_DEFINITIONS].find_one({'_id': definition_id(repo_id)})


async def last_run(db: Database, repo_id: str) -> Document | None:
    """The newest warm run of the repository (scheduled or run now), without its log (index ``dedupe_started``)."""
    return await db[JOB_RUNS].find_one({'dedupe_key': dedupe_key(repo_id)}, {'log': 0}, sort=[('started_at', -1)])


async def popular_metadata(db: Database, repo_id: str, *, since: datetime, limit: int) -> list[Document]:
    """The repository's metadata assets downloaded at or after ``since``, most downloaded first, at most ``limit``:
    ``path``, ``download_count``, ``last_downloaded_at``, ``upstream.fetched_at`` (index ``prefetch_popular``: the
    partial filter is repeated so the planner can use it)."""
    query = {
        'repo_id': repo_id,
        'kind': 'metadata',
        'download_count': {'$gt': 0},
        'last_downloaded_at': {'$gte': since},
    }
    projection = {'_id': 0, 'path': 1, 'download_count': 1, 'last_downloaded_at': 1, 'upstream.fetched_at': 1}
    cursor = (
        db[ASSETS]
        .find(query, projection)
        .sort([('download_count', -1)])
        .hint('prefetch_popular')
        .limit(max(1, min(limit, MAX_TOP_N)))
    )
    return [doc async for doc in cursor]
