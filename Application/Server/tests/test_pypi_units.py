"""PyPI (T-P5-PYPI) protocol helpers: names, negotiation, page parsing/rendering, the multipart reader, distribution
metadata extraction with its archive guards, and routes."""

import io
import json
import tarfile
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from waystation_common.errors import PayloadTooLarge, UpstreamError
from waystation_server.formats.pypi import simple
from waystation_server.formats.pypi.common import Op, latest_of, parse_route
from waystation_server.formats.pypi.descriptor import simple_root
from waystation_server.formats.pypi.distmeta import DistributionInvalid, read_distribution
from waystation_server.formats.pypi.multipart import MultipartError, MultipartReader, boundary_of, parse_header

SHA = 'a' * 64
META_SHA = 'b' * 64


# ---------------------------------------------------------------------------- names and routes
def test_normalise_and_filenames() -> None:
    assert simple.normalise('Foo.Bar__baz-Qux') == 'foo-bar-baz-qux'
    assert simple.valid_project('a') and simple.valid_project('zope.interface')
    assert not simple.valid_project('-x') and not simple.valid_project('x-') and not simple.valid_project('a b')
    wheel = simple.parse_filename('Typing_Extensions-4.12.2-py3-none-any.whl')
    assert wheel == simple.DistName('typing-extensions', '4.12.2', 'wheel')
    assert simple.parse_filename('python-dateutil-2.8.2.tar.gz') == simple.DistName('python-dateutil', '2.8.2', 'sdist')
    assert simple.parse_filename('x-1.0-1-py3-none-any.whl') == simple.DistName('x', '1.0', 'wheel')
    assert simple.parse_filename('nothing.txt') is None and simple.parse_filename('a-b-c.whl') is None
    assert not simple.valid_filename('../x.whl') and not simple.valid_filename('a/b.whl')


def test_routes() -> None:
    assert parse_route('') == parse_route('legacy') and parse_route('').op is Op.UPLOAD  # type: ignore[union-attr]
    assert parse_route('simple').op is Op.INDEX  # type: ignore[union-attr]
    project = parse_route('simple/Foo_Bar')
    assert project is not None and (project.op, project.project) == (Op.PROJECT, 'Foo_Bar')
    file = parse_route('packages/six/six-1.17.0.tar.gz')
    assert file is not None and (file.op, file.project, file.filename) == (Op.FILE, 'six', 'six-1.17.0.tar.gz')
    meta = parse_route('packages/six/six-1.17.0-py2.py3-none-any.whl.metadata')
    assert meta is not None and meta.op is Op.METADATA and meta.filename.endswith('.whl')
    legacy = parse_route('pypi/six/1.17.0/json')
    assert legacy is not None and (legacy.op, legacy.version) == (Op.LEGACY_JSON, '1.17.0')
    assert parse_route('packages/Six/six-1.0.tar.gz') is None  # files live under the normalised name
    assert parse_route('simple/a/b') is None and parse_route('packages/six') is None and parse_route('other') is None


def test_simple_root() -> None:
    assert simple_root('https://pypi.org') == 'https://pypi.org/simple/'
    assert simple_root('https://pypi.org/') == 'https://pypi.org/simple/'
    assert simple_root('https://example.com/root/pypi/+simple/') == 'https://example.com/root/pypi/+simple/'
    assert simple_root('https://example.com/simple') == 'https://example.com/simple/'


def test_latest_prefers_final_releases() -> None:
    assert latest_of(['1.0', '2.0rc1', '1.10']) == '1.10'
    assert latest_of(['2.0rc1', '2.0b1']) == '2.0rc1'
    assert latest_of([]) is None


