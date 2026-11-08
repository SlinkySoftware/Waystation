"""npm protocol helpers (T-P2-NPM): routing, packument rewriting, integrity lookup."""

import base64
import hashlib
import json

import pytest

from waystation_common.errors import UpstreamError
from waystation_server.formats.npm.metadata import (
    Op,
    Route,
    find_dist,
    packument_key,
    parse_route,
    rewrite_packument,
    upstream_packument_path,
    valid_name,
)
from waystation_server.formats.npm.plugin import wants_abbreviated

REPO_URL = 'https://registry.example.com/repository/npm/'
TGZ = b'tarball bytes'
SHA512_SRI = 'sha512-' + base64.b64encode(hashlib.sha512(TGZ).digest()).decode()
SHA1 = hashlib.sha1(TGZ, usedforsecurity=False).hexdigest()


@pytest.mark.parametrize(
    ('path', 'route'),
    [
        ('is-number', Route(Op.PACKUMENT, 'is-number')),
        ('JSONStream', Route(Op.PACKUMENT, 'JSONStream')),  # legacy upper-case name
        ('@sindresorhus/is', Route(Op.PACKUMENT, '@sindresorhus/is')),  # @scope%2fis arrives decoded
        ('is-number/-/is-number-7.0.0.tgz', Route(Op.TARBALL, 'is-number', 'is-number-7.0.0.tgz')),
        ('@sindresorhus/is/-/is-4.6.0.tgz', Route(Op.TARBALL, '@sindresorhus/is', 'is-4.6.0.tgz')),
        ('-/ping', Route(Op.PING)),
        ('-/whoami', Route(Op.WHOAMI)),
        ('-/v1/search', Route(Op.SEARCH)),
        ('-/npm/v1/security/advisories/bulk', Route(Op.AUDIT)),
        ('-/npm/v1/security/audits/quick', Route(Op.AUDIT)),
        ('', None),
        ('-', None),
        ('-/other', None),
        ('@scope', None),
        ('is-number/1.0.0', None),  # version documents: not yet
        ('is-number/-/packument.json', None),  # the cache key of a packument is never a request path
        ('is-number/-/is-number-7.0.0.tar', None),
        ('is-number/-/a/b.tgz', None),
        ('@/x', None),
        ('.hidden', None),
        ('a b', None),
        ('x' * 215, None),
    ],
)
def test_parse_route(path: str, route: Route | None) -> None:
    assert parse_route(path) == route


def test_names_and_keys() -> None:
    assert valid_name('@types/node')
    assert not valid_name('@types/')
    assert not valid_name('@/node')
    assert upstream_packument_path('@types/node') == '@types%2Fnode'
    assert upstream_packument_path('is-number') == 'is-number'
    assert packument_key('@types/node', abbreviated=False) == '@types/node/-/packument.json'
    assert packument_key('is-number', abbreviated=True) == 'is-number/-/packument.install-v1.json'
    assert wants_abbreviated('application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8, */*')
    assert not wants_abbreviated('application/json')


def _packument(name: str = '@scope/pkg', **dist: object) -> bytes:
    return json.dumps(
        {
            'name': name,
            'dist-tags': {'latest': '1.0.0'},
            'versions': {
                '1.0.0': {
                    'name': name,
                    'version': '1.0.0',
                    'dist': {'tarball': f'https://registry.npmjs.org/{name}/-/pkg-1.0.0.tgz', **dist},
                },
                '0.9.0': {'name': name, 'version': '0.9.0'},  # no dist: left alone
            },
        }
    ).encode()


def test_rewrite_points_tarballs_at_the_repository() -> None:
    out = json.loads(rewrite_packument(_packument(shasum=SHA1), package='@scope/pkg', repo_url=REPO_URL))
    assert out['versions']['1.0.0']['dist']['tarball'] == f'{REPO_URL}@scope/pkg/-/pkg-1.0.0.tgz'
    assert out['versions']['1.0.0']['dist']['shasum'] == SHA1
    assert out['dist-tags'] == {'latest': '1.0.0'}
    assert 'dist' not in out['versions']['0.9.0']


def test_rewrite_quotes_odd_file_names() -> None:
    raw = json.dumps({'versions': {'1': {'dist': {'tarball': 'https://registry.npmjs.org/a/-/a%20b?.tgz'}}}}).encode()
    out = json.loads(rewrite_packument(raw, package='a', repo_url=REPO_URL))
    assert out['versions']['1']['dist']['tarball'] == f'{REPO_URL}a/-/a%20b'


@pytest.mark.parametrize(
    'raw',
    [
        b'not json',
        b'[]',
        _packument(name='other'),
        json.dumps({'name': 'p', 'versions': []}).encode(),
        json.dumps({'name': 'p', 'versions': {'1': 'x'}}).encode(),
    ],
)
def test_rewrite_refuses_invalid_packuments(raw: bytes) -> None:
    with pytest.raises(UpstreamError):
        rewrite_packument(raw, package='p' if b'"p"' in raw else '@scope/pkg', repo_url=REPO_URL)


def test_unpublished_packument_has_no_versions() -> None:
    raw = json.dumps({'name': 'gone', 'time': {'unpublished': {}}}).encode()
    assert json.loads(rewrite_packument(raw, package='gone', repo_url=REPO_URL)) == json.loads(raw)


def test_find_dist_integrity_and_shasum() -> None:
    found = find_dist(_packument(integrity=SHA512_SRI, shasum=SHA1), 'pkg-1.0.0.tgz')
    assert found.found
    assert found.expected is not None
    assert found.expected.sha512 == hashlib.sha512(TGZ).hexdigest()
    assert found.expected.sha1 == SHA1

    sha1_only = find_dist(_packument(shasum=SHA1.upper()), 'pkg-1.0.0.tgz').expected
    assert sha1_only is not None
    assert sha1_only.digests() == {'sha1': SHA1}

    assert find_dist(_packument(), 'pkg-1.0.0.tgz').expected is None  # no digests published
    assert not find_dist(_packument(), 'pkg-2.0.0.tgz').found
    assert not find_dist(b'{}', 'pkg-1.0.0.tgz').found


@pytest.mark.parametrize('dist', [{'integrity': 'sha512-!!!'}, {'shasum': 'nothex'}])
def test_find_dist_refuses_garbage_digests(dist: dict[str, str]) -> None:
    with pytest.raises(UpstreamError):
        find_dist(_packument(**dist), 'pkg-1.0.0.tgz')


def test_find_dist_refuses_invalid_json() -> None:
    with pytest.raises(UpstreamError):
        find_dist(b'{', 'x.tgz')
