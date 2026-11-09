"""Maven components (DATA_MODEL ``components``, ``format: 'maven'``): one document per groupId:artifactId:version a
repository holds (cached by a proxy or deployed to a hosted repository), with ``version_sort`` in Maven order.

Written when an artifact file is stored (proxies: in the background after the fill; hosted: on deploy) and removed
when a hosted version loses its last file. Read by the hosted ``maven-metadata.xml`` regeneration (all versions of
an artifact) and the browse views. Queries use ``maven_gav_unique`` (``repo_id, namespace, name, version``) and
``repo_coordinates`` (``repo_id, namespace, name, version_sort``); file counts and sizes are ranges on the assets'
``repo_path_unique``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, COMPONENTS
from waystation_common.searchkeys import fold
from waystation_server.db.browse import subtree_end

FORMAT = 'maven'


def _key(repo_id: str, group_id: str, artifact_id: str, version: str) -> dict[str, Any]:
    return {'repo_id': repo_id, 'format': FORMAT, 'namespace': group_id, 'name': artifact_id, 'version': version}


async def upsert_version(
    db: Database,
    repo_id: str,
    group_id: str,
    artifact_id: str,
    version: str,
    *,
    version_sort: str,
    uploaded_by: str | None = None,
) -> None:
    now = datetime.now(UTC)
    update: dict[str, Any] = {
        '$set': {'version_sort': version_sort, 'updated_at': now, 'name_lc': fold(artifact_id)},
        '$setOnInsert': {'created_at': now},
    }
    if uploaded_by is not None:
        update['$set']['uploaded_by'] = uploaded_by
    for _ in range(2):  # two first writers of one version: the loser of the insert race updates
        try:
            await db[COMPONENTS].update_one(_key(repo_id, group_id, artifact_id, version), update, upsert=True)
            return
        except DuplicateKeyError:
            continue


async def remove_version(db: Database, repo_id: str, group_id: str, artifact_id: str, version: str) -> None:
    await db[COMPONENTS].delete_one(_key(repo_id, group_id, artifact_id, version))


async def versions(db: Database, repo_id: str, group_id: str, artifact_id: str) -> list[str]:
    """Every version of an artifact the repository holds."""
    cursor = db[COMPONENTS].find(
        {'repo_id': repo_id, 'format': FORMAT, 'namespace': group_id, 'name': artifact_id}, {'version': 1, '_id': 0}
    )
    return [str(doc['version']) async for doc in cursor]


async def has_files(db: Database, repo_id: str, prefix: str) -> bool:
    """Does any asset live under ``prefix`` (a folder, ending with ``/``)?"""
    doc = await db[ASSETS].find_one(
        {'repo_id': repo_id, 'path': {'$gte': prefix, '$lt': subtree_end(prefix)}}, {'_id': 1}
    )
    return doc is not None


async def files_under(db: Database, repo_id: str, prefix: str, *, limit: int = 10_000) -> list[dict[str, Any]]:
    """The assets directly or indirectly under ``prefix`` (path, size, times), in path order."""
    cursor = (
        db[ASSETS]
        .find(
            {'repo_id': repo_id, 'path': {'$gte': prefix, '$lt': subtree_end(prefix)}},
            {'path': 1, 'size': 1, 'created_at': 1, 'updated_at': 1, '_id': 0},
        )
        .sort('path', 1)
        .limit(limit)
    )
    return [doc async for doc in cursor]


# ---------------------------------------------------------------------------- browse
@dataclass(frozen=True)
class ArtifactRow:
    group_id: str
    artifact_id: str
    version_count: int
    latest_sort: str
    updated_at: datetime | None


async def artifact_page(
    db: Database, repo_id: str, *, after: tuple[str, str] | None, q: str | None, limit: int
) -> list[ArtifactRow]:
    """Artifacts (groupId, artifactId) in order after ``after``; ``q`` is a case-insensitive substring of
    ``groupId:artifactId``."""
    match: dict[str, Any] = {'repo_id': repo_id, 'format': FORMAT}
    if after is not None:
        match['$or'] = [
            {'namespace': {'$gt': after[0]}},
            {'namespace': after[0], 'name': {'$gt': after[1]}},
        ]
    pipeline: list[dict[str, Any]] = [{'$match': match}, {'$sort': {'namespace': 1, 'name': 1, 'version_sort': 1}}]
    if q:
        pattern = re.escape(q)
        pipeline.append(
            {
                '$match': {
                    '$or': [
                        {'namespace': {'$regex': pattern, '$options': 'i'}},
                        {'name': {'$regex': pattern, '$options': 'i'}},
                    ]
                }
            }
        )
    pipeline += [
        {
            '$group': {
                '_id': {'g': '$namespace', 'a': '$name'},
                'count': {'$sum': 1},
                'latest_sort': {'$last': '$version_sort'},
                'updated_at': {'$max': '$updated_at'},
            }
        },
        {'$sort': {'_id.g': 1, '_id.a': 1}},
        {'$limit': limit},
    ]
    out: list[ArtifactRow] = []
    async for doc in await db[COMPONENTS].aggregate(pipeline):
        out.append(
            ArtifactRow(
                group_id=str(doc['_id']['g']),
                artifact_id=str(doc['_id']['a']),
                version_count=int(doc['count']),
                latest_sort=str(doc['latest_sort']),
                updated_at=doc.get('updated_at'),
            )
        )
    return out


async def latest_versions(db: Database, repo_id: str, rows: Iterable[ArtifactRow]) -> dict[tuple[str, str], str]:
    """The version text of each row's highest ``version_sort``."""
    ors = [{'namespace': r.group_id, 'name': r.artifact_id, 'version_sort': r.latest_sort} for r in rows]
    if not ors:
        return {}
    cursor = db[COMPONENTS].find(
        {'repo_id': repo_id, 'format': FORMAT, '$or': ors}, {'namespace': 1, 'name': 1, 'version': 1, '_id': 0}
    )
    return {(str(d['namespace']), str(d['name'])): str(d['version']) async for d in cursor}


