"""``docker_tags`` / ``docker_manifests`` access (DATA_MODEL): what a docker repository has cached, by image.

Written in the background after a manifest is cached (tag manifests: the tag and its manifest; manifests by digest:
the manifest), never per request. Read by the docker browse views through ``repo_image_tag_unique``,
``repo_image_updated``, ``repo_manifest_digest`` and ``repo_image_digest_unique``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import DOCKER_MANIFESTS, DOCKER_TAGS
from waystation_common.searchkeys import name_keys


async def record_manifest(db: Database, repo_id: str, image: str, digest: str, fields: Mapping[str, Any]) -> None:
    now = datetime.now(UTC)
    await db[DOCKER_MANIFESTS].update_one(
        {'repo_id': repo_id, 'image': image, 'digest': digest},
        {'$set': {**fields, 'updated_at': now}, '$setOnInsert': {'created_at': now}},
        upsert=True,
    )


async def record_tag(db: Database, repo_id: str, image: str, tag: str, digest: str, *, authoritative: bool) -> None:
    """Point ``image:tag`` at ``digest``. A non-authoritative answer (a manifest fetched with a narrower ``Accept``,
    which may be one platform's manifest) only creates the tag when it is unknown."""
    now = datetime.now(UTC)
    fields = {'manifest_digest': digest, 'updated_at': now}
    keys = name_keys(image)  # search keys (always set: also backfills a tag written before they existed)
    update: dict[str, Any] = {'$set': {**fields, **keys}} if authoritative else {'$setOnInsert': fields, '$set': keys}
    await db[DOCKER_TAGS].update_one({'repo_id': repo_id, 'image': image, 'tag': tag}, update, upsert=True)


async def images_page(
    db: Database, repo_id: str, *, after: str | None, q: str | None, limit: int
) -> list[dict[str, Any]]:
    """Images with cached tags, by name: ``{'_id': image, 'tag_count': n}``."""
    image: dict[str, Any] = {}
    if after is not None:
        image['$gt'] = after
    if q:
        image |= {'$regex': re.escape(q), '$options': 'i'}
    match: dict[str, Any] = {'repo_id': repo_id}
    if image:
        match['image'] = image
    pipeline: list[dict[str, Any]] = [
        {'$match': match},
        {'$sort': {'image': 1}},
        {'$group': {'_id': '$image', 'tag_count': {'$sum': 1}}},
        {'$sort': {'_id': 1}},
        {'$limit': limit},
    ]
    return [doc async for doc in await db[DOCKER_TAGS].aggregate(pipeline)]


async def tags_page(
    db: Database, repo_id: str, image: str, *, after: tuple[datetime, str] | None, limit: int
) -> list[dict[str, Any]]:
    """An image's tags, most recently updated first (then by tag)."""
    query: dict[str, Any] = {'repo_id': repo_id, 'image': image}
    if after is not None:
        updated, tag = after
        query['$or'] = [{'updated_at': {'$lt': updated}}, {'updated_at': updated, 'tag': {'$gt': tag}}]
    cursor = db[DOCKER_TAGS].find(query, {'_id': 0}).sort([('updated_at', -1), ('tag', 1)]).limit(limit)
    return await cursor.to_list()


async def get_tag(db: Database, repo_id: str, image: str, tag: str) -> dict[str, Any] | None:
    return await db[DOCKER_TAGS].find_one({'repo_id': repo_id, 'image': image, 'tag': tag}, {'_id': 0})


async def tag_for_digest(db: Database, repo_id: str, image: str, digest: str) -> str | None:
    doc = await db[DOCKER_TAGS].find_one(
        {'repo_id': repo_id, 'manifest_digest': digest, 'image': image}, {'tag': 1, '_id': 0}
    )
    return str(doc['tag']) if doc else None


async def manifests(db: Database, repo_id: str, image: str, digests: Iterable[str]) -> dict[str, dict[str, Any]]:
    wanted = sorted(set(digests))
    if not wanted:
        return {}
    cursor = db[DOCKER_MANIFESTS].find({'repo_id': repo_id, 'image': image, 'digest': {'$in': wanted}}, {'_id': 0})
    return {str(doc['digest']): doc async for doc in cursor}


async def manifests_of_images(db: Database, repo_id: str, images: Iterable[str]) -> list[dict[str, Any]]:
    wanted = sorted(set(images))
    if not wanted:
        return []
    projection = {'image': 1, 'config_digest': 1, 'config_size': 1, 'layers': 1, '_id': 0}
    return await db[DOCKER_MANIFESTS].find({'repo_id': repo_id, 'image': {'$in': wanted}}, projection).to_list()
