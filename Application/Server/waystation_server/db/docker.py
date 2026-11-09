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


# ---------------------------------------------------------------------------- hosted repositories (T-P2-DOCKERH)
async def set_tag(db: Database, repo_id: str, image: str, tag: str, digest: str, *, pushed_by: str | None) -> None:
    """A pushed tag (hosted): always points at ``digest``."""
    now = datetime.now(UTC)
    await db[DOCKER_TAGS].update_one(
        {'repo_id': repo_id, 'image': image, 'tag': tag},
        {'$set': {'manifest_digest': digest, 'updated_at': now, 'pushed_by': pushed_by, **name_keys(image)}},
        upsert=True,
    )


async def tag_names(db: Database, repo_id: str, image: str, *, last: str | None, limit: int) -> list[str]:
    """Tag names of an image in lexical order after ``last`` (OCI ``tags/list``; index ``repo_image_tag_unique``)."""
    query: dict[str, Any] = {'repo_id': repo_id, 'image': image}
    if last is not None:
        query['tag'] = {'$gt': last}
    cursor = db[DOCKER_TAGS].find(query, {'tag': 1, '_id': 0}).sort([('tag', 1)]).limit(limit)
    return [str(doc['tag']) async for doc in cursor]


async def tags_of_digest(db: Database, repo_id: str, image: str, digest: str) -> list[str]:
    cursor = db[DOCKER_TAGS].find({'repo_id': repo_id, 'manifest_digest': digest, 'image': image}, {'tag': 1, '_id': 0})
    return [str(doc['tag']) async for doc in cursor]


async def delete_tag(db: Database, repo_id: str, image: str, tag: str) -> bool:
    result = await db[DOCKER_TAGS].delete_one({'repo_id': repo_id, 'image': image, 'tag': tag})
    return result.deleted_count > 0


async def delete_tags_of_digest(db: Database, repo_id: str, image: str, digest: str) -> int:
    result = await db[DOCKER_TAGS].delete_many({'repo_id': repo_id, 'manifest_digest': digest, 'image': image})
    return result.deleted_count


async def get_manifest(db: Database, repo_id: str, image: str, digest: str) -> dict[str, Any] | None:
    return await db[DOCKER_MANIFESTS].find_one({'repo_id': repo_id, 'image': image, 'digest': digest}, {'_id': 0})


async def delete_manifest(db: Database, repo_id: str, image: str, digest: str) -> bool:
    result = await db[DOCKER_MANIFESTS].delete_one({'repo_id': repo_id, 'image': image, 'digest': digest})
    return result.deleted_count > 0


REFERRERS_MAX = 1000


async def referrers(
    db: Database, repo_id: str, image: str, subject: str, *, artifact_type: str | None
) -> list[dict[str, Any]]:
    """Manifests of ``image`` whose ``subject`` is ``subject`` (OCI referrers API; index ``repo_subject_digest``)."""
    query: dict[str, Any] = {'repo_id': repo_id, 'subject_digest': subject, 'image': image}
    if artifact_type is not None:
        query['artifact_type'] = artifact_type
    projection = {'digest': 1, 'media_type': 1, 'artifact_type': 1, 'size': 1, 'annotations': 1, '_id': 0}
    cursor = db[DOCKER_MANIFESTS].find(query, projection).sort([('digest', 1)]).limit(REFERRERS_MAX)
    return await cursor.to_list()


async def blob_referenced(db: Database, repo_id: str, digest: str) -> bool:
    """Does a manifest of the repository use ``digest`` as its config or a layer (indexes ``repo_config_digest``,
    ``repo_layer_digest``)?"""
    for field in ('config_digest', 'layers.digest'):
        if await db[DOCKER_MANIFESTS].find_one({'repo_id': repo_id, field: digest}, {'_id': 1}) is not None:
            return True
    return False


async def image_names(db: Database, repo_id: str, *, after: str | None, limit: int) -> list[str]:
    """Image names with at least one tag, in order (``/v2/_catalog``)."""
    return [str(doc['_id']) for doc in await images_page(db, repo_id, after=after, q=None, limit=limit)]
