"""RubyGems format units (T-P6-GEMS): routes, compact index lines, the Marshal writer, safe .gem reading, prefetch
items, the descriptor, version order and cleanup coordinates."""

import gzip
import hashlib
import io
import tarfile
import zlib
from datetime import UTC, datetime

import pytest
from ws_gems import make_gem, spec_yaml

from waystation_common import rubygems
from waystation_common.cleanup.plan import coordinates
from waystation_common.components import key_for
from waystation_common.errors import UpstreamError, ValidationFailed
from waystation_common.versionsort import version_sort
from waystation_server.formats.rubygems import marshal, package, prefetch, protocol
from waystation_server.formats.rubygems.descriptor import refresh_request
from waystation_server.formats.rubygems.group import _merge_versions, merged_info
from waystation_server.formats.rubygems.hosted import tuples, versions_of
from waystation_server.formats.rubygems.plugin import check_gemspec, check_specs, summary_fields
from waystation_server.formats.rubygems.protocol import GemVersions, Op


# ---------------------------------------------------------------------------- names, versions, order
@pytest.mark.parametrize(
    ('low', 'high'),
    [
        ('1.0.a', '1.0'),
        ('1.0', '1.0.1'),
        ('1.9', '1.10'),
        ('2.0.0.rc1', '2.0.0'),
        ('1.0.0.pre', '1.0.0.rc'),
        ('1.0.0.rc1', '1.0.0.rc2'),
        ('1.0.0-rc1', '1.0.0'),  # '-' reads as '.pre.'
        ('0.9.9', '1.0.0.a'),
        ('1.0.a', '1.0.a.1'),
        ('1.0.a', '1.0.ab'),
    ],
)
def test_sort_order(low: str, high: str) -> None:
    assert rubygems.sort_string(low) < rubygems.sort_string(high)
    assert version_sort('rubygems', low) < version_sort('rubygems', high)


def test_equal_versions_and_names() -> None:
    assert rubygems.sort_string('1.0') == rubygems.sort_string('1.0.0')
    assert rubygems.sort_string('1.0.a') == rubygems.sort_string('1.0.0.a.0')
    assert rubygems.is_prerelease('1.0.0.rc1') and not rubygems.is_prerelease('1.0.0')
    assert rubygems.newest(['1.0.0', '2.0.0.rc1', '1.10.0']) == '1.10.0'
    assert rubygems.newest(['2.0.0.rc1', '2.0.0.beta']) == '2.0.0.rc1'
    assert rubygems.valid_name('RedCloth') and rubygems.valid_name('net-http2') and not rubygems.valid_name('a/b')
    assert not rubygems.valid_name('..') and not rubygems.valid_version('1.0 ') and rubygems.valid_version('1.0-x.1')
    assert rubygems.full_name('nokogiri', '1.18.8', 'x86_64-linux') == 'nokogiri-1.18.8-x86_64-linux'
    assert rubygems.full_name('rake', '13.2.1') == 'rake-13.2.1'


def test_split_names() -> None:
    assert rubygems.split_full_name('rake-13.2.1') == [('rake', '13.2.1', 'ruby')]
    assert rubygems.split_full_name('nokogiri-1.18.8-x86_64-linux-musl') == [
        ('nokogiri', '1.18.8', 'x86_64-linux-musl')
    ]
    assert rubygems.split_full_name('net-http-2-0.1.0') == [('net-http-2', '0.1.0', 'ruby')]
    assert rubygems.split_full_name('foo-1.0-java') == [('foo', '1.0', 'java')]
    assert rubygems.split_full_name('nodash') == []
    assert rubygems.split_index_version('1.0.0-java') == ('1.0.0', 'java')
    assert rubygems.split_index_version('x1.0') is None


def test_cleanup_and_components() -> None:
    gem = coordinates('rubygems', 'gems/nokogiri-1.18.8-x86_64-linux.gem')
    assert (gem.name, gem.version, gem.group, gem.ordered_by_version) == (
        'nokogiri',
        '1.18.8',
        'nokogiri/x86_64-linux',
        True,
    )
    spec = coordinates('rubygems', 'quick/Marshal.4.8/rake-13.2.1.gemspec.rz')
    assert (spec.name, spec.version, spec.group) == ('rake', '13.2.1', 'rake')
    key = key_for('rubygems', 'gems/rake-13.2.1.gem')
    assert key is not None and (key.name, key.version, key.display) == ('rake', '13.2.1', 'rake 13.2.1')
    assert key_for('rubygems', 'quick/Marshal.4.8/rake-13.2.1.gemspec.rz') is None
    assert key_for('rubygems', 'info/rake') is None


