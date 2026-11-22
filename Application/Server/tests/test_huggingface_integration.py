"""Hugging Face (T-P6-HF) end to end on the real engine, MongoDB and Valkey: a Hub proxy against a scriptable Hub
(info documents with ``blobs=true``, relative redirects for regular files, LFS files redirected to an allowed CDN
host, a CDN host that is not allowed, an LFS file whose bytes do not match its sha256), ``HEAD`` answered the way
huggingface_hub expects, tree listings without ``xetHash`` and with rewritten ``Link`` pages, upstream bearer
credentials sent to the Hub only, browse, search, components and the repository rules (proxy only)."""

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond
from ws_testing import asgi_client

from waystation_common.db.collections import ASSETS, HUGGINGFACE_REPOS
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.problems import ResponseAborted
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
HF_TOKEN = 'hf_example_token_for_tests'
C1 = '1' * 40
C2 = '2' * 40
CONFIG = b'{"model_type": "bert", "hidden_size": 32}\n'
WEIGHTS = bytes(range(256)) * 40
TAMPERED = bytes(reversed(WEIGHTS))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob(data: bytes) -> str:
    return hashlib.sha1(b'blob %d\0' % len(data) + data, usedforsecurity=False).hexdigest()


def info(commit: str, *, config: bytes = CONFIG) -> bytes:
    siblings = [
        {'rfilename': 'config.json', 'blobId': git_blob(config), 'size': len(config)},
        {
            'rfilename': 'model.safetensors',
            'blobId': 'a' * 40,
            'size': len(WEIGHTS),
            'lfs': {'sha256': sha(WEIGHTS), 'size': len(WEIGHTS), 'pointerSize': 130},
        },
        {
            'rfilename': 'tampered.bin',
            'blobId': 'b' * 40,
            'size': len(WEIGHTS),
            'lfs': {'sha256': sha(WEIGHTS), 'size': len(WEIGHTS), 'pointerSize': 130},
        },
        {
            'rfilename': 'elsewhere.bin',
            'blobId': 'c' * 40,
            'size': len(WEIGHTS),
            'lfs': {'sha256': sha(WEIGHTS), 'size': len(WEIGHTS), 'pointerSize': 130},
        },
    ]
    return json.dumps({'id': 'org/tiny', 'sha': commit, 'siblings': siblings, 'private': False}).encode()


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def cdn() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream(host='127.0.0.2') as fake:
        fake.routes.update(
            {
                '/xet-bridge/weights?X-Amz-Signature=s1': Respond(200, {}, WEIGHTS, chunk=1024),
                '/xet-bridge/tampered?X-Amz-Signature=s2': Respond(200, {}, TAMPERED, chunk=1024),
            }
        )
        yield fake


@pytest.fixture
async def elsewhere() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream(host='127.0.0.3') as fake:
        fake.routes['/file'] = Respond(200, {}, WEIGHTS)
        yield fake


