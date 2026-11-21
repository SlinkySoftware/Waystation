"""Swift Package Registry repositories (T-P5-SWIFT) end to end on real MongoDB/Valkey with a scriptable local Git
host: the SCM-backed proxy (tags from the ref advertisement, deterministic archives built once and pinned, manifests
with version-specific alternates, /identifiers, a rebuilt archive that differs is refused), hosted publishing
(multipart, metadata, signature recorded, immutability, removal as 410, write policy, archive checks), login, API
version negotiation, browse and search."""

import base64
import hashlib
import io
import json
import tarfile
import zipfile
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from ws_fake_upstream import FakeUpstream, Respond, Seen
from ws_testing import asgi_client

from waystation_common.db.collections import ASSETS, AUDIT, SWIFT_PACKAGES, SWIFT_RELEASES
from waystation_common.settings import Settings
from waystation_server.bootstrap import ensure_bootstrap_admin
from waystation_server.services import Services

pytestmark = pytest.mark.integration

PASSWORD = 'correct horse battery staple'
MANIFEST = b'// swift-tools-version:5.9\nimport PackageDescription\n\nlet package = Package(name: "LinkedList")\n'
OLD_MANIFEST = b'// swift-tools-version:5.7\nimport PackageDescription\n\nlet package = Package(name: "LinkedList")\n'
ACCEPT_JSON = {'Accept': 'application/vnd.swift.registry.v1+json'}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pkt(line: str) -> bytes:
    data = line.encode()
    return f'{len(data) + 4:04x}'.encode() + data


def advertisement(tags: dict[str, str]) -> bytes:
    out = pkt('# service=git-upload-pack\n') + b'0000' + pkt('1' * 40 + ' HEAD\x00multi_ack\n')
    for tag, oid in tags.items():
        out += pkt(f'{oid} refs/tags/{tag}\n')
    return out + b'0000'


def tag_tarball(top: str, *, extra: bytes = b'') -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, data in (
            ('Package.swift', MANIFEST),
            ('Package@swift-5.7.swift', OLD_MANIFEST),
            ('Sources/LinkedList/LinkedList.swift', b'public struct LinkedList {}\n' + extra),
        ):
            info = tarfile.TarInfo(f'{top}/{name}')
            info.size = len(data)
            info.mtime = 1_700_000_000
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def source_zip(top: str = 'LinkedList', *, manifest: bytes = MANIFEST) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f'{top}/Package.swift', manifest)
        zf.writestr(f'{top}/Sources/LinkedList/LinkedList.swift', b'public struct LinkedList {}\n')
    return buf.getvalue()


def multipart(
    archive: bytes | None, metadata: dict[str, Any] | None, signature: bytes | None = None
) -> tuple[bytes, str]:
    boundary = 'swift-boundary-1'
    body = b''
    if archive is not None:
        body += (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="source-archive"\r\n'
                'Content-Type: application/zip\r\nContent-Transfer-Encoding: binary\r\n\r\n'
            ).encode()
            + archive
            + b'\r\n'
        )
    if signature is not None:
        body += (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="source-archive-signature"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + signature
            + b'\r\n'
        )
    if metadata is not None:
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n'
            'Content-Type: application/json\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n'
            f'{json.dumps(metadata)}\r\n'
        ).encode()
    body += f'--{boundary}--\r\n'.encode()
    return body, f'multipart/form-data;boundary="{boundary}"'


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory(
        ssrf_allow_cidrs=['127.0.0.0/8'], bootstrap_admin_password=SecretStr(PASSWORD), login_rate_limit=10_000
    )


