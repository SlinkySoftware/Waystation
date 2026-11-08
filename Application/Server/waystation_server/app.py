"""Server application factory.

Minimal on purpose (T-F05): it exposes only ``/healthz`` and ``/readyz`` so the container stack can run. The full
factory (routing for ``/repository/``, ``/v2/``, ``/api/v1/``, request context, errors, metrics) is T-S01.
"""

from __future__ import annotations

from fastapi import FastAPI

from waystation_common.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title='Waystation', docs_url=None, redoc_url=None)
    app.state.settings = settings

    @app.get('/healthz', include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness: the process answers. Reveals nothing else (SPECIFICATION §4.4)."""
        return {'status': 'ok'}

    @app.get('/readyz', include_in_schema=False)
    async def readyz() -> dict[str, str]:
        """Readiness. Dependency checks (MongoDB, Valkey) arrive with T-S01."""
        return {'status': 'ok'}

    return app
