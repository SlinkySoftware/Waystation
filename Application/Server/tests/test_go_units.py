"""Go modules (T-P5-GO): module path case-encoding and validation, routes, checksum database records and ``h1``
hashes, version lists and ordering, GONOSUMDB patterns, options and "Set me up"."""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile

import pytest
from pydantic import ValidationError
from ws_testing import make_repo

from waystation_common.cleanup.plan import coordinates
from waystation_common.cleanup.versions import is_prerelease, sort_key
from waystation_common.errors import DigestMismatch, UpstreamError
from waystation_server.formats.base import SetupContext
from waystation_server.formats.go import gosum, modpath, sumdb
from waystation_server.formats.go.common import latest_of, matches_prefix_patterns, newest_first, parse_list
from waystation_server.formats.go.descriptor import DESCRIPTOR, GoOptions
from waystation_server.formats.go.modpath import Op
from waystation_server.formats.go.plugin import validate_info
from waystation_server.repos.model import RepoType

UUID_MOD = b'module github.com/google/uuid\n'
UUID_MOD_H1 = 'h1:TIyPZe4MgqvfeYDBFedMoGGpEw/LqOeaOT+nhxU+yHo='  # sum.golang.org, github.com/google/uuid v1.6.0
PSEUDO = 'v0.0.0-20191109021931-daa7c04131f5'


# ---------------------------------------------------------------------------- module paths
def test_case_encoding_round_trip() -> None:
    assert modpath.escape_path('github.com/Azure/azure-sdk') == 'github.com/!azure/azure-sdk'
    assert modpath.unescape_path('github.com/!azure/azure-sdk') == 'github.com/Azure/azure-sdk'
    assert modpath.unescape_path('github.com/Azure/x') == 'github.com/Azure/x'  # lenient: normalised by escaping
    assert modpath.escape_version('v1.0.0-RC1') == 'v1.0.0-!r!c1'
    assert modpath.unescape_version('v1.0.0-!r!c1') == 'v1.0.0-RC1'


@pytest.mark.parametrize(
    'escaped',
    [
        '',
        'github.com/!Azure/x',  # '!' must be followed by a lower-case letter
        'github.com/x!',
        'github.com//x',
        'github.com/../x',
        'github.com/.hidden',
        'github.com/x.',
        'github.com/x~1',  # Windows short name
        'github.com/x y',
        'github.com/é',
        'localhost/x',  # first element must be a host name with a dot
        '-bad.com/x',
        'Example.com/x',
        'example.com/' + 'x' * 600,
    ],
)
def test_invalid_module_paths(escaped: str) -> None:
    with pytest.raises(modpath.InvalidModule):
        modpath.unescape_path(escaped)


def test_versions() -> None:
    assert modpath.is_canonical('v1.2.3')
    assert modpath.is_canonical('v1.2.3-rc.1')
    assert modpath.is_canonical('v2.0.0+incompatible')
    assert modpath.is_canonical(PSEUDO)
    assert not modpath.is_canonical('v1.2')
    assert not modpath.is_canonical('1.2.3')
    assert not modpath.is_canonical('v1.2.3+build')
    assert not modpath.is_canonical('v01.2.3')
    assert not modpath.is_canonical('v1.2.3-01')
    assert modpath.is_pseudo(PSEUDO)
    assert modpath.is_pseudo('v1.2.4-0.20191109021931-daa7c04131f5')
    assert modpath.is_pseudo('v1.2.3-pre.0.20191109021931-daa7c04131f5+incompatible')
    assert not modpath.is_pseudo('v1.2.3-rc.1')
    for bad in ('', '.v1', 'v1/2', 'a' * 129, 'v1 2'):
        with pytest.raises(modpath.InvalidModule):
            modpath.unescape_version(bad)