@pytest.fixture
async def git() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        tarballs = {'1.0.0': tag_tarball('LinkedList-1.0.0'), 'v1.1.0': tag_tarball('LinkedList-1.1.0', extra=b'//')}
        fake.routes.update(
            {
                '/mona/LinkedList.git/info/refs?service=git-upload-pack': Respond(
                    200,
                    {'Content-Type': 'application/x-git-upload-pack-advertisement'},
                    advertisement({'1.0.0': '2' * 40, 'v1.1.0': '3' * 40, 'nightly': '4' * 40}),
                ),
                '/mona/LinkedList/archive/refs/tags/1.0.0.tar.gz': Respond(302, {'Location': '/codeload/1.0.0'}),
                '/codeload/1.0.0': Respond(200, {}, tarballs['1.0.0']),
                '/mona/LinkedList/archive/refs/tags/v1.1.0.tar.gz': Respond(200, {}, tarballs['v1.1.0']),
                '/nobody/missing.git/info/refs?service=git-upload-pack': Respond(401, {}, b''),
                '/mona/NotSwift.git/info/refs?service=git-upload-pack': Respond(
                    200, {}, advertisement({'1.0.0': '5' * 40})
                ),
                '/mona/NotSwift/archive/refs/tags/1.0.0.tar.gz': Respond(200, {}, _readme_only()),
            }
        )
        yield fake


def _readme_only() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        info = tarfile.TarInfo('NotSwift-1.0.0/README.md')
        info.size = 2
        tar.addfile(info, io.BytesIO(b'hi'))
    return buf.getvalue()


@pytest.fixture
async def admin(services: Services) -> AsyncIterator[httpx.AsyncClient]:
    await ensure_bootstrap_admin(services.db, services.settings)
    async with asgi_client(services) as client:
        response = await client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        yield client


@pytest.fixture
async def token(admin: httpx.AsyncClient) -> dict[str, str]:
    created = await admin.post('/api/v1/auth/me/tokens', json={'name': 'swift', 'scopes': ['read', 'write', 'delete']})
    assert created.status_code == 201, created.text
    return {'Authorization': f'Bearer {created.json()["secret"]}'}


@pytest.fixture
async def repos(admin: httpx.AsyncClient, git: FakeUpstream) -> None:
    proxy = {'upstream_url': git.url, 'allow_private_upstream': True, 'metadata_max_age_s': 3600}
    for body in (
        {'name': 'spm', 'type': 'proxy', 'proxy': proxy},
        {'name': 'packages', 'type': 'hosted'},
        {'name': 'frozen', 'type': 'hosted', 'hosted': {'write_policy': 'read_only'}},
    ):
        made = await admin.post('/api/v1/repositories', json={'format': 'swift', 'online': True, **body})
        assert made.status_code == 201, made.text


def served(services: Services, response: httpx.Response) -> bytes:
    """The bytes behind an X-Accel answer (the ETag names the blob)."""
    assert response.status_code == 200, response.text
    assert 'x-accel-redirect' in response.headers
    digest = response.headers['etag'].strip('"').removeprefix('sha256:')
    return services.blobs.blob_path(digest).read_bytes()


def fetches(git: FakeUpstream, path: str) -> int:
    return sum(1 for s in git.seen if s.path == path)


