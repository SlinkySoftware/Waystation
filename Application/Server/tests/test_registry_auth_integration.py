"""Docker token service and /v2/ challenge plumbing (T-S12) against real MongoDB/Valkey."""

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from starlette.responses import PlainTextResponse, Response
from ws_testing import asgi_client

from waystation_common.db.collections import REPOSITORIES, USERS
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.base import FormatRequest
from waystation_server.security import passwords
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


async def _repo(
    services: Services,
    name: str,
    *,
    repo_type: str = 'proxy',
    upstream: str = 'https://registry-1.docker.io/',
    members: tuple[str, ...] = (),
    **options: Any,
) -> None:
    now = datetime.now(UTC)
    await services.db[REPOSITORIES].insert_one(
        {
            '_id': f'id-{name}',
            'name': name,
            'format': 'docker',
            'type': repo_type,
            'online': True,
            'proxy': {'upstream_url': upstream} if repo_type == 'proxy' else None,
            'group': {'members': list(members)} if repo_type == 'group' else None,
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


async def test_new_repository_is_granted_at_once_on_another_replica(
    docker: httpx.AsyncClient, services: Services, settings: Settings
) -> None:
    """T-P2-PERF finding: a replica whose catalog snapshot predated a repository issued tokens granting nothing on it
    (and refused its /v2/ requests) until the snapshot expired, up to 5 s. A token request that misses now reloads
    the catalog; the replica's change stream is off here, so only that reload can find the repository."""
    replica = Services.create(settings)
    replica.formats.set_plugin(EchoDocker(replica))  # type: ignore[arg-type]
    replica.catalog._watcher = None
    await replica.start()
    try:
        async with asgi_client(replica) as other:
            warm = await get_token(other, 'repository:docker-hub/library/nginx:pull')
            assert claims(warm.json()['token'])['access'][0]['actions'] == ['pull']  # the snapshot is loaded
            await _repo(services, 'fresh')  # created by another process: nothing tells this replica
            token = (await get_token(other, 'repository:fresh/app:pull')).json()['token']
            assert claims(token)['access'] == [{'type': 'repository', 'name': 'fresh/app', 'actions': ['pull']}]
            served = await other.get('/v2/fresh/app/manifests/1', headers={'Authorization': f'Bearer {token}'})
            assert served.status_code == 200
            assert served.text == 'fresh app/manifests/1 anonymous'
            # unknown names do not reload the catalog on every request
            loads = 0
            loader = replica.catalog._loader

            async def counting() -> list[Any]:
                nonlocal loads
                loads += 1
                return await loader()

            replica.catalog._loader = counting
            for _ in range(5):
                denied = (await get_token(other, 'repository:nothing-here/app:pull')).json()['token']
                assert claims(denied)['access'][0]['actions'] == ['pull']  # the default repository serves it
            assert loads <= 1
    finally:
        await replica.stop()


async def test_repository_changes_reach_other_replicas_through_the_change_stream(
    docker: httpx.AsyncClient, services: Services, settings: Settings
) -> None:
    replica = Services.create(settings)
    await replica.start()
    try:
        await asyncio.wait_for(replica.catalog.watching.wait(), timeout=10)
        assert 'streamed' not in {r.name for r in await replica.catalog.all()}
        start = time.monotonic()
        await _repo(services, 'streamed')
        while 'streamed' not in {r.name for r in await replica.catalog.all()}:
            assert time.monotonic() - start < 2.0, 'catalog change not applied by the other replica'  # TTL: 5 s
            await asyncio.sleep(0.02)
    finally:
        await replica.stop()


async def test_mirror_mode_and_aliases(docker: httpx.AsyncClient) -> None:
    for name, expected in (('library/nginx', 'library/nginx'), ('docker.io/library/nginx', 'library/nginx')):
        token = (await get_token(docker, f'repository:{name}:pull')).json()['token']
        assert claims(token)['access'][0]['actions'] == ['pull']
        served = await docker.get(f'/v2/{name}/manifests/1', headers={'Authorization': f'Bearer {token}'})
        assert served.text == f'docker-hub {expected}/manifests/1 anonymous'


async def _pull_with_ns(
    docker: httpx.AsyncClient, name: str, ns: str, auth: tuple[str, str] | None = None
) -> httpx.Response:
    """containerd's flow against a mirror without ``override_path``: ``?ns=`` on every ``/v2/`` request, the token
    fetched from the challenge's realm with the scope of the image name."""
    path = f'/v2/{name}/manifests/1'
    challenge = await docker.get(path, params={'ns': ns})
    assert challenge.status_code == 401
    realm = challenge.headers['www-authenticate'].split('realm="')[1].split('"')[0]
    # keep the realm's own query (``ns``), as containerd does (httpx would replace it with ``params``)
    url = httpx.URL(realm).copy_merge_params({'service': SERVICE, 'scope': f'repository:{name}:pull'})
    issued = await docker.get(url.raw_path.decode(), auth=auth)
    token = issued.json()['token']
    return await docker.get(path, params={'ns': ns}, headers={'Authorization': f'Bearer {token}'})


async def test_containerd_ns_routes_to_the_alias_repository(docker: httpx.AsyncClient, services: Services) -> None:
    """GitHub #5: ``?ns=quay.io`` on an unprefixed name went to the default (Docker Hub) repository."""
    await _repo(services, 'quay', upstream='https://quay.io/', aliases=['quay.io'])
    services.catalog.invalidate()
    challenge = await docker.get('/v2/cilium/cilium/manifests/1', params={'ns': 'quay.io'})
    assert f'realm="https://{SERVICE}/auth/token?ns=quay.io"' in challenge.headers['www-authenticate']
    issued = await docker.get(
        '/auth/token', params={'ns': 'quay.io', 'service': SERVICE, 'scope': 'repository:cilium/cilium:pull'}
    )
    # the token names the repository it was granted on, so it does not depend on ``ns``
    assert claims(issued.json()['token'])['access'] == [
        {'type': 'repository', 'name': 'quay/cilium/cilium', 'actions': ['pull']}
    ]
    served = await _pull_with_ns(docker, 'cilium/cilium', 'quay.io')
    assert served.text == 'quay cilium/cilium/manifests/1 anonymous'
    assert (await _pull_with_ns(docker, 'library/busybox', 'docker.io')).text == (
        'docker-hub library/busybox/manifests/1 anonymous'
    )
    # a Docker Hub token is no good for the Quay name: the client is sent back for one with ``ns``
    hub = (await get_token(docker, 'repository:cilium/cilium:pull')).json()['token']
    again = await docker.get(
        '/v2/cilium/cilium/manifests/1', params={'ns': 'quay.io'}, headers={'Authorization': f'Bearer {hub}'}
    )
    assert again.status_code == 401
    assert 'error="insufficient_scope"' in again.headers['www-authenticate']
    assert 'auth/token?ns=quay.io"' in again.headers['www-authenticate']
    # a prefix still wins over ``ns`` (``override_path`` set-ups)
    assert (await _pull_with_ns(docker, 'quay/cilium/cilium', 'quay.io')).text == (
        'quay cilium/cilium/manifests/1 anonymous'
    )


async def test_containerd_ns_of_an_unknown_registry_is_not_found(docker: httpx.AsyncClient) -> None:
    """Not the default repository's upstream and no alias: 404 (containerd then uses the registry itself), never the
    default repository's image of the same name."""
    for ns in ('registry.k8s.io', 'ghcr.io', 'not a host!'):
        token = (
            await docker.get('/auth/token', params={'ns': ns, 'service': SERVICE, 'scope': 'repository:pause:pull'})
        ).json()['token']
        assert claims(token)['access'][0]['actions'] == []
        response = await _pull_with_ns(docker, 'library/nginx', ns)
        assert response.status_code == 404
        assert response.json()['errors'][0]['code'] == 'NAME_UNKNOWN'
    # this registry's own host is not another registry
    assert (await _pull_with_ns(docker, 'library/nginx', SERVICE)).text == (
        'docker-hub library/nginx/manifests/1 anonymous'
    )


async def test_containerd_ns_and_a_group_default(docker: httpx.AsyncClient, services: Services) -> None:
    """A group default answers ``ns`` only when every proxy member mirrors that registry (hosted members overlay)."""
    await services.db[REPOSITORIES].update_one({'name': 'docker-hub'}, {'$set': {'format_options.default_repo': False}})
    await _repo(services, 'ghcr', upstream='https://ghcr.io/')
    await _repo(services, 'quay', upstream='https://quay.io/')
    await _repo(services, 'public', repo_type='hosted')
    await _repo(services, 'mirror', repo_type='group', members=('public', 'ghcr'), default_repo=True)
    services.catalog.invalidate()
    admin = ('admin', PASSWORD)  # anonymous reads proxy repositories only

    async def pull(name: str, ns: str) -> httpx.Response:
        return await _pull_with_ns(docker, name, ns, auth=admin)

    assert (await pull('owner/app', 'ghcr.io')).text.startswith('mirror owner/app/manifests/1 ')
    assert (await pull('library/nginx', 'docker.io')).text.startswith('docker-hub library/nginx/')  # its alias
    assert (await pull('cilium/cilium', 'quay.io')).status_code == 404
    await services.db[REPOSITORIES].update_one(
        {'name': 'mirror'}, {'$set': {'group.members': ['public', 'ghcr', 'quay']}}
    )
    services.catalog.invalidate()
    assert (await pull('owner/app', 'ghcr.io')).status_code == 404  # its Quay member could answer for that name


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


async def test_a_signed_in_user_without_roles_gets_the_anonymous_pulls(
    services: Services, docker: httpx.AsyncClient
) -> None:
    """``docker login`` as a user without roles: the registry token includes the anonymous pull grants (SPEC §4.10,
    signing in never reduces access), never more (no push, nothing on hidden repositories)."""
    await services.db[USERS].insert_one(
        {
            '_id': 'id-nina',
            'username': 'nina',
            'display_name': 'Nina',
            'source': 'local',
            'password_hash': await passwords.hash_password(PASSWORD),
            'roles': [],
            'created_at': datetime.now(UTC),
        }
    )
    issued = await get_token(
        docker, 'repository:docker-hub/library/nginx:pull,push', 'repository:private/app:pull', auth=('nina', PASSWORD)
    )
    assert issued.status_code == 200
    payload = claims(issued.json()['token'])
    assert payload['sub'] == 'id-nina'
    assert payload['access'] == [
        {'type': 'repository', 'name': 'docker-hub/library/nginx', 'actions': ['pull']},
        {'type': 'repository', 'name': 'private/app', 'actions': []},
    ]
    headers = {'Authorization': f'Bearer {issued.json()["token"]}'}
    served = await docker.get('/v2/docker-hub/library/nginx/manifests/latest', headers=headers)
    assert served.status_code == 200
    assert served.text.endswith(' nina')
    assert (await docker.put('/v2/docker-hub/library/nginx/manifests/latest', headers=headers)).status_code == 401


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
