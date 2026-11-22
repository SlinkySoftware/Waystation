"""NuGet format units (T-P6-NUGET): package reading (zip-slip/zip-bomb guards, defused nuspec), routes, the service
index, upstream resources, URL rewriting, registrations (hosted rendering, group merges), search, the catalog hash,
prefetch items, "Set me up" and the Nexus import mapping."""

import base64
import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from waystation_common.errors import NotFound, UpstreamError, ValidationFailed
from waystation_server.formats.base import Fetched, SetupContext
from waystation_server.formats.nuget import package, prefetch, protocol
from waystation_server.formats.nuget.descriptor import DESCRIPTOR, NugetOptions, refresh_request
from waystation_server.formats.nuget.hosted import summary_fields
from waystation_server.formats.nuget.plugin import registration_summary
from waystation_server.formats.nuget.protocol import HostedVersion, Op, Route, SearchQuery
from waystation_server.migration.nexus_mapping import NexusRepository, map_repository
from waystation_server.repos.model import RepoConfig, RepoType

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
REPO_URL = 'https://ws.example.com/repository/nuget/'
UP = 'https://nuget.example.com/'
FOUR = '1.0.0.0'  # forbidden-strings: allow (a four-part NuGet version, not an address)


def nuspec(package_id: str = 'Demo.Lib', version: str = '1.0.0', *, extra: str = '', ns: str = '2013/05') -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<package xmlns="http://schemas.microsoft.com/packaging/{ns}/nuspec.xsd">\n'
        f'  <metadata minClientVersion="2.12">\n'
        f'    <id>{package_id}</id>\n    <version>{version}</version>\n'
        '    <authors>Ann, Bob</authors>\n    <description>A demo library</description>\n'
        '    <tags>demo test</tags>\n    <projectUrl>https://example.com/demo</projectUrl>\n'
        '    <license type="expression">MIT</license>\n'
        '    <dependencies>\n      <group targetFramework="net8.0">\n'
        '        <dependency id="Other.Lib" version="[1.0.0, )" exclude="Build" />\n'
        '      </group>\n    </dependencies>\n'
        f'{extra}  </metadata>\n</package>\n'
    ).encode()


def nupkg(files: Mapping[str, bytes] | None = None, **kwargs: Any) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in (files if files is not None else {'Demo.Lib.nuspec': nuspec(**kwargs)}).items():
            archive.writestr(name, data)
        archive.writestr('lib/net8.0/Demo.Lib.dll', b'MZ' + b'\0' * 64)
    return buf.getvalue()


# ---------------------------------------------------------------------------- packages
def test_reads_a_package() -> None:
    spec = package.parse_nuspec(package.read_nuspec_from(io.BytesIO(nupkg(version=FOUR))))
    assert (spec.id, spec.version, spec.flat_version) == ('Demo.Lib', '1.0.0', '1.0.0')
    assert spec.authors == 'Ann, Bob' and spec.tags == ('demo', 'test') and spec.license_expression == 'MIT'
    assert spec.min_client_version == '2.12'
    (group,) = spec.dependency_groups
    assert group.target_framework == 'net8.0'
    assert group.dependencies[0].json() == {'id': 'Other.Lib', 'range': '[1.0.0, )', 'exclude': 'Build'}
    details = spec.details()
    assert details['project_url'] == 'https://example.com/demo'
    package.check_package(io.BytesIO(nupkg()), 'demo.lib', '1.0.0')
    with pytest.raises(ValidationFailed, match=r'not demo\.lib 2\.0\.0'):
        package.check_package(io.BytesIO(nupkg()), 'demo.lib', '2.0.0')


def test_old_namespaces_and_flat_dependencies() -> None:
    extra = '<iconUrl>javascript:alert(1)</iconUrl>'
    data = nuspec(ns='2010/07', extra=extra).replace(b'<group targetFramework="net8.0">', b'').replace(b'</group>', b'')
    spec = package.parse_nuspec(data)
    assert spec.icon_url is None  # only http(s) URLs are kept
    assert spec.dependency_groups[0].target_framework is None


