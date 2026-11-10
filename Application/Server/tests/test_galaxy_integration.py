"""Ansible Galaxy repositories (T-P5-GALAXY) end to end on real MongoDB/Valkey with a scriptable local upstream: the
proxy (Galaxy NG paths, every version page, URLs rewritten, artifacts verified against sha256), hosted publishing
(multipart, sha256 field, the outbox job and the worker's import, task polling, immutability, delete, write policy),
groups (merged versions, publishing through the group), legacy roles and the browse views."""

import gzip
import hashlib
import io
import json
import tarfile
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import asgi_client

from waystation_common.db import galaxy as galaxy_common_db
from waystation_common.db.collections import ASSETS, AUDIT, GALAXY_COLLECTIONS, GALAXY_IMPORTS, JOB_OUTBOX, REPO_USAGE
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.formats.galaxy.descriptor import DEFAULT_COLLECTIONS_PATH
from waystation_server.formats.galaxy.plugin import GalaxyPlugin
from waystation_server.problems import ResponseAborted
from waystation_server.services import Services
from waystation_worker import galaxy as worker_galaxy

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
BASE = '/' + DEFAULT_COLLECTIONS_PATH
ARTIFACT = b'upstream collection bytes'
VERSIONS = [f'1.0.{i}' for i in range(120)] + ['2.0.0-rc.1']  # two upstream pages


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collection(namespace: str, name: str, version: str) -> bytes:
    """A minimal collection artifact as ``ansible-galaxy collection build`` writes it."""
    readme = b'# demo\n'
    files = json.dumps(
        {
            'files': [
                {'name': '.', 'ftype': 'dir', 'chksum_type': None, 'chksum_sha256': None},
                {'name': 'README.md', 'ftype': 'file', 'chksum_type': 'sha256', 'chksum_sha256': sha(readme)},
            ],
            'format': 1,
        }
    ).encode()
    manifest = json.dumps(
        {
            'collection_info': {
                'namespace': namespace,
                'name': name,
                'version': version,
                'description': f'{name} {version}',
                'dependencies': {},
            },
            'file_manifest_file': {
                'name': 'FILES.json',
                'ftype': 'file',
                'chksum_type': 'sha256',
                'chksum_sha256': sha(files),
            },
            'format': 1,
        }
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        for path, data in (('MANIFEST.json', manifest), ('FILES.json', files), ('README.md', readme)):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue(), mtime=0)


def multipart(filename: str, data: bytes, digest: str | None) -> tuple[bytes, dict[str, str]]:
    boundary = 'galaxyboundary'
    body = b''
    if digest is not None:
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="sha256"\r\n\r\n{digest}\r\n'.encode()
    body += (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n'
        ).encode()
        + data
        + f'\r\n--{boundary}--\r\n'.encode()
    )
    return body, {'Content-Type': f'multipart/form-data; boundary={boundary}'}


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    def page(offset: int) -> bytes:
        chunk = VERSIONS[offset : offset + 100]
        nxt = f'{BASE}ns/coll/versions/?limit=100&offset={offset + 100}' if offset + 100 < len(VERSIONS) else None
        return json.dumps(
            {
                'meta': {'count': len(VERSIONS)},
                'links': {'next': nxt},
                'data': [
                    {'version': v, 'created_at': '2026-01-02T03:04:05Z', 'requires_ansible': '>=2.15'} for v in chunk
                ],
            }
        ).encode()

    async with FakeUpstream() as fake:
        root = fake.url.rstrip('/')

        def detail(version: str, digest: str, size: int) -> bytes:
            return json.dumps(
                {
                    'version': version,
                    'namespace': {'name': 'ns'},
                    'collection': {'name': 'coll'},
                    'artifact': {'filename': f'ns-coll-{version}.tar.gz', 'sha256': digest, 'size': size},
                    'download_url': f'{root}/download/ns-coll-{version}.tar.gz',
                    'metadata': {'dependencies': {}, 'description': 'From upstream'},
                    'files': [{'name': 'big'}],
                }
            ).encode()

        fake.routes.update(
            {
                f'{BASE}ns/coll/': Respond(
                    200,
                    {},
                    json.dumps(
                        {
                            'namespace': 'ns',
                            'name': 'coll',
                            'deprecated': False,
                            'highest_version': {'version': '1.0.119'},
                            'created_at': '2026-01-01T00:00:00Z',
                        }
                    ).encode(),
                ),
                f'{BASE}ns/coll/versions/?limit=100&offset=0': Respond(200, {}, page(0)),
                f'{BASE}ns/coll/versions/?limit=100&offset=100': Respond(200, {}, page(100)),
                f'{BASE}ns/coll/versions/1.0.0/': Respond(200, {}, detail('1.0.0', sha(ARTIFACT), len(ARTIFACT))),
                f'{BASE}ns/coll/versions/1.0.1/': Respond(200, {}, detail('1.0.1', '0' * 64, 3)),
                '/download/ns-coll-1.0.0.tar.gz': Respond(302, {'Location': f'{root}/cdn/a.tar.gz'}),
                '/cdn/a.tar.gz': Respond(200, {}, ARTIFACT),
                '/download/ns-coll-1.0.1.tar.gz': Respond(200, {}, b'bad'),
                '/api/v1/roles/?owner__username=someone&name=role': Respond(
                    200,
                    {},
                    json.dumps(
                        {
                            'count': 1,
                            'results': [{'id': 7, 'name': 'role', 'github_user': 'someone'}],
                        }
                    ).encode(),
                ),
                '/api/v1/roles/7/versions/?page_size=50': Respond(
                    200,
                    {},
                    json.dumps(
                        {
                            'results': [{'name': '1.0.0', 'download_url': f'{root}/archive/1.0.0.tar.gz'}],
                            'next_link': None,
                        }
                    ).encode(),
                ),
                '/archive/1.0.0.tar.gz': Respond(200, {}, b'role archive'),
            }
        )
        yield fake


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


