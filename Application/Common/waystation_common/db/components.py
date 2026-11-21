"""The component index (DATA_MODEL ``components``; T-S09), maintained by the worker's stats rollup.

- ``index_batch``: assets without ``component_id`` (new fills and uploads, content cached before the index existed,
  assets whose content was replaced) are linked to their component (``waystation_common.components``), creating it
  when needed; files that belong to no component get ``component_id: ''`` so they are not looked at again. The query
  is an equality on null over the ``component_id`` index.
- ``recompute``: a component's figures are sums over its assets (``component_id`` index): ``size_bytes``,
  ``asset_count``, ``download_count``, ``bytes_served`` (downloads times size), ``last_downloaded_at``. Docker tags take
  their size from the manifest records (the image, not the manifest file). A component without assets for
  ``EMPTY_GRACE`` is removed.
- ``sweep_batch``: a rolling pass over every component (cursor on ``_id``) that recomputes them, so deletes (cleanup,
  hosted deletes, repository deletes) and replaced files are reflected within hours without hooks in every writer.
- ``count_downloads``: the stats rollup's download flush adds the downloads of linked assets to their components and
  to ``downloads_daily`` (``_id`` = ``<component id>:<YYYYMMDD>``) in bulk.

Component ``_id`` values are ULID strings (migration ``0004_component_ids`` converted older Maven ones).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

from waystation_common.components import ComponentKey, docker_key, docker_ref, key_for
from waystation_common.db.client import Database
from waystation_common.db.collections import (
    ASSETS,
    COMPONENTS,
    DOCKER_MANIFESTS,
    DOCKER_TAGS,
    DOWNLOADS_DAILY,
    REPOSITORIES,
)
from waystation_common.ids import new_id
from waystation_common.searchkeys import fold

log = logging.getLogger('waystation.components')

INDEX_BATCH = 1000
SWEEP_BATCH = 1000
EMPTY_GRACE = timedelta(minutes=10)
NONE = ''  # ``component_id`` of a file that belongs to no component
ASSET_FIELDS = {'repo_id': 1, 'path': 1, 'kind': 1, 'blob_sha256': 1, 'created_at': 1}


@dataclass(frozen=True)
class IndexResult:
    assets: int = 0
    linked: int = 0
    components: int = 0


async def _formats(db: Database, repo_ids: Iterable[str]) -> dict[str, str]:
    wanted = sorted(set(repo_ids))
    cursor = db[REPOSITORIES].find({'_id': {'$in': wanted}}, {'format': 1})
    return {str(doc['_id']): str(doc.get('format') or '') async for doc in cursor}


async def _docker_tag(db: Database, repo_id: str, image: str, digest: str) -> str | None:
    """A tag pointing at ``digest`` (the first by name), for manifests fetched by digest."""
    doc = await db[DOCKER_TAGS].find_one(
        {'repo_id': repo_id, 'manifest_digest': digest, 'image': image}, {'tag': 1}, sort=[('tag', 1)]
    )
    return str(doc['tag']) if doc else None


async def _key_of(db: Database, fmt: str, asset: Mapping[str, Any]) -> ComponentKey | None:
    path = str(asset['path'])
    if fmt != 'docker':
        return key_for(fmt, path, content=asset.get('kind') == 'content')
    ref = docker_ref(path)
    if ref is None:
        return None
    image, reference, is_digest = ref
    if is_digest:
        tag = await _docker_tag(db, str(asset['repo_id']), image, reference)
        return docker_key(image, tag) if tag else None
    return docker_key(image, reference)


async def upsert(db: Database, repo_id: str, key: ComponentKey, asset: Mapping[str, Any]) -> str:
    """The id of ``key``'s component in ``repo_id``, created from ``asset`` (its first file) when missing."""
    now = datetime.now(UTC)
    query = {'repo_id': repo_id, 'format': key.format, 'namespace': key.namespace, 'name': key.name}
    query['version'] = key.version
    update = {
        '$set': {'version_sort': key.version_sort, 'display': key.display, 'name_lc': fold(key.name)},
        '$setOnInsert': {
            '_id': new_id(),
            'created_at': asset.get('created_at') or now,
            'updated_at': now,
            'path': asset.get('path'),
            'sha256': asset.get('blob_sha256'),
            'attributes': {},
        },
    }
    for _ in range(2):  # two first writers (the Maven plugin and the indexer): the loser updates
        try:
            doc = await db[COMPONENTS].find_one_and_update(
                query, update, upsert=True, projection={'_id': 1}, return_document=ReturnDocument.AFTER
            )
            if doc is not None:
                return str(doc['_id'])
        except DuplicateKeyError:
            continue
    doc = await db[COMPONENTS].find_one(query, {'_id': 1})
    if doc is None:  # pragma: no cover - the key was just written by the other writer
        raise RuntimeError('component vanished')
    return str(doc['_id'])