async def version_page(
    db: Database, repo_id: str, group_id: str, artifact_id: str, *, before: str | None, limit: int
) -> list[dict[str, Any]]:
    """An artifact's versions, highest first, below ``before`` (a ``version_sort``)."""
    query: dict[str, Any] = {'repo_id': repo_id, 'format': FORMAT, 'namespace': group_id, 'name': artifact_id}
    if before is not None:
        query['version_sort'] = {'$lt': before}
    cursor = db[COMPONENTS].find(query, {'_id': 0}).sort('version_sort', -1).limit(limit)
    return [doc async for doc in cursor]


@dataclass(frozen=True)
class FileStats:
    count: int
    size: int


async def file_stats(db: Database, repo_id: str, ga_path: str, versions: Iterable[str]) -> dict[str, FileStats]:
    """Files and bytes in each version folder ``<ga_path>/<version>/`` (one aggregation over their path ranges)."""
    wanted = sorted(set(versions))
    if not wanted:
        return {}
    ranges = [{'path': {'$gte': f'{ga_path}/{v}/', '$lt': subtree_end(f'{ga_path}/{v}/')}} for v in wanted]
    depth = ga_path.count('/') + 1  # index of the version segment in the split path
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id, '$or': ranges}},
        {
            '$group': {
                '_id': {'$arrayElemAt': [{'$split': ['$path', '/']}, depth]},
                'count': {'$sum': 1},
                'size': {'$sum': '$size'},
            }
        },
    ]
    out: dict[str, FileStats] = {}
    async for doc in await db[ASSETS].aggregate(pipeline):
        out[str(doc['_id'])] = FileStats(count=int(doc['count']), size=int(doc['size']))
    return out
