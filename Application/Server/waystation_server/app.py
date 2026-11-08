"""Server application factory (ARCHITECTURE §4).

``create_app(settings)`` builds the FastAPI app; uvicorn calls it with no arguments (``factory=True``). The lifespan
creates and starts the process ``Services`` (MongoDB, Valkey, event emitter, ...) unless the caller passed its own
(tests), and stops them on shutdown.

Mounted:

- format endpoints: ``/repository/<repo>/...`` and ``/v2/...`` (``routing``), native errors;
- the REST API under ``/api/v1/`` (``api``), problem+json errors, OpenAPI at ``/api/v1/openapi.json``;
- ``/healthz``, ``/readyz``, ``/metrics`` (``health``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from waystation_common.settings import Settings, get_settings
from waystation_server import api, health, oidc_auth, plugins, problems, registry_auth, routing
from waystation_server.api import openapi
from waystation_server.clientip import ClientResolver
from waystation_server.middleware import HeadMiddleware, RequestContextMiddleware
from waystation_server.security import access
from waystation_server.services import Services

API_PREFIX = api.API_PREFIX


def create_app(settings: Settings | None = None, *, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own = services is None
        svc = services or Services.create(settings)
        registry_auth.install(svc)
        app.state.services = svc
        await svc.start()
        try:
            yield
        finally:
            if own:
                await svc.stop()

    app = FastAPI(
        title='Waystation API',
        version='0.1.0',
        docs_url=None,
        redoc_url=None,
        openapi_url=f'{API_PREFIX}/openapi.json',
        lifespan=lifespan,
        dependencies=[Depends(access.enforce)],
    )
    app.state.settings = settings
    if services is not None:
        app.state.services = services
        registry_auth.install(services)
    problems.install(app)
    app.include_router(health.router)
    app.include_router(routing.router)
    app.include_router(registry_auth.router)
    app.include_router(oidc_auth.router)
    app.include_router(api.build_router())
    openapi.install(app, services.formats if services is not None else plugins.default_registry())
    app.add_middleware(HeadMiddleware)
    app.add_middleware(RequestContextMiddleware, resolver=ClientResolver.from_settings(settings))
    return app
