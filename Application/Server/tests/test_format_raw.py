"""Raw format plugin: pure helpers (kind, content type, disposition) and request handling that needs no database."""

import pytest
from ws_testing import make_repo

from waystation_common.errors import ValidationFailed
from waystation_server.browse.models import FolderEntry, FolderListing
from waystation_server.formats.base import SetupContext
from waystation_server.formats.raw.descriptor import DESCRIPTOR, RawOptions
from waystation_server.formats.raw.hosted import expected_checksums, stored_type
from waystation_server.formats.raw.listing import merge, render_html, wants_json
from waystation_server.formats.raw.plugin import (
    RawPlugin,
    asset_kind,
    content_disposition,
    safe_type,
    type_by_extension,
    upstream_type,
)
from waystation_server.plugins import default_registry
from waystation_server.proxy.engine import AssetKind
from waystation_server.repos.model import RepoConfig, RepoType


def test_registered_as_the_raw_plugin() -> None:
    plugin = default_registry().plugin('raw')
    assert isinstance(plugin, RawPlugin)


@pytest.mark.parametrize(
    ('regex', 'path', 'kind'),
    [
        (None, 'releases/latest', AssetKind.CONTENT),
        (r'.*/(index\.json|latest)$', 'releases/latest', AssetKind.METADATA),
        (r'.*/(index\.json|latest)$', 'a/b/index.json', AssetKind.METADATA),
        (r'.*/(index\.json|latest)$', 'latest', AssetKind.METADATA),  # searched in '/<path>'
        (r'.*/(index\.json|latest)$', 'a/latest.tar.gz', AssetKind.CONTENT),
        (r'\.json$', 'x/y.json', AssetKind.METADATA),
        (r'^/dists/', 'dists/stable/Release', AssetKind.METADATA),
        (r'^/dists/', 'pool/dists/x', AssetKind.CONTENT),
    ],
)
def test_asset_kind(regex: str | None, path: str, kind: AssetKind) -> None:
    assert asset_kind(path, RawOptions(metadata_path_regex=regex)) is kind


@pytest.mark.parametrize(
    ('path', 'expected'),
    [
        ('a/b.tar.gz', 'application/gzip'),
        ('x.TGZ', 'application/gzip'),
        ('tool.zip', 'application/zip'),
        ('README.txt', 'text/plain'),
        ('index.json', 'application/json'),
        ('pkg.deb', 'application/vnd.debian.binary-package'),
        ('image.iso', 'application/x-iso9660-image'),
        ('install.sh', 'text/x-shellscript'),
        ('page.html', 'text/html'),
        ('README', None),
        ('.hidden', None),
        ('v1.2/latest', None),
        ('file.unknownext', None),
    ],
)
def test_type_by_extension(path: str, expected: str | None) -> None:
    assert type_by_extension(path) == expected


@pytest.mark.parametrize(
    ('header', 'expected'),
    [
        (None, None),
        ('', None),
        ('text/plain', 'text/plain'),
        ('Text/Plain; Charset=UTF-8', 'text/plain; charset=UTF-8'),
        ('application/json; boundary=x', 'application/json'),
        ('text/plain; charset="bad value"', 'text/plain'),
        ('text/html\r\nX-Injected: 1', None),
        ('nonsense', None),
        ('a/b/c', None),
    ],
)
def test_upstream_type(header: str | None, expected: str | None) -> None:
    assert upstream_type(header) == expected


def test_safe_type_neutralises_active_types_only_inline() -> None:
    assert safe_type('text/html', 'attachment') == 'text/html'
    assert safe_type('text/html; charset=utf-8', 'inline') == 'text/plain; charset=utf-8'
    assert safe_type('image/svg+xml', 'inline') == 'text/plain; charset=utf-8'
    assert safe_type('application/gzip', 'inline') == 'application/gzip'


