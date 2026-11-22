"""Auth bypass matrix (T-R01, Documents/Security/ASVS_L2.md V4): the cases ``test_access.py`` does not already hold.

- The non-API routes are exactly the known set: a format that adds a route of its own outside the single format
  router fails here until its access rule is reviewed and added to this matrix.
- Every registered format, with its real descriptor (``request_action``, ``read_posts``, ``client_login``) behind an
  echo plugin: unsafe methods are authorised as ``write``/``delete`` except the reviewed exceptions, and each
  principal column (anonymous, no roles, reader, deployer, admin, a read-scoped token, a token narrowed to another
  repository) gets the expected answer on that format's real publish requests, hidden repositories 404 (ADR 0005:
  401 without credentials).
- Credentials outside their place: a bare token ``Authorization`` value, ``X-NuGet-ApiKey``, a system grant and a
  download grant are each honoured only where adr/0012, adr/0010 and SPEC §4.9 say, nowhere else.
- ``X-Waystation-Connector`` is ignored unless the direct peer is an internal hop (ADR 0013).
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from ws_testing import asgi_client, iter_routes, make_repo

from waystation_common.errors import Unauthorized
from waystation_common.system_grants import PREFETCH, SystemGrants
from waystation_server.app import create_app
from waystation_server.formats.base import FormatDescriptor, FormatRequest
from waystation_server.plugins import default_registry
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.download_urls import DownloadUrls
from waystation_server.security.principal import AuthMethod, Principal, PrincipalKind, TokenLimits
from waystation_server.services import Services

KEY = b'k' * 32
GRANTS = SystemGrants([KEY])
URLS = DownloadUrls([KEY])


def test_the_non_api_routes_are_the_reviewed_set() -> None:
    routes = {
        (path, method)
        for path, methods, _ in iter_routes(create_app())
        if not path.startswith('/api/v1/')
        for method in methods
    }
    expected = {
        *(('/repository/{repo_name}/{path:path}', m) for m in ('DELETE', 'GET', 'HEAD', 'PATCH', 'POST', 'PUT')),
        *(('/repository/{repo_name}', m) for m in ('DELETE', 'GET', 'HEAD', 'PATCH', 'POST', 'PUT')),
        *(('/v2/{name_path:path}', m) for m in ('DELETE', 'GET', 'HEAD', 'PATCH', 'POST', 'PUT')),
        *(('/v2', m) for m in ('DELETE', 'GET', 'HEAD', 'PATCH', 'POST', 'PUT')),
        ('/_internal/prefetch/{repo_id}', 'POST'),  # system grant only (adr/0012)
        ('/auth/token', 'GET'),  # docker token service: authenticates itself
        ('/auth/token', 'POST'),
        ('/auth/oidc/{provider_id}/login', 'GET'),
        ('/auth/oidc/{provider_id}/callback', 'GET'),
        ('/.well-known/terraform.json', 'GET'),
        ('/.well-known/terraform.json', 'HEAD'),
        ('/healthz', 'GET'),
        ('/readyz', 'GET'),
        ('/metrics', 'GET'),  # source CIDRs or a metrics-scoped token, decided in the handler (SPEC §4.4)
    }
    assert routes == expected


# ---------------------------------------------------------------------------- every format's action mapping
# (format, method, path) pairs authorised as something other than write/delete: reviewed, each with a reason.
NOT_A_WRITE = {
    ('npm', 'POST', '-/npm/v1/security/audits'): 'advisory query (read_posts)',
    ('npm', 'POST', '-/npm/v1/security/audits/quick'): 'advisory query (read_posts)',
    ('npm', 'POST', '-/npm/v1/security/advisories/bulk'): 'advisory query (read_posts)',
    ('npm', 'PUT', '-/user/org.couchdb.user:alice'): 'npm login: client_login decides',
    ('npm', 'DELETE', '-/user/token/x'): 'npm logout: client_login decides',
    ('swift', 'POST', 'login'): 'proves credentials only; the plugin refuses anonymous',
    ('swift', 'POST', ''): 'the same login at the repository URL',
}
# Real write requests of each format (FORMATS.md), plus generic paths.
PUBLISH = {
    'npm': [
        ('PUT', 'pkg'),
        ('PUT', '@scope%2fpkg'),
        ('DELETE', '-/package/pkg/dist-tags/beta'),
        ('DELETE', 'pkg/-/pkg-1.0.0.tgz/-rev/1'),
    ],
    'cargo': [
        ('PUT', 'api/v1/crates/new'),
        ('DELETE', 'api/v1/crates/foo/1.0.0/yank'),
        ('PUT', 'api/v1/crates/foo/1.0.0/unyank'),
        ('PUT', 'api/v1/crates/foo/owners'),
    ],
    'rubygems': [('POST', 'api/v1/gems'), ('DELETE', 'api/v1/gems/yank')],
    'nuget': [('PUT', 'api/v2/package'), ('PUT', 'api/v2/package/'), ('DELETE', 'api/v2/package/foo/1.0.0')],
    'composer': [('PUT', 'packages/vendor/pkg/1.0.0'), ('POST', 'api/packages'), ('DELETE', 'packages/vendor/pkg')],
    'conda': [('PUT', 'noarch/pkg-1.0-0.conda'), ('POST', 'upload'), ('DELETE', 'noarch/pkg-1.0-0.conda')],
    'huggingface': [('POST', 'api/models/org/m/preupload/main'), ('PUT', 'org/m/resolve/main/x.bin')],
    'pypi': [('POST', ''), ('POST', 'legacy/')],
    'helm': [('POST', 'api/charts'), ('PUT', 'mychart-1.0.0.tgz'), ('DELETE', 'api/charts/mychart/1.0.0')],
    'terraform': [('POST', 'v1/providers/ns/p/versions'), ('PUT', 'v1/modules/ns/m/aws/1.0.0')],
    'swift': [('PUT', 'mona/pkg/1.0.0'), ('DELETE', 'mona/pkg/1.0.0')],
    'docker': [('PUT', 'v2/x/manifests/1')],
}
GENERIC = [('PUT', 'x'), ('POST', 'x'), ('PATCH', 'x'), ('DELETE', 'x'), ('POST', 'api/v1/x'), ('PUT', '')]
UNSAFE = ('PUT', 'POST', 'PATCH', 'DELETE')


def _descriptors() -> list[FormatDescriptor]:
    descriptors = default_registry().descriptors()
    assert len(descriptors) >= 18, [d.name for d in descriptors]
    return descriptors


def _action(descriptor: FormatDescriptor, method: str, path: str) -> Action | None:
    """The router's decision (``routing._dispatch``); None = ``client_login`` decides."""
    if descriptor.client_login(method, path):
        return None
    action = descriptor.request_action(method, path) or access.method_action(method)
    if method == 'POST' and path in descriptor.read_posts:
        action = Action.READ
    return action