# ---------------------------------------------------------------------------- routes and compact index
def test_routes() -> None:
    assert protocol.parse_route('versions') == protocol.Route(Op.VERSIONS, key='versions')
    assert protocol.parse_route('info/rake') == protocol.Route(Op.INFO, 'rake', key='info/rake')
    route = protocol.parse_route('gems/rake-13.2.1.gem')
    assert route is not None and (route.op, route.name) == (Op.GEM, 'rake-13.2.1')
    route = protocol.parse_route('quick/Marshal.4.8/rake-13.2.1.gemspec.rz')
    assert route is not None and route.op is Op.GEMSPEC
    assert protocol.parse_route('specs.4.8.gz') is not None
    assert protocol.parse_route('api/v1/gems') == protocol.Route(Op.PUSH, key='api/v1/gems')
    assert protocol.parse_route('api/v1/gems/yank') == protocol.Route(Op.YANK, key='api/v1/gems/yank')
    assert protocol.parse_route('api/v1/dependencies.json') is not None
    for bad in ('info/a/b', 'gems/nodash.gem', 'gems/../x-1.gem', 'quick/Marshal.4.8/x.gemspec.rz', 'other'):
        assert protocol.parse_route(bad) is None


def test_info_lines() -> None:
    sha = 'a' * 64
    line = protocol.render_info_line(
        '1.2.0',
        'ruby',
        [('rack', (('>=', '2.0'), ('<', '4'))), ('json', ())],
        sha,
        (('>=', '3.0'),),
        (('>=', '0'),),
    )
    assert line == f'1.2.0 rack:>= 2.0&< 4,json:>= 0|checksum:{sha},ruby:>= 3.0'
    parsed = protocol.parse_info_line(line)
    assert parsed is not None
    assert parsed.dependencies == (('rack', '>= 2.0&< 4'), ('json', '>= 0'))
    assert (parsed.checksum, parsed.ruby, parsed.rubygems, parsed.key) == (sha, '>= 3.0', None, '1.2.0')
    native = protocol.parse_info_line(f'1.0.0-java |checksum:{sha}')
    assert native is not None and (native.version, native.platform, native.dependencies) == ('1.0.0', 'java', ())
    data = f'---\n1.0.0 |checksum:{sha}\nnot a line\n2.0.0 |checksum:short\n'.encode()
    lines = protocol.parse_info(data)
    assert [li.key for li in lines] == ['1.0.0', '2.0.0'] and lines[1].checksum is None
    fields = summary_fields(lines, 'demo')
    assert fields['latest'] == '2.0.0' and fields['versions'][0]['path'] == 'gems/demo-2.0.0.gem'
    merged = merged_info([[lines[0]], protocol.parse_info(f'---\n1.0.0 x:>= 1|checksum:{"b" * 64}\n'.encode())])
    assert merged == f'---\n1.0.0 |checksum:{sha}\n'.encode()  # the first member wins a version


def test_versions_file() -> None:
    data = (
        b'created_at: 2026-11-01T00:00:00Z\n---\n'
        b'rake 13.0.0,13.1.0 aaa\nrack 3.0.0 bbb\nrake 13.2.0,-13.0.0 ccc\nbad line\n'
    )
    parsed = protocol.parse_versions(data)
    assert parsed['rake'].versions == ['13.1.0', '13.2.0'] and parsed['rake'].md5 == 'ccc'
    rendered = protocol.render_versions(parsed, datetime(2026, 11, 2, tzinfo=UTC))
    assert rendered == b'created_at: 2026-11-02T00:00:00Z\n---\nrake 13.1.0,13.2.0 ccc\nrack 3.0.0 bbb\n'
    merged, shared = _merge_versions(
        [parsed, {'rake': GemVersions(['9.0.0', '13.2.0'], 'x'), 'own': GemVersions(['1'], 'y')}]
    )
    assert shared == ['rake'] and merged['rake'].versions == ['13.1.0', '13.2.0', '9.0.0'] and merged['own'].md5 == 'y'
    protocol.check_versions(data)
    with pytest.raises(UpstreamError):
        protocol.check_versions(b'<html>')
    with pytest.raises(UpstreamError):
        protocol.check_index_text(b'<html>', 'info file')


