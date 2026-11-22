"""Alpine apk repositories (T-P6-APK) end to end on real MongoDB/Valkey with a scriptable mirror: the proxy (index
passed through, packages verified against the index C: checksum, unverified-then-verified, mismatches refused),
hosted uploads with the worker's signed APKINDEX, browse, search and prefetch."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import SecretStr
from ws_apk import build_apk, build_index, checksum, make_key
from ws_fake_upstream import FakeUpstream, Respond
from ws_query_audit import request_scope
from ws_testing import BASE_URL, asgi_client

from waystation_common import apk
from waystation_common.crypto import Keyring
from waystation_common.db.collections import APK_PACKAGES, ASSETS, JOB_OUTBOX, REPOSITORIES
from waystation_common.settings import Settings
from waystation_common.signing import KeyStore
from waystation_common.system_grants import SystemGrants
from waystation_server.app import create_app
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.apk.plugin import ApkPlugin
from waystation_server.services import Services
from waystation_worker import apk as worker_apk
from waystation_worker import prefetch

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple 42'
DIR = 'v3.22/main/x86_64'
UPSTREAM_KEY = make_key()
HELLO = build_apk('hello', '1.0-r0', depends=('busybox',), key=UPSTREAM_KEY)
HELLO_NEW = build_apk('hello', '1.0-r1', key=UPSTREAM_KEY)
EVIL = build_apk('hello', '1.0-r1', description='tampered', key=UPSTREAM_KEY)
LOOSE = build_apk('loose', '2.0-r0', key=UPSTREAM_KEY)
INDEX = build_index({'hello-1.0-r0.apk': HELLO, 'hello-1.0-r1.apk': HELLO_NEW}, key=UPSTREAM_KEY)


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        fake.routes[f'/{DIR}/APKINDEX.tar.gz'] = Respond(200, {}, INDEX)
        fake.routes[f'/{DIR}/hello-1.0-r0.apk'] = Respond(200, {}, HELLO)
        fake.routes[f'/{DIR}/hello-1.0-r1.apk'] = Respond(200, {}, EVIL)  # does not match the index
        fake.routes[f'/{DIR}/loose-2.0-r0.apk'] = Respond(200, {}, LOOSE)  # listed by no index (yet)
        fake.routes['/v3.22/main/aarch64/APKINDEX.tar.gz'] = Respond(200, {}, b'not an index')
        yield fake


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


@pytest.fixture
async def auth(admin: httpx.AsyncClient) -> tuple[str, str]:
    created = await admin.post('/api/v1/auth/me/tokens', json={'name': 'apk', 'scopes': ['read', 'write', 'delete']})
    assert created.status_code == 201, created.text
    return 'admin', str(created.json()['secret'])


async def create(admin: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await admin.post('/api/v1/repositories', json={'format': 'apk', 'online': True, **body})


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True, 'metadata_max_age_s': 3600}
    made = await create(admin, {'name': 'alpine', 'type': 'proxy', 'proxy': proxy})
    assert made.status_code == 201, made.text
    for name, policy in (('mine', 'allow_redeploy'), ('strict', 'disable_redeploy'), ('locked', 'read_only')):
        made = await create(admin, {'name': name, 'type': 'hosted', 'hosted': {'write_policy': policy}})
        assert made.status_code == 201, made.text


def plugin(services: Services) -> ApkPlugin:
    found = services.formats.plugin('apk')
    assert isinstance(found, ApkPlugin)
    return found


def served(services: Services, response: httpx.Response) -> bytes:
    assert response.status_code == 200, response.text
    if 'x-accel-redirect' not in response.headers:
        return response.content
    digest = response.headers['etag'].strip('"').removeprefix('sha256:')
    return services.blobs.blob_path(digest).read_bytes()


async def test_options_and_groups(admin: httpx.AsyncClient) -> None:
    group = await create(admin, {'name': 'apk-group', 'type': 'group', 'group': {'members': []}})
    assert group.status_code == 422
    bad = await create(
        admin,
        {
            'name': 'apk-bad',
            'type': 'hosted',
            'format_options': {'branch': '..', 'repositories': ['ok', 'a/b'], 'architectures': ['X86']},
        },
    )
    assert bad.status_code == 422
    fields = {e['field'] for e in bad.json()['errors']}
    assert {'format_options.branch', 'format_options.repositories.1', 'format_options.architectures.0'} <= fields
    proxy = {'upstream_url': 'https://dl-cdn.alpinelinux.org/alpine/'}
    made = await create(
        admin,
        {'name': 'apk-warm', 'type': 'proxy', 'proxy': {**proxy, 'prefetch': {'warm': ['busybox', 'a b c']}}},
    )
    assert made.status_code == 422 and 'prefetch' in made.text


async def test_proxy(
    services: Services, client: httpx.AsyncClient, repos: None, upstream: FakeUpstream, auth: tuple[str, str]
) -> None:
    base = '/repository/alpine'
    index = await client.get(f'{base}/{DIR}/APKINDEX.tar.gz', auth=auth)
    assert served(services, index) == INDEX  # untouched: apk verifies the Alpine signature itself
    bad_index = await client.get(f'{base}/v3.22/main/aarch64/APKINDEX.tar.gz', auth=auth)
    assert bad_index.status_code == 502
    await plugin(services).drain()

    package = await client.get(f'{base}/{DIR}/hello-1.0-r0.apk', auth=auth)
    assert served(services, package) == HELLO
    hit = await client.get(f'{base}/{DIR}/hello-1.0-r0.apk', auth=auth)
    assert hit.headers['x-ws-cache'] == 'HIT' and upstream.hits(f'/{DIR}/hello-1.0-r0.apk') == 1

    tampered = await client.get(f'{base}/{DIR}/hello-1.0-r1.apk', auth=auth)
    assert tampered.status_code == 502
    assert await services.db[ASSETS].count_documents({'path': f'{DIR}/hello-1.0-r1.apk'}) == 0
    assert upstream.hits(f'/{DIR}/APKINDEX.tar.gz') == 2  # revalidated once after the mismatch

    loose = await client.get(f'{base}/{DIR}/loose-2.0-r0.apk', auth=auth)
    assert served(services, loose) == LOOSE  # unverified: no index lists it
    await plugin(services).drain()
    record = await services.db[APK_PACKAGES].find_one({'path': f'{DIR}/loose-2.0-r0.apk'})
    assert record is not None and record['verified'] is False and record['checksum'] == checksum(LOOSE)

    # A new index lists it with another checksum: the unverified copy is dropped.
    other = build_apk('loose', '2.0-r0', description='other build')
    upstream.routes[f'/{DIR}/APKINDEX.tar.gz'] = Respond(
        200, {}, build_index({'hello-1.0-r0.apk': HELLO, 'loose-2.0-r0.apk': other}, key=UPSTREAM_KEY)
    )
    await services.db[ASSETS].update_one(
        {'path': f'{DIR}/APKINDEX.tar.gz'}, {'$set': {'upstream.fetched_at': datetime(2000, 1, 1, tzinfo=UTC)}}
    )
    assert (await client.get(f'{base}/{DIR}/APKINDEX.tar.gz', auth=auth)).status_code == 200
    await plugin(services).drain()
    assert await services.db[ASSETS].count_documents({'path': f'{DIR}/loose-2.0-r0.apk'}) == 0
    listed = await services.db[APK_PACKAGES].find_one({'path': f'{DIR}/loose-2.0-r0.apk'})
    assert listed is not None and listed['verified'] is True and listed['checksum'] == checksum(other)

    packages = (await client.get('/api/v1/repositories/alpine/apk/packages', auth=auth)).json()
    assert [(p['name'], p['latest_version']) for p in packages['items']] == [('hello', '1.0-r0'), ('loose', '2.0-r0')]
    page = (await client.get('/api/v1/repositories/alpine/apk/versions', params={'package': 'hello'}, auth=auth)).json()
    row = page['items'][0]
    assert (row['version'], row['branch'], row['repository'], row['arch'], row['cached'], row['verified']) == (
        '1.0-r0',
        'v3.22',
        'main',
        'x86_64',
        True,
        True,
    )
    assert row['depends'] == ['busybox'] and row['checksum'] == checksum(HELLO)
    found = (await client.get('/api/v1/search', params={'q': 'hello'}, auth=auth)).json()
    assert any(h['format'] == 'apk' and h['browse']['query'] == {'package': 'hello'} for h in found['items'])
    assert (await client.put(f'{base}/{DIR}/x-1.0-r0.apk', content=HELLO, auth=auth)).status_code == 405


async def build(services: Services, name: str) -> worker_apk.IndexResult:
    repo = await services.catalog.get(name)
    assert repo is not None
    jobs = [d async for d in services.db[JOB_OUTBOX].find({'job.kind': 'index.apk'})]
    assert any(d['job']['payload'] == {'repo_id': repo.id} for d in jobs)
    keys = KeyStore.in_data_dir(services.settings.data_dir, Keyring.from_settings(services.settings.secret_keys))
    result = await worker_apk.rebuild(services.db, services.blobs, keys, repo.id, now=datetime.now(UTC))
    assert result is not None
    return result


async def published(services: Services, client: httpx.AsyncClient, auth: tuple[str, str], key: str) -> apk.Index:
    """The signed index of ``mine``/``DIR`` (signature verified with the served public key, as apk does)."""
    data = served(services, await client.get(f'/repository/mine/{DIR}/APKINDEX.tar.gz', auth=auth))
    pem = served(services, await client.get(f'/repository/mine/keys/{key}', auth=auth))
    public = serialization.load_pem_public_key(pem)
    assert isinstance(public, rsa.RSAPublicKey)
    first = apk._gzip_member(io.BytesIO(data), 0, 1 << 20, 'signature')
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(first.data), mode='r:') as tar:
        member = tar.next()
        assert member is not None and member.name == f'.SIGN.RSA256.{key}'
        handle = tar.extractfile(member)
        assert handle is not None
        public.verify(handle.read(), data[first.end :], padding.PKCS1v15(), hashes.SHA256())
    return apk.parse_index(data)


async def test_hosted(
    services: Services, client: httpx.AsyncClient, admin: httpx.AsyncClient, repos: None, auth: tuple[str, str]
) -> None:
    base = '/repository/mine'
    assert (await client.get(f'{base}/{DIR}/APKINDEX.tar.gz', auth=auth)).status_code == 404
    up = await client.put(f'{base}/{DIR}/whatever.apk', content=HELLO, auth=auth)
    assert up.status_code == 201, up.text
    assert up.json()['path'] == f'{DIR}/hello-1.0-r0.apk' and up.json()['checksum'] == checksum(HELLO)
    noarch = build_apk('doc', '1.0-r0', arch='noarch')
    assert (await client.put(f'{base}/{DIR}/doc-1.0-r0.apk', content=noarch, auth=auth)).status_code == 201
    wrong_arch = build_apk('arm', '1.0-r0', arch='aarch64')
    refused = await client.put(f'{base}/{DIR}/arm-1.0-r0.apk', content=wrong_arch, auth=auth)
    assert refused.status_code == 400 and 'aarch64' in refused.text
    junk = await client.put(f'{base}/{DIR}/junk-1.0-r0.apk', content=b'junk', auth=auth)
    assert junk.status_code == 400
    shallow = await client.put(f'{base}/main/hello-1.0-r0.apk', content=HELLO, auth=auth)
    assert shallow.status_code == 400
    assert (await client.put(f'{base}/{DIR}/hello.apk', content=HELLO, auth=None)).status_code == 401

    repo = await services.catalog.get('mine')
    assert repo is not None
    result = await build(services, 'mine')
    key = apk.key_name('mine', repo.id)
    assert (result.directories, result.packages, result.key) == (1, 2, key)
    index = await published(services, client, auth, key)
    assert [(e.name, e.version, e.arch) for e in index.entries] == [
        ('doc', '1.0-r0', 'x86_64'),
        ('hello', '1.0-r0', 'x86_64'),
    ]
    assert index.entries[1].checksum == checksum(HELLO) and index.entries[1].size == len(HELLO)
    assert served(services, await client.get(f'{base}/{DIR}/hello-1.0-r0.apk', auth=auth)) == HELLO

    strict = await client.put(f'/repository/strict/{DIR}/hello-1.0-r0.apk', content=HELLO, auth=auth)
    assert strict.status_code == 201
    again = await client.put(f'/repository/strict/{DIR}/hello-1.0-r0.apk', content=HELLO, auth=auth)
    assert again.status_code == 409
    locked = await client.put(f'/repository/locked/{DIR}/hello-1.0-r0.apk', content=HELLO, auth=auth)
    assert locked.status_code == 403

    versions = (
        await client.get('/api/v1/repositories/mine/apk/versions', params={'package': 'hello'}, auth=auth)
    ).json()
    assert versions['items'][0]['uploaded_by'] == 'admin' and versions['items'][0]['verified'] is True

    deleted = await client.delete(f'{base}/{DIR}/hello-1.0-r0.apk', auth=auth)
    assert deleted.status_code == 204
    await build(services, 'mine')
    assert [e.name for e in (await published(services, client, auth, key)).entries] == ['doc']
    deleted = await client.delete(f'{base}/{DIR}/doc-1.0-r0.apk', auth=auth)
    await build(services, 'mine')
    assert (await published(services, client, auth, key)).entries == []  # an emptied directory keeps an index

    setup = (await admin.get('/api/v1/repositories/mine/setup')).json()
    text = str(setup)
    assert f'keys/{key}' in text and '/etc/apk/keys/' in text


async def test_warm(services: Services, repos: None, upstream: FakeUpstream, settings: Settings) -> None:
    repo = await services.catalog.get('alpine')
    assert repo is not None
    options = {'branch': 'v3.22', 'repositories': ['community', 'main'], 'architectures': ['x86_64']}
    upstream.routes['/v3.22/community/x86_64/APKINDEX.tar.gz'] = Respond(200, {}, build_index({}, key=UPSTREAM_KEY))
    await services.db[REPOSITORIES].update_one(
        {'_id': repo.id},
        {'$set': {'format_options': options, 'proxy.prefetch.warm': ['hello=1.0-r0', f'{DIR}/hello', 'nope']}},
    )
    services.catalog.invalidate()
    grants = SystemGrants.from_secret_keys([k.get_secret_value() for k in settings.secret_keys])
    transport = httpx.ASGITransport(app=request_scope(create_app(services.settings, services=services)))
    server = prefetch.ServerClient(BASE_URL, grants, transport=transport)
    try:
        result = await prefetch.warm(services.db, services.valkey, server, repo_id=repo.id)
    finally:
        await server.aclose()
    # hello (no version) picks the newest listed, 1.0-r1, whose upstream file does not match: refused
    assert (result['items'], result['ok'], result['failed']) == (3, 1, 2), result
    assert await services.db[ASSETS].count_documents({'path': f'{DIR}/hello-1.0-r0.apk'}) == 1