async def index_batch(db: Database, *, limit: int = INDEX_BATCH) -> IndexResult:
    """Link up to ``limit`` unindexed assets to their components."""
    assets = await db[ASSETS].find({'component_id': None}, ASSET_FIELDS).limit(limit).to_list()
    if not assets:
        return IndexResult()
    formats = await _formats(db, (str(a['repo_id']) for a in assets))
    ids: dict[tuple[str, ComponentKey], str] = {}
    ops: list[UpdateOne] = []
    linked = 0
    for asset in assets:
        repo_id = str(asset['repo_id'])
        fmt = formats.get(repo_id)
        key = await _key_of(db, fmt, asset) if fmt else None
        component_id = NONE
        if key is not None:
            found = ids.get((repo_id, key))
            if found is None:
                found = ids[(repo_id, key)] = await upsert(db, repo_id, key, asset)
            component_id = found
            linked += 1
        ops.append(UpdateOne({'_id': asset['_id'], 'component_id': None}, {'$set': {'component_id': component_id}}))
    await db[ASSETS].bulk_write(ops, ordered=False)
    touched = sorted(set(ids.values()))
    await recompute(db, touched)
    return IndexResult(assets=len(assets), linked=linked, components=len(touched))


async def _docker_sizes(db: Database, components: list[dict[str, Any]]) -> dict[str, int]:
    """Image size of docker tag components: the manifest's ``size_total`` (an index: the sum of its entries')."""
    out: dict[str, int] = {}
    for comp in components:
        repo_id, image, tag = comp['repo_id'], comp['name'], comp['version']
        found = await db[DOCKER_TAGS].find_one({'repo_id': repo_id, 'image': image, 'tag': tag}, {'manifest_digest': 1})
        if found is None:
            continue
        manifest = await db[DOCKER_MANIFESTS].find_one(
            {'repo_id': repo_id, 'image': image, 'digest': found['manifest_digest']},
            {'size_total': 1, 'is_index': 1, 'manifests.digest': 1},
        )
        if manifest is None:
            continue
        if manifest.get('is_index'):
            children = [e['digest'] for e in manifest.get('manifests') or []]
            total = 0
            async for child in db[DOCKER_MANIFESTS].find(
                {'repo_id': repo_id, 'image': image, 'digest': {'$in': children}}, {'size_total': 1}
            ):
                total += int(child.get('size_total') or 0)
            out[str(comp['_id'])] = total
        else:
            out[str(comp['_id'])] = int(manifest.get('size_total') or 0)
    return out


async def recompute(db: Database, component_ids: Iterable[str], *, now: datetime | None = None) -> tuple[int, int]:
    """Recompute the figures of ``component_ids`` from their assets; remove the ones left without assets (after
    ``EMPTY_GRACE``). Returns (updated, removed)."""
    ids = sorted(set(component_ids))
    if not ids:
        return 0, 0
    now = now or datetime.now(UTC)
    totals: dict[str, dict[str, Any]] = {}
    pipeline: list[dict[str, Any]] = [
        {'$match': {'component_id': {'$in': ids}}},
        {
            '$group': {
                '_id': '$component_id',
                'size': {'$sum': '$size'},
                'count': {'$sum': 1},
                'downloads': {'$sum': {'$ifNull': ['$download_count', 0]}},
                'served': {'$sum': {'$multiply': [{'$ifNull': ['$download_count', 0]}, '$size']}},
                'last': {'$max': '$last_downloaded_at'},
            }
        },
    ]
    async for doc in await db[ASSETS].aggregate(pipeline):
        totals[str(doc['_id'])] = doc
    components = (
        await db[COMPONENTS]
        .find({'_id': {'$in': ids}}, {'format': 1, 'repo_id': 1, 'name': 1, 'version': 1, 'updated_at': 1})
        .to_list()
    )
    docker = await _docker_sizes(db, [c for c in components if c.get('format') == 'docker' and str(c['_id']) in totals])
    ops: list[UpdateOne] = []
    removed: list[str] = []
    for comp in components:
        cid = str(comp['_id'])
        found = totals.get(cid)
        if found is None:
            updated = comp.get('updated_at')
            if not isinstance(updated, datetime) or updated.replace(tzinfo=UTC) < now - EMPTY_GRACE:
                removed.append(cid)
            continue
        ops.append(
            UpdateOne(
                {'_id': cid},
                {
                    '$set': {
                        'size_bytes': docker.get(cid, int(found['size'])),
                        'asset_count': int(found['count']),
                        'download_count': int(found['downloads']),
                        'bytes_served': int(found['served']),
                        'last_downloaded_at': found.get('last'),
                        'updated_at': now,
                    }
                },
            )
        )
    if ops:
        await db[COMPONENTS].bulk_write(ops, ordered=False)
    if removed:
        await db[COMPONENTS].delete_many({'_id': {'$in': removed}})
    return len(ops), len(removed)


