"""Swift Package Registry records (DATA_MODEL ``swift_releases``, ``swift_packages``).

``swift_releases``, one document per (repository, scope, name, version), never deleted while the repository exists
(a removed release keeps its record so the version stays taken):

- ``repo_id``, ``scope``/``name`` (as first published or pinned), ``scope_key``/``name_key`` (lower case),
  ``version``, ``version_sort`` (SemVer precedence), ``state`` (``published`` | ``removed``), ``source``
  (``hosted`` | ``scm``);
- the source archive: ``archive_path`` (asset), ``checksum`` (sha256 hex: pinned, never changes), ``size``;
  ``manifests`` (``{filename, swift_version, tools_version, path, size}``);
- ``metadata`` (the publisher's release metadata, SE-0292 appendix B; SCM releases: ``repositoryURLs``),
  ``repository_urls`` (normalised source repository URLs, ``GET /identifiers``), ``signature``
  (``{format, base64, signer, metadata_base64}``, recorded, not verified);
- SCM-backed proxy: ``tag``, ``commit``, ``upstream_url`` (the tag's tarball);
- ``published_at``, ``published_by``; ``removed_at``, ``removed_by``.

``swift_packages``, one summary per package with any release record: ``scope``, ``name``, the keys, ``latest`` (the
highest published version), ``release_count`` (published), ``removed_count``, ``description``, ``repository_url``,
search keys (``name_lc`` = ``scope.name``, ``name_sfx``: a package name is found as a substring), ``updated_at``.

Every query uses an index of its collection (``repo_pkg_version_unique``, ``repo_pkg_sort``, ``repo_urls``,
``repo_pkg_unique``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database
from waystation_common.db.collections import SWIFT_PACKAGES, SWIFT_RELEASES
from waystation_common.searchkeys import fold, suffixes

MAX_RELEASES = 10_000
PUBLISHED = 'published'
REMOVED = 'removed'


def _key(repo_id: str, scope_key: str, name_key: str) -> dict[str, str]:
    return {'repo_id': repo_id, 'scope_key': scope_key, 'name_key': name_key}


# ---------------------------------------------------------------------------- releases
async def get_release(db: Database, repo_id: str, scope_key: str, name_key: str, version: str) -> dict[str, Any] | None:
    return await db[SWIFT_RELEASES].find_one({**_key(repo_id, scope_key, name_key), 'version': version}, {'_id': 0})


async def releases(db: Database, repo_id: str, scope_key: str, name_key: str) -> list[dict[str, Any]]:
    """Every release record of a package (removed ones too), newest first."""
    cursor = (
        db[SWIFT_RELEASES]
        .find(_key(repo_id, scope_key, name_key), {'_id': 0})
        .sort('version_sort', DESCENDING)
        .limit(MAX_RELEASES)
    )
    return await cursor.to_list()


async def insert_release(db: Database, record: dict[str, Any]) -> bool:
    """Insert a release; False when the version exists already (published, pinned or removed)."""
    try:
        await db[SWIFT_RELEASES].insert_one(dict(record))
    except DuplicateKeyError:
        return False
    return True


async def delete_release(db: Database, repo_id: str, scope_key: str, name_key: str, version: str) -> None:
    """Drop a record whose files could not be stored (a publish that failed half way)."""
    await db[SWIFT_RELEASES].delete_one({**_key(repo_id, scope_key, name_key), 'version': version})


async def mark_removed(
    db: Database, repo_id: str, scope_key: str, name_key: str, version: str, *, by: str, now: datetime
) -> dict[str, Any] | None:
    """Unpublish a release (its record stays, as ``removed``); None when there is no published release."""
    return await db[SWIFT_RELEASES].find_one_and_update(
        {**_key(repo_id, scope_key, name_key), 'version': version, 'state': PUBLISHED},
        {'$set': {'state': REMOVED, 'removed_at': now, 'removed_by': by}},
        projection={'_id': 0},
        return_document=ReturnDocument.BEFORE,
    )


async def identifiers(db: Database, repo_id: str, url_key: str) -> list[str]:
    """``scope.name`` of the packages with a published release naming the source repository ``url_key``."""
    cursor = db[SWIFT_RELEASES].find(
        {'repo_id': repo_id, 'repository_urls': url_key, 'state': PUBLISHED}, {'scope': 1, 'name': 1, '_id': 0}
    )
    found: dict[str, str] = {}
    async for doc in cursor.limit(1000):
        ident = f'{doc["scope"]}.{doc["name"]}'
        found.setdefault(ident.lower(), ident)
    return sorted(found.values(), key=str.lower)


# ---------------------------------------------------------------------------- package summaries
async def refresh_package(db: Database, repo_id: str, scope: str, name: str) -> None:
    """Recompute a package's summary from its release records (after a publish, a pin or a removal)."""
    key = _key(repo_id, scope.lower(), name.lower())
    docs = await releases(db, repo_id, key['scope_key'], key['name_key'])
    if not docs:
        await db[SWIFT_PACKAGES].delete_one(key)
        return
    published = [d for d in docs if d.get('state') == PUBLISHED]
    latest = published[0] if published else None
    first = docs[-1]
    metadata = (latest or docs[0]).get('metadata') or {}
    urls = metadata.get('repositoryURLs') if isinstance(metadata, dict) else None
    ident = f'{first["scope"]}.{first["name"]}'
    name_lc = fold(ident)
    now = datetime.now(UTC)
    await db[SWIFT_PACKAGES].update_one(
        key,
        {
            '$set': {
                'scope': first['scope'],
                'name': first['name'],
                'latest': latest['version'] if latest else None,
                'release_count': len(published),
                'removed_count': len(docs) - len(published),
                'description': metadata.get('description') if isinstance(metadata, dict) else None,
                'repository_url': urls[0] if isinstance(urls, list) and urls and isinstance(urls[0], str) else None,
                'source': first.get('source'),
                'name_lc': name_lc,
                'name_sfx': suffixes(name_lc),
                'updated_at': now,
            },
            '$setOnInsert': {'created_at': now},
        },
        upsert=True,
    )