# ---------------------------------------------------------------------------- negotiation
@pytest.mark.parametrize(
    ('accept', 'chosen'),
    [
        (None, simple.TEXT_HTML),
        ('*/*', simple.TEXT_HTML),
        ('text/html', simple.TEXT_HTML),
        (f'{simple.SIMPLE_JSON}, {simple.SIMPLE_HTML}; q=0.1, text/html; q=0.01', simple.SIMPLE_JSON),  # pip
        (f'{simple.SIMPLE_JSON}, {simple.SIMPLE_HTML};q=0.2, text/html;q=0.01', simple.SIMPLE_JSON),  # uv
        (simple.SIMPLE_HTML, simple.SIMPLE_HTML),
        ('application/vnd.pypi.simple.latest+json', simple.SIMPLE_JSON),
        ('text/html, application/vnd.pypi.simple.v1+json', simple.SIMPLE_JSON),
        ('text/html, */*;q=0.8', simple.TEXT_HTML),
        ('application/json', None),
        (f'{simple.SIMPLE_JSON};q=0', None),
    ],
)
def test_negotiate(accept: str | None, chosen: str | None) -> None:
    assert simple.negotiate(accept) == chosen


# ---------------------------------------------------------------------------- pages
UPSTREAM_JSON = {
    'meta': {'api-version': '1.1'},
    'name': 'demo',
    'versions': ['1.0', '2.0'],
    'files': [
        {
            'filename': 'demo-1.0.tar.gz',
            'url': 'https://files.example.com/packages/aa/bb/demo-1.0.tar.gz',
            'hashes': {'sha256': SHA, 'blake2b_256': 'c' * 64, 'weird': 'zz'},
            'requires-python': '>=3.8',
            'yanked': 'broken build',
            'size': 10,
            'upload-time': '2026-01-01T00:00:00.000000Z',
        },
        {
            'filename': 'demo-2.0-py3-none-any.whl',
            'url': '../../packages/demo-2.0-py3-none-any.whl',
            'hashes': {'sha256': SHA},
            'core-metadata': {'sha256': META_SHA},
            'yanked': False,
            'size': 20,
            'provenance': 'https://example.com/attestation',
        },
    ],
}


def test_json_page_round_trip_rewrites_urls_and_keeps_details() -> None:
    page = simple.parse_upstream(
        json.dumps(UPSTREAM_JSON).encode(), simple.SIMPLE_JSON, 'https://index.example.com/simple/demo/', 'demo'
    )
    sdist, wheel = page.files
    assert sdist.url == 'https://files.example.com/packages/aa/bb/demo-1.0.tar.gz'
    assert wheel.url == 'https://index.example.com/packages/demo-2.0-py3-none-any.whl'  # relative to the page
    assert sdist.hashes == {'sha256': SHA, 'blake2b_256': 'c' * 64} and sdist.yanked == 'broken build'
    assert wheel.metadata_expected() is not None and wheel.expected() == simple.expected_from({'sha256': SHA}, 20)

    rendered = json.loads(simple.render_json(page, 'demo', internal=True))
    assert rendered['meta'] == {'api-version': '1.1'} and rendered['versions'] == ['1.0', '2.0']
    first = rendered['files'][0]
    assert first['url'] == '../../packages/demo/demo-1.0.tar.gz'
    assert first['requires-python'] == '>=3.8' and first['yanked'] == 'broken build' and first['size'] == 10
    assert first[simple.URL_KEY] == sdist.url
    second = rendered['files'][1]
    assert second['core-metadata'] == {'sha256': META_SHA} == second['data-dist-info-metadata']
    assert 'provenance' not in second
    assert simple.URL_KEY not in json.loads(simple.render_json(page, 'demo'))['files'][0]  # clients never see it

    again = simple.parse_json(simple.render_json(page, 'demo', internal=True), '', internal=True)
    assert [f.url for f in again.files] == [sdist.url, wheel.url]