def test_hosted_documents() -> None:
    sha = 'c' * 64
    docs = [
        {
            'path': 'gems/demo-1.0.0.gem',
            'rubygems': {'name': 'demo', 'version': '1.0.0', 'key': '1.0.0', 'info_line': f'1.0.0 |checksum:{sha}'},
        },
        {
            'path': 'gems/demo-1.1.0.rc1.gem',
            'rubygems': {
                'name': 'demo',
                'version': '1.1.0.rc1',
                'key': '1.1.0.rc1',
                'info_line': f'1.1.0.rc1 |checksum:{sha}',
            },
        },
        {
            'path': 'gems/demo-1.0.0-java.gem',
            'rubygems': {
                'name': 'demo',
                'version': '1.0.0',
                'platform': 'java',
                'key': '1.0.0-java',
                'info_line': f'1.0.0-java |checksum:{sha}',
            },
        },
    ]
    found = versions_of(docs)
    info = protocol.render_info([f'1.0.0 |checksum:{sha}', f'1.1.0.rc1 |checksum:{sha}', f'1.0.0-java |checksum:{sha}'])
    assert found['demo'].versions == ['1.0.0', '1.1.0.rc1', '1.0.0-java']
    assert found['demo'].md5 == hashlib.md5(info, usedforsecurity=False).hexdigest()
    assert tuples(docs, 'specs.4.8.gz') == [('demo', '1.0.0', 'java'), ('demo', '1.0.0', 'ruby')]
    assert tuples(docs, 'prerelease_specs.4.8.gz') == [('demo', '1.1.0.rc1', 'ruby')]
    assert tuples(docs, 'latest_specs.4.8.gz') == [('demo', '1.0.0', 'java'), ('demo', '1.0.0', 'ruby')]


# ---------------------------------------------------------------------------- Marshal writer
def test_marshal_matches_ruby() -> None:
    # Ruby: Marshal.dump(["a", 1, nil, true, :sym, :sym, 300, -1])
    assert marshal.dump(['a', 1, None, True, marshal.Sym('sym'), marshal.Sym('sym'), 300, -1]) == (
        b'\x04\x08[\x0dI"\x06a\x06:\x06ETi\x060T:\x08sym;\x06i\x02\x2c\x01i\xfa'
    )
    # Ruby: Marshal.dump([["rake", Gem::Version.new("13.2.1"), "ruby"]]) with UTF-8 strings
    assert marshal.dump([['rake', marshal.Version('13.2.1'), 'ruby']]) == (
        b'\x04\x08[\x06[\x08I"\x09rake\x06:\x06ETU:\x11Gem::Version[\x06I"\x0b13.2.1\x06;\x00TI"\x09ruby\x06;\x00T'
    )
    assert (
        marshal._long(123) == b'\x01\x7b'
        and marshal._long(-124) == b'\xff\x84'
        and marshal._long(-257) == b'\xfe\xff\xfe'
    )
    with pytest.raises(TypeError):
        marshal.dump(object())


def test_gemspec_and_specs() -> None:
    read = package.read_gem(io.BytesIO(make_gem('demo', '1.0.0', deps=[('rack', '~>', '3.0', 'runtime')])))
    body = zlib.decompress(marshal.gemspec_rz(read.spec.marshal_fields()))
    assert body.startswith(b'\x04\x08u:\x17Gem::Specification')
    for part in (b'Gem::Dependency', b'@requirement', b'Gem::Requirement', b'~>', b'runtime', b'2026-11-01', b'demo'):
        assert part in body
    specs = gzip.decompress(marshal.specs_gz([('demo', '1.0.0', 'ruby')]))
    assert specs.startswith(b'\x04\x08[\x06[\x08') and b'Gem::Version' in specs