def test_unsafe_methods_are_writes_in_every_format() -> None:
    probes = {p for pairs in PUBLISH.values() for p in pairs} | set(GENERIC) | {(m, p) for (_, m, p) in NOT_A_WRITE}
    for descriptor in _descriptors():
        for method, path in sorted(probes):
            if method not in UNSAFE:
                continue
            action = _action(descriptor, method, path)
            if (descriptor.name, method, path) in NOT_A_WRITE:
                assert action in (Action.READ, None), (descriptor.name, method, path, action)
                continue
            assert action in (Action.WRITE, Action.DELETE), f'{descriptor.name} {method} {path}: {action}'
        for method in ('GET', 'HEAD'):
            assert _action(descriptor, method, 'x') is Action.READ, descriptor.name


def test_every_read_post_and_client_login_is_reviewed() -> None:
    for descriptor in _descriptors():
        for path in descriptor.read_posts:
            assert (descriptor.name, 'POST', path) in NOT_A_WRITE, (descriptor.name, path)


# ---------------------------------------------------------------------------- HTTP matrix over every format
def user(*roles: str) -> Principal:
    return Principal(PrincipalKind.USER, f'id-{"-".join(roles)}', '-'.join(roles) or 'nobody', roles, AuthMethod.BASIC)


READER, DEPLOYER, ADMIN = user('reader'), user('deployer'), user('admin')
NOBODY = dataclasses.replace(user(), anonymous_grants=access.Grants(is_admin=False, permissions=()))
READ_TOKEN = dataclasses.replace(
    DEPLOYER, method=AuthMethod.API_TOKEN, token=TokenLimits('t1', access.token_actions(['read']), scopes=('read',))
)
OTHER_REPO_TOKEN = dataclasses.replace(
    ADMIN,
    method=AuthMethod.API_TOKEN,
    token=TokenLimits('t2', access.token_actions(['read', 'write', 'delete']), repositories=('elsewhere',),
                      scopes=('read', 'write', 'delete')),
)  # fmt: skip
BEARERS = {'reader': READER, 'deployer': DEPLOYER, 'admin': ADMIN, 'nobody': NOBODY}
API_TOKENS = {
    'wst_' + 'r' * 43: READ_TOKEN,
    'wst_' + 'o' * 43: OTHER_REPO_TOKEN,
    'wst_' + 'd' * 43: dataclasses.replace(DEPLOYER, method=AuthMethod.API_TOKEN),
}