def test_html_page_parse_and_render() -> None:
    upstream = (
        b'<!DOCTYPE html><html><body><h1>Links for demo</h1>'
        b'<a href="https://files.example.com/p/demo-1.0.tar.gz#sha256=' + SHA.encode() + b'" '
        b'data-requires-python="&gt;=3.8" data-yanked="">demo-1.0.tar.gz</a><br/>'
        b'<a href="demo-2.0-py3-none-any.whl#sha256=' + SHA.encode() + b'" '
        b'data-dist-info-metadata="sha256=' + META_SHA.encode() + b'">demo-2.0-py3-none-any.whl</a>'
        b'</body></html>'
    )
    page = simple.parse_upstream(upstream, 'text/html', 'https://index.example.com/simple/demo/', 'demo')
    sdist, wheel = page.files
    assert sdist.requires_python == '>=3.8' and sdist.yanked is True and sdist.hashes == {'sha256': SHA}
    assert wheel.url == 'https://index.example.com/simple/demo/demo-2.0-py3-none-any.whl'
    assert wheel.core_metadata == {'sha256': META_SHA}
    assert simple.versions_of(page) == ['1.0', '2.0']

    html = simple.render_html(page, 'demo').decode()
    assert f'href="../../packages/demo/demo-1.0.tar.gz#sha256={SHA}"' in html
    assert 'data-requires-python="&gt;=3.8"' in html and 'data-yanked=""' in html
    assert f'data-core-metadata="sha256={META_SHA}"' in html
    assert json.loads(simple.render_json(page, 'demo'))['meta'] == {'api-version': '1.0'}  # no sizes in HTML


def test_pages_refuse_bad_hashes_and_urls() -> None:
    with pytest.raises(UpstreamError):
        simple.parse_html(b'<a href="x-1.0.tar.gz#sha256=nothex">x</a>', 'https://e.example.com/s/x/')
    with pytest.raises(UpstreamError):
        simple.parse_html(b'<a href="file:///etc/passwd">x</a>', 'https://e.example.com/s/x/')
    with pytest.raises(UpstreamError):
        simple.parse_json(b'[1]', 'https://e.example.com/')
    duplicate = simple.parse_html(b'<a href="x-1.0.tar.gz">a</a><a href="x-1.0.tar.gz">b</a>', 'https://e.example.com/')
    assert len(duplicate.files) == 1


def test_index_parse_and_render() -> None:
    html = b'<html><body><a href="/simple/six/">six</a><a href="/simple/Foo-Bar/">Foo_Bar</a></body></html>'
    names = simple.parse_index(html, 'text/html')
    assert names == ['six', 'Foo_Bar']
    rendered = simple.render_index(names + ['foo-bar'], as_json=True)
    assert json.loads(rendered)['projects'] == [{'name': 'Foo_Bar'}, {'name': 'six'}]
    assert b'<a href="foo-bar/">Foo_Bar</a>' in simple.render_index(names, as_json=False)
    as_json = json.dumps({'meta': {'api-version': '1.0'}, 'projects': [{'name': 'six'}]}).encode()
    assert simple.parse_index(as_json, simple.SIMPLE_JSON) == ['six']


def test_legacy_json_rewrite() -> None:
    doc = {
        'info': {'name': 'Demo'},
        'urls': [{'filename': 'demo-1.0.tar.gz', 'url': 'https://files.example.com/x/demo-1.0.tar.gz'}],
        'releases': {'1.0': [{'filename': 'demo-1.0.tar.gz', 'url': 'https://files.example.com/x/demo-1.0.tar.gz'}]},
    }
    out = json.loads(
        simple.rewrite_legacy_json(json.dumps(doc).encode(), 'demo', 'https://r.example.com/repository/p/')
    )
    assert out['urls'][0]['url'] == 'https://r.example.com/repository/p/packages/demo/demo-1.0.tar.gz'
    assert out['releases']['1.0'][0]['url'] == out['urls'][0]['url']
    with pytest.raises(UpstreamError):
        simple.rewrite_legacy_json(json.dumps(doc).encode(), 'other', 'https://r.example.com/repository/p/')


# ---------------------------------------------------------------------------- multipart
def form(boundary: str, parts: list[tuple[str, str | None, bytes]]) -> bytes:
    out = b'preamble\r\n'
    for name, filename, data in parts:
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else '')
        out += f'--{boundary}\r\nContent-Disposition: {disposition}\r\n\r\n'.encode() + data + b'\r\n'
    return out + f'--{boundary}--\r\n'.encode()


async def chunked(data: bytes, size: int) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


