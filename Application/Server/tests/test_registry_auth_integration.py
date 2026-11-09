"""Docker token service and /v2/ challenge plumbing (T-S12) against real MongoDB/Valkey."""

import base64
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from starlette.responses import PlainTextResponse, Response
from ws_testing import asgi_client

from waystation_common.db.collections import REPOSITORIES
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.base import FormatRequest
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
SERVICE = 'registry.example.com'


class EchoDocker:
    def __init__(self, services: Services) -> None:
        descriptor = services.formats.descriptor('docker')
        assert descriptor is not None
        self.descriptor = descriptor

    async def handle(self, req: FormatRequest) -> Response:
        return PlainTextResponse(f'{req.repo.name} {req.path} {req.principal.display}')


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(bootstrap_admin_password=SecretStr(PASSWORD))


async def _repo(services: Services, name: str, *, repo_type: str = 'proxy', **options: Any) -> None:
    now = datetime.now(UTC)
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': f'id-{name}',
            'name': name,
            'format': 'docker',
            'type': repo_type,
            'online': True,
            'proxy': {'upstream_url': 'https://registry-1.docker.io/'} if repo_type == 'proxy' else None,
            'format_options': options,
            'created_at': now,
            'updated_at': now,
        }
    )


@pytest.fixture
async def docker(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    await _repo(services, 'docker-hub', aliases=['docker.io'], docker_hub=True, default_repo=True)
    await _repo(services, 'private', repo_type='hosted')
    services.catalog.invalidate()
    services.formats.set_plugin(EchoDocker(services))  # type: ignore[arg-type]
    async with asgi_client(services) as client:
        yield client


def claims(token: str) -> dict[str, Any]:
    body = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))  # type: ignore[no-any-return]


async def get_token(client: httpx.AsyncClient, *scopes: str, auth: tuple[str, str] | None = None) -> httpx.Response:
    params = [('service', SERVICE), *(('scope', s) for s in scopes)]
    return await client.get('/auth/token', params=params, auth=auth)


async def test_anonymous_pull_flow(docker: httpx.AsyncClient) -> None:
    challenge = await docker.get('/v2/docker-hub/library/nginx/manifests/latest')
    assert challenge.status_code == 401
    assert 'scope="repository:docker-hub/library/nginx:pull"' in challenge.headers['www-authenticate']
    issued = await get_token(docker, 'repository:docker-hub/library/nginx:pull')
    assert issued.status_code == 200
    body = issued.json()
    assert body['token'] == body['access_token']
    assert body['expires_in'] == 300
    payload = claims(body['token'])
    assert payload['sub'] == 'anonymous'
    assert payload['aud'] == SERVICE
    assert payload['access'] == [{'type': 'repository', 'name': 'docker-hub/library/nginx', 'actions': ['pull']}]

    headers = {'Authorization': f'Bearer {body["token"]}'}
    assert (await docker.get('/v2/', headers=headers)).status_code == 200
    served = await docker.get('/v2/docker-hub/library/nginx/manifests/latest', headers=headers)
    assert served.status_code == 200
    assert served.text == 'docker-hub library/nginx/manifests/latest anonymous'
    # the same token on another repository asks for a new scope
    other = await docker.get('/v2/private/app/manifests/1', headers=headers)
    assert other.status_code == 401
    assert 'error="insufficient_scope"' in other.headers['www-authenticate']
    # anonymous tokens never grant push, so a push asks again
    push = await docker.put('/v2/docker-hub/library/nginx/manifests/latest', headers=headers)
    assert push.status_code == 401
    assert 'scope="repository:docker-hub/library/nginx:pull,push"' in push.headers['www-authenticate']


async def test_mirror_mode_and_aliases(docker: httpx.AsyncClient) -> None:
    for name, expected in (('library/nginx', 'library/nginx'), ('docker.io/library/nginx', 'library/nginx')):
        token = (await get_token(docker, f'repository:{name}:pull')).json()['token']
        assert claims(token)['access'][0]['actions'] == ['pull']
        served = await docker.get(f'/v2/{name}/manifests/1', headers={'Authorization': f'Bearer {token}'})
        assert served.text == f'docker-hub {expected}/manifests/1 anonymous'


async def test_hidden_repository_gets_no_grant(docker: httpx.AsyncClient) -> None:
    token = (await get_token(docker, 'repository:private/app:pull,push')).json()['token']
    assert claims(token)['access'] == [{'type': 'repository', 'name': 'private/app', 'actions': []}]
    response = await docker.get('/v2/private/app/manifests/1', headers={'Authorization': f'Bearer {token}'})
    assert response.status_code == 401


async def test_login_with_password(docker: httpx.AsyncClient) -> None:
    assert (await get_token(docker, auth=('admin', 'wrong'))).status_code == 401
    issued = await get_token(docker, 'repository:private/app:pull,push,delete', auth=('admin', PASSWORD))
    assert issued.status_code == 200
    payload = claims(issued.json()['token'])
    assert payload['sub'] != 'anonymous'
    assert payload['access'][0]['actions'] == ['pull', 'push', 'delete']
    headers = {'Authorization': f'Bearer {issued.json()["token"]}'}
    pushed = await docker.put('/v2/private/app/manifests/1', headers=headers)
    assert pushed.status_code == 200
    assert pushed.text.endswith(' admin')


async def test_tokens_are_checked(docker: httpx.AsyncClient) -> None:
    token = (await get_token(docker, 'repository:docker-hub/x:pull')).json()['token']
    head, body, sig = token.split('.')
    # Flip a byte of the decoded signature: editing base64 text can be a no-op (unused low bits of the last char).
    raw = bytearray(base64.urlsafe_b64decode(sig + '=' * (-len(sig) % 4)))
    raw[0] ^= 0x01
    tampered = f'{head}.{body}.{base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()}'
    assert (await docker.get('/v2/', headers={'Authorization': f'Bearer {tampered}'})).status_code == 401
    assert (await get_token(docker, 'x')).status_code == 200  # malformed scopes are ignored
    wrong_service = await docker.get('/auth/token', params={'service': 'other.example.com'})
    assert wrong_service.status_code == 400
    # registry tokens are not API credentials
    assert (await docker.get('/api/v1/auth/me', headers={'Authorization': f'Bearer {token}'})).status_code == 401


async def test_catalog_scope(docker: httpx.AsyncClient) -> None:
    # Everyone may ask for the catalog: it lists only what RBAC lets the caller see (T-P2-DOCKERH).
    anon = claims((await get_token(docker, 'registry:catalog:*')).json()['token'])
    assert anon['access'] == [{'type': 'registry', 'name': 'catalog', 'actions': ['*']}]
    admin = claims((await get_token(docker, 'registry:catalog:*', auth=('admin', PASSWORD))).json()['token'])
    assert admin['access'][0]['actions'] == ['*']
    other = claims((await get_token(docker, 'registry:other:*')).json()['token'])
    assert other['access'][0]['actions'] == []
    without = (await get_token(docker, 'repository:docker-hub/x:pull')).json()['token']
    refused = await docker.get('/v2/_catalog', headers={'Authorization': f'Bearer {without}'})
    assert refused.status_code == 401
    assert 'scope="registry:catalog:*"' in refused.headers['www-authenticate']
