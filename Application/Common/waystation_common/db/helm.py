"""Helm chart records (``helm_charts``, DATA_MODEL §1) and the published ``index.yaml`` of hosted Helm repositories.

Shared by the server (records chart versions when a chart is uploaded and when a proxy caches an ``index.yaml``;
browse) and the worker (``index.helm``: regenerates a hosted repository's index from its records). Document fields:

- ``repo_id``, ``name``, ``version``, ``version_sort`` (semver precedence as a sortable string), ``path`` (the chart
  file's path in the repository, also the relative URL the served index lists), ``digest`` (sha256 hex of the chart
  file, or null when a proxied index gives none), ``created`` (the index entry's time), ``deprecated``,
  ``app_version``, ``description``, ``icon`` (browse summary);
- hosted: ``entry`` (the chart's ``Chart.yaml`` fields, as listed in the index), ``size``, ``uploaded_by``;
- proxy: ``gen`` (sha256 of the cached index blob the entry came from; a new generation replaces the listing).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo import DESCENDING, ReturnDocument

from waystation_common.db import usage as usage_db
from waystation_common.db.client import Database, Document
from waystation_common.db.collections import ASSETS, HELM_CHARTS, REPOSITORIES
from waystation_common.ids import new_id

INDEX_PATH = 'index.yaml'
INDEX_TYPE = 'application/x-yaml'


async def repository(db: Database, repo_id: str) -> Document | None:
    """The repository's ``name``, ``format`` and ``type`` (None when it was deleted)."""
    return await db[REPOSITORIES].find_one({'_id': repo_id}, {'name': 1, 'format': 1, 'type': 1})


async def hosted_entries(db: Database, repo_id: str) -> list[Document]:
    """Every chart version of a hosted repository: by name, newest version first (index ``repo_name_sort``)."""
    cursor = db[HELM_CHARTS].find({'repo_id': repo_id}, {'_id': 0}).sort([('name', 1), ('version_sort', DESCENDING)])
    return [doc async for doc in cursor]


async def publish_index(db: Database, repo_id: str, *, sha256: str, size: int, now: datetime) -> None:
    """Make the committed blob ``sha256`` the repository's ``index.yaml`` (one atomic document replace; clients see
    the old or the new index, never a mix). The repository's usage follows the size change."""
    fields: dict[str, Any] = {
        'repo_id': repo_id,
        'path': INDEX_PATH,
        'kind': 'metadata',
        'blob_sha256': sha256,
        'size': size,
        'content_type': INDEX_TYPE,
        'checksums': {'sha256': sha256},
        'headers': {},
        'upstream': {'url': '', 'etag': None, 'last_modified': None, 'fetched_at': now, 'status': 201},
        'updated_at': now,
    }
    before = await db[ASSETS].find_one_and_update(
        {'repo_id': repo_id, 'path': INDEX_PATH},
        {'$set': fields, '$setOnInsert': {'_id': new_id(), 'created_at': now}},
        upsert=True,
        return_document=ReturnDocument.BEFORE,
        projection={'size': 1},
    )
    if before is None:
        await usage_db.add(db, repo_id, size, 1)
    else:
        await usage_db.add(db, repo_id, size - int(before.get('size') or 0))