class Echo:
    def __init__(self, descriptor: FormatDescriptor) -> None:
        self.descriptor = descriptor

    async def handle(self, req: FormatRequest) -> Response:
        return PlainTextResponse(f'{req.method} {req.repo.name}/{req.path} as {req.principal.display}')


def _repos() -> list[RepoConfig]:
    out = []
    for d in _descriptors():
        if d.name == 'docker':
            continue  # /v2/, below
        out.append(make_repo(f'{d.name}-proxy', d.name))
        if RepoType.HOSTED in d.types:
            out.append(make_repo(f'{d.name}-hosted', d.name, type=RepoType.HOSTED, proxy=None))
    out.append(make_repo('docker-hosted', 'docker', type=RepoType.HOSTED, proxy=None))
    out.append(make_repo('docker-hub', 'docker', format_options={'connector_port': 5001}))
    return out


async def _bearer(request: Request, token: str) -> Principal | None:
    return BEARERS.get(token)


@pytest.fixture
def unit_repos() -> list[RepoConfig]:
    return _repos()


@pytest.fixture
async def services(unit_services: Services, monkeypatch: pytest.MonkeyPatch) -> Services:
    for descriptor in _descriptors():
        unit_services.formats.set_plugin(Echo(descriptor))  # type: ignore[arg-type]
    unit_services.bearer_authenticators.append(_bearer)
    unit_services.system_grants = GRANTS
    unit_services.download_urls = URLS

    async def authenticate(token: str, **_: object) -> Principal:
        principal = API_TOKENS.get(token)
        if principal is None:
            raise Unauthorized('invalid credentials')
        return principal

    monkeypatch.setattr(unit_services.tokens, 'authenticate', authenticate)
    return unit_services