@pytest.mark.parametrize(
    ('files', 'message'),
    [
        ({'../evil.nuspec': nuspec()}, 'unsafe file name'),
        ({'Demo.Lib.nuspec': nuspec(), '/etc/passwd': b'x'}, 'unsafe file name'),
        ({'Demo.Lib.nuspec': nuspec(), 'lib\\..\\..\\x.dll': b'x'}, 'unsafe file name'),
        ({'Demo.Lib.nuspec': nuspec(), 'C:/x.dll': b'x'}, 'unsafe file name'),
        ({'lib/Demo.Lib.nuspec': nuspec()}, 'exactly one .nuspec'),
        ({'a.nuspec': nuspec(), 'b.nuspec': nuspec()}, 'exactly one .nuspec'),
        ({'Demo.Lib.nuspec': b'<package><metadata><id>Demo</id></metadata></package>'}, 'valid <version>'),
        ({'Demo.Lib.nuspec': nuspec(package_id='bad id')}, 'valid <id>'),
        ({'Demo.Lib.nuspec': b'not xml'}, 'not valid XML'),
    ],
)
def test_refuses_unsafe_packages(files: dict[str, bytes], message: str) -> None:
    with pytest.raises(ValidationFailed, match=message):
        package.parse_nuspec(package.read_nuspec_from(io.BytesIO(nupkg(files))))


