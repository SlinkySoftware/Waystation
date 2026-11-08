"""``repositories`` collection access (ARCHITECTURE §3: only ``*.db`` modules touch MongoDB)."""

from __future__ import annotations

from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database
from waystation_common.db.collections import (
    APT_PACKAGES,
    APT_POOL_INDEX,
    ASSETS,
    CASE_INSENSITIVE,
    DOCKER_MANIFESTS,
    DOCKER_TAGS,
    NPM_PACKAGES,
    REPOSITORIES,
    YUM_PACKAGES,
)
from waystation_common.errors import Conflict
from waystation_server.repos.model import RepoConfig

# Per-repository content records dropped with the repository (blobs are reclaimed later by GC).
CONTENT_COLLECTIONS = (ASSETS, APT_POOL_INDEX, APT_PACKAGES, NPM_PACKAGES, DOCKER_TAGS, DOCKER_MANIFESTS, YUM_PACKAGES)


async def list_all(db: Database) -> list[RepoConfig]:
    return [RepoConfig.model_validate(doc) async for doc in db[REPOSITORIES].find({})]


async def get_by_name_ci(db: Database, name: str) -> RepoConfig | None:
    doc = await db[REPOSITORIES].find_one({'name': name}, collation=CASE_INSENSITIVE)
    return RepoConfig.model_validate(doc) if doc else None


async def insert(db: Database, repo: RepoConfig) -> None:
    try:
        await db[REPOSITORIES].insert_one(repo.to_doc())
    except DuplicateKeyError:
        raise Conflict('a repository with this name already exists') from None


async def replace(db: Database, repo: RepoConfig, *, expected_version: int) -> bool:
    """Replace when the stored ``config_version`` is still ``expected_version`` (optimistic concurrency)."""
    result = await db[REPOSITORIES].replace_one({'_id': repo.id, 'config_version': expected_version}, repo.to_doc())
    return result.matched_count == 1


async def unset_default_docker(db: Database, *, except_id: str) -> None:
    await db[REPOSITORIES].update_many(
        {'format': 'docker', 'format_options.default_repo': True, '_id': {'$ne': except_id}},
        {'$set': {'format_options.default_repo': False}, '$inc': {'config_version': 1}},
    )


async def delete(db: Database, repo_id: str) -> bool:
    result = await db[REPOSITORIES].delete_one({'_id': repo_id})
    if result.deleted_count:
        # Blobs are reclaimed later by GC (mark-and-sweep); the content records go now.
        for name in CONTENT_COLLECTIONS:
            await db[name].delete_many({'repo_id': repo_id})
    return bool(result.deleted_count)
