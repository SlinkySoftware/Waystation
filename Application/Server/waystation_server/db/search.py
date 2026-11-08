"""Global search queries (SPECIFICATION §4.6; ``waystation_server.search`` builds pages from them).

A **stream** is one search key (``name_lc`` or ``base_lc``, ``waystation_common.searchkeys``) of one collection,
read in ``(key, repo_id, _id)`` order straight from the ``search_name`` / ``search_base`` index: the prefix range and
the repositories the principal may browse are index bounds, the keyset position is a filter on index keys, so a page
never sorts in memory and never scans more than its range. ``base_lc`` streams skip documents whose ``name_lc``
already matches (each document is found once, also in facet counts).

Facet counts group the same range by repository (``$limit`` first: counting stops at ``FACET_CAP`` per stream).
Digest lookups use ``assets.blob_sha256`` and ``docker_tags.repo_manifest_digest``; per-page enrichment (docker image
sizes, "is this listed package cached") uses the unique keys of ``docker_manifests`` and ``assets``.

Document ids are ObjectIds (format indexes) or strings (assets); ``id_token`` / ``parse_id_token`` carry them in
cursors.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from bson import ObjectId
from bson.errors import InvalidId

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, DOCKER_MANIFESTS, DOCKER_TAGS
from waystation_common.errors import ValidationFailed
from waystation_common.searchkeys import prefix_range

FACET_CAP = 10_000
Key = Literal['name_lc', 'base_lc']
INDEX: dict[str, str] = {'name_lc': 'search_name', 'base_lc': 'search_base'}


@dataclass(frozen=True)
class Stream:
    collection: str
    key: Key
    prefix: str  # folded
    repo_ids: tuple[str, ...]
    projection: Mapping[str, int]
    version_field: str | None = None
    version_prefix: str | None = None


@dataclass(frozen=True)
class Start:
    """Where a page starts in a stream: after ``(key, repo_id, id)``, or at/after ``key`` alone (``id`` None)."""

    key: str
    inclusive: bool = False
    repo_id: str | None = None
    id: Any = None


def id_token(value: Any) -> str:
    return f'o{value}' if isinstance(value, ObjectId) else f's{value}'


def parse_id_token(token: str) -> Any:
    if token.startswith('s'):
        return token[1:]
    if token.startswith('o'):
        try:
            return ObjectId(token[1:])
        except InvalidId:
            pass
    raise ValidationFailed('invalid cursor')


def _range(prefix: str) -> dict[str, str]:
    lo, hi = prefix_range(prefix)
    return {'$gte': lo, '$lt': hi} if hi is not None else {'$gte': lo}


def _filter(stream: Stream) -> dict[str, Any]:
    rng = _range(stream.prefix)
    query: dict[str, Any] = {stream.key: rng, 'repo_id': {'$in': list(stream.repo_ids)}}
    if stream.key == 'base_lc' and stream.prefix:
        query['name_lc'] = {'$not': rng}
    if stream.version_field and stream.version_prefix:
        query[stream.version_field] = {'$regex': '^' + re.escape(stream.version_prefix)}
    return query


async def stream_page(db: Database, stream: Stream, *, start: Start | None, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` documents of ``stream`` from ``start``, in ``(key, repo_id, _id)`` order."""
    if not stream.repo_ids:
        return []
    query = _filter(stream)
    if start is not None:
        key = stream.key
        bound = {'$gte': start.key} if start.inclusive or start.id is not None else {'$gt': start.key}
        clauses: list[dict[str, Any]] = [query, {key: bound}]
        if start.id is not None:
            clauses.append(
                {
                    '$or': [
                        {key: {'$gt': start.key}},
                        {key: start.key, 'repo_id': {'$gt': start.repo_id}},
                        {key: start.key, 'repo_id': start.repo_id, '_id': {'$gt': start.id}},
                    ]
                }
            )
        query = {'$and': clauses}
    projection = {**stream.projection, stream.key: 1, 'repo_id': 1, '_id': 1}
    cursor = (
        db[stream.collection]
        .find(query, projection)
        .sort([(stream.key, 1), ('repo_id', 1), ('_id', 1)])
        .hint(INDEX[stream.key])
        .limit(limit)
    )
    return await cursor.to_list()


async def stream_counts(db: Database, stream: Stream) -> tuple[dict[str, int], bool]:
    """Matches per repository (at most ``FACET_CAP`` counted) and whether the cap was reached."""
    if not stream.repo_ids:
        return {}, False
    pipeline: list[dict[str, Any]] = [
        {'$match': _filter(stream)},
        {'$limit': FACET_CAP},
        {'$group': {'_id': '$repo_id', 'n': {'$sum': 1}}},
    ]
    counts = {
        str(doc['_id']): int(doc['n'])
        async for doc in await db[stream.collection].aggregate(pipeline, hint=INDEX[stream.key])
    }
    return counts, sum(counts.values()) >= FACET_CAP


# ---------------------------------------------------------------------------- digests
async def assets_by_digest(
    db: Database, hex_prefix: str, repo_ids: Sequence[str], projection: Mapping[str, int], *, limit: int
) -> list[dict[str, Any]]:
    """Cached files whose blob sha256 starts with ``hex_prefix`` (lower-case hex)."""
    if not repo_ids:
        return []
    query = {'blob_sha256': _range(hex_prefix), 'repo_id': {'$in': list(repo_ids)}}
    cursor = db[ASSETS].find(query, {**projection, 'repo_id': 1}).hint('blob_sha256').limit(limit)
    return await cursor.to_list()


async def tags_by_digest(
    db: Database, digest_prefix: str, repo_ids: Sequence[str], projection: Mapping[str, int], *, limit: int
) -> list[dict[str, Any]]:
    """Docker tags whose manifest digest starts with ``digest_prefix`` (``sha256:<hex>``)."""
    if not repo_ids:
        return []
    query = {'repo_id': {'$in': list(repo_ids)}, 'manifest_digest': _range(digest_prefix)}
    cursor = db[DOCKER_TAGS].find(query, {**projection, 'repo_id': 1}).hint('repo_manifest_digest').limit(limit)
    return await cursor.to_list()


# ---------------------------------------------------------------------------- per-page enrichment
async def manifest_sizes(db: Database, refs: Iterable[tuple[str, str, str]]) -> dict[tuple[str, str, str], int]:
    """``size_total`` of the manifests ``(repo_id, image, digest)``."""
    wanted = sorted(set(refs))
    if not wanted:
        return {}
    query = {'$or': [{'repo_id': r, 'image': i, 'digest': d} for r, i, d in wanted]}
    projection = {'repo_id': 1, 'image': 1, 'digest': 1, 'size_total': 1, '_id': 0}
    out: dict[tuple[str, str, str], int] = {}
    async for doc in db[DOCKER_MANIFESTS].find(query, projection):
        size = doc.get('size_total')
        if isinstance(size, int):
            out[(str(doc['repo_id']), str(doc['image']), str(doc['digest']))] = size
    return out


async def cached_paths(db: Database, paths: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
    """Which ``(repo_id, path)`` pairs are cached files."""
    by_repo: dict[str, set[str]] = {}
    for repo_id, path in paths:
        by_repo.setdefault(repo_id, set()).add(path)
    if not by_repo:
        return set()
    query = {'$or': [{'repo_id': r, 'path': {'$in': sorted(p)}} for r, p in sorted(by_repo.items())]}
    projection = {'repo_id': 1, 'path': 1, '_id': 0}
    return {(str(doc['repo_id']), str(doc['path'])) async for doc in db[ASSETS].find(query, projection)}
