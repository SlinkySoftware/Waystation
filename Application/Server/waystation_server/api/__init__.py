"""The REST API under ``/api/v1/`` (SPECIFICATION §7). Uses only format descriptors, never format internals."""

from __future__ import annotations

from fastapi import APIRouter

from waystation_server.api import auth, browse, cleanup, live, repositories, stats, tokens

API_PREFIX = '/api/v1'


def build_router() -> APIRouter:
    router = APIRouter(prefix=API_PREFIX)
    router.include_router(auth.config_router)
    router.include_router(auth.router)
    router.include_router(tokens.router)
    router.include_router(repositories.router)
    router.include_router(browse.router)
    router.include_router(cleanup.router)
    router.include_router(stats.router)
    router.include_router(live.router)
    return router
