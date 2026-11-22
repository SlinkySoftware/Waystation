"""Global search queries (SPECIFICATION §4.6; ``waystation_server.search`` builds pages from them).

A **stream** is one search key (``name_lc`` or ``base_lc``, ``waystation_common.searchkeys``) of one collection,
read in ``(key, repo_id, _id)`` order straight from the ``search_name`` / ``search_base`` index: the prefix range and
the repositories the principal may browse are index bounds, the keyset position is a filter on index keys, so a page
never sorts in memory and never scans more than its range. ``base_lc`` streams skip documents whose ``name_lc``
already matches (each document is found once, also in facet counts).

A **substring stream** (``Stream.substring``, adr/0009) reads the ``search_sfx`` index: the term (cut to ``SFX``
characters) is a prefix range over the multikey ``name_sfx`` (the name's suffixes from its second character on), so
it finds exactly the names containing the term after their start. A document's position in the stream is
``(m, repo_id, _id)`` with ``m`` its smallest matching suffix (``SORT_KEY`` on the returned document); the page is read
in index order with ``min``/``max`` key bounds as the keyset (a multikey index cannot provide a sort, so there is no
``sort``: a single hinted index scan returns its keys in order). A name holding the term twice has two keys in the
range: the scan finds it at its smallest one first; read again from a later cursor it is skipped as already listed.
A regular expression on ``name_lc`` leaves out what a prefix stream already found (the term at the start of the name,
or of its last ``/`` segment for sources with ``base_lc``) and checks terms longer than ``SFX`` in full.

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

from bson import ObjectId, Regex
from bson.errors import InvalidId
from bson.max_key import MaxKey
from bson.min_key import MinKey
from pymongo.errors import ExecutionTimeout

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS, DOCKER_MANIFESTS, DOCKER_TAGS
from waystation_common.errors import ValidationFailed
from waystation_common.searchkeys import SFX, prefix_range, suffixes

FACET_CAP = 10_000
Key = Literal['name_lc', 'base_lc']
INDEX: dict[str, str] = {'name_lc': 'search_name', 'base_lc': 'search_base'}
SFX_INDEX = 'search_sfx'
SORT_KEY = '_search_key'  # set on substring stream documents: their position key
# A substring facet count gives up after SUBSTRING_COUNT_MS (the counts are then lower bounds, ``capped``); a page
# read re-scans at most SUBSTRING_ROUNDS times when names holding the term twice were skipped.
SUBSTRING_COUNT_MS = 1_000
SUBSTRING_ROUNDS = 8


@dataclass(frozen=True)
class Stream:
    collection: str
    key: Key
    prefix: str  # folded; the whole term for a substring stream
    repo_ids: tuple[str, ...]
    projection: Mapping[str, int]
    version_field: str | None = None
    version_prefix: str | None = None
    substring: bool = False  # a substring stream (key ``name_lc``)
    has_base: bool = False  # substring stream of a source that also has a ``base_lc`` stream
    # equalities every query repeats: the partial filter of the collection's search indexes beyond the key
    # (``components``: ``format: maven``)
    match: Mapping[str, Any] | None = None

    @property
    def index(self) -> str:
        return SFX_INDEX if self.substring else INDEX[self.key]


@dataclass(frozen=True)
class Start:
    """Where a page starts in a stream: after ``(key, repo_id, id)``, or at/after ``key`` alone (``id`` None)."""

    key: str
    inclusive: bool = False
    repo_id: str | None = None
    id: Any = None

    def precedes(self, key: str, repo_id: str, ident: Any) -> bool:
        """Whether the position ``(key, repo_id, ident)`` is on the page this start begins."""
        if self.id is None:
            return key >= self.key if self.inclusive else key > self.key
        return (key, repo_id) > (self.key, self.repo_id) or (
            (key, repo_id) == (self.key, self.repo_id) and ident > self.id
        )

    def admits(self, key: str, repo_id: str, ident: Any) -> bool:
        """Whether an index scan from this start (inclusive ``min`` bound) reaches ``(key, repo_id, ident)``."""
        return self.precedes(key, repo_id, ident) or (self.id is not None and (key, repo_id, ident) == self.position)

    @property
    def position(self) -> tuple[str, str | None, Any]:
        return self.key, self.repo_id, self.id


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


def substring_pattern(term: str, *, has_base: bool) -> Regex[str]:
    """Names containing ``term`` other than where a prefix stream finds it: not at the start of the name and, with
    ``has_base``, not at the start of its last ``/`` segment."""
    t = re.escape(term)
    base = f'(?!.*/{t}[^/]*$)' if has_base else ''
    return Regex(f'^(?!{t}){base}.+{t}', 's')


def matching_keys(name_lc: str, term: str) -> list[str]:
    """The ``name_sfx`` keys of ``name_lc`` in the substring range of ``term``, in order."""
    head = term[:SFX]
    return [k for k in suffixes(name_lc) if k.startswith(head)]


def _filter(stream: Stream) -> dict[str, Any]:
    query: dict[str, Any]
    if stream.substring:
        query = {
            'name_sfx': {'$elemMatch': _range(stream.prefix[:SFX])},
            'repo_id': {'$in': list(stream.repo_ids)},
            'name_lc': substring_pattern(stream.prefix, has_base=stream.has_base),
        }
    else:
        rng = _range(stream.prefix)
        query = {stream.key: rng, 'repo_id': {'$in': list(stream.repo_ids)}}
        if stream.key == 'base_lc' and stream.prefix:
            query['name_lc'] = {'$not': rng}
    if stream.version_field and stream.version_prefix:
        query[stream.version_field] = {'$regex': '^' + re.escape(stream.version_prefix)}
    if stream.match:
        query.update(stream.match)
    return query


async def stream_page(db: Database, stream: Stream, *, start: Start | None, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` documents of ``stream`` from ``start``, in ``(key, repo_id, _id)`` order."""
    if not stream.repo_ids:
        return []
    if stream.substring:
        return await _substring_page(db, stream, start=start, limit=limit)
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
        .hint(stream.index)
        .limit(limit)
    )
    return await cursor.to_list()