def test_refuses_entities_bombs_and_non_zips() -> None:
    xxe = b'<?xml version="1.0"?><!DOCTYPE p [<!ENTITY x SYSTEM "file:///etc/passwd">]><package>&x;</package>'
    with pytest.raises(ValidationFailed, match='not valid XML'):
        package.parse_nuspec(xxe)
    with pytest.raises(ValidationFailed, match='zip archive'):
        package.read_nuspec_from(io.BytesIO(b'PK\x03\x04 truncated'))
    big = io.BytesIO()
    with zipfile.ZipFile(big, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('Demo.Lib.nuspec', nuspec() + b' ' * (package.MAX_NUSPEC + 1))
    with pytest.raises(ValidationFailed, match='too large'):
        package.read_nuspec_from(io.BytesIO(big.getvalue()))


def test_declared_sizes_bound_the_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(package, 'MAX_UNPACKED', 100)
    with pytest.raises(ValidationFailed, match='too large when unpacked'):
        package.read_nuspec_from(io.BytesIO(nupkg()))
    monkeypatch.setattr(package, 'MAX_ENTRIES', 1)
    with pytest.raises(ValidationFailed, match='too many files'):
        package.read_nuspec_from(io.BytesIO(nupkg()))


# ---------------------------------------------------------------------------- routes and documents
def test_routes() -> None:
    parse = protocol.parse_route
    assert parse('index.json') == Route(Op.SERVICE_INDEX)
    assert parse('v3-flatcontainer/Demo.Lib/index.json') == Route(Op.FLAT_INDEX, 'demo.lib')
    nupkg_route = parse(f'v3-flatcontainer/demo.lib/{FOUR}/demo.lib.{FOUR}.nupkg')
    assert nupkg_route == Route(Op.NUPKG, 'demo.lib', '1.0.0')
    assert nupkg_route.key == 'v3-flatcontainer/demo.lib/1.0.0/demo.lib.1.0.0.nupkg'
    assert parse('v3-flatcontainer/demo.lib/1.0.0-BETA/demo.lib.nuspec') == Route(Op.NUSPEC, 'demo.lib', '1.0.0-beta')
    assert parse('v3/registration/Demo.Lib/index.json') == Route(Op.REGISTRATION, 'demo.lib')
    part = parse('v3/registration/demo.lib/page/1.0.0/2.0.0.json')
    assert part == Route(Op.REGISTRATION_PART, 'demo.lib', rest='page/1.0.0/2.0.0.json')
    assert parse('query') == Route(Op.SEARCH)
    assert parse('api/v2/package') == Route(Op.PUBLISH)
    assert parse('api/v2/package/Demo.Lib/1.0') == Route(Op.PACKAGE, 'demo.lib', '1.0.0')
    for bad in (
        '',
        '_upstream/index.json',
        '_catalog/demo.lib/1.0.0.json',
        'v3-flatcontainer/demo.lib/1.0.0/other.1.0.0.nupkg',
        'v3-flatcontainer/demo.lib/notaversion/demo.lib.nuspec',
        'v3-flatcontainer/bad id/index.json',
        'v3/registration/demo.lib/a/b/c/d/e.json',
        'v3/registration/demo.lib/page.txt',
        'api/v2/package/demo.lib',
    ):
        assert parse(bad) is None, bad


def test_service_index() -> None:
    doc = json.loads(protocol.service_index(REPO_URL, publish=True))
    by_type = {r['@type']: r['@id'] for r in doc['resources']}
    assert by_type['PackageBaseAddress/3.0.0'] == f'{REPO_URL}v3-flatcontainer/'
    assert by_type['RegistrationsBaseUrl/3.6.0'] == f'{REPO_URL}v3/registration/'
    assert by_type['SearchQueryService/3.5.0'] == f'{REPO_URL}query'
    assert by_type['PackagePublish/2.0.0'] == f'{REPO_URL}api/v2/package'
    proxy = json.loads(protocol.service_index(REPO_URL, publish=False))
    assert all(r['@type'] != 'PackagePublish/2.0.0' for r in proxy['resources'])


def upstream_index() -> bytes:
    return json.dumps(
        {
            'version': '3.0.0',
            'resources': [
                {'@id': f'{UP}v3-flatcontainer/', '@type': 'PackageBaseAddress/3.0.0'},
                {'@id': f'{UP}v3/registration5-semver1/', '@type': 'RegistrationsBaseUrl'},
                {'@id': f'{UP}v3/registration5-gz-semver2/', '@type': 'RegistrationsBaseUrl/3.6.0'},
                {'@id': 'https://search.example.com/query', '@type': 'SearchQueryService/3.5.0'},
                {'@id': 'ftp://nope.example.com/', '@type': 'SearchQueryService'},
                'junk',
            ],
        }
    ).encode()


def test_upstream_resources() -> None:
    assert protocol.service_index_url('https://api.nuget.org/v3/index.json') == 'https://api.nuget.org/v3/index.json'
    assert protocol.service_index_url('https://nuget.example.com/v3') == 'https://nuget.example.com/v3/index.json'
    up = protocol.parse_upstream_index(upstream_index(), f'{UP}v3/index.json')
    assert up.flat == f'{UP}v3-flatcontainer/'
    assert up.registration == f'{UP}v3/registration5-gz-semver2/'
    assert up.registrations == (f'{UP}v3/registration5-gz-semver2/', f'{UP}v3/registration5-semver1/')
    assert up.search == 'https://search.example.com/query'
    assert up.hosts() == {'nuget.example.com', 'search.example.com'}
    assert protocol.Upstream.from_json(json.loads(protocol.dump(up.json()))) == up
    for bad in (b'[]', b'{"resources": [{"@id": "https://x.example.com/", "@type": "SearchQueryService"}]}', b'{'):
        with pytest.raises(UpstreamError):
            protocol.parse_upstream_index(bad, UP)


def test_rewrites_registration_and_search() -> None:
    up = protocol.parse_upstream_index(upstream_index(), f'{UP}v3/index.json')
    doc = {
        '@id': f'{UP}v3/registration5-gz-semver2/demo.lib/index.json',
        'items': [
            {
                '@id': f'{UP}v3/registration5-gz-semver2/demo.lib/page/1.0.0/1.0.0.json',
                'items': [
                    {
                        'catalogEntry': {'@id': f'{UP}v3/catalog0/data/demo.lib.1.0.0.json', 'version': '1.0.0'},
                        'packageContent': f'{UP}v3-flatcontainer/demo.lib/1.0.0/demo.lib.1.0.0.nupkg',
                    }
                ],
            }
        ],
    }
    out = json.loads(protocol.rewrite_registration(json.dumps(doc).encode(), up, REPO_URL))
    assert out['@id'] == f'{REPO_URL}v3/registration/demo.lib/index.json'
    leaf = out['items'][0]['items'][0]
    assert leaf['packageContent'] == f'{REPO_URL}v3-flatcontainer/demo.lib/1.0.0/demo.lib.1.0.0.nupkg'
    assert leaf['catalogEntry']['@id'].startswith(UP)  # catalog ids stay the upstream's
    search = {'totalHits': 1, 'data': [{'@id': f'{UP}v3/registration5-semver1/demo.lib/index.json'}]}
    rewritten = json.loads(protocol.rewrite_search(json.dumps(search).encode(), up, REPO_URL))
    assert rewritten['data'][0]['@id'] == f'{REPO_URL}v3/registration/demo.lib/index.json'
    with pytest.raises(UpstreamError):
        protocol.rewrite_registration(b'[1]', up, REPO_URL)
    with pytest.raises(UpstreamError):
        protocol.rewrite_search(b'{"data": 3}', up, REPO_URL)
    assert protocol.reprefix(b'{"a":"https://ws.example.com/repository/nuget/x"}', REPO_URL, 'G/') == b'{"a":"G/x"}'


def test_version_lists() -> None:
    assert protocol.parse_versions(f'{{"versions": ["{FOUR}", "1.0.0", "2.0.0-Beta", 3, "bad"]}}'.encode()) == [
        '1.0.0',
        '2.0.0-beta',
    ]
    assert json.loads(protocol.render_versions(['2.0.0', '1.0.0-rc.1', '1.0.0', '1.0.0'])) == {
        'versions': ['1.0.0-rc.1', '1.0.0', '2.0.0']
    }
    for bad in (b'{}', b'[]', b'{"versions": "x"}'):
        with pytest.raises(UpstreamError):
            protocol.parse_versions(bad)


def hosted(version: str, *, listed: bool = True, package_id: str = 'Demo.Lib') -> HostedVersion:
    spec = package.parse_nuspec(nuspec(package_id, version))
    details = {**spec.details(), 'listed': listed, 'published': NOW, 'uploaded_by': 'ann'}
    return HostedVersion(details=details, size=10, sha512_b64=protocol.sha512_b64('ab' * 64))


def test_hosted_registration_and_merge() -> None:
    versions = [hosted('2.0.0'), hosted('1.0.0', listed=False), hosted('2.1.0-beta.1')]
    doc = json.loads(protocol.render_registration(REPO_URL, 'Demo.Lib', versions))
    (page,) = doc['items']
    assert (page['lower'], page['upper'], page['count']) == ('1.0.0', '2.1.0-beta.1', 3)
    first = page['items'][0]
    entry = first['catalogEntry']
    assert entry['listed'] is False and entry['published'] == protocol.UNLISTED_PUBLISHED
    assert first['packageContent'] == f'{REPO_URL}v3-flatcontainer/demo.lib/1.0.0/demo.lib.1.0.0.nupkg'
    dep = entry['dependencyGroups'][0]['dependencies'][0]
    assert dep['registration'] == f'{REPO_URL}v3/registration/other.lib/index.json'
    assert entry['packageHash'] == base64.b64encode(bytes.fromhex('ab' * 64)).decode()
    assert page['items'][1]['catalogEntry']['published'] == NOW.isoformat()
    leaf = json.loads(protocol.render_leaf(REPO_URL, versions[0]))
    assert leaf['@id'] == f'{REPO_URL}v3/registration/demo.lib/2.0.0.json' and leaf['listed'] is True

    other = json.loads(protocol.render_registration('G/', 'Demo.Lib', [hosted('2.0.0'), hosted('3.0.0')]))
    merged = protocol.merge_registrations([doc, other])
    assert merged is not None
    items = merged['items'][0]['items']
    assert [protocol.leaf_version(i) for i in items] == ['1.0.0', '2.0.0', '2.1.0-beta.1', '3.0.0']
    assert items[1]['packageContent'].startswith(REPO_URL)  # the first member wins 2.0.0
    assert protocol.merge_registrations([{'items': []}]) is None
    assert protocol.in_page({'lower': '1.0.0', 'upper': '2.0.0'}, '1.5.0')
    assert not protocol.in_page({'lower': '1.0.0', 'upper': '2.0.0'}, '2.0.1')

    summary = summary_fields(versions)
    assert (summary['display_name'], summary['latest'], summary['version_count']) == ('Demo.Lib', '2.0.0', 3)
    assert registration_summary(doc)['display_name'] == 'Demo.Lib'
    assert registration_summary({'items': [{'@id': 'x'}]}) == {}


def test_search() -> None:
    query = SearchQuery.from_params({'q': 'demo id:demo.lib', 'take': '5000', 'prerelease': 'true'})
    assert (query.take, query.skip, query.prerelease, query.semver2) == (1000, 0, True, False)
    assert query.upstream_query() == 'q=demo%20id%3Ademo.lib&skip=0&take=1000&prerelease=true'
    assert query.matches('Demo.Lib', {'description': 'x'})
    assert not query.matches('Other', {'description': 'demo'})
    assert SearchQuery.from_params({'q': 'library'}).matches('X', {'description': 'A demo library'})
    assert not query.visible('1.0.0-beta.1')  # SemVer 2 needs semVerLevel=2.0.0
    assert SearchQuery.from_params({'semVerLevel': '2.0.0', 'prerelease': 'true'}).visible('1.0.0-beta.1')
    assert not SearchQuery.from_params({}).visible('1.0.0-beta')
    with pytest.raises(ValidationFailed):
        SearchQuery.from_params({'skip': '-1'})
    hit = protocol.search_hit(REPO_URL, [hosted('1.0.0'), hosted('2.0.0')])
    assert hit['version'] == '2.0.0' and hit['authors'] == ['Ann', 'Bob']
    assert [v['version'] for v in hit['versions']] == ['1.0.0', '2.0.0']
    a = {'totalHits': 2, 'data': [{'id': 'A'}, {'id': 'B'}]}
    b = {'totalHits': 2, 'data': [{'id': 'b'}, {'id': 'C'}]}
    merged = json.loads(protocol.merge_search([a, b, None], SearchQuery.from_params({'skip': '1', 'take': '1'})))
    assert merged == {'totalHits': 4, 'data': [{'id': 'B'}]}


def test_catalog_hash() -> None:
    digest = hashlib.sha512(b'pkg').digest()
    leaf = protocol.parse_catalog_leaf(
        json.dumps(
            {
                'id': 'Demo.Lib',
                'version': '1.0.0',
                'packageHash': base64.b64encode(digest).decode(),
                'packageHashAlgorithm': 'SHA512',
                'packageSize': 3,
                'packageEntries': [{'name': 'x'}],
            }
        ).encode()
    )
    assert 'packageEntries' not in leaf
    found = protocol.catalog_hash(leaf)
    assert found == protocol.CatalogHash(digest.hex(), 3)
    assert protocol.catalog_hash({'packageHashAlgorithm': 'SHA256', 'packageHash': 'x'}) is None
    with pytest.raises(UpstreamError):
        protocol.catalog_hash({'packageHash': 'AAAA'})
    with pytest.raises(UpstreamError):
        protocol.parse_catalog_leaf(b'[]')
    assert protocol.sha512_b64('zz') is None and protocol.sha512_b64(None) is None


# ---------------------------------------------------------------------------- descriptor, prefetch, import
def repo(**fields: Any) -> RepoConfig:
    return RepoConfig.model_validate({'format': 'nuget', 'created_at': NOW, 'updated_at': NOW, **fields})


def test_setup_snippets_and_options() -> None:
    def contents(r: RepoConfig, base: str = 'https://ws.example.com') -> str:
        ctx = SetupContext(repo=r, base_url=base, registry_host='ws.example.com', username='u')
        return '\n'.join(s.content for c in DESCRIPTOR.setup_snippets(ctx) for s in c.snippets)

    proxy = contents(repo(id='r1', name='nuget', type=RepoType.PROXY))
    assert 'dotnet nuget add source https://ws.example.com/repository/nuget/index.json --name waystation' in proxy
    assert '<add key="ClearTextPassword" value="{{TOKEN}}" />' in proxy and 'allowInsecureConnections' not in proxy
    assert 'nuget push' not in proxy
    plain = contents(repo(id='r2', name='mine', type=RepoType.HOSTED), base='http://ws.example.com')
    assert 'dotnet nuget push' in plain and '--api-key {{TOKEN}}' in plain and '-ApiKey {{TOKEN}}' in plain
    assert '--allow-insecure-connections' in plain and 'allowInsecureConnections="true"' in plain
    group = repo(
        id='r3', name='all', type=RepoType.GROUP, group={'members': ['mine', 'nuget'], 'writable_member': 'mine'}
    )
    assert '(stored in mine)' in '\n'.join(
        s.title
        for c in DESCRIPTOR.setup_snippets(SetupContext(group, 'https://ws.example.com', 'ws.example.com', None))
        for s in c.snippets
    )
    assert NugetOptions().model_dump() == {'verify_catalog': True, 'delete_mode': 'unlist'}
    with pytest.raises(ValueError):
        NugetOptions.model_validate({'delete_mode': 'purge'})
    assert refresh_request('v3-flatcontainer/demo.lib/index.json') is not None
    assert refresh_request('v3/registration/demo.lib/index.json') is not None
    assert refresh_request('v3/registration/demo.lib/page/1/2.json') is None
    assert refresh_request('_upstream/index.json') is None


class FakeWarm:
    def __init__(self, versions: list[str]) -> None:
        self.repo = repo(id='r1', name='nuget', type=RepoType.PROXY)
        self.versions = versions
        self.asked: list[str] = []

    async def get(self, path: str, headers: Mapping[str, str] | None = None) -> Fetched:
        self.asked.append(path)
        return Fetched(path=path, status=200, cache='MISS', headers={})

    async def read(self, fetched: Fetched, *, max_bytes: int) -> bytes:
        return json.dumps({'versions': self.versions}).encode()


async def test_prefetch() -> None:
    assert prefetch.check_item('Newtonsoft.Json@13.0.3') is None
    assert prefetch.check_item('Newtonsoft.Json') is None
    assert prefetch.check_item('bad id') is not None
    assert prefetch.check_item('Demo@latest') is not None
    assert prefetch.parse_item('Demo.Lib@1.0') == ('Demo.Lib', '1.0.0')
    ctx = FakeWarm(['1.0.0', '2.0.0', '3.0.0-beta'])
    await prefetch.warm(ctx, 'Demo.Lib')
    assert ctx.asked == [
        'v3-flatcontainer/demo.lib/index.json',
        'v3-flatcontainer/demo.lib/2.0.0/demo.lib.2.0.0.nupkg',
        'v3-flatcontainer/demo.lib/2.0.0/demo.lib.nuspec',
    ]
    with pytest.raises(NotFound):
        await prefetch.warm(FakeWarm(['1.0.0']), 'Demo.Lib@9.9.9')
    with pytest.raises(NotFound):
        await prefetch.warm(FakeWarm([]), 'Demo.Lib')
    assert prefetch.newest(['1.0.0-rc.1']) == '1.0.0-rc.1'


def test_nexus_import() -> None:
    supported = {'nuget': frozenset({'proxy', 'hosted', 'group'})}
    entry = {'name': 'nuget.org-proxy', 'format': 'nuget', 'type': 'proxy'}
    detail = {
        'name': 'nuget.org-proxy',
        'proxy': {'remoteUrl': 'https://api.nuget.org/v3/index.json', 'contentMaxAge': 1440, 'metadataMaxAge': 1440},
        'nugetProxy': {'queryCacheItemMaxAge': 3600, 'nugetVersion': 'V3'},
    }
    mapped = map_repository(NexusRepository(entry=entry, detail=detail), supported=supported)
    assert mapped.action == 'create' and mapped.body is not None
    assert mapped.body['proxy']['upstream_url'] == 'https://api.nuget.org/v3/index.json'
    assert mapped.body['format_options'] == {}
    assert any(d.field == 'nugetProxy.queryCacheItemMaxAge' for d in mapped.differences)
    v2 = {**detail, 'nugetProxy': {'nugetVersion': 'V2'}}
    refused = map_repository(NexusRepository(entry=entry, detail=v2), supported=supported)
    assert refused.action == 'invalid' and 'V2' in (refused.reason or '')
    hosted_entry = {'name': 'nuget-hosted', 'format': 'nuget', 'type': 'hosted'}
    assert map_repository(NexusRepository(entry=hosted_entry), supported=supported).action == 'create'
