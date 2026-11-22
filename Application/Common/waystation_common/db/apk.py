"""Hosted apk repositories: their package records (``apk_packages`` with ``gen: 'hosted'``) and the published
``APKINDEX.tar.gz`` files (DATA_MODEL; FORMATS.md "Alpine apk").

Shared by the server (records an uploaded package and, in the same transaction, the ``index.apk`` outbox entry;
``waystation_server.db.apk``) and the worker (``index.apk``: builds every directory's signed index from the records
and publishes them). Hosted record fields, besides the listing fields of every ``apk_packages`` entry (``path``,
``dir`` = ``<branch>/<repository>/<arch>``, ``name``, ``version``, ``arch``, ``checksum`` (``C:``), ``size``,
``verified`` = true, ``version_sort``): ``stanza`` (the package's ``APKINDEX`` stanza, rendered at upload),
``uploaded_at``, ``uploaded_by``.
"""

from __future__ import annotations

from pymongo import ASCENDING

from waystation_common.db.client import Database, Document
from waystation_common.db.collections import APK_PACKAGES, ASSETS, REPOSITORIES
from waystation_common.db.published import Published, count_usage, get_asset, publish, remove_assets

__all__ = ['Published', 'count_usage', 'get_asset', 'publish', 'remove_assets']

HOSTED_GEN = 'hosted'
MAX_PACKAGES = 200_000  # per directory index


async def repository(db: Database, repo_id: str) -> Document | None:
    """The repository's ``name``, ``format`` and ``type`` (None when it was deleted)."""
    return await db[REPOSITORIES].find_one({'_id': repo_id}, {'name': 1, 'format': 1, 'type': 1})


async def hosted_dirs(db: Database, repo_id: str) -> list[str]:
    """Every directory that holds (or held) uploaded packages."""
    return sorted(str(d) for d in await db[APK_PACKAGES].distinct('dir', {'repo_id': repo_id, 'gen': HOSTED_GEN}))


async def published_dirs(db: Database, repo_id: str) -> list[str]:
    """Directories with a published ``APKINDEX.tar.gz`` (an emptied directory keeps an empty index)."""
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$regex': '/APKINDEX\\.tar\\.gz$'}}, {'path': 1})
    return [str(doc['path']).rsplit('/', 1)[0] async for doc in cursor]


async def hosted_records(db: Database, repo_id: str, directory: str) -> list[Document]:
    """A directory's package records whose file is still stored, by name and version; records of removed files
    (cleanup policies) are deleted."""
    cursor = (
        db[APK_PACKAGES]
        .find({'repo_id': repo_id, 'gen': HOSTED_GEN, 'dir': directory}, {'_id': 0})
        .sort([('name', ASCENDING), ('version_sort', ASCENDING), ('path', ASCENDING)])
        .limit(MAX_PACKAGES)
    )
    docs = [doc async for doc in cursor]
    paths = [str(d['path']) for d in docs]
    stored: set[str] = set()
    for start in range(0, len(paths), 1000):
        chunk = paths[start : start + 1000]
        async for asset in db[ASSETS].find({'repo_id': repo_id, 'path': {'$in': chunk}}, {'path': 1}):
            stored.add(str(asset['path']))
    gone = [p for p in paths if p not in stored]
    if gone:
        await db[APK_PACKAGES].delete_many({'repo_id': repo_id, 'gen': HOSTED_GEN, 'path': {'$in': gone}})
    return [d for d in docs if d['path'] in stored]