def position_key(stream: Stream, doc: Mapping[str, Any]) -> str:
    """A document's key in its stream: the search key, or the smallest matching suffix (substring streams)."""
    return str(doc[SORT_KEY] if stream.substring else doc[stream.key])


def _lower(lo: str, start: Start | None) -> list[tuple[str, Any]]:
    """The ``min`` index key (inclusive) of a substring page."""
    if start is None or start.key < lo:
        return [('name_sfx', lo), ('repo_id', MinKey()), ('_id', MinKey())]
    if start.id is not None:
        return [('name_sfx', start.key), ('repo_id', start.repo_id), ('_id', start.id)]
    edge = MinKey() if start.inclusive else MaxKey()
    return [('name_sfx', start.key), ('repo_id', edge), ('_id', edge)]


async def _substring_page(db: Database, stream: Stream, *, start: Start | None, limit: int) -> list[dict[str, Any]]:
    lo, hi = prefix_range(stream.prefix[:SFX])
    if start is not None and hi is not None and start.key >= hi:
        return []
    upper: list[tuple[str, Any]] | None = (
        [('name_sfx', hi), ('repo_id', MinKey()), ('_id', MinKey())] if hi is not None else None
    )
    projection = {**stream.projection, 'name_lc': 1, 'repo_id': 1, '_id': 1}
    query = _filter(stream)
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    scan = start
    for _ in range(SUBSTRING_ROUNDS):
        cursor = db[stream.collection].find(query, projection).hint(SFX_INDEX).min(_lower(lo, scan))
        if upper is not None:
            cursor = cursor.max(upper)
        # a later round starts at the last document read (inclusive bound): one more to read past it
        want = limit - len(out) + (0 if scan is start else 1)
        docs = await cursor.limit(want).to_list()
        for doc in docs:
            keys = matching_keys(str(doc.get('name_lc', '')), stream.prefix)
            repo_id = str(doc['repo_id'])
            if not keys or doc['_id'] in seen:
                continue
            seen.add(doc['_id'])
            # its position: the smallest matching key; found later only when an earlier page listed it
            if start is None or start.precedes(keys[0], repo_id, doc['_id']):
                out.append({**doc, SORT_KEY: keys[0]})
        if len(out) >= limit or len(docs) < want:
            break
        # every document read was listed already: scan on from where the last one was read
        last, last_repo = docs[-1], str(docs[-1]['repo_id'])
        keys = matching_keys(str(last.get('name_lc', '')), stream.prefix)
        here = next((k for k in keys if scan is None or scan.admits(k, last_repo, last['_id'])), None)
        if here is None:
            break
        scan = Start(here, repo_id=last_repo, id=last['_id'])
    return out[:limit]


async def stream_counts(db: Database, stream: Stream) -> tuple[dict[str, int], bool]:
    """Matches per repository (at most ``FACET_CAP`` counted) and whether the cap was reached."""
    if not stream.repo_ids:
        return {}, False
    pipeline: list[dict[str, Any]] = [
        {'$match': _filter(stream)},
        {'$limit': FACET_CAP},
        {'$group': {'_id': '$repo_id', 'n': {'$sum': 1}}},
    ]
    options: dict[str, Any] = {'hint': stream.index}
    if stream.substring:
        options['maxTimeMS'] = SUBSTRING_COUNT_MS
    try:
        counts = {
            str(doc['_id']): int(doc['n']) async for doc in await db[stream.collection].aggregate(pipeline, **options)
        }
    except ExecutionTimeout:  # counted no further: lower bounds
        return {}, True
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