async def sweep_batch(db: Database, after: str | None, *, limit: int = SWEEP_BATCH) -> tuple[str | None, int, int]:
    """Recompute the next ``limit`` components after ``after`` (by ``_id``); returns (cursor for the next call,
    None at the end of a pass; updated; removed)."""
    query: dict[str, Any] = {'_id': {'$gt': after}} if after else {}
    docs = await db[COMPONENTS].find(query, {'_id': 1}).sort('_id', 1).limit(limit).to_list()
    if not docs:
        return None, 0, 0
    ids = [str(d['_id']) for d in docs]
    updated, removed = await recompute(db, ids)
    return (ids[-1] if len(ids) == limit else None), updated, removed


async def count_downloads(db: Database, downloads: Mapping[str, tuple[int, datetime | None]]) -> int:
    """Add asset downloads (asset id → (count, last download time)) to their components and ``downloads_daily``.
    Assets not linked yet are skipped: the indexer's recompute adds their counts when it links them."""
    wanted = [asset_id for asset_id, (n, _) in downloads.items() if n > 0]
    if not wanted:
        return 0
    comp_inc: dict[str, list[Any]] = defaultdict(lambda: [0, 0, None, ''])
    daily: dict[tuple[str, str], list[Any]] = defaultdict(lambda: [0, 0, ''])
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    cursor = db[ASSETS].find({'_id': {'$in': wanted}}, {'component_id': 1, 'size': 1, 'repo_id': 1})
    async for asset in cursor:
        cid = asset.get('component_id')
        if not cid:
            continue
        n, when = downloads[str(asset['_id'])]
        size = int(asset.get('size') or 0)
        entry = comp_inc[str(cid)]
        entry[0] += n
        entry[1] += n * size
        if when is not None and (entry[2] is None or when > entry[2]):
            entry[2] = when
        day = (when or today).replace(hour=0, minute=0, second=0, microsecond=0)
        bucket = daily[(str(cid), day.strftime('%Y%m%d'))]
        bucket[0] += n
        bucket[1] += n * size
        bucket[2] = str(asset['repo_id'])
    if not comp_inc:
        return 0
    ops = []
    for cid, (n, served, when, _) in comp_inc.items():
        update: dict[str, Any] = {'$inc': {'download_count': n, 'bytes_served': served}}
        if when is not None:
            update['$max'] = {'last_downloaded_at': when}
        ops.append(UpdateOne({'_id': cid}, update))
    await db[COMPONENTS].bulk_write(ops, ordered=False)
    day_ops = [
        UpdateOne(
            {'_id': f'{cid}:{day}'},
            {
                '$inc': {'count': n, 'bytes': served},
                '$setOnInsert': {
                    'component_id': cid,
                    'repo_id': repo_id,
                    'day': datetime.strptime(day, '%Y%m%d').replace(tzinfo=UTC),
                },
            },
            upsert=True,
        )
        for (cid, day), (n, served, repo_id) in daily.items()
    ]
    await db[DOWNLOADS_DAILY].bulk_write(day_ops, ordered=False)
    return len(ops)


__all__ = [
    'EMPTY_GRACE',
    'NONE',
    'IndexResult',
    'count_downloads',
    'index_batch',
    'recompute',
    'sweep_batch',
    'upsert',
]
