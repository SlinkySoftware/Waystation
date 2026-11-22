"""Cargo (T-P6-CARGO): crate names and the index layout, routes, index entries, ``dl`` templates, publish payloads
and ``.crate`` checks, prefetch items, request actions and "Set me up"."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
from ws_testing import make_repo

from waystation_common.errors import UpstreamError
from waystation_server.formats.base import SetupContext
from waystation_server.formats.cargo import prefetch
from waystation_server.formats.cargo.common import (
    Op,
    canonical,
    crate_path,
    download_url,
    index_path,
    latest_of,
    newest_first,
    parse_index,
    parse_route,
    parse_upstream_config,
    render_index,
    summary_fields,
    valid_name,
    valid_version,
)
from waystation_server.formats.cargo.descriptor import DESCRIPTOR
from waystation_server.formats.cargo.hosted import BodyReader, Refused, entries_of
from waystation_server.formats.cargo.publish import Invalid, index_entry, inspect_crate, parse_metadata
from waystation_server.repos.model import RepoType
from waystation_server.security.actions import Action

CKSUM = 'a' * 64


def line(name: str = 'serde', vers: str = '1.0.0', **extra: object) -> dict[str, object]:
    return {'name': name, 'vers': vers, 'deps': [], 'cksum': CKSUM, 'features': {}, 'yanked': False, **extra}


def crate_file(top: str, manifest: str | None, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        files = dict(extra or {})
        if manifest is not None:
            files[f'{top}/Cargo.toml'] = manifest.encode()
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# ---------------------------------------------------------------------------- names, layout, routes
@pytest.mark.parametrize(
    ('name', 'path'),
    [('a', 'index/1/a'), ('ab', 'index/2/ab'), ('abc', 'index/3/a/abc'), ('Serde', 'index/se/rd/serde')],
)
def test_index_layout(name: str, path: str) -> None:
    assert index_path(name) == path
    route = parse_route(path, 'GET')
    assert route is not None and route.op is Op.INDEX and route.crate == name.lower()


def test_names_and_versions() -> None:
    assert valid_name('serde_json') and valid_name('a') and valid_name('A-b_c') and valid_name('x' * 64)
    assert not valid_name('1abc') and not valid_name('-x') and not valid_name('x' * 65) and not valid_name('a.b')
    assert canonical('Foo-Bar') == canonical('foo_bar') == 'foo_bar'
    assert valid_version('1.0.0') and valid_version('1.0.0-rc.1+build.7') and not valid_version('v1.0.0')
    assert not valid_version('1.0') and not valid_version('01.0.0') and not valid_version('1.0.0-01')
    assert newest_first(['1.0.0', '1.10.0', '1.2.0', '1.10.0-rc.1']) == ['1.10.0', '1.10.0-rc.1', '1.2.0', '1.0.0']


def test_routes() -> None:
    assert parse_route('index/config.json', 'GET') == parse_route('index/config.json', 'HEAD')
    assert parse_route('index/se/rd/Serde', 'GET') is None  # index paths are lower case
    assert parse_route('index/xx/rd/serde', 'GET') is None  # wrong prefix
    assert parse_route('index/se/rd', 'GET') is None
    download = parse_route('crates/Inflector/Inflector-0.11.4.crate', 'GET')
    assert download is not None and (download.op, download.crate, download.version) == (
        Op.DOWNLOAD,
        'inflector',
        '0.11.4',
    )
    assert crate_path('Inflector', '0.11.4') == 'crates/inflector/inflector-0.11.4.crate'
    assert parse_route('crates/serde/other-1.0.0.crate', 'GET') is None
    assert parse_route('crates/serde/serde-1.0.crate', 'GET') is None
    assert parse_route('api/v1/crates/new', 'PUT') is not None
    yank = parse_route('api/v1/crates/Serde/1.0.0/yank', 'DELETE')
    assert yank is not None and (yank.op, yank.crate, yank.version) == (Op.YANK, 'serde', '1.0.0')
    unyank = parse_route('api/v1/crates/serde/1.0.0/unyank', 'PUT')
    assert unyank is not None and unyank.op is Op.UNYANK
    owners = parse_route('api/v1/crates/serde/owners', 'GET')
    assert owners is not None and owners.op is Op.OWNERS
    search = parse_route('api/v1/crates', 'GET')
    assert search is not None and search.op is Op.SEARCH
    assert parse_route('api/v1/crates/serde/x/yank', 'DELETE') is None
    assert parse_route('simple/', 'GET') is None


# ---------------------------------------------------------------------------- index entries
def test_parse_index_checks_every_line() -> None:
    data = render_index([line(vers='1.0.0'), line(name='Serde', vers='1.1.0', yanked=True)])
    entries = parse_index(data + b'\n\n', 'serde')
    assert [e['vers'] for e in entries] == ['1.0.0', '1.1.0']
    assert latest_of(entries) == '1.0.0'  # the yanked one does not count
    assert latest_of([line(vers='2.0.0-rc.1'), line(vers='1.0.0')]) == '1.0.0'
    assert latest_of([line(vers='2.0.0', yanked=True)]) == '2.0.0'
    assert latest_of([]) is None
    for bad in (
        b'not json\n',
        b'[1]\n',
        json.dumps(line(name='other')).encode(),
        json.dumps(line(vers='1.0')).encode(),
        json.dumps(line(cksum='ABC')).encode(),
        json.dumps(line(deps={})).encode(),
        json.dumps(line(yanked='no')).encode(),
    ):
        with pytest.raises(UpstreamError):
            parse_index(bad, 'serde')
    fields = summary_fields(entries, 'serde', 'Serialization')
    assert fields == {
        'display_name': 'Serde',
        'canonical': 'serde',
        'latest': '1.0.0',
        'version_count': 2,
        'description': 'Serialization',
    }


def test_download_url_templates() -> None:
    entry = line(name='Serde', vers='1.0.0')
    assert (
        download_url('https://static.crates.io/crates', entry) == 'https://static.crates.io/crates/Serde/1.0.0/download'
    )
    template = 'https://dl.example.com/{prefix}/{lowerprefix}/{crate}/{version}/{sha256-checksum}'
    assert download_url(template, entry) == f'https://dl.example.com/Se/rd/se/rd/Serde/1.0.0/{CKSUM}'
    assert parse_upstream_config(b'{"dl":"https://static.crates.io/crates","api":"https://crates.io"}').startswith(
        'https://'
    )
    for bad in (b'{}', b'[]', b'nope', b'{"dl":"file:///etc"}'):
        with pytest.raises(UpstreamError):
            parse_upstream_config(bad)


# ---------------------------------------------------------------------------- publish
def test_publish_metadata_and_index_entry() -> None:
    meta = parse_metadata(
        json.dumps(
            {
                'name': 'demo-crate',
                'vers': '0.2.0',
                'deps': [
                    {
                        'name': 'serde',
                        'version_req': '^1',
                        'features': ['derive'],
                        'optional': True,
                        'default_features': True,
                        'target': None,
                        'kind': 'normal',
                        'registry': 'https://github.com/rust-lang/crates.io-index',
                        'explicit_name_in_toml': 'serde1',
                    },
                    {
                        'name': 'itoa',
                        'version_req': '^1',
                        'features': [],
                        'optional': False,
                        'default_features': False,
                        'target': 'cfg(unix)',
                        'kind': 'dev',
                    },
                ],
                'features': {'default': ['std'], 'std': [], 'json': ['dep:serde1', 'serde1?/std']},
                'description': 'A demo',
                'rust_version': '1.70',
                'links': None,
            }
        ).encode()
    )
    entry = index_entry(meta, CKSUM)
    assert entry['name'] == 'demo-crate' and entry['vers'] == '0.2.0' and entry['cksum'] == CKSUM
    assert entry['features'] == {'default': ['std'], 'std': []}
    assert entry['features2'] == {'json': ['dep:serde1', 'serde1?/std']} and entry['v'] == 2
    assert entry['rust_version'] == '1.70'
    renamed = next(d for d in entry['deps'] if d['name'] == 'serde1')
    assert renamed['package'] == 'serde' and renamed['req'] == '^1' and renamed['registry'].endswith('crates.io-index')
    dev = next(d for d in entry['deps'] if d['name'] == 'itoa')
    assert dev['kind'] == 'dev' and dev['target'] == 'cfg(unix)' and 'registry' not in dev and 'package' not in dev
    stored = entries_of([{'cargo': {'entry': entry, 'yanked': True, 'version_sort': '1'}}, {'cargo': None}])
    assert stored == [{**entry, 'yanked': True}]


@pytest.mark.parametrize(
    'doc',
    [
        {'name': '1bad', 'vers': '1.0.0'},
        {'name': 'ok', 'vers': '1.0'},
        {'name': 'ok', 'vers': '1.0.0', 'deps': [{'name': 'x'}]},
        {'name': 'ok', 'vers': '1.0.0', 'deps': [{'name': 'x', 'version_req': '1', 'kind': 'weird'}]},
        {'name': 'ok', 'vers': '1.0.0', 'deps': [{'name': 'x', 'version_req': '1', 'optional': 'yes'}]},
        {'name': 'ok', 'vers': '1.0.0', 'features': {'a': 'b'}},
        {'name': 'ok', 'vers': '1.0.0', 'description': 5},
        [],
    ],
)
def test_publish_metadata_refused(doc: object) -> None:
    with pytest.raises(Invalid):
        parse_metadata(json.dumps(doc).encode())
    with pytest.raises(Invalid):
        parse_metadata(b'{')


def test_inspect_crate(tmp_path: Path) -> None:
    good = tmp_path / 'good.crate'
    manifest = '[package]\nname = "demo"\nversion = "0.1.0"\n'
    good.write_bytes(crate_file('demo-0.1.0', manifest, {'demo-0.1.0/src/lib.rs': b'pub fn f() {}\n'}))
    inspect_crate(good, 'demo', '0.1.0')
    cases = [
        crate_file('demo-0.1.0', None),
        crate_file('demo-0.1.0', '[package]\nname = "other"\nversion = "0.1.0"\n'),
        crate_file('demo-0.1.0', 'not = [toml'),
        crate_file('demo-0.1.0', manifest, {'../evil': b'x'}),
        crate_file('demo-0.1.0', manifest, {'other-0.1.0/x': b'x'}),
        b'not a tarball',
    ]
    for i, data in enumerate(cases):
        path = tmp_path / f'bad{i}.crate'
        path.write_bytes(data)
        with pytest.raises(Invalid):
            inspect_crate(path, 'demo', '0.1.0')


async def test_body_reader() -> None:
    async def stream(parts: list[bytes]):  # type: ignore[no-untyped-def]
        for part in parts:
            yield part

    reader = BodyReader(stream([b'\x03\x00', b'\x00\x00ab', b'cXYZ']))
    assert await reader.u32() == 3 and await reader.read_exact(3) == b'abc'
    assert b''.join([c async for c in reader.chunks(3)]) == b'XYZ'
    assert await reader.at_end()
    short = BodyReader(stream([b'\x05\x00\x00\x00ab']))
    assert await short.u32() == 5
    with pytest.raises(Refused):
        await short.read_exact(5)


# ---------------------------------------------------------------------------- prefetch, actions, setup
def test_prefetch_items_and_refresh() -> None:
    assert prefetch.check_item('serde@1.0.228') is None and prefetch.check_item('itoa') is None
    assert prefetch.check_item('serde@^1') is not None and prefetch.check_item('9x') is not None
    assert prefetch.parse_item(' Serde@1.0.0 ') == ('Serde', '1.0.0')
    assert prefetch.refresh_request('index/se/rd/serde') is not None
    assert prefetch.refresh_request('_upstream/config.json') is None
    assert prefetch.refresh_request('crates/serde/serde-1.0.0.crate') is None
    assert DESCRIPTOR.warm_item is prefetch.check_item


def test_request_actions_and_snippets() -> None:
    assert DESCRIPTOR.request_action('DELETE', 'api/v1/crates/x/1.0.0/yank') is Action.WRITE
    assert DESCRIPTOR.request_action('PUT', 'api/v1/crates/x/owners') is Action.WRITE
    assert DESCRIPTOR.request_action('GET', 'api/v1/crates/x/owners') is None
    assert DESCRIPTOR.probe_path(DESCRIPTOR.parse_options({})) == 'config.json'
    proxy = make_repo('crates', 'cargo')
    ctx = SetupContext(repo=proxy, base_url='https://ws.example.com', registry_host='ws.example.com', username='u')
    clients = DESCRIPTOR.setup_snippets(ctx)
    assert [c.id for c in clients] == ['cargo']
    replace = next(s for s in clients[0].snippets if s.id == 'cargo-replace' and s.os is None)
    assert 'index = "sparse+https://ws.example.com/repository/crates/index/"' in replace.content
    assert 'replace-with = "waystation"' in replace.content
    hosted = make_repo('internal', 'cargo', type=RepoType.HOSTED, proxy=None)
    ids = [c.id for c in DESCRIPTOR.setup_snippets(SetupContext(hosted, 'https://ws.example.com', 'x', None))]
    assert ids == ['cargo', 'cargo-publish']
