"""Composer format units (T-P6-COMPOSER): routes, packages.json, p2 parsing and dist rewriting, archive checks
(zip-slip/zip-bomb guards, composer.json validation), group merges, prefetch items, "Set me up", options and the
Nexus import mapping."""

import io
import json
import tarfile
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from waystation_common import composer
from waystation_common.errors import NotFound, UpstreamError, ValidationFailed
from waystation_server.formats.base import Fetched, SetupContext
from waystation_server.formats.composer import archive, prefetch, protocol
from waystation_server.formats.composer.descriptor import DESCRIPTOR, ComposerOptions, refresh_request
from waystation_server.formats.composer.hosted import entry_of, split
from waystation_server.formats.composer.plugin import summary_fields
from waystation_server.formats.composer.protocol import Op, Route
from waystation_server.migration.nexus_mapping import NexusRepository, map_repository
from waystation_server.repos.model import RepoConfig, RepoType

N_1_0_0_0 = '1.0.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_1_1_2_0 = '1.1.2.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_2_0_0_0 = '2.0.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_2_0_2_0 = '2.0.2.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_2_1_0_0 = '2.1.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_3_0_0_0 = '3.0.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
N_9_0_0_0 = '9.0.0.0'  # forbidden-strings: allow (a normalised Composer version, not an address)
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
REPO_URL = 'https://ws.example.com/repository/php/'
UP = 'https://packages.example.com/'
HOSTS = frozenset({'github.com', 'codeload.github.com', 'api.github.com', 'packages.example.com'})
REF = 'c71ecc56dfe541dbd90c5360474fbc405f8d5963'


