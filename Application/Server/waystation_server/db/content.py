"""Read queries over the component index (DATA_MODEL ``components``, ``downloads_daily``; T-S09) and digest
"where used": top artifacts for the stats API, download counters for the browse views.

Every query has a ``repo_id`` equality or ``$in`` and sorts on the second field of a ``repo_*`` index of
``components`` (a ``$in`` merges the per-repository index ranges), or is an equality on ``component_key_unique``,
``blob_sha256`` (assets) or ``repo_id`` + ``layers.digest`` / ``config_digest`` (docker manifests).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, BLOBS, COMPONENTS, DOCKER_MANIFESTS, DOWNLOADS_DAILY

TOP_SORTS = {'downloads': 'download_count', 'bytes': 'bytes_served', 'size': 'size_bytes', 'recent': 'created_at'}
COMPONENT_FIELDS = {
    'repo_id': 1,
    'format': 1,
    'namespace': 1,
    'name': 1,
    'version': 1,
    'display': 1,
    'size_bytes': 1,
    'download_count': 1,
    'bytes_served': 1,
    'last_downloaded_at': 1,
    'created_at': 1,
    'path': 1,
    'sha256': 1,
}
COUNTER_FIELDS = {'download_count': 1, 'last_downloaded_at': 1, '_id': 0}


def _repo_match(repo_ids: Sequence[str]) -> Any:
    return repo_ids[0] if len(repo_ids) == 1 else {'$in': list(repo_ids)}


async def top_components(db: Database, repo_ids: Sequence[str], *, by: str, limit: int) -> list[dict[str, Any]]:
    """The best ``limit`` components of ``repo_ids`` by ``by`` (``TOP_SORTS``); downloads and bytes skip the
    components never downloaded."""
    if not repo_ids:
        return []
    field = TOP_SORTS[by]
    query: dict[str, Any] = {'repo_id': _repo_match(repo_ids)}
    if by in ('downloads', 'bytes'):
        query[field] = {'$gt': 0}
    cursor = db[COMPONENTS].find(query, COMPONENT_FIELDS).sort([(field, -1), ('_id', 1)]).limit(limit)
    return await cursor.to_list()


async def top_downloaded_since(
    db: Database, repo_ids: Sequence[str], since: datetime, *, by: str, limit: int
) -> list[tuple[dict[str, Any], int, int]]:
    """Components with the most downloads (or bytes served) since ``since`` (whole UTC days), from
    ``downloads_daily``: (component, downloads, bytes)."""
    if not repo_ids:
        return []
    order = 'bytes' if by == 'bytes' else 'count'
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': _repo_match(repo_ids), 'day': {'$gte': since}}},
        {'$group': {'_id': '$component_id', 'count': {'$sum': '$count'}, 'bytes': {'$sum': '$bytes'}}},
        {'$sort': {order: -1, '_id': 1}},
        {'$limit': limit},
    ]
    rows = [doc async for doc in await db[DOWNLOADS_DAILY].aggregate(pipeline)]
    if not rows:
        return []
    found = {
        str(doc['_id']): doc
        async for doc in db[COMPONENTS].find({'_id': {'$in': [str(r['_id']) for r in rows]}}, COMPONENT_FIELDS)
    }
    return [(found[str(r['_id'])], int(r['count']), int(r['bytes'])) for r in rows if str(r['_id']) in found]


async def counters(
    db: Database, repo_id: str, fmt: str, coordinates: Iterable[tuple[str, str, str]]
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Download counters of the components (namespace, name, version) of one repository."""
    wanted = sorted(set(coordinates))
    if not wanted:
        return {}
    ors = [{'namespace': ns, 'name': name, 'version': version} for ns, name, version in wanted]
    cursor = db[COMPONENTS].find(
        {'repo_id': repo_id, 'format': fmt, '$or': ors},
        {'namespace': 1, 'name': 1, 'version': 1, 'download_count': 1, 'last_downloaded_at': 1, '_id': 0},
    )
    return {(str(d['namespace']), str(d['name']), str(d['version'])): d async for d in cursor}


async def name_counters(db: Database, repo_id: str, fmt: str, names: Iterable[str]) -> dict[str, tuple[int, Any]]:
    """Per component name (all versions or tags) of one repository: (downloads, last download)."""
    wanted = sorted(set(names))
    if not wanted:
        return {}
    pipeline: list[dict[str, Any]] = [
        {'$match': {'repo_id': repo_id, 'format': fmt, 'namespace': {'$gte': ''}, 'name': {'$in': wanted}}},
        {
            '$group': {
                '_id': '$name',
                'downloads': {'$sum': {'$ifNull': ['$download_count', 0]}},
                'last': {'$max': '$last_downloaded_at'},
            }
        },
    ]
    return {str(d['_id']): (int(d['downloads']), d.get('last')) async for d in await db[COMPONENTS].aggregate(pipeline)}


# ---------------------------------------------------------------------------- digest "where used"
async def blob_size(db: Database, sha256: str) -> int | None:
    doc = await db[BLOBS].find_one({'_id': sha256}, {'size': 1})
    return int(doc['size']) if doc and doc.get('size') is not None else None


async def assets_with_blob(db: Database, sha256: str, *, limit: int) -> list[dict[str, Any]]:
    cursor = db[ASSETS].find({'blob_sha256': sha256}, {'repo_id': 1, 'path': 1, '_id': 0}).limit(limit)
    return await cursor.to_list()


async def manifests_using(db: Database, repo_id: str, digest: str, *, limit: int) -> list[tuple[str, str, str]]:
    """Docker manifests of a repository referencing ``digest`` as a layer or config: (image, manifest digest, kind)."""
    out: list[tuple[str, str, str]] = []
    for field, kind in (('layers.digest', 'docker_layer'), ('config_digest', 'docker_config')):
        cursor = db[DOCKER_MANIFESTS].find({'repo_id': repo_id, field: digest}, {'image': 1, 'digest': 1, '_id': 0})
        async for doc in cursor.limit(limit):
            out.append((str(doc['image']), str(doc['digest']), kind))
    return out