@pytest.fixture
async def client(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(services) as c:
        yield c


def _hosted_publish_cases() -> list[tuple[str, str, str]]:
    cases = []
    for d in _descriptors():
        if d.name == 'docker' or RepoType.HOSTED not in d.types:
            continue
        for method, path in PUBLISH.get(d.name, GENERIC[:1]):
            cases.append((d.name, method, path))
    return cases


@pytest.mark.parametrize(('fmt', 'method', 'path'), _hosted_publish_cases())
async def test_publish_matrix_on_every_hosted_format(
    client: httpx.AsyncClient, fmt: str, method: str, path: str
) -> None:
    url = f'/repository/{fmt}-hosted/{path}'
    expect = {
        None: 401,  # anonymous: hidden hosted repository → the Basic challenge (ADR 0005)
        'Bearer nobody': 404,  # signed in, nothing visible
        'Bearer reader': 403,
        'Bearer deployer': 200,
        'Bearer admin': 200,
        'Bearer ' + 'wst_' + 'r' * 43: 403,  # a read-scoped token of a deployer
        'Bearer ' + 'wst_' + 'o' * 43: 404,  # an admin's token narrowed to another repository
    }
    descriptor = next(d for d in _descriptors() if d.name == fmt)
    if _action(descriptor, method, path) is Action.DELETE:
        expect['Bearer deployer'] = 403  # delete is not a deployer permission (yank, dist-tag rm are writes)
    if (fmt, method, path) in NOT_A_WRITE:
        pytest.skip('authorised as a read (reviewed above)')
    for header, status in expect.items():
        headers = {'Authorization': header} if header else {}
        response = await client.request(method, url, headers=headers, content=b'{}')
        assert response.status_code == status, f'{header} {method} {url}: {response.status_code} {response.text}'
        if status == 200:
            assert response.text.startswith(f'{method} {fmt}-hosted/')


@pytest.mark.parametrize('fmt', [d.name for d in _descriptors() if d.name != 'docker'])
async def test_read_matrix_on_every_proxy_format(client: httpx.AsyncClient, fmt: str) -> None:
    url = f'/repository/{fmt}-proxy/some/file'
    for header, status in {
        None: 200,  # the default anonymous access reads proxies
        'Bearer nobody': 404,
        'Bearer reader': 200,
        'Bearer ' + 'wst_' + 'o' * 43: 404,
    }.items():
        response = await client.get(url, headers={'Authorization': header} if header else {})
        assert response.status_code == status, f'{header} {url}: {response.status_code}'
    # no write on a proxy for anonymous or a reader, whatever the format
    assert (await client.put(url)).status_code == 401
    assert (await client.put(url, headers={'Authorization': 'Bearer reader'})).status_code == 403


# ---------------------------------------------------------------------------- credentials out of place
TOKEN = 'wst_' + 'd' * 43


async def test_bare_token_authorization_only_on_format_routes(client: httpx.AsyncClient) -> None:
    """Cargo and RubyGems send the token as the whole header value: accepted on ``/repository/`` only."""
    hosted = await client.put('/repository/cargo-hosted/api/v1/crates/new', headers={'Authorization': TOKEN})
    assert hosted.status_code == 200, hosted.text
    api = await client.get('/api/v1/auth/me', headers={'Authorization': TOKEN})
    assert api.status_code == 401
    v2 = await client.get('/v2/docker-hosted/tags/list', headers={'Authorization': TOKEN})
    assert v2.status_code == 401
    assert v2.headers['www-authenticate'].startswith('Bearer ')
    # a wrong bare value is refused (never anonymous), the same for hidden and missing repositories
    hidden = await client.put('/repository/cargo-hosted/api/v1/crates/new', headers={'Authorization': 'wst_nope'})
    missing = await client.put('/repository/no-such/api/v1/crates/new', headers={'Authorization': 'wst_nope'})
    assert hidden.status_code == missing.status_code == 401
    assert hidden.content == missing.content


async def test_nuget_api_key_only_on_format_routes(client: httpx.AsyncClient) -> None:
    pushed = await client.put('/repository/nuget-hosted/api/v2/package', headers={'X-NuGet-ApiKey': TOKEN})
    assert pushed.status_code == 200, pushed.text
    assert (await client.get('/api/v1/auth/me', headers={'X-NuGet-ApiKey': TOKEN})).status_code == 401
    v2 = await client.get('/v2/docker-hosted/tags/list', headers={'X-NuGet-ApiKey': TOKEN})
    assert v2.status_code == 401
    # not a token: refused, never anonymous; Authorization wins over the key
    bad = await client.put('/repository/nuget-hosted/api/v2/package', headers={'X-NuGet-ApiKey': 'secret'})
    assert bad.status_code == 401
    both = await client.put(
        '/repository/nuget-hosted/api/v2/package', headers={'X-NuGet-ApiKey': TOKEN, 'Authorization': 'Bearer reader'}
    )
    assert both.status_code == 403


async def test_a_system_grant_is_honoured_by_the_prefetch_route_only(client: httpx.AsyncClient) -> None:
    for name in ('npm-proxy', 'npm-hosted'):
        grant = {'X-Ws-System-Grant': GRANTS.sign(PREFETCH, f'id-{name}')}
        # a format route sees no credentials: proxies stay anonymous reads, hosted repositories stay hidden
        read = await client.get(f'/repository/{name}/pkg', headers=grant)
        assert read.status_code == (200 if name == 'npm-proxy' else 401)
        if read.status_code == 200:
            assert read.text.endswith('as anonymous')
        assert (await client.put(f'/repository/{name}/pkg', headers=grant)).status_code == 401
        assert (await client.get(f'/api/v1/repositories/{name}/assets', headers=grant)).status_code in (401, 404)
    grant = {'X-Ws-System-Grant': GRANTS.sign(PREFETCH, 'id-docker-hosted')}
    assert (await client.get('/v2/docker-hosted/tags/list', headers=grant)).status_code == 401
    # expired, tampered, wrong purpose, another repository: 404 on the prefetch route
    body = {'op': 'warm', 'items': ['a']}
    for bad in (
        GRANTS.sign(PREFETCH, 'id-npm-proxy', now=0),
        GRANTS.sign(PREFETCH, 'id-npm-proxy')[:-2] + 'xx',
        GRANTS.sign('download', 'id-npm-proxy'),
        GRANTS.sign(PREFETCH, 'id-pypi-proxy'),
        URLS.sign('id-npm-proxy', 'pkg'),
    ):
        got = await client.post('/_internal/prefetch/id-npm-proxy', json=body, headers={'X-Ws-System-Grant': bad})
        assert got.status_code == 404, bad


async def test_a_download_grant_reads_one_file_of_one_repository(client: httpx.AsyncClient) -> None:
    grant = URLS.sign('id-terraform-hosted', 'v1/providers/ns/p/1.0.0/download/linux/amd64/p.zip')
    url = '/repository/terraform-hosted/v1/providers/ns/p/1.0.0/download/linux/amd64/p.zip'
    ok = await client.get(url, params={'ws_grant': grant})
    assert ok.status_code == 200, ok.text
    assert (await client.head(url, params={'ws_grant': grant})).status_code == 200
    for method in ('PUT', 'POST', 'DELETE'):  # never a write
        assert (await client.request(method, url, params={'ws_grant': grant})).status_code == 401
    other_file = url.replace('p.zip', 'q.zip')
    assert (await client.get(other_file, params={'ws_grant': grant})).status_code == 401
    other_repo = url.replace('terraform-hosted', 'helm-hosted')
    assert (await client.get(other_repo, params={'ws_grant': grant})).status_code == 401
    assert (await client.get('/api/v1/auth/me', params={'ws_grant': grant})).status_code == 401
    expired = URLS.sign('id-terraform-hosted', 'v1/providers/ns/p/1.0.0/download/linux/amd64/p.zip', now=0)
    assert (await client.get(url, params={'ws_grant': expired})).status_code == 401


# ---------------------------------------------------------------------------- docker port connectors (ADR 0013)
async def test_connector_header_from_an_external_peer_is_ignored(services: Services) -> None:
    """docker-hub (anonymous reads) is connector 5001; no repository answers ``library/x`` by name."""
    async with asgi_client(services) as internal:  # the web tier's hop: the connector's repository takes every name
        params = {'scope': 'repository:library/x:pull', 'connector': '5001'}
        issued = await internal.get('/auth/token', params=params)
        assert issued.status_code == 200, issued.text
        headers = {'X-Waystation-Connector': '5001', 'Authorization': 'Bearer ' + issued.json()['token']}
        routed = await internal.get('/v2/library/x/tags/list', headers=headers)
        assert routed.status_code == 200, routed.text
        assert routed.text == 'GET docker-hub/library/x/tags/list as anonymous'
    async with asgi_client(services, client=('203.0.113.9', 40000)) as external:
        spoofed = await external.get('/v2/library/x/tags/list', headers=headers)
        assert spoofed.status_code != 200, spoofed.text  # routed by the name alone: nothing there
        assert 'docker-hub' not in spoofed.text


async def test_a_connector_selects_a_repository_but_grants_nothing(services: Services) -> None:
    async with asgi_client(services) as internal:
        nobody = await internal.put(
            '/v2/anything/manifests/1', headers={'X-Waystation-Connector': '5001', 'Authorization': 'Bearer reader'}
        )
        assert nobody.status_code in (401, 403)
        unknown = await internal.get('/v2/anything/tags/list', headers={'X-Waystation-Connector': '5999'})
        assert unknown.status_code in (401, 404)


# ---------------------------------------------------------------------------- response headers (ASVS V8.2.1)
async def test_api_and_sign_in_answers_are_not_stored(client: httpx.AsyncClient) -> None:
    for path in ('/api/v1/config', '/api/v1/auth/me', '/api/v1/no-such-route', '/auth/token'):
        response = await client.get(path)
        assert response.headers.get_list('cache-control') == ['no-store'], path
    # format answers are left to the format (cached artifacts must stay cacheable)
    assert 'cache-control' not in (await client.get('/repository/npm-proxy/pkg')).headers