@pytest.fixture
async def token(admin: httpx.AsyncClient) -> dict[str, str]:
    created = await admin.post('/api/v1/auth/me/tokens', json={'name': 'galaxy', 'scopes': ['read', 'write', 'delete']})
    assert created.status_code == 201, created.text
    return {'Authorization': f'Token {created.json()["secret"]}'}


async def create(admin: httpx.AsyncClient, body: dict[str, Any]) -> None:
    made = await admin.post('/api/v1/repositories', json={'format': 'galaxy', 'online': True, **body})
    assert made.status_code == 201, made.text


@pytest.fixture
async def repos(admin: httpx.AsyncClient, upstream: FakeUpstream) -> None:
    proxy = {'upstream_url': upstream.url, 'allow_private_upstream': True, 'metadata_max_age_s': 3600}
    await create(admin, {'name': 'gal', 'type': 'proxy', 'proxy': proxy})
    await create(admin, {'name': 'mine', 'type': 'hosted'})
    await create(
        admin, {'name': 'all', 'type': 'group', 'group': {'members': ['mine', 'gal'], 'writable_member': 'mine'}}
    )


def plugin(services: Services) -> GalaxyPlugin:
    found = services.formats.plugin('galaxy')
    assert isinstance(found, GalaxyPlugin)
    return found


def served(services: Services, response: httpx.Response) -> bytes:
    """The bytes behind an X-Accel answer (the ETag names the blob)."""
    assert response.status_code == 200, response.text
    if 'x-accel-redirect' not in response.headers:
        return response.content
    digest = response.headers['etag'].strip('"').removeprefix('sha256:')
    return services.blobs.blob_path(digest).read_bytes()


async def publish(
    client: httpx.AsyncClient, repo: str, auth: dict[str, str], data: bytes, filename: str, digest: str | None = None
) -> httpx.Response:
    body, headers = multipart(filename, data, sha(data) if digest is None else digest or None)
    return await client.post(
        f'/repository/{repo}/api/v3/artifacts/collections/', content=body, headers={**headers, **auth}
    )


async def run_imports(services: Services) -> None:
    jobs = [d async for d in services.db[JOB_OUTBOX].find({'job.kind': 'import.galaxy'})]
    for doc in jobs:
        task_id = doc['job']['payload']['task_id']
        await worker_galaxy.run_import(services.db, services.blobs, task_id, now=datetime.now(UTC))
    await services.db[JOB_OUTBOX].delete_many({'job.kind': 'import.galaxy'})


