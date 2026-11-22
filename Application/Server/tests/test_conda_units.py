"""Conda plugin units: routes, prefetch items, the package reader and "Set me up" (T-P6-CONDA)."""

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ws_conda import conda_package, index_json, tar_bz2_package

from waystation_common.errors import ValidationFailed
from waystation_server.formats.base import SetupContext
from waystation_server.formats.conda import package, prefetch
from waystation_server.formats.conda.common import Op, parse_route, repodata_type
from waystation_server.formats.conda.descriptor import DESCRIPTOR
from waystation_server.repos.model import RepoConfig, RepoType


def test_routes() -> None:
    assert parse_route('channeldata.json') is not None
    assert parse_route('channeldata.json').op is Op.CHANNELDATA  # type: ignore[union-attr]
    for name in ('repodata.json', 'repodata.json.zst', 'current_repodata.json', 'repodata_from_packages.json.bz2'):
        route = parse_route(f'linux-64/{name}')
        assert route is not None and route.op is Op.REPODATA and route.subdir == 'linux-64' and route.name == name
    for name in ('repodata.jlap', 'repodata_shards.msgpack.zst', 'shards/abc.msgpack.zst', 'index.html'):
        route = parse_route(f'noarch/{name}')
        assert route is not None and route.op is Op.UNSUPPORTED
    route = parse_route('noarch/tzdata-2024b-hc8b5060_0.conda')
    assert route is not None and route.op is Op.PACKAGE and route.package is not None
    assert route.package.name == 'tzdata'
    route = parse_route('_ws/files/noarch/tzdata/-/-')
    assert route is not None and route.op is Op.RESOLVE and route.version is None and route.build is None
    route = parse_route('_ws/files/linux-64/numpy/2.1.3/py312_0')
    assert route is not None and (route.version, route.build) == ('2.1.3', 'py312_0')
    for bad in ('', 'noarch', 'noarch/', 'Linux/repodata.json', 'noarch/other.json', 'a/b/c.conda', '_ws/files/x'):
        assert parse_route(bad) is None, bad
    assert parse_route('_ws/files/noarch/Bad/-/-') is None
    assert repodata_type('repodata.json.zst') == 'application/zstd'
    assert repodata_type('repodata.json.bz2') == 'application/x-bzip2'


def test_prefetch_items() -> None:
    assert prefetch.parse_item('noarch/tzdata') == ('noarch', 'tzdata', None, None)
    assert prefetch.parse_item(' linux-64/numpy==2.1.3 ') == ('linux-64', 'numpy', '2.1.3', None)
    assert prefetch.parse_item('linux-64/numpy==2.1.3=py312_0') == ('linux-64', 'numpy', '2.1.3', 'py312_0')
    assert prefetch.check_item('noarch/tzdata==2024b') is None
    for bad in ('tzdata', 'noarch/', 'noarch/Tz', 'noarch/tz==1 0', 'noarch/tz==1=b@d', 'x y/tz'):
        assert prefetch.check_item(bad) is not None, bad
    assert prefetch.refresh_request('channeldata.json') is not None
    assert prefetch.refresh_request('noarch/repodata.json') is None
    assert DESCRIPTOR.warm_item is not None and DESCRIPTOR.types == frozenset({RepoType.PROXY, RepoType.HOSTED})


def _write(tmp_path: Path, data: bytes, name: str = 'pkg') -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_read_conda_and_tar_bz2(tmp_path: Path) -> None:
    index = index_json('demo-pkg', '1.0', 'py_0', features='', extra_field='dropped', track_features='x')
    meta = package.read_package(_write(tmp_path, conda_package(index)), '.conda')
    assert (meta.name, meta.version, meta.build, meta.subdir, meta.build_number) == (
        'demo-pkg',
        '1.0',
        'py_0',
        'noarch',
        0,
    )
    assert meta.entry['depends'] == ['python >=3.9'] and meta.entry['noarch'] == 'python'
    assert 'extra_field' not in meta.entry and meta.entry['timestamp'] == 1_700_000_000_000
    old = package.read_package(_write(tmp_path, tar_bz2_package(index_json('a', '2', '0', 'linux-64'))), '.tar.bz2')
    assert old.subdir == 'linux-64' and 'noarch' not in old.entry
    legacy = package.parse_index(json.dumps(index_json('a', '1', '0', 'noarch', noarch=True)).encode())
    assert legacy.entry['noarch'] == 'generic'


def _bad(**changes: Any) -> bytes:
    return json.dumps({**index_json('a', '1', '0'), **changes}).encode()


@pytest.mark.parametrize(
    'raw',
    [
        b'not json',
        b'[]',
        _bad(name='A'),
        _bad(version='1 0'),
        _bad(build='b/d'),
        _bad(subdir='../x'),
        _bad(build_number=-1),
        _bad(build_number=True),
        _bad(depends='python'),
        _bad(depends=[1]),
        _bad(license=['MIT']),
        _bad(noarch='rust'),
        _bad(timestamp='today'),
    ],
)
def test_index_json_is_checked(raw: bytes) -> None:
    with pytest.raises(ValidationFailed):
        package.parse_index(raw)


def test_broken_packages_are_refused(tmp_path: Path) -> None:
    index = index_json('a', '1', '0')
    cases = {
        '.conda': [
            b'not a zip',
            conda_package(index, metadata=b'{"conda_pkg_format_version": 1}'),
            conda_package(index, metadata=b'[]'),
            conda_package(index, members=20),
        ],
        '.tar.bz2': [b'BZh9 not bzip2', tar_bz2_package(index)[:40]],
    }
    no_info = io.BytesIO()
    with zipfile.ZipFile(no_info, 'w') as archive:
        archive.writestr('metadata.json', b'{"conda_pkg_format_version": 2}')
    cases['.conda'].append(no_info.getvalue())
    for suffix, bodies in cases.items():
        for n, body in enumerate(bodies):
            with pytest.raises(ValidationFailed):
                package.read_package(_write(tmp_path, body, f'{n}{suffix}'), suffix)


def test_decompression_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(package, 'MAX_TAR_BYTES', 2048)
    with pytest.raises(ValidationFailed, match='too large'):
        package.read_package(_write(tmp_path, tar_bz2_package(index_json('a', '1', '0'))), '.tar.bz2')
    monkeypatch.setattr(package, 'MAX_MEMBERS', 1)
    monkeypatch.setattr(package, 'MAX_TAR_BYTES', 1 << 30)
    with pytest.raises(ValidationFailed, match='too many'):
        package.read_package(_write(tmp_path, tar_bz2_package(index_json('a', '1', '0'))), '.tar.bz2')


def test_setup_snippets() -> None:
    def ctx(kind: RepoType) -> SetupContext:
        now = datetime.now(UTC)
        raw = {'id': 'r1', 'name': 'conda-forge', 'format': 'conda', 'type': kind, 'created_at': now, 'updated_at': now}
        repo = RepoConfig.model_validate(raw)
        return SetupContext(repo=repo, base_url='https://ws.example.com', registry_host='ws.example.com', username=None)

    proxy = DESCRIPTOR.setup_snippets(ctx(RepoType.PROXY))
    assert [c.id for c in proxy] == ['conda', 'micromamba', 'pixi']
    text = json.dumps([c.model_dump() for c in proxy])
    assert 'https://ws.example.com/repository/conda-forge' in text and 'custom_channels' in text
    hosted = DESCRIPTOR.setup_snippets(ctx(RepoType.HOSTED))
    assert hosted[-1].id == 'conda-upload' and '--upload-file' in hosted[-1].snippets[0].content