def _filter(after: str | None, q: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if after is not None:
        out['$gt'] = after
    if q:
        out['$regex'] = re.escape(q.lower())
    return out


async def scopes(db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` scopes (by key) after ``after``, with their number of packages."""
    match: dict[str, Any] = {'repo_id': repo_id}
    condition = _filter(after, q)
    if condition:
        match['scope_key'] = condition
    pipeline: list[dict[str, Any]] = [
        {'$match': match},
        {'$sort': {'repo_id': 1, 'scope_key': 1, 'name_key': 1}},
        {'$group': {'_id': '$scope_key', 'scope': {'$first': '$scope'}, 'package_count': {'$sum': 1}}},
        {'$sort': {'_id': 1}},
        {'$limit': limit},
    ]
    return [
        {'key': str(d['_id']), 'scope': str(d['scope']), 'package_count': int(d['package_count'])}
        async for d in await db[SWIFT_PACKAGES].aggregate(pipeline)
    ]


async def packages(
    db: Database,
    repo_id: str,
    *,
    scope_key: str | None,
    after: tuple[str, str] | None,
    q: str | None,
    limit: int,
    name_key: str | None = None,
) -> list[dict[str, Any]]:
    """Up to ``limit`` package summaries by (scope, name) after ``after``; ``q``: a substring of ``scope.name``."""
    query: dict[str, Any] = {'repo_id': repo_id}
    if scope_key is not None:
        query['scope_key'] = scope_key
    if name_key is not None:
        query['name_key'] = name_key
    if after is not None:
        query['$or'] = [
            {'scope_key': {'$gt': after[0]}},
            {'scope_key': after[0], 'name_key': {'$gt': after[1]}},
        ]
    if q:
        query['name_lc'] = {'$regex': re.escape(q.lower())}
    cursor = db[SWIFT_PACKAGES].find(query, {'_id': 0}).sort([('scope_key', 1), ('name_key', 1)]).limit(limit)
    return await cursor.to_list()


async def release_page(
    db: Database, repo_id: str, scope_key: str, name_key: str, *, before: str | None, limit: int
) -> list[dict[str, Any]]:
    """A package's releases newest first, after the ``version_sort`` cursor ``before``."""
    query: dict[str, Any] = _key(repo_id, scope_key, name_key)
    if before is not None:
        query['version_sort'] = {'$lt': before}
    cursor = db[SWIFT_RELEASES].find(query, {'_id': 0}).sort('version_sort', DESCENDING).limit(limit)
    return await cursor.to_list()


async def set_manifests(
    db: Database, repo_id: str, scope_key: str, name_key: str, version: str, manifests: list[dict[str, Any]]
) -> None:
    await db[SWIFT_RELEASES].update_one(
        {**_key(repo_id, scope_key, name_key), 'version': version}, {'$set': {'manifests': manifests}}
    )