def make_zip(files: Mapping[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def p2_doc(*entries: dict[str, Any], name: str = 'psr/container', minified: bool = True) -> bytes:
    versions = composer.minify(list(entries)) if minified else list(entries)
    doc: dict[str, Any] = {'packages': {name: versions}}
    if minified:
        doc['minified'] = composer.MINIFIED
    return json.dumps(doc).encode()


def upstream_entry(version: str, ref: str = REF, **extra: Any) -> dict[str, Any]:
    return {
        'name': 'psr/container',
        'description': 'Common Container Interface',
        'version': version,
        'version_normalized': composer.normalise(version),
        'source': {'type': 'git', 'url': 'https://github.com/php-fig/container.git', 'reference': ref},
        'dist': {
            'type': 'zip',
            'url': f'https://api.github.com/repos/php-fig/container/zipball/{ref}',
            'reference': ref,
            'shasum': '',
        },
        'type': 'library',
        'scripts': {'post-install-cmd': 'rm -rf /'},
        **extra,
    }


def test_routes() -> None:
    assert protocol.parse_route('packages.json') == Route(Op.ROOT)
    assert protocol.parse_route('p2/PSR/Container.json') == Route(Op.P2, 'psr/container')
    assert protocol.parse_route('p2/psr/container~dev.json') == Route(Op.P2, 'psr/container', branches=True)
    dist = protocol.parse_route(f'dists/psr/container/{N_2_0_2_0}/{REF}.zip')
    assert dist == Route(Op.DIST, 'psr/container', version=N_2_0_2_0, file=f'{REF}.zip')
    assert dist.key == f'dists/psr/container/{N_2_0_2_0}/{REF}.zip'
    upload = protocol.parse_route('packages/upload/Acme/Demo/1.0.0')
    assert upload == Route(Op.UPLOAD, 'acme/demo', version='1.0.0') and upload.key.endswith('acme/demo/1.0.0')
    for bad in (
        'p2/psr.json',
        'p2/a/b/c.json',
        'p2/-a/b.json',
        f'dists/psr/container/{N_2_0_2_0}/x.exe',
        'dists/psr/container/a%b/x.zip',
        'packages/upload/a/b',
        'other',
    ):
        assert protocol.parse_route(bad) is None, bad
    assert Route(Op.ROOT).key == 'packages.json' and Route(Op.P2, 'a/b', branches=True).key == 'p2/a/b~dev.json'


def test_root_and_upstream() -> None:
    root = json.loads(protocol.render_root(REPO_URL, available=['acme/demo'], patterns=['acme/*']))
    assert root == {
        'packages': [],
        'metadata-url': f'{REPO_URL}p2/%package%.json',
        'available-packages': ['acme/demo'],
        'available-package-patterns': ['acme/*'],
    }
    assert 'available-packages' not in json.loads(protocol.render_root(REPO_URL))
    upstream = protocol.parse_upstream_root(
        json.dumps(
            {
                'packages': [],
                'metadata-url': '/p2/%package%.json',
                'providers-url': '/p/%package%$%hash%.json',
                'available-package-patterns': ['acme/*', 'BAD PATTERN'],
            }
        ).encode(),
        f'{UP}packages.json',
    )
    assert upstream.metadata_url == f'{UP}p2/%package%.json'
    assert upstream.p2_url('psr/log', dev=True) == f'{UP}p2/psr/log~dev.json'
    assert upstream.patterns == ('acme/*',) and upstream.lists('acme/x') and not upstream.lists('psr/log')
    assert protocol.upstream_from_json(upstream.json()) == upstream
    listed = protocol.parse_upstream_root(
        b'{"metadata-url": "p2/%package%.json", "available-packages": ["Psr/Log", 5]}', f'{UP}packages.json'
    )
    assert listed.available == ('psr/log',) and listed.lists('psr/log') and not listed.lists('psr/container')
    assert protocol.parse_upstream_root(b'{"metadata-url": "/p2/%package%.json"}', UP).lists('any/thing')
    for bad in (
        b'[]',
        b'{"providers-url": "/p/%package%.json"}',
        b'{"metadata-url": "https://evil.example.net/p2/%package%.json"}',
        b'{"metadata-url": "/p2/%package%.json", "available-packages": "x"}',
        b'not json',
    ):
        with pytest.raises(UpstreamError):
            protocol.parse_upstream_root(bad, f'{UP}packages.json')
    with pytest.raises(UpstreamError):
        protocol.upstream_from_json([])


def test_p2_parse_and_rewrite() -> None:
    data = p2_doc(upstream_entry('v2.0.2'), upstream_entry('1.1.2', ref='a' * 40), {'version': 'bogus'})
    entries = protocol.parse_p2(data, 'psr/container')
    assert [e['version_normalized'] for e in entries] == [N_2_0_2_0, N_1_1_2_0]
    assert all('scripts' not in e for e in entries)
    rewritten = protocol.rewrite_entries(entries, REPO_URL, HOSTS, strip_source=False)
    dist = rewritten[0]['dist']
    assert dist['url'] == f'{REPO_URL}dists/psr/container/{N_2_0_2_0}/{REF}.zip'
    assert dist[protocol.UPSTREAM_URL_KEY] == f'https://api.github.com/repos/php-fig/container/zipball/{REF}'
    assert 'source' in rewritten[0]
    stripped = protocol.rewrite_entries(entries, REPO_URL, HOSTS, strip_source=True)
    assert 'source' not in stripped[0]
    other = protocol.rewrite_entries(entries, REPO_URL, frozenset({'gitlab.com'}), strip_source=False)
    assert other[0]['dist']['url'].startswith('https://api.github.com/')
    # stored and parsed again: the archive is found by version and file
    stored = protocol.parse_p2(protocol.render_p2('psr/container', rewritten), 'psr/container')
    target = protocol.find_dist(stored, N_2_0_2_0, f'{REF}.zip')
    assert target is not None and target.kind == 'zip' and target.shasum is None and target.reference == REF
    assert protocol.find_dist(stored, N_2_0_2_0, 'other.zip') is None
    assert protocol.find_dist(stored, N_9_0_0_0, f'{REF}.zip') is None
    assert protocol.find_dist(protocol.parse_p2(data, 'psr/container'), N_2_0_2_0, f'{REF}.zip') is None
    # an upstream cannot plant its own upstream URL
    planted = upstream_entry('3.0.0')
    planted['dist'] = {'type': 'zip', 'url': 'https://elsewhere.example.net/x.zip', protocol.UPSTREAM_URL_KEY: 'x'}
    out = protocol.rewrite_entries(
        protocol.parse_p2(p2_doc(planted), 'psr/container'), REPO_URL, HOSTS, strip_source=False
    )
    assert protocol.UPSTREAM_URL_KEY not in out[0]['dist']
    # not minified; a package of another name; tar archives; shasum
    plain = p2_doc(
        upstream_entry('1.0.0', dist={'type': 'tar', 'url': f'{UP}a.tar', 'shasum': 'AB' * 20}), minified=False
    )
    tar = protocol.rewrite_entries(protocol.parse_p2(plain, 'psr/container'), REPO_URL, HOSTS, strip_source=False)
    assert tar[0]['dist']['url'] == f'{REPO_URL}dists/psr/container/{N_1_0_0_0}/dist.tar'
    found = protocol.find_dist(tar, N_1_0_0_0, 'dist.tar')
    assert found is not None and found.shasum == 'ab' * 20
    assert protocol.parse_p2(p2_doc(upstream_entry('1.0.0'), name='other/pkg'), 'psr/container') == []
    for bad in (b'[]', b'{"packages": []}', json.dumps({'packages': {'psr/container': 'x'}}).encode()):
        with pytest.raises(UpstreamError):
            protocol.parse_p2(bad, 'psr/container')


def test_fetch_url_and_merge() -> None:
    api = f'https://api.github.com/repos/php-fig/container/zipball/{REF}'
    assert protocol.fetch_url(api, HOSTS) == f'https://codeload.github.com/php-fig/container/legacy.zip/{REF}'
    assert protocol.fetch_url(api, frozenset({'api.github.com'})) == api
    assert (
        protocol.fetch_url('https://gitlab.com/a/b/-/archive/x.zip', HOSTS) == 'https://gitlab.com/a/b/-/archive/x.zip'
    )
    a = [{'version': '1.0.0', 'version_normalized': N_1_0_0_0, 'dist': {'url': f'{REPO_URL}dists/x'}, 'from': 'a'}]
    b = [
        {'version': '1.0.0', 'version_normalized': N_1_0_0_0, 'from': 'b'},
        {'version': '2.0.0', 'version_normalized': N_2_0_0_0, 'from': 'b'},
    ]
    moved = protocol.reprefix(a, REPO_URL, 'https://ws.example.com/repository/all/')
    assert moved[0]['dist']['url'] == 'https://ws.example.com/repository/all/dists/x'
    merged = protocol.merge([moved, b])
    assert [(e['version'], e['from']) for e in merged] == [('2.0.0', 'b'), ('1.0.0', 'a')]


def test_archive_checks() -> None:
    good = make_zip({'demo-abc/composer.json': b'{"name": "acme/demo"}', 'demo-abc/src/A.php': b'<?php'})
    archive.check_zip(io.BytesIO(good))
    assert archive.read_composer_json(io.BytesIO(good)) == {'name': 'acme/demo'}
    root = make_zip({'composer.json': b'\xef\xbb\xbf{"name": "acme/demo"}', 'x/y.php': b''})
    assert archive.read_composer_json(io.BytesIO(root))['name'] == 'acme/demo'
    for files, message in (
        ({'../evil.php': b''}, 'unsafe'),
        ({'/abs.php': b''}, 'unsafe'),
        ({'C:/x.php': b''}, 'unsafe'),
        ({'a/composer.json': b'{}', 'b/x': b''}, 'no composer.json'),
        ({'composer.json': b'[1]'}, 'not a JSON object'),
        ({'composer.json': b'{'}, 'not valid JSON'),
    ):
        with pytest.raises(ValidationFailed, match=message):
            archive.read_composer_json(io.BytesIO(make_zip(files)))
    with pytest.raises(ValidationFailed, match='not a zip'):
        archive.check_zip(io.BytesIO(b'not a zip'))
    with pytest.raises(ValidationFailed, match='not a Composer package'):
        archive.read_composer_json(io.BytesIO(b'not a zip'))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        info = tarfile.TarInfo('pkg/composer.json')
        info.size = 2
        tar.addfile(info, io.BytesIO(b'{}'))
    archive.check_archive(io.BytesIO(buf.getvalue()), 'tar')
    linked = io.BytesIO()
    with tarfile.open(fileobj=linked, mode='w') as tar:
        link = tarfile.TarInfo('pkg/link')
        link.type = tarfile.SYMTYPE
        link.linkname = '/etc/passwd'
        tar.addfile(link)
    with pytest.raises(ValidationFailed, match='links'):
        archive.check_archive(io.BytesIO(linked.getvalue()), 'tar')
    with pytest.raises(ValidationFailed, match='not a tar'):
        archive.check_tar(io.BytesIO(b'x' * 10))


def test_declared_sizes_bound_the_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive, 'MAX_UNPACKED', 10)
    with pytest.raises(ValidationFailed, match='too large when unpacked'):
        archive.check_zip(io.BytesIO(make_zip({'composer.json': b'{"name": "acme/demo"}'})))
    monkeypatch.setattr(archive, 'MAX_ENTRIES', 1)
    with pytest.raises(ValidationFailed, match='too many files'):
        archive.check_zip(io.BytesIO(make_zip({'a': b'', 'b': b''})))


def test_package_details() -> None:
    doc = {
        'name': 'Acme/Demo',
        'version': 'v1.0',
        'type': 'library',
        'require': {'php': '>=8.1'},
        'scripts': {'x': 'y'},
        'repositories': [{'type': 'vcs', 'url': 'https://example.com/x.git'}],
        'dist': {'url': 'https://example.com/evil.zip'},
    }
    details = archive.package_details(doc, 'acme/demo', '1.0.0')
    assert details == {'type': 'library', 'require': {'php': '>=8.1'}}
    for bad, message in (
        ({'name': 'other/pkg'}, 'names the package'),
        ({'version': '2.0.0'}, 'declares the version'),
        ({'type': 5}, 'invalid type'),
        ({'require': 'php'}, 'invalid require'),
        ({'require': {'php': 8}}, 'invalid require entry'),
        ({'description': 'x' * (archive.MAX_DETAILS + 1)}, 'too large'),
    ):
        with pytest.raises(ValidationFailed, match=message):
            archive.package_details(bad, 'acme/demo', '1.0.0')


def test_hosted_entries_and_summaries() -> None:
    doc = {
        'path': f'dists/acme/demo/{N_1_0_0_0}/demo-1.0.0.0.zip',
        'composer': {
            'name': 'acme/demo',
            'version': '1.0.0',
            'version_normalized': N_1_0_0_0,
            'package_json': json.dumps({'type': 'library', 'description': 'Demo'}),
            'sha1': 'b' * 40,
            'published': NOW,
        },
    }
    entry = entry_of(doc, REPO_URL)
    assert entry['dist'] == {
        'type': 'zip',
        'url': f'{REPO_URL}dists/acme/demo/{N_1_0_0_0}/demo-1.0.0.0.zip',
        'reference': 'b' * 40,
        'shasum': 'b' * 40,
    }
    assert entry['time'] == '2026-11-22T12:00:00+00:00' and entry['type'] == 'library'
    assert entry_of({'path': 'p', 'composer': {'package_json': '{'}}, '')['dist']['url'] == 'p'
    dev = {**entry, 'version': 'dev-main', 'version_normalized': 'dev-main'}
    assert split([entry, dev], dev=True) == [dev] and split([entry, dev], dev=False) == [entry]
    proxied = protocol.rewrite_entries(
        protocol.parse_p2(p2_doc(upstream_entry('2.0.0'), upstream_entry('2.1.0-beta1')), 'psr/container'),
        REPO_URL,
        HOSTS,
        strip_source=False,
    )
    fields = summary_fields(proxied)
    assert fields['latest'] == '2.0.0' and fields['version_count'] == 2 and fields['type'] == 'library'
    assert fields['versions'][0]['normalized'] == f'{N_2_1_0_0}-beta1'
    assert fields['versions'][1]['path'] == f'dists/psr/container/{N_2_0_0_0}/{REF}.zip'
    assert summary_fields([])['latest'] is None


def repo(**fields: Any) -> RepoConfig:
    return RepoConfig.model_validate({'format': 'composer', 'created_at': NOW, 'updated_at': NOW, **fields})


def test_setup_snippets_and_options() -> None:
    def contents(r: RepoConfig, base: str = 'https://ws.example.com') -> str:
        ctx = SetupContext(repo=r, base_url=base, registry_host='ws.example.com', username='u')
        return '\n'.join(s.content for c in DESCRIPTOR.setup_snippets(ctx) for s in c.snippets)

    proxy = contents(repo(id='r1', name='php', type=RepoType.PROXY))
    assert f'composer config repositories.waystation composer {REPO_URL}' in proxy
    assert 'composer config http-basic.ws.example.com {{USERNAME}} {{TOKEN}}' in proxy
    assert 'secure-http' not in proxy and 'packages/upload' not in proxy
    plain = contents(repo(id='r2', name='mine', type=RepoType.HOSTED), base='http://ws.example.com:8080')
    assert 'composer config secure-http false' in plain and 'http-basic.ws.example.com:8080' in plain
    assert (
        '--upload-file ../acme-demo-1.0.0.zip http://ws.example.com:8080/repository/mine/packages/upload/acme/demo/1.0.0'
        in plain
    )
    group = repo(id='r3', name='all', type=RepoType.GROUP, group={'members': ['mine', 'php']})
    ctx = SetupContext(group, 'https://ws.example.com', 'ws.example.com', None)
    assert 'in order: mine, php' in (DESCRIPTOR.setup_snippets(ctx)[0].snippets[0].description or '')
    assert ComposerOptions().model_dump() == {
        'dist_hosts': ['github.com', 'codeload.github.com', 'api.github.com', 'gitlab.com'],
        'source_mode': 'keep',
    }
    assert ComposerOptions.model_validate({'dist_hosts': ['GitHub.com', 'github.com']}).dist_hosts == ['github.com']
    with pytest.raises(ValueError):
        ComposerOptions.model_validate({'dist_hosts': ['not a host']})
    with pytest.raises(ValueError):
        ComposerOptions.model_validate({'source_mode': 'drop'})
    hosted_opts = ComposerOptions(dist_hosts=['example.com'], source_mode='strip')
    assert {f for f, _ in DESCRIPTOR.check_options(hosted_opts, RepoType.HOSTED)} == {'dist_hosts', 'source_mode'}
    assert DESCRIPTOR.check_options(hosted_opts, RepoType.PROXY) == []
    assert DESCRIPTOR.check_options(ComposerOptions(), RepoType.GROUP) == []
    assert refresh_request('p2/psr/log.json') is not None and refresh_request('p2/psr/log~dev.json') is not None
    assert refresh_request('_upstream/packages.json') is None
    assert refresh_request(f'dists/psr/log/{N_1_0_0_0}/{REF}.zip') is None


class FakeWarm:
    def __init__(self, body: bytes) -> None:
        self.repo = repo(id='r1', name='php', type=RepoType.PROXY)
        self.body = body
        self.asked: list[str] = []

    async def get(self, path: str, headers: Mapping[str, str] | None = None) -> Fetched:
        self.asked.append(path)
        return Fetched(path=path, status=200, cache='MISS', headers={})

    async def read(self, fetched: Fetched, *, max_bytes: int) -> bytes:
        return self.body


async def test_prefetch() -> None:
    assert prefetch.check_item('psr/log:3.0.2') is None
    assert prefetch.check_item('Psr/Log') is None
    assert prefetch.check_item('psr') is not None
    assert prefetch.check_item('psr/log:latest!') is not None
    assert prefetch.parse_item('psr/log:v3.0') == ('psr/log', N_3_0_0_0)
    entries = protocol.rewrite_entries(
        protocol.parse_p2(p2_doc(upstream_entry('2.0.0'), upstream_entry('2.1.0-beta1')), 'psr/container'),
        'https://ws.example.com/repository/php/',
        HOSTS,
        strip_source=False,
    )
    body = protocol.render_p2('psr/container', entries)
    ctx = FakeWarm(body)
    await prefetch.warm(ctx, 'psr/container')
    assert ctx.asked == ['p2/psr/container.json', f'dists/psr/container/{N_2_0_0_0}/{REF}.zip']
    dev = FakeWarm(body)
    with pytest.raises(NotFound):
        await prefetch.warm(dev, 'psr/container:dev-main')
    assert dev.asked == ['p2/psr/container~dev.json']
    with pytest.raises(NotFound):
        await prefetch.warm(FakeWarm(p2_doc()), 'psr/container')
    outside = FakeWarm(protocol.render_p2('psr/container', [upstream_entry('2.0.0')]))
    await prefetch.warm(outside, 'psr/container:2.0.0')
    assert outside.asked == ['p2/psr/container.json']  # the archive is not served by the repository


def test_nexus_import() -> None:
    supported = {'composer': frozenset({'proxy', 'hosted', 'group'})}
    entry = {'name': 'packagist', 'format': 'composer', 'type': 'proxy'}
    detail = {'name': 'packagist', 'proxy': {'remoteUrl': 'https://repo.packagist.org', 'contentMaxAge': 1440}}
    mapped = map_repository(NexusRepository(entry=entry, detail=detail), supported=supported)
    assert mapped.action == 'create' and mapped.body is not None
    assert mapped.body['proxy']['upstream_url'] == 'https://repo.packagist.org'
