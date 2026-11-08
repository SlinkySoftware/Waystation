import httpx
import pytest
from ws_testing import asgi_client, make_repo, static_catalog

from waystation_common.settings import Settings
from waystation_server import health
from waystation_server.app import create_app
from waystation_server.services import Services


def test_create_app_defaults_to_process_settings() -> None:
    assert isinstance(create_app().state.settings, Settings)


async def test_healthz_and_request_id(unit_client: httpx.AsyncClient) -> None:
    response = await unit_client.get('/healthz')
    assert response.json() == {'status': 'ok'}
    assert len(response.headers['x-request-id']) == 26  # a fresh ULID


async def test_request_id_reused_from_internal_hop(unit_client: httpx.AsyncClient) -> None:
    response = await unit_client.get('/healthz', headers={'X-Request-Id': 'abcdef0123456789'})
    assert response.headers['x-request-id'] == 'abcdef0123456789'


async def test_request_id_not_reused_from_external_peer(unit_services: Services) -> None:
    async with asgi_client(unit_services, client=('203.0.113.9', 4000)) as client:
        response = await client.get('/healthz', headers={'X-Request-Id': 'abcdef0123456789'})
    assert response.headers['x-request-id'] != 'abcdef0123456789'


async def test_head_on_api_style_route(unit_client: httpx.AsyncClient) -> None:
    response = await unit_client.head('/healthz')
    assert response.status_code == 200
    assert response.content == b''
    assert response.headers['content-length'] == str(len(b'{"status":"ok"}'))


async def test_readyz_unavailable(unit_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, 'READY_TIMEOUT_S', 0.2)
    response = await unit_client.get('/readyz')
    assert response.status_code == 503
    assert response.json() == {'status': 'unavailable'}


async def test_metrics_allowed_from_loopback(unit_client: httpx.AsyncClient) -> None:
    response = await unit_client.get('/metrics')
    assert response.status_code == 200
    assert 'waystation_inflight_transfers' in response.text


async def test_metrics_hidden_from_others(unit_services: Services) -> None:
    async with asgi_client(unit_services, client=('203.0.113.9', 4000)) as client:
        assert (await client.get('/metrics')).status_code == 404


async def test_metrics_via_web_tier_uses_real_ip(unit_services: Services) -> None:
    async with asgi_client(unit_services, client=('10.0.0.2', 4000)) as client:  # forbidden-strings: allow
        denied = await client.get('/metrics', headers={'X-Real-IP': '203.0.113.9'})
        allowed = await client.get('/metrics', headers={'X-Real-IP': '127.0.0.1'})
    assert denied.status_code == 404
    assert allowed.status_code == 200


async def test_unknown_repository_is_native_404(unit_client: httpx.AsyncClient) -> None:
    response = await unit_client.get('/repository/nope/some/file.txt')
    assert response.status_code == 404
    assert response.headers['content-type'].startswith('text/plain')


@pytest.mark.parametrize('unit_repos', [[make_repo('files')]])
async def test_traversal_rejected(unit_client: httpx.AsyncClient) -> None:
    response = await unit_client.get('/repository/files/a/%2e%2e/b')
    assert response.status_code == 400


@pytest.mark.parametrize('unit_repos', [[make_repo('files', fmt='pypi')]])  # a format with no plugin (yet)
async def test_repository_without_plugin_is_404(unit_client: httpx.AsyncClient) -> None:
    response = await unit_client.get('/repository/files/a/b')
    assert response.status_code == 404
    assert response.headers['content-type'].startswith('text/plain')


async def test_repository_names_match_case_sensitively(unit_services: Services) -> None:
    unit_services.catalog = static_catalog([make_repo('Files')])
    async with asgi_client(unit_services) as client:
        assert (await client.get('/repository/files/x')).status_code == 404


@pytest.mark.parametrize('unit_repos', [[make_repo('files', online=False)]])
async def test_offline_repository(unit_client: httpx.AsyncClient) -> None:
    assert (await unit_client.get('/repository/files/x')).status_code == 503


async def test_api_unknown_path_is_problem_json(unit_client: httpx.AsyncClient) -> None:
    response = await unit_client.get('/api/v1/nope')
    assert response.status_code == 404
    assert response.headers['content-type'] == 'application/problem+json'
    assert response.json()['status'] == 404


async def test_v2_without_token_is_challenged(unit_client: httpx.AsyncClient) -> None:
    base = await unit_client.get('/v2/')
    assert base.status_code == 401
    assert base.headers['docker-distribution-api-version'] == 'registry/2.0'
    assert base.headers['www-authenticate'] == (
        'Bearer realm="https://registry.example.com/auth/token",service="registry.example.com"'
    )
    named = await unit_client.get('/v2/library/nginx/manifests/latest')
    assert named.status_code == 401
    assert named.json()['errors'][0]['code'] == 'UNAUTHORIZED'
    assert 'scope="repository:library/nginx:pull"' in named.headers['www-authenticate']
    push = await unit_client.put('/v2/org/app/manifests/1.0')
    assert 'scope="repository:org/app:pull,push"' in push.headers['www-authenticate']
