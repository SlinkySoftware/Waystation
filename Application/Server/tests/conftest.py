"""Server test fixtures.

- ``unit_services``: ``Services`` that never connect (MongoDB/Valkey clients are created lazily); the repository
  catalog is backed by an in-memory list (``unit_repos``). For routing, error and middleware tests.
- ``services`` / ``client`` (integration): ``Services`` against the throwaway MongoDB/Valkey of
  ``make test-integration``, schema and migrations applied, started; ``client`` is an httpx client on the ASGI app.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from ws_testing import asgi_client, make_settings, static_catalog

from waystation_common.db.client import Database
from waystation_common.db.migrations import apply_migrations
from waystation_common.db.schema import ensure_schema
from waystation_common.settings import Settings
from waystation_server.repos.model import RepoConfig
from waystation_server.services import Services


@pytest.fixture
def unit_repos() -> list[RepoConfig]:
    return []


@pytest.fixture
def unit_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def unit_services(unit_settings: Settings, unit_repos: list[RepoConfig]) -> Services:
    services = Services.create(unit_settings, check_schema_on_start=False)
    services.catalog = static_catalog(unit_repos)
    return services


@pytest.fixture
async def unit_client(unit_services: Services) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(unit_services) as client:
        yield client


# ---------------------------------------------------------------------------- integration
@pytest.fixture
def settings_factory(tmp_path: Path, mongo_db: Database, valkey: Redis) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        return make_settings(
            tmp_path,
            mongo_uri=os.environ['WAYSTATION_TEST_MONGO_URI'],
            mongo_db=mongo_db.name,
            valkey_url=os.environ['WAYSTATION_TEST_VALKEY_URL'],
            **overrides,
        )

    return factory


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory()


@pytest.fixture
async def services(settings: Settings, mongo_db: Database) -> AsyncIterator[Services]:
    await ensure_schema(mongo_db)
    await apply_migrations(mongo_db)
    svc = Services.create(settings)
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()


@pytest.fixture
async def client(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(services) as c:
        yield c