def test_routes() -> None:
    route = modpath.parse_route('github.com/!azure/x/@v/list')
    assert route is not None and (route.op, route.module) == (Op.LIST, 'github.com/Azure/x')
    assert route.key == 'github.com/!azure/x/@v/list'
    latest = modpath.parse_route('github.com/Azure/x/@latest')  # upper case accepted, key canonical
    assert latest is not None and latest.op is Op.LATEST and latest.key == 'github.com/!azure/x/@latest'
    zipped = modpath.parse_route('example.com/m/v2/@v/v2.0.0+incompatible.zip')
    assert zipped is not None and (zipped.op, zipped.module, zipped.version) == (
        Op.ZIP,
        'example.com/m/v2',
        'v2.0.0+incompatible',
    )
    query = modpath.parse_route('example.com/m/@v/master.info')
    assert query is not None and (query.op, query.version) == (Op.INFO, 'master')
    assert modpath.parse_route('example.com/m/@v/master.zip') is None  # .mod/.zip: canonical versions only
    assert modpath.parse_route('example.com/m/@v/v1.0.0.tar') is None
    assert modpath.parse_route('example.com/m/@v/v1.0.0') is None
    assert modpath.parse_route('example.com/m') is None
    assert modpath.parse_route('example.com/@v/list') is not None
    assert modpath.parse_route('localhost/m/@v/list') is None
    sum_route = modpath.parse_route('sumdb/sum.golang.org/lookup/example.com/m@v1.0.0')
    assert sum_route is not None and (sum_route.op, sum_route.sumdb, sum_route.rest) == (
        Op.SUMDB,
        'sum.golang.org',
        'lookup/example.com/m@v1.0.0',
    )
    assert modpath.module_of_asset('example.com/!m/@v/v1.0.0.zip') == ('example.com/M', 'v1.0.0')
    assert modpath.module_of_asset('example.com/m/@v/list') == ('example.com/m', None)
    assert modpath.module_of_asset('sumdb/sum.golang.org/latest') is None


# ---------------------------------------------------------------------------- checksum database
def lookup(module: str, version: str, zip_h1: str, mod_h1: str) -> bytes:
    return f'4242\n{module} {version} {zip_h1}\n{module} {version}/go.mod {mod_h1}\n\ngo.sum database tree\n'.encode()


H1_A = 'h1:' + base64.b64encode(b'a' * 32).decode()
H1_B = 'h1:' + base64.b64encode(b'b' * 32).decode()


def test_parse_lookup() -> None:
    record = gosum.parse_lookup(lookup('example.com/M', 'v1.0.0', H1_A, H1_B), 'example.com/M', 'v1.0.0')
    assert record == gosum.SumRecord(zip_h1=H1_A, mod_h1=H1_B)
    with pytest.raises(UpstreamError):
        gosum.parse_lookup(lookup('example.com/other', 'v1.0.0', H1_A, H1_B), 'example.com/M', 'v1.0.0')
    with pytest.raises(UpstreamError):
        gosum.parse_lookup(lookup('example.com/M', 'v1.0.1', H1_A, H1_B), 'example.com/M', 'v1.0.0')
    with pytest.raises(UpstreamError):
        gosum.parse_lookup(b'not a record\n', 'example.com/M', 'v1.0.0')
    with pytest.raises(UpstreamError):
        gosum.parse_lookup(b'1\nexample.com/M v1.0.0 h2:abc\n', 'example.com/M', 'v1.0.0')
    with pytest.raises(UpstreamError):
        gosum.parse_lookup(b'1\n\nnote\n', 'example.com/M', 'v1.0.0')
    with pytest.raises(UpstreamError):
        gosum.parse_lookup(b'1\n' + b'x' * gosum.MAX_LOOKUP, 'example.com/M', 'v1.0.0')
    with pytest.raises(UpstreamError):
        gosum.parse_lookup(b'\xff\n', 'example.com/M', 'v1.0.0')


