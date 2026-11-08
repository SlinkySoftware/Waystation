"""Raw format plugin: pure helpers (kind, content type, disposition) and request handling that needs no database."""

import httpx
import pytest
from ws_testing import make_repo

from waystation_server.formats.raw.descriptor import RawOptions
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
from waystation_server.repos.model import RepoConfig


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


@pytest.fixture
def unit_repos() -> list[RepoConfig]:
    return [make_repo('files')]


@pytest.mark.parametrize('path', ['/repository/files', '/repository/files/', '/repository/files/dir/'])
async def test_directories_are_not_listed(unit_client: httpx.AsyncClient, path: str) -> None:
    response = await unit_client.get(path)
    assert response.status_code == 404
    assert response.headers['content-type'].startswith('text/plain')
