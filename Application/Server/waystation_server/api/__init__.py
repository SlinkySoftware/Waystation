"""The REST API under ``/api/v1/`` (SPECIFICATION §7). Uses only format descriptors, never format internals."""

from __future__ import annotations

from fastapi import APIRouter

from waystation_server.api import (
    account,
    audit,
    auth,
    auth_providers,
    browse,
    cleanup,
    groups,
    imports,
    live,
    maintenance,
    repositories,
    roles,
    search,
    settings,
    stats,
    storage_guard,
    tasks,
    tokens,
    two_factor,
    users,
    webhooks,
)

API_PREFIX = '/api/v1'


def build_router() -> APIRouter:
    router = APIRouter(prefix=API_PREFIX)
    router.include_router(auth.config_router)
    router.include_router(auth.router)
    router.include_router(account.router)
    router.include_router(two_factor.router)
    router.include_router(auth_providers.router)
    router.include_router(tokens.router)
    router.include_router(users.router)
    router.include_router(groups.router)
    router.include_router(roles.router)
    router.include_router(repositories.router)
    router.include_router(imports.router)
    router.include_router(browse.router)
    router.include_router(cleanup.router)
    router.include_router(maintenance.router)
    router.include_router(tasks.router)
    router.include_router(search.router)
    router.include_router(stats.router)
    router.include_router(storage_guard.router)
    router.include_router(live.router)
    router.include_router(audit.router)
    router.include_router(settings.router)
    router.include_router(webhooks.router)
    return router