async def test_scm_proxy(
    services: Services, client: httpx.AsyncClient, token: dict[str, str], repos: None, git: FakeUpstream
) -> None:
    auth = {**token, **ACCEPT_JSON}
    listed = await client.get('/repository/spm/mona/LinkedList', headers=auth)
    assert listed.status_code == 200, listed.text
    assert listed.headers['content-version'] == '1'
    assert list(listed.json()['releases']) == ['1.1.0', '1.0.0']
    assert listed.json()['releases']['1.0.0']['url'].endswith('/repository/spm/mona/LinkedList/1.0.0')
    assert '/mona/LinkedList/1.1.0>; rel="latest-version"' in listed.headers['link']
    assert f'<{git.url.rstrip("/")}/mona/LinkedList>; rel="canonical"' in listed.headers['link']
    assert (await client.get('/repository/spm/mona/LinkedList.json', headers=auth)).json() == listed.json()
    assert (await client.get('/repository/spm/nobody/missing', headers=auth)).status_code == 404

    info = await client.get('/repository/spm/mona/LinkedList/1.0.0', headers=auth)
    assert info.status_code == 200, info.text
    doc = info.json()
    checksum = doc['resources'][0]['checksum']
    assert doc['id'] == 'mona.LinkedList' and doc['version'] == '1.0.0' and doc['publishedAt']
    assert doc['metadata']['repositoryURLs'] == [f'{git.url.rstrip("/")}/mona/LinkedList']
    assert 'rel="successor-version"' in info.headers['link'] and 'rel="latest-version"' in info.headers['link']

    archive = await client.get('/repository/spm/mona/LinkedList/1.0.0.zip', headers=token)
    data = served(services, archive)
    assert sha(data) == checksum
    assert archive.headers['content-type'] == 'application/zip'
    assert archive.headers['content-disposition'] == 'attachment; filename="LinkedList-1.0.0.zip"'
    assert archive.headers['digest'] == 'sha-256=' + base64.b64encode(bytes.fromhex(checksum)).decode()
    assert archive.headers['content-version'] == '1'
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == [
            'LinkedList/Package.swift',
            'LinkedList/Package@swift-5.7.swift',
            'LinkedList/Sources/LinkedList/LinkedList.swift',
        ]

    manifest = await client.get('/repository/spm/mona/LinkedList/1.0.0/Package.swift', headers=token)
    assert served(services, manifest) == MANIFEST
    assert manifest.headers['content-type'] == 'text/x-swift'
    assert manifest.headers['link'].endswith(
        '/repository/spm/mona/LinkedList/1.0.0/Package.swift?swift-version=5.7>; rel="alternate"; '
        'filename="Package@swift-5.7.swift"; swift-tools-version="5.7"'
    )
    old = await client.get('/repository/spm/mona/LinkedList/1.0.0/Package.swift?swift-version=5.7', headers=token)
    assert served(services, old) == OLD_MANIFEST
    other = await client.get('/repository/spm/mona/LinkedList/1.0.0/Package.swift?swift-version=6.0', headers=token)
    assert other.status_code == 303 and other.headers['location'].endswith('/mona/LinkedList/1.0.0/Package.swift')

    # built once, then served from the pinned record (case-insensitive identity)
    again = await client.get('/repository/spm/MONA/linkedlist/1.0.0', headers=auth)
    assert again.json()['resources'][0]['checksum'] == checksum and again.json()['id'] == 'mona.LinkedList'
    assert fetches(git, '/codeload/1.0.0') == 1
    assert await services.db[SWIFT_RELEASES].count_documents({'source': 'scm'}) == 1

    # the files are lost and the tag now gives different bytes: the pinned checksum wins, nothing else is served
    spm = await services.catalog.get('spm')
    assert spm is not None
    await services.db[ASSETS].delete_many({'repo_id': spm.id, 'path': {'$regex': r'^(mona/|_scm/.*\.tar\.gz$)'}})
    git.routes['/codeload/1.0.0'] = Respond(200, {}, tag_tarball('LinkedList-1.0.0', extra=b'// moved tag\n'))
    moved = await client.get('/repository/spm/mona/LinkedList/1.0.0.zip', headers=token)
    assert moved.status_code == 502 and 'pinned' in moved.json()['detail']
    assert (await client.get('/repository/spm/mona/LinkedList/1.0.0', headers=auth)).json() == doc
    # the tag is moved back (and the cached tarball of the moved tag dropped): the rebuild matches again
    await services.db[ASSETS].delete_many({'repo_id': spm.id, 'path': {'$regex': r'^_scm/.*\.tar\.gz$'}})
    git.routes['/codeload/1.0.0'] = Respond(200, {}, tag_tarball('LinkedList-1.0.0'))
    assert sha(served(services, await client.get('/repository/spm/mona/LinkedList/1.0.0.zip', headers=token))) == (
        checksum
    )

    # a tag without Package.swift is not a release; unknown versions and packages are problems
    not_swift = await client.get('/repository/spm/mona/NotSwift/1.0.0', headers=auth)
    assert not_swift.status_code == 404 and 'Package.swift' in not_swift.json()['detail']
    missing = await client.get('/repository/spm/mona/LinkedList/9.9.9', headers=auth)
    assert missing.status_code == 404 and missing.headers['content-type'] == 'application/problem+json'
    assert missing.json()['detail'] == 'release not found'

    # identifiers on the Git host
    ids = await client.get('/repository/spm/identifiers', params={'url': f'{git.url}mona/LinkedList.git'}, headers=auth)
    assert ids.status_code == 200 and ids.json() == {'identifiers': ['mona.LinkedList']}
    for url, status in (
        (f'{git.url}nobody/missing', 404),
        ('https://elsewhere.example.com/mona/LinkedList', 404),
        ('not a url', 400),
    ):
        assert (
            await client.get('/repository/spm/identifiers', params={'url': url}, headers=auth)
        ).status_code == status
    assert (await client.get('/repository/spm/identifiers', headers=auth)).status_code == 400

    # publishing to a proxy is not supported; API versions are negotiated
    body, ctype = multipart(source_zip(), {})
    put = await client.put(
        '/repository/spm/mona/LinkedList/2.0.0', content=body, headers={**token, 'Content-Type': ctype}
    )
    assert put.status_code == 405 and put.headers['content-version'] == '1'
    v2 = await client.get(
        '/repository/spm/mona/LinkedList', headers={**token, 'Accept': 'application/vnd.swift.registry.v2+json'}
    )
    assert v2.status_code == 415 and v2.json()['detail'] == 'unsupported API version'
    bad = await client.get(
        '/repository/spm/mona/LinkedList', headers={**token, 'Accept': 'application/vnd.swift.registry.vx'}
    )
    assert bad.status_code == 400