def test_mod_h1_matches_the_checksum_database() -> None:
    assert gosum.mod_h1(UUID_MOD) == UUID_MOD_H1
    gosum.mod_check(UUID_MOD_H1)(io.BytesIO(UUID_MOD))
    with pytest.raises(DigestMismatch):
        gosum.mod_check(UUID_MOD_H1)(io.BytesIO(UUID_MOD + b'// changed\n'))


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def expected_h1(files: dict[str, bytes]) -> str:
    summary = ''.join(f'{hashlib.sha256(files[n]).hexdigest()}  {n}\n' for n in sorted(files))
    return 'h1:' + base64.b64encode(hashlib.sha256(summary.encode()).digest()).decode()


def test_zip_h1() -> None:
    files = {
        'example.com/m@v1.0.0/go.mod': b'module example.com/m\n',
        'example.com/m@v1.0.0/a.go': b'package m\n',
        'example.com/m@v1.0.0/Z/b.go': b'package z\n',
    }
    data = make_zip(files)
    assert gosum.zip_h1(io.BytesIO(data)) == expected_h1(files)
    gosum.zip_check(expected_h1(files))(io.BytesIO(data))
    with pytest.raises(DigestMismatch):
        gosum.zip_check(H1_A)(io.BytesIO(data))
    with pytest.raises(DigestMismatch):
        gosum.zip_h1(io.BytesIO(b'not a zip'))
    with pytest.raises(DigestMismatch):
        gosum.zip_h1(io.BytesIO(make_zip({'a\nb': b'x'})))


def test_zip_h1_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    data = make_zip({'a': b'x' * 100, 'b': b'y'})
    monkeypatch.setattr(gosum, 'MAX_UNCOMPRESSED', 50)
    with pytest.raises(DigestMismatch, match='too large'):
        gosum.zip_h1(io.BytesIO(data))
    monkeypatch.setattr(gosum, 'MAX_FILES', 1)
    with pytest.raises(DigestMismatch, match='too many'):
        gosum.zip_h1(io.BytesIO(data))


def test_sumdb_paths() -> None:
    assert sumdb.parse('supported') == sumdb.SumdbPath('supported', 'supported')
    assert sumdb.parse('latest') == sumdb.SumdbPath('latest', 'latest')
    found = sumdb.parse('lookup/github.com/Azure/x@v1.0.0')
    assert found is not None and found.rest == 'lookup/github.com/!azure/x@v1.0.0'
    assert found.module == 'github.com/Azure/x'
    assert sumdb.parse('lookup/github.com/x@master') is None
    assert sumdb.parse('lookup/github.com/x') is None
    assert sumdb.parse('lookup/../x@v1.0.0') is None
    full = sumdb.parse('tile/8/0/x001/234')
    assert full is not None and (full.height, full.width, full.data) == (8, None, False)
    partial = sumdb.parse('tile/8/1/005.p/17')
    assert partial is not None and partial.width == 17
    data = sumdb.parse('tile/8/data/000')
    assert data is not None and data.data
    for bad in ('tile/8/0/1234', 'tile/8/0/005.p/256', 'tile/8/0/005.p/0', 'tile/0/0/000', 'tile/8/x/000', 'other'):
        assert sumdb.parse(bad) is None
    spec = sumdb.spec('https://sum.golang.org/', 'sum.golang.org', full)
    assert spec.upstream_url == 'https://sum.golang.org/tile/8/0/x001/234'
    assert spec.expected is not None and spec.expected.size == 256 * 32
    assert spec.path == 'sumdb/sum.golang.org/tile/8/0/x001/234'
    assert sumdb.spec('https://sum.golang.org', 'sum.golang.org', partial).expected.size == 17 * 32  # type: ignore[union-attr]
    assert sumdb.spec('https://sum.golang.org', 'sum.golang.org', partial).kind.value == 'metadata'
    assert sumdb.spec('https://sum.golang.org', 'sum.golang.org', data).expected is None


