"""apt proxy queries of the server (DATA_MODEL ``apt_pool_index``, ``apt_packages``; FORMATS.md "apt").

The pool index and listing writes (``record_listing``, ``record_unverified``, ``drop_cached``, ``record_packages``,
``index_packages``) are shared with the worker's ``apt.refresh`` job and live in ``waystation_common.db.apt``; they
are re-exported here for the apt proxy.

``known_suites`` tells the apt proxy which suite directories (``dists/bookworm/mongodb-org/8.0``) have a cached
``Release`` (``assets``, ``repo_path_unique``), so an index path is split at the right suite.

``apt_packages`` (browse) holds one document per stanza of every cached ``Packages`` index: ``(suite, component,
arch)`` identify the index, ``name``/``version``/``architecture``/``section`` come from the stanza. Each index's
documents are replaced when a new generation of it is indexed (``gen`` = the index blob's sha256). Pages are read
through ``repo_suite_name``; "is it cached" joins ``assets`` (``repo_path_unique``) the other way round through
``filename_repo``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from waystation_common.db.apt import (
    Listing,
    PoolRecord,
    Stanza,
    drop_cached,
    index_packages,
    lookup,
    record_listing,
    record_packages,
    record_unverified,
)
from waystation_common.db.client import Database
from waystation_common.db.collections import APT_PACKAGES, ASSETS
from waystation_common.db.retry import read_retry

__all__ = [
    'PACKAGE_SORT',
    'Listing',
    'PoolRecord',
    'Stanza',
    'cached_counts',
    'cached_package_page',
    'drop_cached',
    'index_packages',
    'known_suites',
    'lookup',
    'package_page',
    'record_listing',
    'record_packages',
    'record_unverified',
    'suite_count',
]


@read_retry  # on the request path of every index request with a choice of suite
async def known_suites(db: Database, repo_id: str, suite_dirs: Sequence[str]) -> set[str]:
    """The directories among ``suite_dirs`` whose ``InRelease`` or ``Release`` is cached (``repo_path_unique``)."""
    paths = [f'{d}/{name}' for d in suite_dirs for name in ('InRelease', 'Release')]
    if not paths:
        return set()
    cursor = db[ASSETS].find({'repo_id': repo_id, 'path': {'$in': paths}}, {'path': 1, '_id': 0})
    return {str(doc['path']).rpartition('/')[0] async for doc in cursor}


# ---------------------------------------------------------------------------- apt_packages (browse)
PACKAGE_SORT: tuple[str, ...] = ('name', 'filename', 'arch', 'component')


def _after(values: Sequence[Any]) -> dict[str, Any]:
    """Keyset condition: strictly after ``values`` in ``PACKAGE_SORT`` order."""
    branches = []
    for i, key in enumerate(PACKAGE_SORT):
        branch: dict[str, Any] = {PACKAGE_SORT[j]: values[j] for j in range(i)}
        branch[key] = {'$gt': values[i]}
        branches.append(branch)
    return {'$or': branches}


def _listing_filter(
    repo_id: str, suite: str, component: str | None, arch: str | None, name_regex: str | None
) -> dict[str, Any]:
    query: dict[str, Any] = {'repo_id': repo_id, 'suite': suite}
    if arch is not None:
        query['arch'] = arch
    if component is not None:
        query['component'] = component
    if name_regex is not None:
        query['name'] = {'$regex': name_regex, '$options': 'i'}
    return query


async def package_page(
    db: Database,
    repo_id: str,
    *,
    suite: str,
    component: str | None,
    arch: str | None,
    name_regex: str | None,
    after: Sequence[Any] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """A page of a suite's listing, by ``PACKAGE_SORT``."""
    query = _listing_filter(repo_id, suite, component, arch, name_regex)
    if after is not None:
        query = {'$and': [query, _after(after)]}
    return await db[APT_PACKAGES].find(query, {'_id': 0}).sort([(k, 1) for k in PACKAGE_SORT]).limit(limit).to_list()


async def cached_package_page(
    db: Database,
    repo_id: str,
    *,
    suite: str,
    component: str | None,
    arch: str | None,
    name_regex: str | None,
    after: Sequence[Any] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Like ``package_page`` but only entries whose file is cached: walks the repository's assets (what is cached is
    usually far less than what is listed) and joins their listings."""
    listing = _listing_filter(repo_id, suite, component, arch, None)
    match: dict[str, Any] = {}
    if name_regex is not None:
        match['name'] = {'$regex': name_regex, '$options': 'i'}
    if after is not None:
        match = {'$and': [match, _after(after)]} if match else _after(after)
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id}},
        {'$project': {'path': 1, '_id': 0}},
        {
            '$lookup': {
                'from': APT_PACKAGES,
                'localField': 'path',
                'foreignField': 'filename',
                'pipeline': [{'$match': listing}, {'$project': {'_id': 0}}],
                'as': 'listed',
            }
        },
        {'$unwind': '$listed'},
        {'$replaceRoot': {'newRoot': '$listed'}},
    ]
    if match:
        pipeline.append({'$match': match})
    pipeline += [{'$sort': dict.fromkeys(PACKAGE_SORT, 1)}, {'$limit': limit}]
    return [doc async for doc in await db[ASSETS].aggregate(pipeline, allowDiskUse=True)]


async def suite_count(db: Database, repo_id: str, suite: str) -> int:
    return await db[APT_PACKAGES].count_documents({'repo_id': repo_id, 'suite': suite})


async def cached_counts(db: Database, repo_id: str) -> dict[str, int]:
    """Listing entries whose file is cached, per suite."""
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id}},
        {'$project': {'path': 1, '_id': 0}},
        {
            '$lookup': {
                'from': APT_PACKAGES,
                'localField': 'path',
                'foreignField': 'filename',
                'pipeline': [{'$match': {'repo_id': repo_id}}, {'$project': {'suite': 1, '_id': 0}}],
                'as': 'listed',
            }
        },
        {'$unwind': '$listed'},
        {'$group': {'_id': '$listed.suite', 'count': {'$sum': 1}}},
    ]
    return {str(doc['_id']): int(doc['count']) async for doc in await db[ASSETS].aggregate(pipeline)}