async def test_hosted_publish_and_remove(
    services: Services, client: httpx.AsyncClient, token: dict[str, str], repos: None
) -> None:
    auth = {**token, **ACCEPT_JSON}
    metadata = {'description': 'One thing links to another.', 'repositoryURLs': ['https://github.com/mona/LinkedList']}
    archive = source_zip()
    body, ctype = multipart(archive, metadata)
    anonymous = await client.put(
        '/repository/packages/mona/LinkedList/1.0.0', content=body, headers={'Content-Type': ctype}
    )
    assert anonymous.status_code == 401
    published = await client.put(
        '/repository/packages/mona/LinkedList/1.0.0', content=body, headers={**auth, 'Content-Type': ctype}
    )
    assert published.status_code == 201, published.text
    assert published.headers['location'].endswith('/repository/packages/mona/LinkedList/1.0.0')
    assert published.headers['content-version'] == '1'
    again = await client.put(
        '/repository/packages/mona/linkedlist/1.0.0', content=body, headers={**auth, 'Content-Type': ctype}
    )
    assert again.status_code == 409 and 'already exists' in again.json()['detail']

    info = (await client.get('/repository/packages/mona/LinkedList/1.0.0', headers=auth)).json()
    assert info['resources'][0]['checksum'] == sha(archive) and info['metadata'] == metadata
    got = await client.get('/repository/packages/mona/LinkedList/1.0.0.zip', headers=token)
    assert served(services, got) == archive
    assert (
        served(services, await client.get('/repository/packages/mona/LinkedList/1.0.0/Package.swift', headers=token))
        == MANIFEST
    )
    ids = await client.get(
        '/repository/packages/identifiers', params={'url': 'git@github.com:mona/linkedlist.git'}, headers=auth
    )
    assert ids.json() == {'identifiers': ['mona.LinkedList']}

    # a second release, signed (the signer is recorded, the signature is passed on)
    signature = b'\x30\x80not-really-cms'
    body2, _ = multipart(source_zip(), {}, signature=signature)
    unsigned_format = await client.put(
        '/repository/packages/mona/LinkedList/1.1.0', content=body2, headers={**auth, 'Content-Type': ctype}
    )
    assert unsigned_format.status_code == 422 and 'X-Swift-Package-Signature-Format' in unsigned_format.json()['detail']
    not_cms = await client.put(
        '/repository/packages/mona/LinkedList/1.1.0',
        content=body2,
        headers={**auth, 'Content-Type': ctype, 'X-Swift-Package-Signature-Format': 'cms-1.0.0'},
    )
    assert not_cms.status_code == 422 and 'CMS' in not_cms.json()['detail']

    for broken, detail in (
        (source_zip(manifest=b'import PackageDescription\n'), 'swift-tools-version'),
        (b'PK\x03\x04garbage', 'not a readable zip'),
    ):
        body3, _ = multipart(broken, None)
        refused = await client.put(
            '/repository/packages/mona/LinkedList/1.2.0', content=body3, headers={**auth, 'Content-Type': ctype}
        )
        assert refused.status_code == 422 and detail in refused.json()['detail']
    no_archive, _ = multipart(None, metadata)
    assert (
        await client.put(
            '/repository/packages/mona/LinkedList/1.2.0', content=no_archive, headers={**auth, 'Content-Type': ctype}
        )
    ).status_code == 422
    bad_meta, _ = multipart(source_zip(), None)
    bad_meta = bad_meta.replace(
        b'--swift-boundary-1--',
        b'--swift-boundary-1\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n{nope\r\n--swift-boundary-1--',
    )
    assert (
        await client.put(
            '/repository/packages/mona/LinkedList/1.2.0', content=bad_meta, headers={**auth, 'Content-Type': ctype}
        )
    ).status_code == 422
    frozen = await client.put(
        '/repository/frozen/mona/LinkedList/1.0.0', content=body, headers={**auth, 'Content-Type': ctype}
    )
    assert frozen.status_code == 403

    body4, _ = multipart(source_zip(), {})
    second = await client.put(
        '/repository/packages/mona/LinkedList/2.0.0-beta.1', content=body4, headers={**auth, 'Content-Type': ctype}
    )
    assert second.status_code == 201
    listed = await client.get('/repository/packages/mona/LinkedList', headers=auth)
    assert list(listed.json()['releases']) == ['2.0.0-beta.1', '1.0.0']
    assert '<https://github.com/mona/LinkedList>; rel="canonical"' in listed.headers['link']

    # removal: the version stays taken and answers 410
    assert (await client.delete('/repository/packages/mona/LinkedList/1.0.0')).status_code == 401
    removed = await client.delete('/repository/packages/mona/LinkedList/1.0.0', headers=auth)
    assert removed.status_code == 204
    gone = await client.get('/repository/packages/mona/LinkedList/1.0.0', headers=auth)
    assert gone.status_code == 410
    assert (await client.get('/repository/packages/mona/LinkedList/1.0.0.zip', headers=token)).status_code == 410
    listed = (await client.get('/repository/packages/mona/LinkedList', headers=auth)).json()
    assert listed['releases']['1.0.0']['problem']['status'] == 410
    assert (await client.delete('/repository/packages/mona/LinkedList/1.0.0', headers=auth)).status_code == 410
    reuse = await client.put(
        '/repository/packages/mona/LinkedList/1.0.0', content=body, headers={**auth, 'Content-Type': ctype}
    )
    assert reuse.status_code == 409 and 'removed' in reuse.json()['detail']
    packages = await services.catalog.get('packages')
    assert packages is not None
    assert (
        await services.db[ASSETS].count_documents(
            {'repo_id': packages.id, 'path': {'$regex': '^mona/linkedlist/1.0.0'}}
        )
        == 0
    )
    summary = await services.db[SWIFT_PACKAGES].find_one({'repo_id': packages.id})
    assert summary is not None and (summary['latest'], summary['release_count'], summary['removed_count']) == (
        '2.0.0-beta.1',
        1,
        1,
    )
    await services.audit.flush()
    actions = [d['action'] async for d in services.db[AUDIT].find({'target.kind': 'swift_release'})]
    assert actions.count('content.upload') == 2 and actions.count('content.delete') == 1


