"""Fixtures for integration tests (``-m integration``), run by ``make test-integration`` against throwaway
MongoDB/Valkey containers. Each test gets its own database / Valkey DB contents, dropped afterwards."""

import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from waystation_common.db.client import Database, create_client
from waystation_common.db.replset import ensure_replica_set
from waystation_common.ids import new_id
from waystation_common.settings import Settings

MONGO_URI = os.environ.get('WAYSTATION_TEST_MONGO_URI', '')
VALKEY_URL = os.environ.get('WAYSTATION_TEST_VALKEY_URL', '')

_replset_ready = False


@pytest.fixture
def mongo_settings() -> Settings:
    if not MONGO_URI:
        pytest.skip('WAYSTATION_TEST_MONGO_URI not set (run make test-integration)')
    return Settings(mongo_uri=MONGO_URI, mongo_db=f'waystation_test_{new_id().lower()}')  # type: ignore[arg-type]


@pytest.fixture
async def mongo_db(mongo_settings: Settings) -> AsyncIterator[Database]:
    global _replset_ready  # noqa: PLW0603
    if not _replset_ready:
        await ensure_replica_set(MONGO_URI)
        _replset_ready = True
    client = create_client(mongo_settings)
    db = client[mongo_settings.mongo_db]
    try:
        yield db
    finally:
        await client.drop_database(mongo_settings.mongo_db)
        await client.close()


@pytest.fixture
async def valkey() -> AsyncIterator['Redis']:
    if not VALKEY_URL:
        pytest.skip('WAYSTATION_TEST_VALKEY_URL not set (run make test-integration)')
    client: Redis = Redis.from_url(VALKEY_URL)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()
