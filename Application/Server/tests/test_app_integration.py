from collections.abc import Callable

import httpx
import pytest

from waystation_common.db.client import Database
from waystation_common.db.migrations import SchemaMismatch
from waystation_common.settings import Settings
from waystation_server.services import Services

pytestmark = pytest.mark.integration


async def test_readyz_ok(client: httpx.AsyncClient) -> None:
    response = await client.get('/readyz')
    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


async def test_server_refuses_unmigrated_database(
    settings_factory: Callable[..., Settings], mongo_db: Database
) -> None:
    services = Services.create(settings_factory())
    with pytest.raises(SchemaMismatch):
        await services.start()
    await services.valkey.aclose()
    assert services.mongo_client is not None
    await services.mongo_client.close()