# ---------------------------------------------------------------------------- lists, versions, patterns
def test_parse_list_and_order() -> None:
    assert parse_list(b'v1.0.0\nv1.1.0 extra\n\nv1.0.0\n') == ['v1.0.0', 'v1.1.0']
    with pytest.raises(UpstreamError):
        parse_list(b'<html>not found</html>\n')
    with pytest.raises(UpstreamError):
        parse_list(b'\xff')
    versions = ['v1.0.0', 'v1.10.0', 'v1.2.0', 'v2.0.0+incompatible', 'v1.11.0-rc.1', PSEUDO]
    assert newest_first(versions) == ['v2.0.0+incompatible', 'v1.11.0-rc.1', 'v1.10.0', 'v1.2.0', 'v1.0.0', PSEUDO]
    assert latest_of(versions) == 'v2.0.0+incompatible'
    assert latest_of(['v1.1.0-rc.1', PSEUDO]) == 'v1.1.0-rc.1'
    assert latest_of([]) is None
    assert is_prerelease('go', PSEUDO) and not is_prerelease('go', 'v2.0.0+incompatible')
    assert sort_key('go', 'v0.0.0-20200101000000-aaaaaaaaaaaa') > sort_key('go', PSEUDO)


def test_cleanup_coordinates() -> None:
    coord = coordinates('go', 'github.com/!azure/x/@v/v1.2.0.zip')
    assert (coord.name, coord.version, coord.group, coord.ordered_by_version) == (
        'github.com/Azure/x',
        'v1.2.0',
        'github.com/Azure/x',
        True,
    )
    assert coordinates('go', 'github.com/x/@v/list').ordered_by_version is False


def test_prefix_patterns() -> None:
    patterns = ['example.com/private/*', 'corp.example.com', 'git.example.com/team?/[a-c]x']
    assert matches_prefix_patterns(patterns, 'example.com/private/thing')
    assert matches_prefix_patterns(patterns, 'example.com/private/thing/v2')
    assert not matches_prefix_patterns(patterns, 'example.com/private')
    assert not matches_prefix_patterns(patterns, 'example.com/public/thing')
    assert matches_prefix_patterns(patterns, 'corp.example.com/a/b')
    assert matches_prefix_patterns(patterns, 'git.example.com/team1/bx/y')
    assert not matches_prefix_patterns(patterns, 'git.example.com/team1/dx')
    assert not matches_prefix_patterns(['', '/'], 'example.com/x')
    assert not matches_prefix_patterns(['example.com/[x'], 'example.com/x')


def test_validate_info() -> None:
    validate_info(b'{"Version": "v1.0.0", "Time": "2024-01-02T03:04:05Z"}', 'v1.0.0')
    validate_info(b'{"Version": "v1.0.0"}', None)
    for bad, version in ((b'{"Version": "v1.0.1"}', 'v1.0.0'), (b'[]', None), (b'nope', None), (b'{}', None)):
        with pytest.raises(DigestMismatch):
            validate_info(bad, version)


# ---------------------------------------------------------------------------- descriptor
def test_options() -> None:
    options = GoOptions()
    assert (options.sumdb_proxy, options.sumdb_name, options.sumdb_url) == (
        True,
        'sum.golang.org',
        'https://sum.golang.org',
    )
    assert GoOptions(nosumdb=[' example.com/private/* ']).nosumdb == ['example.com/private/*']
    for bad in ({'nosumdb': ['a b']}, {'nosumdb': ['/x']}, {'sumdb_name': 'Not A Host'}, {'sumdb_url': 'ftp://x'}):
        with pytest.raises(ValidationError):
            GoOptions.model_validate(bad)
    assert DESCRIPTOR.types == frozenset({RepoType.PROXY, RepoType.GROUP})


def test_setup_snippets() -> None:
    repo = make_repo('go-proxy', 'go')
    clients = DESCRIPTOR.setup_snippets(
        SetupContext(repo, 'https://registry.example.com', 'registry.example.com', None)
    )
    text = '\n'.join(s.content for c in clients for s in c.snippets)
    assert 'GOPROXY=https://registry.example.com/repository/go-proxy' in text
    assert 'machine registry.example.com' in text and '{{TOKEN}}' in text
    plain = DESCRIPTOR.setup_snippets(SetupContext(repo, 'http://registry.example.com', 'registry.example.com', None))
    assert any('plain HTTP' in (s.description or '') for c in plain for s in c.snippets)
