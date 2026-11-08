import httpx

from waystation_common.settings import Settings
from waystation_server.app import create_app


async def test_health_endpoints() -> None:
    transport = httpx.ASGITransport(app=create_app(Settings()))
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        assert (await client.get('/healthz')).json() == {'status': 'ok'}
        assert (await client.get('/readyz')).json() == {'status': 'ok'}


def test_create_app_defaults_to_process_settings() -> None:
    assert isinstance(create_app().state.settings, Settings)
