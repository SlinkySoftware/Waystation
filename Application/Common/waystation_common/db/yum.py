"""Hosted yum repositories: their package records (``yum_packages`` with ``gen: 'hosted'``) and the published
repodata assets (DATA_MODEL; FORMATS.md "yum / dnf").

Shared by the server (records an uploaded package and, in the same transaction, the ``index.yum`` outbox entry;
``waystation_server.db.yum``) and the worker (``index.yum``: builds the repository's repodata from the records and
publishes it). Hosted record fields, besides the listing fields of every ``yum_packages`` entry (``path``,
``algorithm`` = ``sha256``, ``digest``, ``size``, ``verified`` = true, ``repo_dir`` = ``''``, ``name``, ``epoch``,
``version``, ``release``, ``arch``, ``version_sort``): ``summary``, ``uploaded_at``, ``uploaded_by`` and ``meta``
(the package's ``primary``/``filelists``/``other`` XML entries, JSON, gzip-compressed, BSON binary).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from pymongo import ASCENDING

from waystation_common.db.client import Database, Document
from waystation_common.db.collections import ASSETS, REPOSITORIES, YUM_PACKAGES
from waystation_common.db.published import Published, count_usage, get_asset, publish, remove_assets

__all__ = ['Published', 'count_usage', 'get_asset', 'publish', 'remove_assets']

HOSTED_GEN = 'hosted'
REPODATA_PREFIX = 'repodata/'
BATCH = 500


async def repository(db: Database, repo_id: str) -> Document | None:
    """The repository's ``name``, ``format`` and ``type`` (None when it was deleted)."""
    return await db[REPOSITORIES].find_one({'_id': repo_id}, {'name': 1, 'format': 1, 'type': 1})


async def hosted_packages(db: Database, repo_id: str) -> AsyncIterator[list[Document]]:
    """A hosted repository's package records in batches, by name, EVR, architecture and path."""
    cursor = (
        db[YUM_PACKAGES]
        .find({'repo_id': repo_id, 'gen': HOSTED_GEN}, {'_id': 0})
        .sort([('name', ASCENDING), ('version_sort', ASCENDING), ('arch', ASCENDING), ('path', ASCENDING)])
        .batch_size(BATCH)
    )
    batch: list[Document] = []
    async for doc in cursor:
        batch.append(doc)
        if len(batch) >= BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


async def drop_missing(db: Database, repo_id: str, docs: list[Document]) -> list[Document]:
    """``docs`` whose package file is still stored; records of removed files (cleanup policies) are deleted."""
    paths = [str(d['path']) for d in docs]
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$in': paths}}, {'path': 1})
    stored = {str(a['path']) async for a in cursor}
    gone = [d['path'] for d in docs if d['path'] not in stored]
    if gone:
        await db[YUM_PACKAGES].delete_many({'repo_id': repo_id, 'gen': HOSTED_GEN, 'path': {'$in': gone}})
    return [d for d in docs if d['path'] in stored]


async def repodata_paths(db: Database, repo_id: str) -> list[str]:
    """Every asset path under ``repodata/``."""
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$regex': '^repodata/'}}, {'path': 1})
    return [str(doc['path']) async for doc in cursor]
