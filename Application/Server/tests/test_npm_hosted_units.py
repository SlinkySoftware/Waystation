"""npm hosted and group helpers (T-P5-NPMH): routes, publish bodies, tarball checks, generated and merged packuments,
descriptor rules and "Set me up"."""

import base64
import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from typing import Any

import pytest

from waystation_common.errors import PayloadTooLarge, ValidationFailed
from waystation_server.formats.base import SetupContext
from waystation_server.formats.npm import publish
from waystation_server.formats.npm.descriptor import DESCRIPTOR, client_login, request_action
from waystation_server.formats.npm.group import write_action
from waystation_server.formats.npm.metadata import Op, Route, parse_route, valid_tag, valid_version
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.security.actions import Action

BASE = 'https://registry.example.com/repository/npm-hosted/'


def tarball(package_json: dict[str, Any] | bytes | None, *, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        files = dict(extra or {})
        if package_json is not None:
            raw = package_json if isinstance(package_json, bytes) else json.dumps(package_json).encode()
            files['package/package.json'] = raw
        files.setdefault('package/index.js', b'module.exports = 1\n')
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def publish_body(name: str = 'demo', version: str = '1.0.0', *, data: bytes | None = None, **meta: Any) -> bytes:
    tgz = data if data is not None else tarball({'name': name, 'version': version})
    filename = f'{name}-{version}.tgz'
    dist = {
        'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(tgz).digest()).decode(),
        'shasum': hashlib.sha1(tgz, usedforsecurity=False).hexdigest(),
        'tarball': f'http://registry.example.com/{name}/-/{filename}',
    }
    manifest = {'name': name, 'version': version, 'dist': dist, '_resolved': 'x', 'readme': '# Demo', **meta}
    return json.dumps(
        {
            '_id': name,
            'name': name,
            'dist-tags': {'latest': version},
            'versions': {version: manifest},
            '_attachments': {
                filename: {
                    'content_type': 'application/octet-stream',
                    'data': base64.b64encode(tgz).decode(),
                    'length': len(tgz),
                }
            },
        }
    ).encode()


# ---------------------------------------------------------------------------- routes
@pytest.mark.parametrize(
    ('path', 'route'),
    [
        ('demo/-rev/3-abc', Route(Op.PACKAGE_REV, 'demo', arg='3-abc')),
        ('@scope/demo/-rev/1', Route(Op.PACKAGE_REV, '@scope/demo', arg='1')),
        ('demo/-/demo-1.0.0.tgz/-rev/2-x', Route(Op.TARBALL, 'demo', 'demo-1.0.0.tgz')),
        ('repository/npm-hosted/demo/-/demo-1.0.0.tgz/-rev/2-x', Route(Op.TARBALL, 'demo', 'demo-1.0.0.tgz')),
        ('repository/npm-all/@scope/demo/-/demo-1.0.0.tgz', Route(Op.TARBALL, '@scope/demo', 'demo-1.0.0.tgz')),
        ('repository', Route(Op.PACKUMENT, 'repository')),  # a package may be called "repository"
        ('repository/-/repository-1.0.0.tgz', Route(Op.TARBALL, 'repository', 'repository-1.0.0.tgz')),
        ('repository/npm-hosted/demo', None),  # only tarballs behind the prefix
        ('-/package/demo/dist-tags', Route(Op.DIST_TAGS, 'demo')),
        ('-/package/@scope/demo/dist-tags', Route(Op.DIST_TAGS, '@scope/demo')),
        ('-/package/demo/dist-tags/beta', Route(Op.DIST_TAG, 'demo', arg='beta')),
        ('-/package/demo/dist-tags/1.0.0', None),  # a version is not a tag
        ('-/package/demo/other', None),
        ('-/package/@scope', None),
        ('-/user/org.couchdb.user:alice', Route(Op.LOGIN, arg='alice')),
        ('-/user/org.couchdb.user:', None),
        ('-/user/token/wst_abc', Route(Op.LOGOUT, arg='wst_abc')),
        ('-/user/other', None),
        ('demo/-rev/', None),
        ('demo/-rev/a b', None),
        ('demo/-/demo-1.0.0.tgz/x/y', None),
    ],
)
def test_parse_hosted_routes(path: str, route: Route | None) -> None:
    assert parse_route(path) == route


def test_tags_and_versions() -> None:
    assert valid_tag('beta') and valid_tag('next-2') and valid_tag('release.1')
    assert not valid_tag('1.0.0') and not valid_tag('v2.3.4') and not valid_tag('$set') and not valid_tag('.x')
    assert valid_version('1.0.0-rc.1+build.5') and not valid_version('v1.0.0') and not valid_version('1.0')


# ---------------------------------------------------------------------------- publish bodies
def test_publish_parsed_verified_and_cleaned() -> None:
    parsed = publish.parse_put(publish_body(scripts={'postinstall': 'node x.js'}), 'demo')
    assert isinstance(parsed, publish.Published)
    assert (parsed.package, parsed.version, parsed.filename) == ('demo', '1.0.0', 'demo-1.0.0.tgz')
    assert parsed.dist_tags == {'latest': '1.0.0'} and parsed.readme == '# Demo'
    manifest = parsed.manifest
    assert manifest['_id'] == 'demo@1.0.0' and '_resolved' not in manifest and 'readme' not in manifest
    assert manifest['hasInstallScript'] is True
    dist = manifest['dist']
    assert dist['integrity'].startswith('sha512-') and len(dist['shasum']) == 40 and 'tarball' not in dist
    assert dist['fileCount'] == 2 and dist['unpackedSize'] > 0
    assert parsed.sha256 == hashlib.sha256(parsed.tarball).hexdigest()


def test_scoped_publish_attachment_names() -> None:
    tgz = tarball({'name': '@scope/demo', 'version': '2.0.0'})
    body = json.loads(publish_body('@scope/demo', '2.0.0', data=tgz))
    assert '@scope/demo-2.0.0.tgz' in body['_attachments']  # what npm sends
    parsed = publish.parse_put(json.dumps(body).encode(), '@scope/demo')
    assert isinstance(parsed, publish.Published) and parsed.filename == 'demo-2.0.0.tgz'


def mutate(body: bytes, change: Any) -> bytes:
    doc = json.loads(body)
    change(doc)
    return json.dumps(doc).encode()


@pytest.mark.parametrize(
    ('change', 'message'),
    [
        (lambda d: d.update(name='other'), 'names package'),
        (lambda d: d['versions']['1.0.0'].update(version='2.0.0'), 'another package or version'),
        (lambda d: d['versions'].update({'2.0.0': {}}), 'one version'),
        (lambda d: d.update(_attachments={'evil.tgz': d['_attachments']['demo-1.0.0.tgz']}), 'must be named'),
        (lambda d: d['_attachments']['demo-1.0.0.tgz'].update(data='***'), 'base64'),
        (lambda d: d['_attachments']['demo-1.0.0.tgz'].update(data=''), 'no data'),
        (lambda d: d['_attachments']['demo-1.0.0.tgz'].update(length=1), 'length says'),
        (lambda d: d['versions']['1.0.0']['dist'].update(shasum='0' * 40), 'dist.shasum'),
        (
            lambda d: d['versions']['1.0.0']['dist'].update(integrity='sha512-' + base64.b64encode(b'x' * 64).decode()),
            'dist.integrity',
        ),
        (lambda d: d['versions']['1.0.0']['dist'].update(integrity='sha512-***'), 'invalid dist.integrity'),
        (lambda d: d.update({'dist-tags': {'1.0.0': '1.0.0'}}), 'dist-tag name'),
        (lambda d: d.update({'dist-tags': {'beta': 'nope'}}), 'does not name a version'),
        (lambda d: d.update(versions={'v1': {'name': 'demo', 'version': 'v1'}}), 'SemVer'),
        (lambda d: d.update(versions=[]), 'versions must be'),
    ],
)
def test_publish_refusals(change: Any, message: str) -> None:
    with pytest.raises(ValidationFailed, match=message):
        publish.parse_put(mutate(publish_body(), change), 'demo')


def test_publish_tarball_must_match_the_version() -> None:
    other = tarball({'name': 'demo', 'version': '9.9.9'})
    with pytest.raises(ValidationFailed, match=r'demo@9\.9\.9'):
        publish.parse_put(publish_body(data=other), 'demo')
    with pytest.raises(ValidationFailed, match='not valid JSON'):
        publish.parse_put(b'{', 'demo')
    with pytest.raises(ValidationFailed, match='JSON object'):
        publish.parse_put(b'[]', 'demo')
    with pytest.raises(PayloadTooLarge):
        publish.parse_put(b' ' * (publish.MAX_PUT_BYTES + 1), 'demo')


@pytest.mark.parametrize(
    ('data', 'message'),
    [
        (b'not gzip', 'not gzip'),
        (b'\x1f\x8bgarbage', 'not a valid'),
        (tarball(None), 'no package/package.json'),
        (tarball(b'{oops'), 'not valid JSON'),
        (tarball(b'[]'), 'JSON object'),
        (tarball({'name': 'demo', 'version': '1.0.0'}, extra={'package/../evil': b'x'}), 'unsafe entry'),
        (tarball({'name': 'demo', 'version': '1.0.0'}, extra={'/abs': b'x'}), 'unsafe entry'),
    ],
)
def test_inspect_tarball_refusals(data: bytes, message: str) -> None:
    with pytest.raises(ValidationFailed, match=message):
        publish.inspect_tarball(data)


def test_inspect_tarball_any_top_directory() -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        raw = json.dumps({'name': 'x', 'version': '1.0.0'}).encode()
        info = tarfile.TarInfo('node/package.json')
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
        folder = tarfile.TarInfo('node/lib')
        folder.type = tarfile.DIRTYPE
        archive.addfile(folder)
    found = publish.inspect_tarball(buffer.getvalue())
    assert found.package_json['name'] == 'x' and found.file_count == 1


def test_update_body() -> None:
    body = json.dumps(
        {'_id': 'demo', '_rev': '3-x', 'name': 'demo', 'dist-tags': {'latest': '1.0.0'}, 'versions': {'1.0.0': {}}}
    ).encode()
    update = publish.parse_put(body, 'demo')
    assert isinstance(update, publish.Update)
    assert update.versions == {'1.0.0': {}} and update.dist_tags == {'latest': '1.0.0'}
    assert publish.parse_put(b'{"versions": {}}', 'demo') == publish.Update(versions={}, dist_tags=None)
    with pytest.raises(ValidationFailed, match=r'version 1\.0\.0'):
        publish.parse_put(b'{"versions": {"1.0.0": "x"}}', 'demo')
    assert publish.deprecation({'deprecated': 'old'}) == 'old'
    assert publish.deprecation({'deprecated': ''}) is None and publish.deprecation({}) is None


# ---------------------------------------------------------------------------- packuments
def record(version: str, **extra: Any) -> publish.VersionRecord:
    manifest = {
        'name': 'demo',
        'version': version,
        'description': f'v{version}',
        'license': 'MIT',
        'scripts': {'test': 'x'},
        'dependencies': {'lodash.get': '^4'},
        'dist': {'integrity': 'sha512-x', 'shasum': 'a' * 40},
        **extra,
    }
    return publish.VersionRecord(version, manifest, f'demo-{version}.tgz', datetime(2026, 1, 1, tzinfo=UTC))


def test_effective_tags_and_latest() -> None:
    assert publish.latest_of(['1.0.0', '2.0.0-rc.1', '1.10.0']) == '1.10.0'
    assert publish.latest_of(['2.0.0-rc.1', '2.0.0-rc.2']) == '2.0.0-rc.2'
    assert publish.latest_of([]) is None
    assert publish.effective_tags({'beta': '9.9.9', 'next': '1.0.0'}, ['1.0.0']) == {'latest': '1.0.0', 'next': '1.0.0'}
    assert publish.effective_tags({}, []) == {}


def test_build_packuments() -> None:
    state = publish.PackageState(
        name='demo',
        rev=3,
        dist_tags={'latest': '1.0.0', 'beta': '2.0.0-rc.1'},
        readme='# Demo',
        created=None,
        modified=datetime(2026, 2, 1, tzinfo=UTC),
        versions=[record('2.0.0-rc.1', deprecated='do not use'), record('1.0.0')],
    )
    full_raw, corgi_raw = publish.build_packuments(state, BASE)
    full, corgi = json.loads(full_raw), json.loads(corgi_raw)
    assert full['_id'] == 'demo' and full['_rev'].startswith('3-')
    assert list(full['versions']) == ['1.0.0', '2.0.0-rc.1']
    assert full['versions']['1.0.0']['dist']['tarball'] == f'{BASE}demo/-/demo-1.0.0.tgz'
    assert full['dist-tags'] == {'beta': '2.0.0-rc.1', 'latest': '1.0.0'}
    assert full['description'] == 'v1.0.0' and full['license'] == 'MIT' and full['readme'] == '# Demo'
    assert full['time']['created'] == '2026-01-01T00:00:00.000Z' and full['time']['modified'].startswith('2026-02-01')
    assert set(corgi) == {'name', 'modified', 'dist-tags', 'versions'}
    assert 'scripts' not in corgi['versions']['1.0.0'] and 'description' not in corgi['versions']['1.0.0']
    assert corgi['versions']['2.0.0-rc.1']['deprecated'] == 'do not use'
    assert corgi['versions']['1.0.0']['dependencies'] == {'lodash.get': '^4'}
    summary = publish.summary(state)
    assert summary['latest'] == '1.0.0' and summary['version_count'] == 2 and summary['license'] == 'MIT'


def test_records_of_skips_unreadable() -> None:
    docs = [
        {
            'version': '1.0.0',
            'manifest': json.dumps({'a': 1}),
            'tarball': 'd-1.0.0.tgz',
            'published_at': datetime.now(UTC),
        },
        {'version': '2.0.0', 'manifest': '{', 'tarball': 'd-2.0.0.tgz', 'published_at': datetime.now(UTC)},
        {'version': '3.0.0', 'manifest': '[]', 'tarball': 'd-3.0.0.tgz', 'published_at': datetime.now(UTC)},
    ]
    assert [r.version for r in publish.records_of(docs)] == ['1.0.0']


def test_merge_packuments_first_member_wins() -> None:
    hosted = {
        'name': 'demo',
        'dist-tags': {'latest': '1.0.0', 'beta': '2.0.0-rc.1'},
        'versions': {
            '1.0.0': {'version': '1.0.0', 'dist': {'tarball': f'{BASE}demo/-/demo-1.0.0.tgz', 'integrity': 'h1'}},
            '2.0.0-rc.1': {'version': '2.0.0-rc.1', 'dist': {'tarball': f'{BASE}demo/-/demo-2.0.0-rc.1.tgz'}},
        },
        'time': {'created': '2026-01-01T00:00:00.000Z', 'modified': '2026-01-02T00:00:00.000Z', '1.0.0': 'h'},
        'readme': 'hosted',
    }
    proxy = {
        'name': 'demo',
        'dist-tags': {'latest': '3.0.0', 'next': '3.0.0'},
        'versions': {
            '1.0.0': {'version': '1.0.0', 'dist': {'tarball': 'https://up.example.com/demo/-/demo-1.0.0.tgz'}},
            '3.0.0': {'version': '3.0.0', 'dist': {'tarball': 'https://registry.example.com/x/demo/-/demo-3.0.0.tgz'}},
        },
        'time': {'modified': '2026-03-01T00:00:00.000Z', '3.0.0': 'p'},
    }
    group = 'https://registry.example.com/repository/npm-all/'
    bodies = [b'not json', b'[]', json.dumps(hosted).encode(), json.dumps(proxy).encode()]
    merged = json.loads(publish.merge_packuments(bodies, 'demo', group, abbreviated=False))
    assert merged['versions']['1.0.0']['dist'] == {'tarball': f'{group}demo/-/demo-1.0.0.tgz', 'integrity': 'h1'}
    assert merged['versions']['3.0.0']['dist']['tarball'] == f'{group}demo/-/demo-3.0.0.tgz'
    assert merged['dist-tags'] == {'latest': '1.0.0', 'beta': '2.0.0-rc.1', 'next': '3.0.0'}
    assert merged['time'] == {
        'created': '2026-01-01T00:00:00.000Z',
        'modified': '2026-03-01T00:00:00.000Z',
        '1.0.0': 'h',
        '3.0.0': 'p',
    }
    assert merged['readme'] == 'hosted' and merged['_id'] == 'demo'
    corgi = json.loads(publish.merge_packuments(bodies[2:], 'demo', group, abbreviated=True))
    assert 'time' not in corgi and '_id' not in corgi and corgi['modified'] == '2026-03-01T00:00:00.000Z'
    with pytest.raises(ValidationFailed):
        publish.merge_packuments([b'x'], 'demo', group, abbreviated=True)
    assert publish.tags_of(json.dumps(proxy).encode()) == {'latest': '3.0.0', 'next': '3.0.0'}
    assert publish.tags_of(b'x') == {} and publish.tags_of(b'[]') == {}


# ---------------------------------------------------------------------------- descriptor
def test_descriptor_rules() -> None:
    assert client_login('PUT', '-/user/org.couchdb.user:alice')
    assert client_login('DELETE', '-/user/token/wst_x')
    assert not client_login('GET', '-/user/org.couchdb.user:alice') and not client_login('PUT', 'demo')
    assert request_action('DELETE', '-/package/demo/dist-tags/beta') is Action.WRITE
    assert request_action('DELETE', 'demo/-rev/1') is None and request_action('PUT', '-/package/x/dist-tags/y') is None
    assert write_action('DELETE', Route(Op.DIST_TAG, 'demo', arg='beta')) is Action.WRITE
    assert write_action('DELETE', Route(Op.PACKAGE_REV, 'demo')) is Action.DELETE
    assert write_action('PUT', Route(Op.PACKUMENT, 'demo')) is Action.WRITE
    assert DESCRIPTOR.types == frozenset({RepoType.PROXY, RepoType.HOSTED, RepoType.GROUP})
    assert DESCRIPTOR.writable_group_member


def _repo(kind: RepoType, **extra: Any) -> RepoConfig:
    now = datetime.now(UTC)
    return RepoConfig.model_validate(
        {
            '_id': 'r',
            'name': 'npm-x',
            'format': 'npm',
            'type': kind.value,
            'created_at': now,
            'updated_at': now,
            **extra,
        }
    )


def test_setup_snippets_publish_only_when_writable() -> None:
    def ids(repo: RepoConfig) -> list[str]:
        ctx = SetupContext(repo=repo, base_url='https://registry.example.com', registry_host='x', username='alice')
        return [c.id for c in DESCRIPTOR.setup_snippets(ctx)]

    assert ids(_repo(RepoType.HOSTED))[-2:] == ['npm-publish', 'pnpm-publish']
    assert 'npm-publish' not in ids(_repo(RepoType.PROXY, proxy={'upstream_url': 'https://registry.npmjs.org'}))
    assert 'npm-publish' not in ids(_repo(RepoType.GROUP, group={'members': []}))
    assert 'npm-publish' in ids(_repo(RepoType.GROUP, group={'members': ['a'], 'writable_member': 'a'}))
    ctx = SetupContext(
        repo=_repo(RepoType.HOSTED), base_url='https://registry.example.com', registry_host='x', username=None
    )
    content = {s.id: s.content for c in DESCRIPTOR.setup_snippets(ctx) for s in c.snippets}
    assert content['npmrc-publish'] == '//registry.example.com/repository/npm-x/:_authToken={{TOKEN}}\n'
    assert '"registry": "https://registry.example.com/repository/npm-x/"' in content['publish-config']