async def test_proxy_collections(
    services: Services, client: httpx.AsyncClient, repos: None, upstream: FakeUpstream
) -> None:
    discovery = (await client.get('/repository/gal/')).json()
    assert discovery['available_versions'] == {'v3': 'api/v3/', 'v1': 'api/v1/'}
    assert (await client.get('/repository/gal/api/')).json()['available_versions']['v3'] == 'v3/'

    coll = (await client.get('/repository/gal/api/v3/collections/ns/coll/')).json()
    assert coll['highest_version']['version'] == '1.0.119'
    assert coll['versions_url'] == '/repository/gal/api/v3/collections/ns/coll/versions/'
    alias = await client.get(f'/repository/gal{BASE}ns/coll/')
    assert alias.json() == coll and alias.headers['etag']
    assert (
        await client.get(
            '/repository/gal/api/v3/collections/ns/coll/', headers={'If-None-Match': alias.headers['etag']}
        )
    ).status_code == 304

    page = (await client.get('/repository/gal/api/v3/collections/ns/coll/versions/?limit=5&offset=0')).json()
    assert page['meta']['count'] == len(VERSIONS)
    assert [v['version'] for v in page['data']] == ['2.0.0-rc.1', '1.0.119', '1.0.118', '1.0.117', '1.0.116']
    assert page['links']['next'] == '/repository/gal/api/v3/collections/ns/coll/versions/?limit=5&offset=5'

    detail = (await client.get('/repository/gal/api/v3/collections/ns/coll/versions/1.0.0/')).json()
    assert (
        detail['download_url'] == 'https://registry.example.com/repository/gal/collections/ns/coll/ns-coll-1.0.0.tar.gz'
    )
    assert detail['artifact']['sha256'] == sha(ARTIFACT) and 'files' not in detail
    first = await client.get('/repository/gal/collections/ns/coll/ns-coll-1.0.0.tar.gz')
    assert first.status_code == 200 and first.content == ARTIFACT
    second = await client.get(f'/repository/gal{BASE[: -len("index/")]}artifacts/ns-coll-1.0.0.tar.gz')
    assert served(services, second) == ARTIFACT and second.headers['x-ws-cache'] == 'HIT'

    with pytest.raises(ResponseAborted):  # the sha256 does not match: the transfer is cut, nothing stored
        await client.get('/repository/gal/collections/ns/coll/ns-coll-1.0.1.tar.gz')
    assert await services.db[ASSETS].count_documents({'path': 'collections/ns/coll/ns-coll-1.0.1.tar.gz'}) == 0
    assert (await client.get('/repository/gal/collections/ns/coll/ns-coll-9.9.9.tar.gz')).status_code == 404
    assert (await client.get('/repository/gal/api/v3/collections/ns/none/')).status_code == 404
    assert (await client.get('/repository/gal/api/v3/collections/ns/none/versions/')).status_code == 404
    assert (await client.get('/repository/gal/other')).status_code == 404
    body, headers = multipart('ns-coll-3.0.0.tar.gz', b'x', None)
    refused = await client.post('/repository/gal/api/v3/artifacts/collections/', content=body, headers=headers)
    assert refused.status_code in (401, 403)

    await plugin(services).drain()
    assert await services.db[GALAXY_COLLECTIONS].count_documents({'repo_id': {'$exists': True}}) == len(VERSIONS)
    namespaces = (await client.get('/api/v1/repositories/gal/galaxy/namespaces')).json()
    assert namespaces['items'] == [{'namespace': 'ns', 'collection_count': 1}]
    colls = (await client.get('/api/v1/repositories/gal/galaxy/collections', params={'q': 'COL'})).json()
    assert [(c['name'], c['latest_version'], c['version_count']) for c in colls['items']] == [
        ('coll', '2.0.0-rc.1', len(VERSIONS))
    ]
    versions = (
        await client.get(
            '/api/v1/repositories/gal/galaxy/versions', params={'namespace': 'ns', 'collection': 'coll', 'limit': 3}
        )
    ).json()
    assert [v['version'] for v in versions['items']] == ['2.0.0-rc.1', '1.0.119', '1.0.118']
    assert versions['items'][0]['prerelease'] is True and versions['next_cursor']
    more = (
        await client.get(
            '/api/v1/repositories/gal/galaxy/versions',
            params={'namespace': 'ns', 'collection': 'coll', 'limit': 200, 'cursor': versions['next_cursor']},
        )
    ).json()
    oldest = more['items'][-1]
    assert oldest['version'] == '1.0.0' and oldest['cached'] is True and oldest['sha256'] == sha(ARTIFACT)
    missing = await client.get(
        '/api/v1/repositories/gal/galaxy/versions', params={'namespace': 'ns', 'collection': 'nope'}
    )
    assert missing.status_code == 404


async def test_proxy_roles(services: Services, client: httpx.AsyncClient, repos: None) -> None:
    search = (await client.get('/repository/gal/api/v1/roles/?owner__username=someone&name=role')).json()
    assert search['results'][0]['id'] == 7
    nobody = (await client.get('/repository/gal/api/v1/roles/?owner__username=nobody&name=x')).json()
    assert nobody['count'] == 0
    versions = (await client.get('/repository/gal/api/v1/roles/7/versions/')).json()
    assert versions['results'][0]['download_url'] == 'https://registry.example.com/repository/gal/roles/7/1.0.0.tar.gz'
    archive = await client.get('/repository/gal/roles/7/1.0.0.tar.gz')
    assert archive.status_code == 200 and archive.content == b'role archive'
    assert (await client.get('/repository/gal/roles/7/9.9.9.tar.gz')).status_code == 404
    assert (await client.get('/repository/mine/api/v1/roles/7/versions/')).status_code == 404


