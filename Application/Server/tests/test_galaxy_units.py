"""Ansible Galaxy format units (T-P5-GALAXY): naming rules and SemVer order, routes (v3, Galaxy NG aliases, roles),
upstream documents (validated, bounded), rendering (URLs into the repository, pagination), group merges, options and
"Set me up"."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from waystation_common import galaxy
from waystation_common.errors import ValidationFailed
from waystation_server.formats.base import SetupContext
from waystation_server.formats.galaxy import api
from waystation_server.formats.galaxy.descriptor import (
    DEFAULT_COLLECTIONS_PATH,
    DESCRIPTOR,
    GalaxyOptions,
    upstream_root,
)
from waystation_server.formats.galaxy.hosted import detail_of, info_of, iso, version_of
from waystation_server.formats.galaxy.plugin import Route, parse_route
from waystation_server.repos.model import RepoConfig, RepoType, StoredGroup

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
SHA = 'a' * 64
URLS = api.Urls('https://ws.example.com/repository/gal/')


# ---------------------------------------------------------------------------- naming rules
def test_names_versions_and_files() -> None:
    assert galaxy.valid_name('community') and galaxy.valid_name('a_b1')
    for bad in ('Community', '1abc', 'a__b', 'a-b', '', 'a' * 65, '../x'):
        assert not galaxy.valid_name(bad)
    assert galaxy.valid_name('Old_Name', strict=False) and not galaxy.valid_name('a.b', strict=False)
    assert galaxy.valid_version('1.0.0-rc.1+build.5') and not galaxy.valid_version('1.0')
    assert not galaxy.valid_version('01.0.0') and not galaxy.valid_version('1.0.0-' + 'a' * 130)
    found = galaxy.parse_filename('my_ns-coll-1.2.3-beta.1.tar.gz')
    assert found == galaxy.Artifact('my_ns', 'coll', '1.2.3-beta.1')
    assert found.path == 'collections/my_ns/coll/my_ns-coll-1.2.3-beta.1.tar.gz' and found.fqcn == 'my_ns.coll'
    for bad in ('ns-coll-1.0.0.zip', 'ns-coll.tar.gz', 'NS-coll-1.0.0.tar.gz', 'ns-coll-1.0.tar.gz'):
        assert galaxy.parse_filename(bad) is None
    assert galaxy.parse_filename('NS-coll-1.0.0.tar.gz', strict=False) is not None
    assert galaxy.parse_artifact_path(found.path) == found
    for bad in ('collections/other/coll/my_ns-coll-1.0.0.tar.gz', 'x/my_ns/coll/my_ns-coll-1.0.0.tar.gz', 'a/b'):
        assert galaxy.parse_artifact_path(bad) is None


def test_version_sort_is_semver_precedence() -> None:
    order = [
        '1.0.0-alpha',
        '1.0.0-alpha.1',
        '1.0.0-alpha.beta',
        '1.0.0-beta.2',
        '1.0.0-beta.11',
        '1.0.0',
        '1.2.0',
        '1.10.0',
        '10.0.0',
    ]
    assert sorted(order, key=galaxy.version_sort) == order
    assert galaxy.version_sort('1.0.0+a') == galaxy.version_sort('1.0.0+b')
    assert galaxy.version_sort('junk') < galaxy.version_sort('0.0.1')
    assert galaxy.is_prerelease('1.0.0-rc.1') and not galaxy.is_prerelease('1.0.0') and not galaxy.is_prerelease('x')


# ---------------------------------------------------------------------------- routes
@pytest.mark.parametrize(
    ('path', 'route'),
    [
        ('', Route('discovery', prefix='api/')),
        ('api/', Route('discovery')),
        ('api/v3/collections/ns/coll/', Route('collection', 'ns', 'coll')),
        ('api/v3/collections/ns/coll/versions/', Route('versions', 'ns', 'coll')),
        ('api/v3/collections/ns/coll/versions/1.0.0+b/', Route('version', 'ns', 'coll', '1.0.0+b')),
        ('api/v3/plugin/ansible/content/published/collections/index/ns/coll/', Route('collection', 'ns', 'coll')),
        (
            'api/v3/plugin/ansible/content/published/collections/artifacts/ns-coll-1.0.0.tar.gz',
            Route('artifact', 'ns', 'coll', '1.0.0'),
        ),
        ('api/v3/plugin/ansible/content/staging/collections/artifacts/', Route('publish')),
        ('collections/ns/coll/ns-coll-1.0.0.tar.gz', Route('artifact', 'ns', 'coll', '1.0.0')),
        ('api/v3/artifacts/collections/', Route('publish')),
        ('api/v3/imports/collections/abc123/', Route('task', ident='abc123')),
        ('api/v1/roles/', Route('role_search')),
        ('api/v1/roles/42/versions/', Route('role_versions', ident='42')),
        ('roles/42/v1.2.tar.gz', Route('role_archive', version='v1.2', ident='42')),
    ],
)
def test_routes(path: str, route: Route) -> None:
    assert parse_route(path) == route


@pytest.mark.parametrize(
    'path',
    [
        'other',
        'api/v2/collections/ns/coll/',
        'api/v3/collections/ns/',
        'api/v3/collections/ns/coll/other/',
        'api/v3/collections/ns/coll/versions/1.0/',
        'api/v3/collections/a.b/coll/',
        'collections/ns/coll/other-coll-1.0.0.tar.gz',
        'collections/ns/coll/x.zip',
        'roles/abc/1.0.tar.gz',
        'roles/1/..tar.gz',
        'api/v1/roles/x/versions/',
        'api/v1/other/',
        'api/v3/imports/collections/' + 'x' * 65 + '/',
        'api/v3/plugin/ansible/content/published/collections/other/x/',
    ],
)
def test_unknown_routes(path: str) -> None:
    assert parse_route(path) is None


# ---------------------------------------------------------------------------- upstream documents
def versions_page(*versions: str, count: int | None = None, nxt: str | None = None) -> bytes:
    return json.dumps(
        {
            'meta': {'count': len(versions) if count is None else count},
            'links': {'next': nxt},
            'data': [{'version': v, 'created_at': '2026-01-02T03:04:05Z', 'marks': []} for v in versions],
        }
    ).encode()


def detail_doc(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        'version': '1.0.0',
        'namespace': {'name': 'ns'},
        'collection': {'name': 'coll'},
        'artifact': {'filename': 'ns-coll-1.0.0.tar.gz', 'sha256': SHA.upper(), 'size': 10},
        'download_url': 'https://galaxy.example.com/download/ns-coll-1.0.0.tar.gz',
        'metadata': {'dependencies': {'other.x': '>=1.0'}, 'contents': ['big'], 'description': 'demo'},
        'signatures': [{'signature': 's', 'pubkey_fingerprint': 'f', 'extra': 'dropped'}, 'junk'],
        'files': [{'name': 'x'}],
        'manifest': {},
        'requires_ansible': '>=2.15',
        'marks': ['m'],
    }
    doc.update(over)
    return doc


def test_parse_versions_page() -> None:
    page = api.parse_versions_page(versions_page('1.0.0', 'not-semver', '2.0.0-rc.1', nxt='/next'))
    assert [v.version for v in page.versions] == ['1.0.0', '2.0.0-rc.1']
    assert page.count == 3 and page.has_next
    assert api.parse_versions_page(versions_page()).versions == []
    for bad in (
        b'[]',
        b'{',
        b'{"data": [], "meta": {}}',
        b'{"data": [{"x": 1}], "meta": {"count": 1}}',
        b'{"meta": {"count": 1}}',
        b'x' * (api.MAX_DOC + 1),
    ):
        with pytest.raises(ValidationFailed):
            api.parse_versions_page(bad)


def test_parse_detail_validates_and_slims() -> None:
    data = json.dumps(detail_doc()).encode()
    detail = api.parse_detail(data, 'ns', 'coll', '1.0.0')
    assert detail.sha256 == SHA and detail.size == 10 and detail.metadata['dependencies'] == {'other.x': '>=1.0'}
    assert detail.signatures == ({'signature': 's', 'pubkey_fingerprint': 'f'},) and detail.marks == ('m',)
    slim = json.loads(api.slim_detail(data, None))
    assert 'files' not in slim and 'manifest' not in slim and 'contents' not in slim['metadata']
    again = api.parse_detail(api.slim_detail(data, None), 'ns', 'coll', '1.0.0')
    assert again.sha256 == detail.sha256 and again.download_url == detail.download_url
    assert again.metadata == {'dependencies': {'other.x': '>=1.0'}, 'description': 'demo'}
    plain = api.parse_detail(
        json.dumps(detail_doc(namespace='ns', collection=None, name='coll', metadata=None, signatures=None)).encode(),
        'ns',
        'coll',
        '1.0.0',
    )
    assert plain.metadata == {'dependencies': {}} and plain.signatures == ()
    for over, message in (
        ({'version': '2.0.0'}, 'another'),
        ({'artifact': None}, 'no artifact'),
        ({'artifact': {'sha256': 'xyz'}}, 'sha256'),
        ({'download_url': 'file:///etc/passwd'}, 'download_url'),
        ({'download_url': None}, 'download_url'),
    ):
        with pytest.raises(ValidationFailed, match=message):
            api.parse_detail(json.dumps(detail_doc(**over)).encode(), 'ns', 'coll', '1.0.0')
    api.check_collection(b'{"namespace": "ns", "name": "coll"}', 'ns', 'coll')
    with pytest.raises(ValidationFailed):
        api.check_collection(b'{"namespace": "ns", "name": "other"}', 'ns', 'coll')


def test_parse_time_and_urls() -> None:
    assert api.parse_time('2026-01-02T03:04:05Z') == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert api.parse_time('2026-01-02T03:04:05') == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert api.parse_time('junk') is None and api.parse_time(5) is None
    assert api.http_url('https://a.example.com/x') and api.http_url('ftp://a.example.com/') is None
    assert api.http_url('https:///x') is None and api.http_url(None) is None


# ---------------------------------------------------------------------------- rendering
def test_merge_and_highest() -> None:
    a = [api.Version('1.0.0', created_at='a'), api.Version('2.0.0-rc.1')]
    b = [api.Version('1.0.0', created_at='b'), api.Version('1.5.0')]
    merged = api.merge([a, b])
    assert [v.version for v in merged] == ['2.0.0-rc.1', '1.5.0', '1.0.0'] and merged[2].created_at == 'a'
    top = api.highest(merged)
    assert top is not None and top.version == '1.5.0'  # releases win over prereleases
    only_pre = api.highest([api.Version('1.0.0-a'), api.Version('1.0.0-b')])
    assert only_pre is not None and only_pre.version == '1.0.0-b'
    assert api.highest([]) is None


def test_render_documents() -> None:
    assert api.discovery(prefix='api/', roles=True) == {
        'available_versions': {'v3': 'api/v3/', 'v1': 'api/v1/'},
        'current_version': 'v3',
    }
    assert api.discovery(roles=False)['available_versions'] == {'v3': 'v3/'}
    coll = api.render_collection(URLS, 'ns', 'coll', api.Info(created_at='c'), '1.0.0')
    assert coll['href'] == '/repository/gal/api/v3/collections/ns/coll/'
    assert coll['highest_version'] == {
        'href': '/repository/gal/api/v3/collections/ns/coll/versions/1.0.0/',
        'version': '1.0.0',
    }
    assert api.render_collection(URLS, 'ns', 'coll', api.Info(), None)['highest_version'] is None

    versions = [api.Version(f'1.0.{i}') for i in range(25, 0, -1)]
    first = api.render_versions(URLS, 'ns', 'coll', versions, limit=10, offset=0)
    assert first['meta'] == {'count': 25} and len(first['data']) == 10
    assert first['links']['next'] == '/repository/gal/api/v3/collections/ns/coll/versions/?limit=10&offset=10'
    assert first['links']['previous'] is None and first['links']['last'].endswith('offset=20')
    last = api.render_versions(URLS, 'ns', 'coll', versions, limit=10, offset=20)
    assert last['links']['next'] is None and last['links']['previous'].endswith('offset=10')
    assert [d['version'] for d in last['data']] == ['1.0.5', '1.0.4', '1.0.3', '1.0.2', '1.0.1']
    assert api.page_params({'limit': '1000', 'offset': '5'}) == (100, 5)
    assert api.page_params({'limit': 'x', 'offset': '-1'}) == (10, 0)
    assert api.page_params({'limit': '0'}) == (1, 0)

    detail = api.parse_detail(json.dumps(detail_doc()).encode(), 'ns', 'coll', '1.0.0')
    rendered = api.render_detail(URLS, detail)
    assert rendered['download_url'] == 'https://ws.example.com/repository/gal/collections/ns/coll/ns-coll-1.0.0.tar.gz'
    assert rendered['artifact'] == {'filename': 'ns-coll-1.0.0.tar.gz', 'sha256': SHA, 'size': 10}
    assert rendered['namespace'] == {'name': 'ns'} and rendered['collection']['name'] == 'coll'

    task = api.render_task(
        URLS,
        {
            '_id': 't1',
            'state': 'failed',
            'created_at': NOW,
            'finished_at': NOW,
            'error': {'code': 'x', 'description': 'bad'},
            'messages': [{'level': 'ERROR', 'message': 'bad', 'time': NOW}, 'junk'],
            'namespace': 'ns',
        },
    )
    assert task['href'] == '/repository/gal/api/v3/imports/collections/t1/' and task['started_at'] is None
    assert task['finished_at'] == '2026-11-10T12:00:00Z' and task['messages'][0]['level'] == 'ERROR'
    assert len(task['messages']) == 1
    assert api.error_doc(400, 'bad', 'invalid')['errors'][0]['code'] == 'invalid'


def test_roles() -> None:
    search = api.render_role_search(json.dumps({'results': [{'id': 7, 'name': 'r'}, {'id': 'x'}, 'junk']}).encode())
    assert search['count'] == 1 and search['results'][0]['id'] == 7 and search['next'] is None
    with pytest.raises(ValidationFailed):
        api.role_search_doc(b'{"results": 1}')
    page = api.parse_role_versions(
        json.dumps(
            {
                'results': [
                    {'name': '1.0', 'download_url': 'https://github.example.com/a/1.0.tar.gz', 'source': 's'},
                    {'name': 'bad name', 'download_url': 'https://github.example.com/x.tar.gz'},
                    {'name': '2.0', 'download_url': 'file:///x'},
                    {'no': 'name'},
                ],
                'next_link': '/api/v1/roles/7/versions/?page=2',
            }
        ).encode()
    )
    assert len(page.results) == 3 and page.next_link == '/api/v1/roles/7/versions/?page=2'
    rendered = api.render_role_versions(URLS, '7', page.results)
    assert rendered['results'][0]['download_url'] == 'https://ws.example.com/repository/gal/roles/7/1.0.tar.gz'
    assert 'source' not in rendered['results'][0]
    assert 'download_url' not in rendered['results'][1] and 'download_url' not in rendered['results'][2]
    assert api.role_download(page.results, '1.0') == 'https://github.example.com/a/1.0.tar.gz'
    assert api.role_download(page.results, '2.0') is None and api.role_download(page.results, '3.0') is None
    with pytest.raises(ValidationFailed):
        api.parse_role_versions(b'{"results": {}}')


# ---------------------------------------------------------------------------- hosted records
def test_hosted_records_render() -> None:
    doc = {
        'namespace': 'ns',
        'name': 'coll',
        'version': '1.0.0',
        'sha256': SHA,
        'size': 5,
        'created': datetime(2026, 1, 2, 3, 4, 5),  # naive from MongoDB: UTC
        'requires_ansible': '>=2.15',
        'metadata': {'description': 'd'},
    }
    assert iso(doc['created']) == '2026-01-02T03:04:05Z' and iso('x') == 'x' and iso(None) is None
    assert version_of(doc).created_at == '2026-01-02T03:04:05Z'
    detail = detail_of(doc)
    assert detail.metadata == {'description': 'd', 'dependencies': {}} and detail.size == 5
    info = info_of([doc, {**doc, 'created': NOW}])
    assert info.created_at == '2026-01-02T03:04:05Z' and info.updated_at == '2026-11-10T12:00:00Z'


# ---------------------------------------------------------------------------- options and "Set me up"
def test_options() -> None:
    assert GalaxyOptions().collections_path == DEFAULT_COLLECTIONS_PATH
    assert GalaxyOptions(collections_path='/api/v3/collections').collections_path == 'api/v3/collections/'
    for bad in ('../x', 'a/./b', 'a b', 'a?b'):
        with pytest.raises(ValueError, match='relative path'):
            GalaxyOptions(collections_path=bad)
    assert DESCRIPTOR.check_options(GalaxyOptions(), RepoType.HOSTED) == []
    assert DESCRIPTOR.check_options(GalaxyOptions(collections_path='x/'), RepoType.PROXY) == []
    assert DESCRIPTOR.check_options(GalaxyOptions(collections_path='x/'), RepoType.GROUP)[0][0] == 'collections_path'
    assert upstream_root('https://galaxy.example.com/api/') == 'https://galaxy.example.com/'
    assert upstream_root('https://galaxy.example.com') == 'https://galaxy.example.com/'
    assert DESCRIPTOR.probe_path is not None and DESCRIPTOR.probe_path(GalaxyOptions()) == 'api/'


def repo(**fields: Any) -> RepoConfig:
    return RepoConfig.model_validate({'format': 'galaxy', 'created_at': NOW, 'updated_at': NOW, **fields})


def test_setup_snippets() -> None:
    def contents(repo: RepoConfig) -> str:
        ctx = SetupContext(repo=repo, base_url='https://ws.example.com', registry_host='ws.example.com', username='u')
        return '\n'.join(s.content for c in DESCRIPTOR.setup_snippets(ctx) for s in c.snippets)

    proxy = contents(repo(id='r1', name='gal', type=RepoType.PROXY))
    assert 'url = https://ws.example.com/repository/gal/' in proxy
    assert 'collection install community.general -s https://ws.example.com/repository/gal/' in proxy
    assert 'role install' in proxy and 'collection publish' not in proxy
    hosted = contents(repo(id='r2', name='mine', type=RepoType.HOSTED))
    assert 'collection publish' in hosted and 'DELETE' in hosted and 'role install' not in hosted
    group = repo(id='r3', name='all', type=RepoType.GROUP, group=StoredGroup(members=('mine',), writable_member='mine'))
    text = contents(group)
    assert 'collection publish' in text and 'role install' in text