# ---------------------------------------------------------------------------- reading .gem files
def test_read_gem() -> None:
    gem = make_gem(
        'demo',
        '1.2.0',
        deps=[('rack', '>=', '2.0', 'runtime'), ('rspec', '~>', '3.0', 'development')],
        ruby=('>=', '3.1'),
    )
    read = package.read_gem(io.BytesIO(gem))
    spec = read.spec
    assert read.sha256 == hashlib.sha256(gem).hexdigest()
    assert (spec.name, spec.version, spec.platform, spec.full_name) == ('demo', '1.2.0', 'ruby', 'demo-1.2.0')
    assert [(d.name, d.type, d.text()) for d in spec.dependencies] == [
        ('rack', 'runtime', '>= 2.0'),
        ('rspec', 'development', '~> 3.0'),
    ]
    assert [d.name for d in spec.runtime] == ['rack']
    assert spec.required_ruby_version == (('>=', '3.1'),) and spec.date == '2026-11-01'
    assert spec.licenses == ('MIT',) and spec.metadata == {'source_code_uri': 'https://example.com/demo'}
    native = package.read_gem(io.BytesIO(make_gem('demo', '1.2.0', platform='x86_64-linux')))
    assert native.spec.full_name == 'demo-1.2.0-x86_64-linux'


def _tar_with(info: tarfile.TarInfo, data: bytes = b'') -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as tar:
        tar.addfile(info, io.BytesIO(data) if data else None)
    return buf.getvalue()


def test_refused_gems() -> None:
    link = tarfile.TarInfo('metadata.gz')
    link.type = tarfile.SYMTYPE
    link.linkname = '/etc/passwd'
    evil = tarfile.TarInfo('../metadata.gz')
    evil.size = 1
    unsafe_yaml = spec_yaml('demo', '1.0.0').replace('name: demo', 'name: !!python/object/apply:os.system ["true"]')
    cases = [
        b'not a tar',
        _tar_with(link),
        _tar_with(evil, b'x'),
        make_gem('demo', '1.0.0', tamper_checksum=True),
        make_gem('demo', '1.0.0', yaml_text='--- [unclosed\n'),
        make_gem('demo', '1.0.0', yaml_text=unsafe_yaml),
        make_gem('demo', 'not-a-version'),
        make_gem('demo', '1.0.0', extra=[('evil.rb', b'x')]),
        make_gem('de/mo', '1.0.0'),
    ]
    for gem in cases:
        with pytest.raises(ValidationFailed):
            package.read_gem(io.BytesIO(gem))
    bomb = '---\na: &a ["x","x","x","x","x","x","x","x","x"]\n' + ''.join(
        f'{chr(98 + i)}: &{chr(98 + i)} [*{chr(97 + i)},*{chr(97 + i)},*{chr(97 + i)}]\n' for i in range(3)
    )
    assert isinstance(package.load_yaml(bomb.encode()), dict)  # aliases share objects: nothing is expanded
    with pytest.raises(ValidationFailed):
        package.load_yaml(('---\na: &a 1\nb: [' + ','.join(['*a'] * 1001) + ']\n').encode())


def test_upstream_checks() -> None:
    from waystation_server.proxy.engine import UpstreamInfo

    info = UpstreamInfo(url='https://rubygems.org/x', status=200, headers={}, latency_ms=1)
    check_gemspec(zlib.compress(b'\x04\x08u:\x17Gem::Specification'), info)
    for bad in (b'not zlib', zlib.compress(b'{"json": true}')):
        with pytest.raises(UpstreamError):
            check_gemspec(bad, info)
    check_specs(gzip.compress(b'\x04\x08[\x00'), info)
    with pytest.raises(UpstreamError):
        check_specs(b'plain', info)


# ---------------------------------------------------------------------------- prefetch, descriptor
def test_prefetch_items() -> None:
    assert prefetch.parse_item('rake') == ('rake', None)
    assert prefetch.parse_item('rake:13.2.1') == ('rake', ('13.2.1', 'ruby'))
    assert prefetch.parse_item(' nokogiri:1.18.8-x86_64-linux ') == ('nokogiri', ('1.18.8', 'x86_64-linux'))
    assert prefetch.check_item('rake@1') is not None and prefetch.check_item('rake:') is not None
    assert prefetch.check_item('rake:13.2.1') is None


def test_refresh_requests() -> None:
    assert refresh_request('versions') is not None and refresh_request('info/rake') is not None
    assert refresh_request('names') is not None
    assert refresh_request('gems/rake-13.2.1.gem') is None and refresh_request('specs.4.8.gz') is None
