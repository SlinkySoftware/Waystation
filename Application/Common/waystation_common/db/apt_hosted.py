"""Hosted apt repositories: their package records (``apt_hosted_packages``), the browse listing of the published
indexes (``apt_packages``) and the published ``dists/`` files (DATA_MODEL; FORMATS.md "apt").

Shared by the server (records an uploaded ``.deb`` and, in the same transaction, the ``index.apt`` outbox entry;
deletes likewise) and the worker (``index.apt``: renders ``Packages``/``Release`` from the records, signs and
publishes them). Record fields: ``repo_id``, ``path`` (the pool path, ``pool/<component>/<prefix>/<name>/<file>``),
``component``, ``name``, ``version``, ``version_sort`` (dpkg order), ``architecture`` (``all`` = every architecture),
``section``, ``sha256``, ``size``, ``stanza`` (the package's ``Packages`` paragraph, rendered at upload, with
``Filename``/``Size``/hashes), ``uploaded_at``, ``uploaded_by``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pymongo import ASCENDING, UpdateOne
from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database, Document, Session
from waystation_common.db.collections import APT_HOSTED, APT_PACKAGES, ASSETS, REPOSITORIES
from waystation_common.errors import Conflict
from waystation_common.searchkeys import name_keys

BATCH = 500
DISTS_PREFIX = 'dists/'
_ORDER = [('name', ASCENDING), ('version_sort', ASCENDING), ('architecture', ASCENDING), ('path', ASCENDING)]


# ---------------------------------------------------------------------------- records (server)
async def by_path(db: Database, repo_id: str, path: str) -> Document | None:
    return await db[APT_HOSTED].find_one({'repo_id': repo_id, 'path': path}, {'_id': 0, 'stanza': 0})


async def by_package(db: Database, repo_id: str, name: str, version: str, architecture: str) -> Document | None:
    """The record of ``name`` ``version`` for ``architecture`` (``repo_package_unique``)."""
    query = {'repo_id': repo_id, 'name': name, 'version': version, 'architecture': architecture}
    return await db[APT_HOSTED].find_one(query, {'_id': 0, 'stanza': 0})


async def save(db: Database, repo_id: str, record: dict[str, Any], *, session: Session) -> None:
    """Insert or replace the record at ``record['path']``; ``Conflict`` when the same package version and
    architecture is stored at another path (``repo_package_unique``: a concurrent upload)."""
    try:
        await db[APT_HOSTED].replace_one(
            {'repo_id': repo_id, 'path': record['path']}, {'repo_id': repo_id, **record}, upsert=True, session=session
        )
    except DuplicateKeyError:
        raise Conflict(f'{record["name"]} {record["version"]} ({record["architecture"]}) is already stored') from None


async def delete(db: Database, repo_id: str, path: str, *, session: Session) -> None:
    await db[APT_HOSTED].delete_one({'repo_id': repo_id, 'path': path}, session=session)


# ---------------------------------------------------------------------------- index generation (worker)
async def repository(db: Database, repo_id: str) -> Document | None:
    """The repository's ``name``, ``format``, ``type`` and ``format_options`` (None when it was deleted)."""
    return await db[REPOSITORIES].find_one({'_id': repo_id}, {'name': 1, 'format': 1, 'type': 1, 'format_options': 1})


async def components(db: Database, repo_id: str) -> list[str]:
    """The components that hold at least one record."""
    return sorted(str(c) for c in await db[APT_HOSTED].distinct('component', {'repo_id': repo_id}))


async def architectures(db: Database, repo_id: str) -> list[str]:
    """The architectures of the stored packages (``all`` included)."""
    return sorted(str(a) for a in await db[APT_HOSTED].distinct('architecture', {'repo_id': repo_id}))


async def records(db: Database, repo_id: str, component: str) -> AsyncIterator[list[Document]]:
    """A component's records in batches, by name, dpkg version, architecture and path (``repo_component_order``)."""
    cursor = (
        db[APT_HOSTED].find({'repo_id': repo_id, 'component': component}, {'_id': 0}).sort(_ORDER).batch_size(BATCH)
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
    gone = [p for p in paths if p not in stored]
    if gone:
        await db[APT_HOSTED].delete_many({'repo_id': repo_id, 'path': {'$in': gone}})
    return [d for d in docs if d['path'] in stored]


async def dists_paths(db: Database, repo_id: str) -> list[str]:
    """Every asset path under ``dists/``."""
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$regex': '^dists/'}}, {'path': 1})
    return [str(doc['path']) async for doc in cursor]


@dataclass(frozen=True)
class Listed:
    """One package of a published ``Packages`` index (browse)."""

    filename: str
    sha256: str
    size: int
    name: str
    version: str
    architecture: str
    section: str | None


async def record_listing(
    db: Database,
    repo_id: str,
    entries: Sequence[Listed],
    *,
    suite: str,
    component: str,
    arch: str,
    gen: str,
    now: datetime,
) -> None:
    """Make ``entries`` the browse listing (``apt_packages``) of the published index ``(suite, component, arch)``,
    as the apt proxy does for a cached ``Packages`` file: upsert them, drop the previous generation's entries."""
    listing = {'repo_id': repo_id, 'suite': suite, 'component': component, 'arch': arch}
    ops = [
        UpdateOne(
            {**listing, 'filename': e.filename},
            {
                '$set': {
                    'name': e.name,
                    **name_keys(e.name),
                    'version': e.version,
                    'architecture': e.architecture,
                    'section': e.section or None,
                    'sha256': e.sha256,
                    'size': e.size,
                    'gen': gen,
                    'updated_at': now,
                }
            },
            upsert=True,
        )
        for e in entries
    ]
    for start in range(0, len(ops), BATCH):
        await db[APT_PACKAGES].bulk_write(ops[start : start + BATCH], ordered=False)
    await db[APT_PACKAGES].delete_many({**listing, 'gen': {'$ne': gen}})


async def drop_listings(
    db: Database, repo_id: str, *, suite: str, components: Sequence[str], archs: Sequence[str]
) -> None:
    """Remove browse listings of indexes that are no longer published (another distribution, a component or
    architecture that went away)."""
    await db[APT_PACKAGES].delete_many(
        {
            'repo_id': repo_id,
            '$or': [
                {'suite': {'$ne': suite}},
                {'component': {'$nin': list(components)}},
                {'arch': {'$nin': list(archs)}},
            ],
        }
    )