def test_content_disposition() -> None:
    assert content_disposition('a/b/tool.tar.gz', 'attachment') == 'attachment; filename="tool.tar.gz"'
    assert content_disposition('x/na"me\\.txt', 'inline') == (
        'inline; filename="na_me_.txt"; filename*=UTF-8\'\'na%22me%5C.txt'
    )
    value = content_disposition('d/résumé.pdf', 'attachment')
    assert value == 'attachment; filename="r_sum_.pdf"; filename*=UTF-8\'\'r%C3%A9sum%C3%A9.pdf'


def test_expected_checksums() -> None:
    headers = {'x-checksum-sha256': 'A' * 64, 'x-checksum-md5': ' ' + 'b' * 32 + ' '}
    assert expected_checksums(headers) == {'sha256': 'a' * 64, 'md5': 'b' * 32}
    assert expected_checksums({}) == {}
    for bad in ({'x-checksum-sha1': 'a' * 39}, {'x-checksum-sha1': 'g' * 40}, {'x-checksum-md5': 'a' * 64}):
        with pytest.raises(ValidationFailed):
            expected_checksums(bad)


def test_stored_type() -> None:
    assert stored_type('a/b.tar.gz') == 'application/gzip'
    assert stored_type('latest') == 'application/octet-stream'


def _entry(name: str, kind: str = 'file', size: int | None = 1) -> FolderEntry:
    return FolderEntry(name=name, path=f'd/{name}', kind=kind, size=size)  # type: ignore[arg-type]


def test_merge_listings_first_member_wins() -> None:
    one = FolderListing(path='d', entries=[_entry('x', 'folder', None), _entry('b', size=1)], next_cursor='c')
    two = FolderListing(
        path='d',
        entries=[_entry('a', 'folder', None), _entry('x', 'folder', None), _entry('b', size=2)],
        next_cursor=None,
    )
    merged = merge([one, two], 'd')
    assert [(e.kind, e.name, e.size) for e in merged.entries] == [
        ('folder', 'a', None),
        ('folder', 'x', None),
        ('file', 'b', 1),
    ]
    assert merged.next_cursor is None


def test_listing_html_escapes_names() -> None:
    listing = FolderListing(path='d', entries=[_entry('<script>x</script>'), _entry('a b?c#d')], next_cursor='abc')
    page = render_html('files<i>', listing)
    assert '<script>' not in page and '<i>' not in page
    assert 'href="./a%20b%3Fc%23d"' in page
    assert 'href="./%3Cscript%3Ex%3C/script%3E"' in page
    assert '<a href="../">' in page and '?cursor=abc' in page
    assert wants_json('application/json, text/plain') and not wants_json('text/html') and not wants_json(None)


def _guide(repo: RepoConfig) -> dict[str, list[str]]:
    ctx = SetupContext(
        repo=repo, base_url='https://registry.example.com', registry_host='registry.example.com', username=None
    )
    return {c.id: [s.id for s in c.snippets] for c in DESCRIPTOR.setup_snippets(ctx)}


def test_setup_snippets_per_type() -> None:
    assert _guide(make_repo('files')) == {'curl': ['curl-get', 'curl-auth'], 'wget': ['wget-get']}
    hosted = make_repo('files', type=RepoType.HOSTED, proxy=None, hosted={})
    assert _guide(hosted)['curl'] == [
        'curl-get',
        'curl-auth',
        'curl-put',
        'curl-put-checksum',
        'curl-delete',
        'curl-list',
    ]
    group = make_repo('all', type=RepoType.GROUP, proxy=None, group={'members': ['a', 'b'], 'writable_member': 'a'})
    assert _guide(group)['curl'] == ['curl-get', 'curl-auth', 'curl-put', 'curl-put-checksum']
    read_only = make_repo('all', type=RepoType.GROUP, proxy=None, group={'members': ['a', 'b']})
    assert _guide(read_only)['curl'] == ['curl-get', 'curl-auth']


def test_metadata_regex_only_for_proxies() -> None:
    options = RawOptions(metadata_path_regex='^/x')
    assert DESCRIPTOR.check_options(options, RepoType.PROXY) == []
    assert [f for f, _ in DESCRIPTOR.check_options(options, RepoType.HOSTED)] == ['metadata_path_regex']
    assert DESCRIPTOR.check_options(RawOptions(), RepoType.GROUP) == []