async def test_hosted_publish_import_delete(
    services: Services, client: httpx.AsyncClient, admin: httpx.AsyncClient, token: dict[str, str], repos: None
) -> None:
    v1 = collection('harness', 'demo', '1.0.0')
    assert (await client.get('/repository/mine/api/v3/collections/harness/demo/')).status_code == 404
    assert (await client.get('/repository/mine/api/v3/collections/harness/demo/', headers=token)).status_code == 404

    wrong = await publish(client, 'mine', token, v1, 'harness-demo-1.0.0.tar.gz', digest='0' * 64)
    assert wrong.status_code == 400 and wrong.json()['errors'][0]['code'] == 'checksum_mismatch'
    for name in ('demo.tar.gz', 'Harness-demo-1.0.0.tar.gz', 'harness-demo-1.0.tar.gz'):
        bad = await publish(client, 'mine', token, v1, name)
        assert bad.status_code == 400, bad.text
    no_file = await client.post(
        '/repository/mine/api/v3/artifacts/collections/',
        content=b'--b\r\nContent-Disposition: form-data; name="sha256"\r\n\r\nx\r\n--b--\r\n',
        headers={'Content-Type': 'multipart/form-data; boundary=b', **token},
    )
    assert no_file.status_code == 400

    posted = await publish(client, 'mine', token, v1, 'harness-demo-1.0.0.tar.gz')
    assert posted.status_code == 202, posted.text
    task_url = posted.json()['task']
    assert task_url.startswith('/repository/mine/api/v3/imports/collections/')
    waiting = (await client.get(task_url, headers=token)).json()
    assert waiting['state'] == 'waiting' and waiting['finished_at'] is None
    busy = await publish(client, 'mine', token, v1, 'harness-demo-1.0.0.tar.gz')
    assert busy.status_code == 409  # the same version is being imported

    await run_imports(services)
    done = (await client.get(task_url, headers=token)).json()
    assert done['state'] == 'completed' and done['finished_at'] and done['error'] is None
    assert [m['level'] for m in done['messages']] == ['INFO', 'INFO', 'INFO']
    assert (await client.get(task_url)).status_code == 401  # hidden from anonymous: the challenge (ADR 0005)
    mine = await services.catalog.get('mine')
    assert mine is not None
    usage = await services.db[REPO_USAGE].find_one({'_id': mine.id})
    assert usage is not None and (usage['bytes'], usage['assets'], usage['artifacts']) == (len(v1), 1, 1)
    assert (await client.get('/repository/gal' + task_url.removeprefix('/repository/mine'))).status_code == 404

    again = await publish(client, 'mine', token, v1, 'harness-demo-1.0.0.tar.gz')
    assert again.status_code == 409 and 'immutable' in again.json()['errors'][0]['detail']

    detail = (
        await client.get('/repository/mine/api/v3/collections/harness/demo/versions/1.0.0/', headers=token)
    ).json()
    assert detail['artifact']['sha256'] == sha(v1) and detail['metadata']['description'] == 'demo 1.0.0'
    got = await client.get('/repository/mine/collections/harness/demo/harness-demo-1.0.0.tar.gz', headers=token)
    assert served(services, got) == v1
    coll = (await client.get('/repository/mine/api/v3/collections/harness/demo/', headers=token)).json()
    assert coll['highest_version']['version'] == '1.0.0'
    assert (await client.get('/repository/mine/', headers=token)).json()['available_versions'] == {'v3': 'api/v3/'}

    # an artifact whose contents do not match its name: the import fails with the reason
    other = collection('harness', 'demo', '9.9.9')
    posted = await publish(client, 'mine', token, other, 'harness-demo-2.0.0.tar.gz')
    await run_imports(services)
    failed = (await client.get(posted.json()['task'], headers=token)).json()
    assert failed['state'] == 'failed' and failed['error']['code'] == 'mismatch'
    assert await services.db[GALAXY_IMPORTS].count_documents({'state': 'failed'}) == 1

    v2 = collection('harness', 'demo', '1.1.0')
    assert (await publish(client, 'mine', token, v2, 'harness-demo-1.1.0.tar.gz')).status_code == 202
    await run_imports(services)
    page = (
        await client.get(
            '/api/v1/repositories/mine/galaxy/versions',
            params={'namespace': 'harness', 'collection': 'demo'},
            headers=token,
        )
    ).json()
    assert [(v['version'], v['cached'], v['uploaded_by']) for v in page['items']] == [
        ('1.1.0', True, 'admin'),
        ('1.0.0', True, 'admin'),
    ]

    url = '/repository/mine/api/v3/collections/harness/demo/versions/1.0.0/'
    assert (await client.delete(url, headers=token)).status_code == 204
    assert (await client.delete(url, headers=token)).status_code == 404
    assert (await client.get(url, headers=token)).status_code == 404
    assert (
        await client.get('/repository/mine/collections/harness/demo/harness-demo-1.0.0.tar.gz', headers=token)
    ).status_code == 404
    assert (await client.delete('/repository/mine/api/v3/collections/harness/demo/', headers=token)).status_code == 204
    assert await services.db[GALAXY_COLLECTIONS].count_documents({}) == 0

    # a deleted version can be published again; an artifact removed behind the records' back (cleanup policy):
    # hosted records follow, proxy listings stay
    assert (await publish(client, 'mine', token, v1, 'harness-demo-1.0.0.tar.gz')).status_code == 202
    await run_imports(services)
    gal = await services.catalog.get('gal')
    assert gal is not None
    await services.db[ASSETS].delete_one(
        {'repo_id': mine.id, 'path': 'collections/harness/demo/harness-demo-1.0.0.tar.gz'}
    )
    assert await galaxy_common_db.drop_missing(services.db, mine.id) == 1
    assert await galaxy_common_db.drop_missing(services.db, gal.id) == 0
    assert await galaxy_common_db.drop_missing(services.db, 'gone') == 0

    await services.audit.flush()
    actions = [d['action'] async for d in services.db[AUDIT].find({'target.kind': 'galaxy_collection'})]
    assert actions.count('content.upload') == 4 and actions.count('content.delete') == 2

    await create(admin, {'name': 'frozen', 'type': 'hosted', 'hosted': {'write_policy': 'read_only'}})
    frozen = await publish(client, 'frozen', token, v1, 'harness-demo-1.0.0.tar.gz')
    assert frozen.status_code == 403