async def test_login(client: httpx.AsyncClient, token: dict[str, str], repos: None) -> None:
    for path in ('/repository/packages/login', '/repository/packages/'):
        ok = await client.post(path, headers=token)
        assert ok.status_code == 200 and ok.headers['content-version'] == '1'
        assert (await client.post(path)).status_code == 401
    wrong = await client.post('/repository/packages/login', headers={'Authorization': 'Bearer nope'})
    assert wrong.status_code == 401
    assert (await client.get('/repository/packages/login', headers=token)).status_code == 405


async def test_browse_and_search(
    services: Services, client: httpx.AsyncClient, admin: httpx.AsyncClient, token: dict[str, str], repos: None
) -> None:
    for version in ('1.0.0', '1.1.0'):
        body, ctype = multipart(source_zip(), {'description': 'Lists'})
        made = await client.put(
            f'/repository/packages/mona/LinkedList/{version}', content=body, headers={**token, 'Content-Type': ctype}
        )
        assert made.status_code == 201
    scopes = (await admin.get('/api/v1/repositories/packages/swift/scopes')).json()
    assert scopes == {'items': [{'scope': 'mona', 'package_count': 1}], 'next_cursor': None}
    packages = (await admin.get('/api/v1/repositories/packages/swift/packages', params={'scope': 'MONA'})).json()
    assert packages['items'][0]['id'] == 'mona.LinkedList' and packages['items'][0]['latest_version'] == '1.1.0'
    assert packages['items'][0]['description'] == 'Lists'
    releases = (
        await admin.get(
            '/api/v1/repositories/packages/swift/releases',
            params={'scope': 'mona', 'package': 'linkedlist', 'limit': 1},
        )
    ).json()
    assert [r['version'] for r in releases['items']] == ['1.1.0'] and releases['next_cursor']
    first = releases['items'][0]
    assert first['state'] == 'published' and first['source'] == 'hosted' and first['tools_version'] == '5.9'
    assert first['download_url'].endswith('/repository/packages/mona/linkedlist/1.1.0.zip')
    assert first['manifests'][0]['url'].endswith('/mona/linkedlist/1.1.0/Package.swift')
    rest = (
        await admin.get(
            '/api/v1/repositories/packages/swift/releases',
            params={'scope': 'mona', 'package': 'LinkedList', 'cursor': releases['next_cursor']},
        )
    ).json()
    assert [r['version'] for r in rest['items']] == ['1.0.0']
    assert (
        await admin.get('/api/v1/repositories/packages/swift/releases', params={'scope': 'x', 'package': 'y'})
    ).status_code == 404
    hits = (await admin.get('/api/v1/search', params={'q': 'linkedl', 'format': 'swift'})).json()
    assert [(h['name'], h['version']) for h in hits['items']] == [('mona.LinkedList', '1.1.0')]
    assert hits['items'][0]['browse']['query'] == {'scope': 'mona', 'package': 'LinkedList'}


async def test_upstream_requests_carry_no_credentials(
    client: httpx.AsyncClient, token: dict[str, str], repos: None, git: FakeUpstream
) -> None:
    assert (await client.get('/repository/spm/mona/LinkedList/1.1.0', headers=token)).status_code == 200
    seen: list[Seen] = [s for s in git.seen if 'LinkedList' in s.path]
    assert seen and all('authorization' not in s.headers for s in seen)