@pytest.mark.parametrize('size', [1, 7, 64, 100_000])
async def test_multipart_streams_files_and_collects_fields(size: int) -> None:
    payload = bytes(range(256)) * 50 + b'\r\n--almost'
    body = form('xYz', [(':action', None, b'file_upload'), ('content', 'demo.whl', payload), ('name', None, b'demo')])
    reader = MultipartReader(chunked(body, size), 'xYz')
    fields: dict[str, str] = {}
    got = b''
    async for part in reader.parts():
        if part.filename is None:
            fields[part.name] = await reader.text()
        else:
            assert part.filename == 'demo.whl'
            async for chunk in reader.chunks():
                got += chunk
    assert got == payload and fields == {':action': 'file_upload', 'name': 'demo'}


async def test_multipart_errors() -> None:
    assert boundary_of('multipart/form-data; boundary="a b"') == 'a b'
    assert parse_header('form-data; name="x\\"y"; filename=f.whl') == (
        'form-data',
        {'name': 'x"y', 'filename': 'f.whl'},
    )
    with pytest.raises(MultipartError):
        boundary_of('application/json')
    truncated = form('b', [('content', 'f', b'data')])[:-12]
    with pytest.raises(MultipartError):
        async for _ in MultipartReader(chunked(truncated, 5), 'b').parts():
            pass
    big = form('b', [('long_description', None, b'x' * (4 * 1024 * 1024 + 1))])
    reader = MultipartReader(chunked(big, 65536), 'b')
    with pytest.raises(PayloadTooLarge):
        async for _ in reader.parts():
            await reader.text()


# ---------------------------------------------------------------------------- distribution metadata
METADATA = b'Metadata-Version: 2.1\nName: Demo_Pkg\nVersion: 1.0\nSummary: A demo\nRequires-Python: >=3.8\n\nbody'


def wheel(tmp_path: Path, entries: dict[str, bytes]) -> Path:
    path = tmp_path / 'demo_pkg-1.0-py3-none-any.whl'
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def sdist(tmp_path: Path, entries: dict[str, bytes], *, symlink: tuple[str, str] | None = None) -> Path:
    path = tmp_path / 'demo_pkg-1.0.tar.gz'
    with tarfile.open(path, 'w:gz') as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(symlink[0])
            info.type, info.linkname = tarfile.SYMTYPE, symlink[1]
            archive.addfile(info)
    return path


def test_read_wheel_and_sdist(tmp_path: Path) -> None:
    meta = read_distribution(
        wheel(tmp_path, {'demo_pkg-1.0.dist-info/METADATA': METADATA, 'demo/__init__.py': b''}), 'x.whl'
    )
    assert (meta.name, meta.version, meta.summary, meta.requires_python) == ('Demo_Pkg', '1.0', 'A demo', '>=3.8')
    assert meta.raw == METADATA
    tar = sdist(tmp_path, {'demo_pkg-1.0/PKG-INFO': METADATA, 'demo_pkg-1.0/setup.py': b''})
    assert read_distribution(tar, 'demo_pkg-1.0.tar.gz').name == 'Demo_Pkg'


def test_archive_guards(tmp_path: Path) -> None:
    with pytest.raises(DistributionInvalid):  # zip slip
        read_distribution(wheel(tmp_path, {'demo_pkg-1.0.dist-info/METADATA': METADATA, '../evil': b''}), 'x.whl')
    with pytest.raises(DistributionInvalid):  # no metadata
        read_distribution(wheel(tmp_path, {'demo/__init__.py': b''}), 'x.whl')
    with pytest.raises(DistributionInvalid):  # tar slip through a symlink
        read_distribution(sdist(tmp_path, {'d-1.0/PKG-INFO': METADATA}, symlink=('d-1.0/x', '../../etc')), 'd.tar.gz')
    with pytest.raises(DistributionInvalid):
        read_distribution(sdist(tmp_path, {'/abs/PKG-INFO': METADATA}), 'd.tar.gz')
    (tmp_path / 'junk.whl').write_bytes(b'not a zip')
    with pytest.raises(DistributionInvalid):
        read_distribution(tmp_path / 'junk.whl', 'junk.whl')
    with pytest.raises(DistributionInvalid):  # no Name
        read_distribution(wheel(tmp_path, {'d-1.0.dist-info/METADATA': b'Version: 1\n'}), 'x.whl')
    with pytest.raises(DistributionInvalid):
        read_distribution(tmp_path / 'junk.whl', 'x.egg')