@pytest.fixture
async def hub(cdn: FakeUpstream, elsewhere: FakeUpstream) -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        tree = json.dumps(
            [
                {'type': 'file', 'oid': git_blob(CONFIG), 'size': len(CONFIG), 'path': 'config.json'},
                {
                    'type': 'file',
                    'oid': 'a' * 40,
                    'size': len(WEIGHTS),
                    'lfs': {'oid': sha(WEIGHTS), 'size': len(WEIGHTS), 'pointerSize': 130},
                    'xetHash': 'f' * 64,
                    'path': 'model.safetensors',
                },
            ]
        ).encode()
        link = f'<{fake.url}api/models/org/tiny/tree/{C1}?recursive=true&expand=false&cursor=abc>; rel="next"'
        json_type = {'Content-Type': 'application/json'}
        fake.routes.update(
            {
                '/api/models/org/tiny/revision/main?blobs=true': Respond(200, json_type, info(C1)),
                f'/api/models/org/tiny/revision/{C1}?blobs=true': Respond(200, json_type, info(C1)),
                '/api/models/org/tiny?blobs=true': Respond(200, json_type, info(C1)),
                '/api/datasets/org/data/revision/main?blobs=true': Respond(200, json_type, info(C2)),
                '/api/models/org/gone/revision/main?blobs=true': Respond(404, {'X-Error-Code': 'RepoNotFound'}, b''),
                f'/api/models/org/tiny/tree/{C1}?recursive=true&expand=false': Respond(
                    200, {**json_type, 'Link': link}, tree
                ),
                f'/org/tiny/resolve/{C1}/config.json': Respond(
                    307, {'Location': f'/api/resolve-cache/models/org/tiny/{C1}/config.json?etag=x'}, b''
                ),
                f'/api/resolve-cache/models/org/tiny/{C1}/config.json?etag=x': Respond(200, {}, CONFIG),
                f'/org/tiny/resolve/{C1}/model.safetensors': Respond(
                    302, {'Location': f'{cdn.url}xet-bridge/weights?X-Amz-Signature=s1'}, b''
                ),
                f'/org/tiny/resolve/{C1}/tampered.bin': Respond(
                    302, {'Location': f'{cdn.url}xet-bridge/tampered?X-Amz-Signature=s2'}, b''
                ),
                f'/org/tiny/resolve/{C1}/elsewhere.bin': Respond(302, {'Location': f'{elsewhere.url}file'}, b''),
                f'/datasets/org/data/resolve/{C2}/config.json': Respond(200, {}, CONFIG),
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
async def repo(admin: httpx.AsyncClient, hub: FakeUpstream) -> None:
    body = {
        'name': 'hf',
        'format': 'huggingface',
        'type': 'proxy',
        'online': True,
        'proxy': {
            'upstream_url': hub.url,
            'allow_private_upstream': True,
            'auth': {'kind': 'bearer', 'secret': HF_TOKEN},
        },
        'format_options': {'cdn_hosts': ['127.0.0.2', '*.hf.co']},
    }
    made = await admin.post('/api/v1/repositories', json=body)
    assert made.status_code == 201, made.text


async def stored(services: Services, path: str) -> dict[str, Any] | None:
    repo = await services.catalog.get('hf')
    assert repo is not None
    doc: dict[str, Any] | None = await services.db[ASSETS].find_one({'repo_id': repo.id, 'path': path})
    return doc


def blob(services: Services, response: httpx.Response) -> bytes:
    """The bytes of an X-Accel answer (the blob it points at) or of a streamed one."""
    accel = response.headers.get('x-accel-redirect')
    if accel is None:
        return response.content
    return Path(services.proxy.blobs.blob_path(accel.rsplit('/', 1)[-1])).read_bytes()


# ---------------------------------------------------------------------------- files
@pytest.mark.usefixtures('repo')
async def test_head_get_and_verified_files(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services, hub: FakeUpstream
) -> None:
    url = '/repository/hf/org/tiny/resolve/main/config.json'
    head = await client.head(url)
    assert head.status_code == 200
    assert head.headers['x-repo-commit'] == C1
    assert head.headers['etag'] == head.headers['x-linked-etag'] == f'"{git_blob(CONFIG)}"'
    assert head.headers['x-linked-size'] == head.headers['content-length'] == str(len(CONFIG))
    assert 'x-xet-hash' not in head.headers and 'location' not in head.headers
    assert hub.hits(f'/org/tiny/resolve/{C1}/config.json') == 0  # HEAD comes from the info document

    got = await client.get(url)
    assert got.status_code == 200 and got.content == CONFIG and got.headers['x-repo-commit'] == C1
    asset = await stored(services, f'org/tiny/resolve/{C1}/config.json')
    assert asset is not None and asset['blob_sha256'] == sha(CONFIG) and asset['kind'] == 'content'
    again = await client.get(f'/repository/hf/org/tiny/resolve/{C1}/config.json')
    assert again.headers['x-ws-cache'] == 'HIT' and blob(services, again) == CONFIG
    assert hub.hits(f'/org/tiny/resolve/{C1}/config.json') == 1
    assert hub.hits('/api/models/org/tiny/revision/main?blobs=true') == 1

    lfs = await client.head('/repository/hf/org/tiny/resolve/main/model.safetensors')
    assert lfs.headers['x-linked-etag'] == f'"{sha(WEIGHTS)}"' and lfs.headers['x-linked-size'] == str(len(WEIGHTS))
    weights = await client.get('/repository/hf/org/tiny/resolve/main/model.safetensors')
    assert weights.status_code == 200 and weights.content == WEIGHTS
    assert await stored(services, f'org/tiny/resolve/{C1}/model.safetensors') is not None

    # Credentials: the Hub gets the repository's token, the CDN nothing; the client's own are never forwarded.
    seen = [s for s in hub.seen if s.path.startswith('/api/models/org/tiny/revision/')]
    assert seen and all(s.headers.get('authorization') == f'Bearer {HF_TOKEN}' for s in seen)

    # A commit a cached revision named is answered without its own document (a restart: nothing in memory).
    plugin: Any = services.formats.plugin('huggingface')
    plugin._commits.clear()
    plugin._parsed.clear()
    del hub.routes[f'/api/models/org/tiny/revision/{C1}?blobs=true']
    by_commit = await client.head(f'/repository/hf/org/tiny/resolve/{C1}/model.safetensors')
    assert by_commit.status_code == 200 and by_commit.headers['x-repo-commit'] == C1
    assert hub.hits(f'/api/models/org/tiny/revision/{C1}?blobs=true') == 0

    missing = await client.head('/repository/hf/org/tiny/resolve/main/nothere.json')
    assert missing.status_code == 404 and missing.headers['x-error-code'] == 'EntryNotFound'
    assert (await client.get('/repository/hf/org/gone/resolve/main/config.json')).status_code == 404
    assert (await admin.post(url)).status_code == 405


@pytest.mark.usefixtures('repo')
async def test_cdn_redirects_and_mismatches(
    client: httpx.AsyncClient, services: Services, cdn: FakeUpstream, elsewhere: FakeUpstream
) -> None:
    await client.get('/repository/hf/org/tiny/resolve/main/model.safetensors')
    assert all('authorization' not in s.headers for s in cdn.seen)

    with pytest.raises(ResponseAborted):  # right size, wrong sha256: cut before the last bytes
        await client.get('/repository/hf/org/tiny/resolve/main/tampered.bin')
    assert await stored(services, f'org/tiny/resolve/{C1}/tampered.bin') is None

    refused = await client.get('/repository/hf/org/tiny/resolve/main/elsewhere.bin')
    assert refused.status_code == 502
    assert elsewhere.seen == [] and await stored(services, f'org/tiny/resolve/{C1}/elsewhere.bin') is None


# ---------------------------------------------------------------------------- metadata
@pytest.mark.usefixtures('repo')
async def test_info_and_tree(client: httpx.AsyncClient, services: Services, hub: FakeUpstream) -> None:
    for path in ('api/models/org/tiny', 'api/models/org/tiny/revision/main', f'api/models/org/tiny/revision/{C1}'):
        got = await client.get(f'/repository/hf/{path}', params={'expand[]': 'siblings'})
        assert got.status_code == 200, path
        doc = json.loads(blob(services, got))
        assert doc['sha'] == C1 and {s['rfilename'] for s in doc['siblings']} >= {'config.json'}

    tree_url = f'/repository/hf/api/models/org/tiny/tree/{C1}'
    tree = await client.get(tree_url, params={'recursive': 'True', 'expand': 'False'})
    assert tree.status_code == 200
    entries = json.loads(blob(services, tree))
    assert [e['path'] for e in entries] == ['config.json', 'model.safetensors']
    assert all('xetHash' not in e for e in entries)
    assert tree.headers['link'].endswith(
        f'/repository/hf/api/models/org/tiny/tree/{C1}?recursive=true&expand=false&cursor=abc>; rel="next"'
    )
    assert hub.url not in tree.headers['link']
    assert (await client.get(tree_url, params={'recursive': 'true'})).headers['x-ws-cache'] == 'HIT'

    dataset = await client.get('/repository/hf/datasets/org/data/resolve/main/config.json')
    assert dataset.status_code == 200 and dataset.content == CONFIG
    assert await stored(services, f'datasets/org/data/resolve/{C2}/config.json') is not None


# ---------------------------------------------------------------------------- browse, search, components, rules
@pytest.mark.usefixtures('repo')
async def test_browse_search_components(
    client: httpx.AsyncClient, admin: httpx.AsyncClient, services: Services
) -> None:
    await client.get('/repository/hf/org/tiny/resolve/main/config.json')
    await client.get('/repository/hf/org/tiny/resolve/main/model.safetensors')
    await client.get('/repository/hf/datasets/org/data/resolve/main/config.json')
    repo = await services.catalog.get('hf')
    assert repo is not None
    summary = await services.db[HUGGINGFACE_REPOS].find_one({'repo_id': repo.id, 'name': 'org/tiny'})
    assert summary is not None and summary['latest'] == C1 and summary['refs'][0]['revision'] == 'main'

    base = '/api/v1/repositories/hf/huggingface'
    models = (await admin.get(f'{base}/models')).json()
    assert [m['name'] for m in models['items']] == ['datasets/org/data', 'org/tiny']
    tiny = models['items'][1]
    assert (tiny['kind'], tiny['latest_commit'], tiny['files']) == ('model', C1, 2)
    assert tiny['size_bytes'] == len(CONFIG) + len(WEIGHTS)
    revisions = (await admin.get(f'{base}/revisions', params={'model': 'org/tiny'})).json()
    assert revisions['refs'][0]['commit'] == C1
    assert [(c['commit'], c['files'], c['revisions']) for c in revisions['commits']] == [(C1, 2, ['main'])]
    files = (await admin.get(f'{base}/files', params={'model': 'org/tiny', 'commit': C1})).json()
    assert [f['path'] for f in files['items']] == ['config.json', 'model.safetensors']
    assert files['items'][1]['sha256'] == sha(WEIGHTS)
    assert (await admin.get(f'{base}/revisions', params={'model': 'org/none'})).status_code == 404

    found = (await admin.get('/api/v1/search', params={'q': 'tiny'})).json()
    assert any(h['name'] == 'org/tiny' and h['format'] == 'huggingface' for h in found['items']), found


async def test_only_proxies(admin: httpx.AsyncClient, hub: FakeUpstream) -> None:
    for kind in ('hosted', 'group'):
        body: dict[str, Any] = {'name': f'hf-{kind}', 'format': 'huggingface', 'type': kind, 'online': True}
        if kind == 'group':
            body['group'] = {'members': []}
        made = await admin.post('/api/v1/repositories', json=body)
        assert made.status_code == 422, made.text
    bad = await admin.post(
        '/api/v1/repositories',
        json={
            'name': 'hf-bad',
            'format': 'huggingface',
            'type': 'proxy',
            'online': True,
            'proxy': {'upstream_url': hub.url, 'allow_private_upstream': True},
            'format_options': {'cdn_hosts': ['https://cdn.example.com/']},
        },
    )
    assert bad.status_code == 422, bad.text