async def test_group_merges_and_publishes(
    services: Services, client: httpx.AsyncClient, token: dict[str, str], repos: None
) -> None:
    own = collection('ns', 'coll', '1.0.0')  # the hosted member has 1.0.0 too: it wins
    posted = await publish(client, 'all', token, own, 'ns-coll-1.0.0.tar.gz')
    assert posted.status_code == 202, posted.text
    task = posted.json()['task']
    assert task.startswith('/repository/all/api/v3/imports/collections/')
    await run_imports(services)
    assert (await client.get(task, headers=token)).json()['state'] == 'completed'

    page = (await client.get('/repository/all/api/v3/collections/ns/coll/versions/?limit=200', headers=token)).json()
    assert page['meta']['count'] == len(VERSIONS)
    detail = (await client.get('/repository/all/api/v3/collections/ns/coll/versions/1.0.0/', headers=token)).json()
    assert detail['artifact']['sha256'] == sha(own)
    assert detail['download_url'].endswith('/repository/all/collections/ns/coll/ns-coll-1.0.0.tar.gz')
    mine = await client.get('/repository/all/collections/ns/coll/ns-coll-1.0.0.tar.gz', headers=token)
    assert served(services, mine) == own and mine.headers['x-ws-group-member'] == 'mine'
    coll = (await client.get('/repository/all/api/v3/collections/ns/coll/', headers=token)).json()
    assert coll['highest_version']['version'] == '1.0.119'
    upstream_only = await client.get('/repository/all/api/v3/collections/ns/coll/versions/1.0.5/', headers=token)
    assert upstream_only.status_code == 404  # the fake upstream has no document for it
    assert (await client.get('/repository/all/api/v3/collections/zz/none/', headers=token)).status_code == 404
    assert (await client.get('/repository/all/', headers=token)).json()['available_versions']['v1'] == 'api/v1/'
    roles = (await client.get('/repository/all/api/v1/roles/7/versions/', headers=token)).json()
    assert roles['results'][0]['download_url'].endswith('/repository/all/roles/7/1.0.0.tar.gz')

    await plugin(services).drain()
    versions = (
        await client.get(
            '/api/v1/repositories/all/galaxy/versions',
            params={'namespace': 'ns', 'collection': 'coll', 'limit': 200},
            headers=token,
        )
    ).json()
    first = next(v for v in versions['items'] if v['version'] == '1.0.0')
    assert first['sha256'] == sha(own) and first['uploaded_by'] == 'admin'  # first member wins
    assert len(versions['items']) == len(VERSIONS)
