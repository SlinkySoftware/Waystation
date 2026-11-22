"""Hostile-input corpora for every archive reader (T-R01; ARCHITECTURE §7 "Input hardening").

Every reader of an uploaded or proxied archive gets the same hostile corpus, built in memory by ``hostile``:
extension-header bombs (pax and GNU long names), chained extension headers, sparse maps, decompression bombs with
honest and lying sizes, absurd entry counts, zip/tar-slip names, links to ``/etc/passwd``, encrypted and duplicate
entries, oversized metadata, and truncated or corrupted copies of a valid archive. A reader must refuse with its own
clean error (never another exception, which would be a 500 or a failed job), and its memory must stay bounded by its
limits whatever the archive expands to. Limits are lowered with ``monkeypatch`` so the corpus stays small and fast.

The documents inside archives get theirs too: YAML merge and alias bombs, XML entity expansion, deeply nested JSON,
and RPM headers that expand on reading.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import tarfile
import time
import tracemalloc
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import hostile
import pytest
import ws_apk
import ws_conda
import ws_deb
import ws_gems
import ws_rpm

from waystation_common import apk
from waystation_common import galaxy as galaxy_names
from waystation_common.apt import deb822
from waystation_common.archives import MAX_EXTENSION_CHAIN, MAX_EXTENSION_HEADER, BoundedTarInfo
from waystation_common.conda import repodata as conda_repodata
from waystation_common.errors import UpstreamError, ValidationFailed
from waystation_server.formats.apk import common as apk_common
from waystation_server.formats.apt import deb
from waystation_server.formats.cargo import publish as cargo
from waystation_server.formats.composer import archive as composer
from waystation_server.formats.conda import package as conda
from waystation_server.formats.go import gosum
from waystation_server.formats.helm import chart as helm
from waystation_server.formats.helm import index as helm_index
from waystation_server.formats.maven import metadata as maven_metadata
from waystation_server.formats.npm import publish as npm
from waystation_server.formats.nuget import package as nuget
from waystation_server.formats.pypi import distmeta
from waystation_server.formats.rubygems import package as gems
from waystation_server.formats.rubygems import plugin as gems_plugin
from waystation_server.formats.swift import archive as swift
from waystation_server.formats.terraform import modules as terraform
from waystation_server.formats.terraform import ziphash
from waystation_server.formats.yum import repodata as yum_repodata
from waystation_server.formats.yum import rpm
from waystation_worker import galaxy

MiB = 1024 * 1024
BOMB = 16 * MiB  # what a bomb expands to: far beyond every (lowered) limit below
PEAK = 4 * MiB  # the most a reader may allocate while refusing one
SLOW = 5.0  # seconds: anything exponential takes far longer
Limit = tuple[ModuleType, str]


def _file(tmp_path: Path, data: bytes, name: str = 'upload') -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def measured(call: Callable[[], object]) -> tuple[BaseException | None, int]:
    """The exception ``call`` raised (or None) and the peak of Python allocations while it ran."""
    tracemalloc.start()
    error: BaseException | None = None
    try:
        call()
    except BaseException as exc:
        error = exc
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    return error, peak


# ---------------------------------------------------------------------------- the readers
NUSPEC = (
    b'<?xml version="1.0"?><package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd"><metadata>'
    b'<id>Demo</id><version>1.0.0</version><authors>a</authors><description>d</description></metadata></package>'
)
CARGO_TOML = b'[package]\nname = "foo"\nversion = "1.0.0"\n'
CHART_YAML = b'apiVersion: v2\nname: demo\nversion: 1.0.0\ndescription: d\n'
PACKAGE_JSON = b'{"name": "demo", "version": "1.0.0"}'
PKG_INFO = b'Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n\nA description.\n'
PACKAGE_SWIFT = b'// swift-tools-version:5.9\nimport PackageDescription\n'
INDEX_JSON = json.dumps(ws_conda.index_json('demo', '1.0', 'py_0')).encode()
CONDA_STEM = 'demo-1.0-py_0'
GALAXY = galaxy_names.Artifact('harness', 'demo', '1.0.0')


def _galaxy_members() -> list[tuple[str, bytes]]:
    readme = b'# demo\n'
    files = {
        'files': [
            {'name': '.', 'ftype': 'dir', 'chksum_type': None, 'chksum_sha256': None, 'format': 1},
            {
                'name': 'README.md',
                'ftype': 'file',
                'chksum_type': 'sha256',
                'chksum_sha256': hashlib.sha256(readme).hexdigest(),
                'format': 1,
            },
        ],
        'format': 1,
    }
    raw_files = json.dumps(files).encode()
    manifest = {
        'collection_info': {'namespace': 'harness', 'name': 'demo', 'version': '1.0.0'},
        'file_manifest_file': {
            'name': 'FILES.json',
            'ftype': 'file',
            'chksum_type': 'sha256',
            'chksum_sha256': hashlib.sha256(raw_files).hexdigest(),
            'format': 1,
        },
        'format': 1,
    }
    return [('MANIFEST.json', json.dumps(manifest).encode()), ('FILES.json', raw_files), ('README.md', readme)]


def _gem_members() -> list[tuple[str, bytes]]:
    metadata = gzip.compress(ws_gems.spec_yaml('demo', '1.0.0').encode(), mtime=0)
    data = gzip.compress(hostile.tar([('lib/demo.rb', b'1')], ''), mtime=0)
    return [('metadata.gz', metadata), ('data.tar.gz', data)]


def _deb(control_tar: bytes) -> bytes:
    data = hostile.tar([('./usr/share/demo/hello.txt', b'hello')], 'gz')
    members = [('debian-binary', b'2.0\n'), ('control.tar.gz', control_tar), ('data.tar.gz', data)]
    return b'!<arch>\n' + b''.join(ws_deb.ar_member(n, d) for n, d in members)


def _conda_v2(info_tar_zst: bytes, metadata: bytes = b'{"conda_pkg_format_version": 2}') -> bytes:
    return hostile.zip_bytes(
        [
            ('metadata.json', metadata),
            (
                f'pkg-{CONDA_STEM}.tar.zst',
                hostile.compress(hostile.tar([('site-packages/demo.py', b'X = 1\n')], ''), 'zst'),
            ),
            (f'info-{CONDA_STEM}.tar.zst', info_tar_zst),
        ],
        method=zipfile.ZIP_STORED,
    )


@dataclass(frozen=True)
class TarReader:
    """A reader of a tar (possibly compressed, possibly inside another container)."""

    id: str
    read: Callable[[bytes, Path], object]
    refused: type[BaseException]
    members: Callable[[], list[tuple[str, bytes]]]  # a valid archive; the first member is the metadata
    pack: Callable[[bytes], bytes]  # an uncompressed tar -> what the reader is given
    top: str = ''  # the folder every member is under
    entries: Limit | None = None
    unpacked: Limit | None = None
    meta_limit: Limit | None = None
    checks_names: bool = True
    backslash: bool = True  # a backslash in a name is refused
    refuses_links: bool = True  # a link leaving the archive is refused anywhere (not only dropped or ignored)
    peak: int = PEAK  # the reader's documented in-memory bound
    entry_name: Callable[[int], str] | None = None

    def name(self, i: int) -> str:
        return self.entry_name(i) if self.entry_name else f'{self.top}f{i}'

    def build(self, members: list[tuple[str, bytes]] | None = None) -> bytes:
        return self.pack(hostile.tar(members if members is not None else self.members(), ''))

    def __str__(self) -> str:
        return self.id


def gz(raw: bytes) -> bytes:
    return hostile.compress(raw, 'gz')


TAR_READERS = [
    TarReader(
        'cargo',
        lambda data, tmp: cargo.inspect_crate(_file(tmp, data), 'foo', '1.0.0'),
        cargo.Invalid,
        lambda: [('foo-1.0.0/Cargo.toml', CARGO_TOML), ('foo-1.0.0/src/lib.rs', b'')],
        gz,
        top='foo-1.0.0/',
        entries=(cargo, 'MAX_ENTRIES'),
        unpacked=(cargo, 'MAX_UNPACKED'),
        meta_limit=(cargo, 'MAX_MANIFEST'),
    ),
    TarReader(
        'helm',
        lambda data, tmp: helm.read_chart(_file(tmp, data)),
        ValidationFailed,
        lambda: [('demo/Chart.yaml', CHART_YAML), ('demo/values.yaml', b'a: 1\n')],
        gz,
        top='demo/',
        entries=(helm, 'MAX_MEMBERS'),
        unpacked=(helm, 'MAX_UNPACKED'),
        meta_limit=(helm, 'MAX_CHART_YAML'),
    ),
    TarReader(
        'npm',
        lambda data, tmp: npm.inspect_tarball(data),
        ValidationFailed,
        lambda: [('package/package.json', PACKAGE_JSON), ('package/index.js', b'')],
        gz,
        top='package/',
        entries=(npm, 'MAX_MEMBERS'),
        unpacked=(npm, 'MAX_UNPACKED_BYTES'),
        meta_limit=(npm, 'MAX_PACKAGE_JSON_BYTES'),
        refuses_links=False,  # links are not files: never read, and npm does not extract them
    ),
    TarReader(
        'composer-tar',
        lambda data, tmp: composer.check_tar(io.BytesIO(data)),
        ValidationFailed,
        lambda: [('pkg/composer.json', b'{}'), ('pkg/src/a.php', b'<?php')],
        gz,
        top='pkg/',
        entries=(composer, 'MAX_ENTRIES'),
        unpacked=(composer, 'MAX_UNPACKED'),
    ),
    TarReader(
        'conda-tar-bz2',
        lambda data, tmp: conda.read_package(_file(tmp, data), '.tar.bz2'),
        ValidationFailed,
        lambda: [('info/index.json', INDEX_JSON), ('site-packages/demo.py', b'')],
        lambda raw: hostile.compress(raw, 'bz2'),
        entries=(conda, 'MAX_MEMBERS'),
        unpacked=(conda, 'MAX_TAR_BYTES'),
        meta_limit=(conda, 'MAX_INDEX_JSON'),
        checks_names=False,  # nothing is extracted or served by name: only info/index.json is read
        refuses_links=False,
    ),
    TarReader(
        'conda-v2',
        lambda data, tmp: conda.read_package(_file(tmp, data), '.conda'),
        ValidationFailed,
        lambda: [('info/index.json', INDEX_JSON), ('info/about.json', b'{}')],
        lambda raw: _conda_v2(hostile.compress(raw, 'zst')),
        entries=(conda, 'MAX_MEMBERS'),
        unpacked=(conda, 'MAX_INFO_BYTES'),
        meta_limit=(conda, 'MAX_INDEX_JSON'),
        checks_names=False,
        refuses_links=False,
    ),
    TarReader(
        'pypi-sdist',
        lambda data, tmp: distmeta.read_distribution(_file(tmp, data), 'demo-1.0.tar.gz'),
        ValidationFailed,
        lambda: [('demo-1.0/PKG-INFO', PKG_INFO), ('demo-1.0/setup.py', b'')],
        gz,
        top='demo-1.0/',
        entries=(distmeta, 'MAX_ENTRIES'),
        unpacked=(distmeta, 'MAX_UNPACKED'),
        meta_limit=(distmeta, 'MAX_METADATA'),
    ),
    TarReader(
        'terraform-module',
        lambda data, tmp: terraform.check_module_archive(_file(tmp, data)),
        ValidationFailed,
        lambda: [('main.tf', b'variable "x" {}\n'), ('modules/a.tf', b'')],
        gz,
        entries=(terraform, 'MAX_MEMBERS'),
        unpacked=(terraform, 'MAX_UNPACKED'),
    ),
    TarReader(
        'swift-tarball',
        lambda data, tmp: swift.build_from_tarball(_file(tmp, data), tmp / 'out.zip', 'demo', tmp),
        swift.ArchiveRefused,
        lambda: [('demo-1/Package.swift', PACKAGE_SWIFT), ('demo-1/Sources/a.swift', b'')],
        gz,
        top='demo-1/',
        entries=(swift, 'MAX_ENTRIES'),
        unpacked=(swift, 'MAX_UNPACKED'),
        meta_limit=(swift, 'MAX_MANIFEST'),
        refuses_links=False,  # links leaving the package are dropped from the built zip (see below)
    ),
    TarReader(
        'gem',
        lambda data, tmp: gems.read_gem(io.BytesIO(data)),
        ValidationFailed,
        _gem_members,
        lambda raw: raw,
        entries=(gems, 'MAX_MEMBERS'),
        meta_limit=(gems, 'MAX_METADATA_GZ'),
        entry_name=lambda i: f'f{i}.sig',
    ),
    TarReader(
        'deb-control',
        lambda data, tmp: deb.read_deb(_file(tmp, data), sha256=hashlib.sha256(data).hexdigest()),
        ValidationFailed,
        lambda: [('./control', ws_deb.control_text('demo', '1.0', 'all').encode()), ('./md5sums', b'')],
        lambda raw: _deb(gz(raw)),
        top='./',
        entries=(deb, 'MAX_TAR_MEMBERS'),
        meta_limit=(deb, 'CONTROL_MAX'),
        backslash=False,  # a Debian path may hold one: dpkg reads names as bytes
        peak=3 * deb.CONTROL_TAR_MAX,  # the control archive is decompressed in memory, up to its limit
        refuses_links=False,  # the control archive is read, never extracted by Waystation
    ),
    TarReader(
        'galaxy',
        lambda data, tmp: galaxy.read_collection(_file(tmp, data), GALAXY),
        galaxy.ImportFailed,
        _galaxy_members,
        gz,
        entries=(galaxy, 'MAX_MEMBERS'),
        unpacked=(galaxy, 'MAX_UNPACKED'),
        meta_limit=(galaxy, 'MAX_MANIFEST'),
    ),
]


def _refused(reader: TarReader | ZipReader, data: bytes, tmp_path: Path) -> BaseException:
    with pytest.raises(reader.refused) as caught:
        reader.read(data, tmp_path)
    return caught.value


# ---------------------------------------------------------------------------- tar corpus
def _members(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz', tarinfo=BoundedTarInfo) as archive:
        return [m.name for m in archive]


def test_bounded_tar_info_keeps_ordinary_extension_headers() -> None:
    long_name = 'pkg/' + 'n' * 300  # written with a GNU long name / a pax path header
    for fmt in (tarfile.GNU_FORMAT, tarfile.PAX_FORMAT):
        assert _members(hostile.tar([(long_name, b'x')], fmt=fmt)) == [long_name]
    assert _members(hostile.longname_chain(MAX_EXTENSION_CHAIN)) == ['name']


@pytest.mark.parametrize(
    'data',
    [
        hostile.longname_chain(MAX_EXTENSION_CHAIN + 1),
        hostile.longname_chain(MAX_EXTENSION_CHAIN + 1, pax=True),
        hostile.pax_bomb(MAX_EXTENSION_HEADER + 1),
        hostile.pax_bomb(MAX_EXTENSION_HEADER + 1, gnu_longname=True),
        hostile.sparse_bomb(2048),
    ],
    ids=['gnu-chain', 'pax-chain', 'pax-bomb', 'gnu-bomb', 'sparse'],
)
def test_bounded_tar_info_refuses_hostile_extension_headers(data: bytes) -> None:
    with pytest.raises(tarfile.ReadError):
        _members(data)


@pytest.mark.parametrize('reader', TAR_READERS, ids=str)
def test_tar_valid_archive_is_read(reader: TarReader, tmp_path: Path) -> None:
    reader.read(reader.build(), tmp_path)


@pytest.mark.parametrize('reader', TAR_READERS, ids=str)
@pytest.mark.parametrize('gnu', [False, True], ids=['pax', 'gnu-longname'])
def test_tar_extension_header_bomb_is_refused_in_bounded_memory(reader: TarReader, gnu: bool, tmp_path: Path) -> None:
    """``tarfile`` read a pax or GNU long-name header whole: 16 MiB held for a 16 KiB download (regression)."""
    data = reader.pack(hostile.pax_bomb(BOMB, '', gnu_longname=gnu))
    error, peak = measured(lambda: reader.read(data, tmp_path))
    assert isinstance(error, reader.refused), repr(error)
    assert peak < reader.peak


@pytest.mark.parametrize('reader', TAR_READERS, ids=str)
@pytest.mark.parametrize('pax', [False, True], ids=['gnu', 'pax'])
def test_tar_chained_extension_headers_are_refused(reader: TarReader, pax: bool, tmp_path: Path) -> None:
    """``tarfile`` recursed once per chained header: RecursionError, a 500 (regression)."""
    _refused(reader, reader.pack(hostile.longname_chain(3000, '', pax=pax)), tmp_path)


@pytest.mark.parametrize('reader', TAR_READERS, ids=str)
def test_tar_sparse_map_bomb_is_refused(reader: TarReader, tmp_path: Path) -> None:
    """``tarfile`` kept every GNU sparse map entry and raised IndexError at a truncated map (regression)."""
    data = reader.pack(hostile.sparse_bomb(4096, ''))
    error, peak = measured(lambda: reader.read(data, tmp_path))
    assert isinstance(error, reader.refused), repr(error)
    assert peak < reader.peak


@pytest.mark.parametrize('reader', TAR_READERS, ids=str)
def test_tar_damaged_copies_are_refused_cleanly(reader: TarReader, tmp_path: Path) -> None:
    for damaged in hostile.mutations(reader.build(), flips=60, cuts=24):
        with contextlib.suppress(reader.refused):
            reader.read(damaged, tmp_path)


@pytest.mark.parametrize('reader', [r for r in TAR_READERS if r.entries], ids=str)
def test_tar_too_many_entries_are_refused(reader: TarReader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert reader.entries
    monkeypatch.setattr(*reader.entries, 10)
    members = [(reader.name(i), b'') for i in range(11)]
    _refused(reader, reader.build(members + reader.members()), tmp_path)


@pytest.mark.parametrize('reader', [r for r in TAR_READERS if r.unpacked], ids=str)
def test_tar_decompression_bomb_stops_at_the_limit(
    reader: TarReader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert reader.unpacked
    monkeypatch.setattr(*reader.unpacked, MiB)
    data = reader.pack(hostile.tar([(reader.name(0), bytes(BOMB)), *reader.members()], ''))
    error, peak = measured(lambda: reader.read(data, tmp_path))
    assert isinstance(error, reader.refused), repr(error)
    assert peak < reader.peak


@pytest.mark.parametrize('reader', [r for r in TAR_READERS if r.meta_limit], ids=str)
def test_tar_oversized_metadata_is_refused(reader: TarReader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert reader.meta_limit
    members = reader.members()
    _, data = members[0]
    monkeypatch.setattr(*reader.meta_limit, len(data) - 1)
    _refused(reader, reader.build(members), tmp_path)


SLIPS = ['../evil', '{top}../../evil', '/etc/passwd', '{top}a/../../../evil']


@pytest.mark.parametrize('reader', [r for r in TAR_READERS if r.checks_names], ids=str)
@pytest.mark.parametrize('slip', [*SLIPS, '{top}a\\..\\..\\evil'])
def test_tar_slip_names_are_refused(reader: TarReader, slip: str, tmp_path: Path) -> None:
    if '\\' in slip and not reader.backslash:
        pytest.skip('a backslash is an ordinary character for this format')
    name = slip.format(top=reader.top)
    raw = hostile.raw_tar([*reader.members(), (name, b'x')], '')
    _refused(reader, reader.pack(raw), tmp_path)


@pytest.mark.parametrize('reader', TAR_READERS, ids=str)
@pytest.mark.parametrize('kind', [b'2', b'1'], ids=['symlink', 'hardlink'])
@pytest.mark.parametrize('target', ['/etc/passwd', '../../../../etc/passwd'])
def test_tar_metadata_as_a_link_is_refused(reader: TarReader, kind: bytes, target: str, tmp_path: Path) -> None:
    members: list[hostile.Member] = list(reader.members())
    name, _ = members[0]
    members[0] = hostile.info(name, kind=kind, link=target)
    _refused(reader, reader.pack(hostile.tar(members, '')), tmp_path)


@pytest.mark.parametrize('reader', [r for r in TAR_READERS if r.refuses_links], ids=str)
@pytest.mark.parametrize('kind', [b'2', b'1'], ids=['symlink', 'hardlink'])
def test_tar_links_leaving_the_archive_are_refused(reader: TarReader, kind: bytes, tmp_path: Path) -> None:
    for target in ('/etc/passwd', '../../../../etc/passwd'):
        link = hostile.info(reader.name(0), kind=kind, link=target)
        _refused(reader, reader.pack(hostile.tar([*reader.members(), link], '')), tmp_path)


def test_swift_tarball_drops_links_leaving_the_package(tmp_path: Path) -> None:
    members: list[hostile.Member] = [
        ('demo-1/Package.swift', PACKAGE_SWIFT),
        hostile.info('demo-1/out', kind=b'2', link='../../etc/passwd'),
        hostile.info('demo-1/abs', kind=b'2', link='/etc/passwd'),
        hostile.info('demo-1/hard', kind=b'1', link='/etc/passwd'),
        hostile.info('demo-1/in', kind=b'2', link='Package.swift'),
    ]
    swift.build_from_tarball(_file(tmp_path, hostile.tar(members)), tmp_path / 'out.zip', 'demo', tmp_path)
    with zipfile.ZipFile(tmp_path / 'out.zip') as built:
        assert sorted(built.namelist()) == ['demo/Package.swift', 'demo/in']


def test_swift_links_through_other_links_are_refused(tmp_path: Path) -> None:
    """``d -> .`` then ``l -> d/../x``: lexically inside, one level outside once extracted (``d/..`` is the parent
    of the package). Dropped from a built zip, refused in an uploaded one."""
    members: list[hostile.Member] = [
        ('demo-1/Package.swift', PACKAGE_SWIFT),
        hostile.info('demo-1/d', kind=b'2', link='.'),
        hostile.info('demo-1/l', kind=b'2', link='d/../x'),
        hostile.info('demo-1/m', kind=b'2', link='d/Package.swift'),
    ]
    swift.build_from_tarball(_file(tmp_path, hostile.tar(members)), tmp_path / 'out.zip', 'demo', tmp_path)
    with zipfile.ZipFile(tmp_path / 'out.zip') as built:
        assert sorted(built.namelist()) == ['demo/Package.swift', 'demo/d']
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('demo/Package.swift', PACKAGE_SWIFT)
        for name, target in (('demo/d', '.'), ('demo/l', 'd/../x')):
            link = zipfile.ZipInfo(name)
            link.external_attr = (0o120777 << 16) | 0x20
            archive.writestr(link, target)
    with pytest.raises(swift.ArchiveRefused, match='goes through another'):
        swift.inspect_zip(_file(tmp_path, buffer.getvalue(), 'upload.zip'))


def test_npm_never_reads_through_links(tmp_path: Path) -> None:
    members: list[hostile.Member] = [
        hostile.info('package/package.json', kind=b'2', link='/etc/passwd'),
        ('package/index.js', b''),
    ]
    with pytest.raises(ValidationFailed, match=r'no package/package\.json'):
        npm.inspect_tarball(hostile.tar(members))


def test_galaxy_duplicate_entries_are_refused(tmp_path: Path) -> None:
    members = _galaxy_members()
    with pytest.raises(galaxy.ImportFailed, match='twice'):
        galaxy.read_collection(_file(tmp_path, hostile.tar([*members, ('MANIFEST.json', b'{}')])), GALAXY)


# ---------------------------------------------------------------------------- zip corpus
@dataclass(frozen=True)
class ZipReader:
    id: str
    read: Callable[[bytes, Path], object]
    refused: type[BaseException]
    members: list[tuple[str, bytes]]  # a valid archive; the first member is the metadata
    entries: Limit | None = None
    meta_limit: Limit | None = None  # None: every member is read (hashed), bounded by ``total``
    total: Limit | None = None
    checks_names: bool = True
    refuses_duplicates: bool = True

    def build(self, members: list[tuple[str, bytes]] | None = None) -> bytes:
        return hostile.zip_bytes(members if members is not None else self.members)

    def __str__(self) -> str:
        return self.id


ZIP_READERS = [
    ZipReader(
        'nuget',
        lambda data, tmp: nuget.parse_nuspec(nuget.read_nuspec_from(io.BytesIO(data))),
        ValidationFailed,
        [('Demo.nuspec', NUSPEC), ('lib/net8.0/demo.dll', b'MZ')],
        entries=(nuget, 'MAX_ENTRIES'),
        meta_limit=(nuget, 'MAX_NUSPEC'),
    ),
    ZipReader(
        'composer-zip',
        lambda data, tmp: composer.read_composer_json(io.BytesIO(data)),
        ValidationFailed,
        [('pkg/composer.json', b'{"name": "acme/demo"}'), ('pkg/src/a.php', b'<?php')],
        entries=(composer, 'MAX_ENTRIES'),
        meta_limit=(composer, 'MAX_COMPOSER_JSON'),
    ),
    ZipReader(
        'conda-v2-zip',
        lambda data, tmp: conda.read_package(_file(tmp, data), '.conda'),
        ValidationFailed,
        [
            ('metadata.json', b'{"conda_pkg_format_version": 2}'),
            (f'info-{CONDA_STEM}.tar.zst', hostile.compress(hostile.tar([('info/index.json', INDEX_JSON)], ''), 'zst')),
        ],
        entries=(conda, 'MAX_ZIP_MEMBERS'),
        meta_limit=(conda, 'MAX_METADATA_JSON'),
        checks_names=False,  # only the two fixed members are read
        refuses_duplicates=False,
    ),
    ZipReader(
        'pypi-wheel',
        lambda data, tmp: distmeta.read_distribution(_file(tmp, data), 'demo-1.0-py3-none-any.whl'),
        ValidationFailed,
        [('demo-1.0.dist-info/METADATA', PKG_INFO), ('demo/__init__.py', b'')],
        entries=(distmeta, 'MAX_ENTRIES'),
        meta_limit=(distmeta, 'MAX_METADATA'),
    ),
    ZipReader(
        'pypi-sdist-zip',
        lambda data, tmp: distmeta.read_distribution(_file(tmp, data), 'demo-1.0.zip'),
        ValidationFailed,
        [('demo-1.0/PKG-INFO', PKG_INFO), ('demo-1.0/setup.py', b'')],
        entries=(distmeta, 'MAX_ENTRIES'),
        meta_limit=(distmeta, 'MAX_METADATA'),
    ),
    ZipReader(
        'swift-zip',
        lambda data, tmp: swift.inspect_zip(_file(tmp, data)),
        swift.ArchiveRefused,
        [('demo/Package.swift', PACKAGE_SWIFT), ('demo/Sources/a.swift', b'')],
        entries=(swift, 'MAX_ENTRIES'),
        meta_limit=(swift, 'MAX_MANIFEST'),
    ),
    ZipReader(
        'go-module-zip',
        lambda data, tmp: gosum.zip_h1(io.BytesIO(data)),
        gosum.DigestMismatch,
        [('example.com/m@v1.0.0/go.mod', b'module example.com/m\n'), ('example.com/m@v1.0.0/m.go', b'package m\n')],
        entries=(gosum, 'MAX_FILES'),
        total=(gosum, 'MAX_UNCOMPRESSED'),
        checks_names=False,  # hashed as stored, then checked against the checksum database
        refuses_duplicates=False,
    ),
    ZipReader(
        'terraform-provider-zip',
        lambda data, tmp: ziphash.zip_h1(io.BytesIO(data)),
        ziphash.NotHashable,
        [('terraform-provider-demo', b'\x7fELF')],
        entries=(ziphash, 'MAX_FILES'),
        total=(ziphash, 'MAX_UNCOMPRESSED'),
        checks_names=False,
        refuses_duplicates=False,
    ),
    ZipReader(
        'terraform-module-zip',
        lambda data, tmp: terraform.archive_top(io.BytesIO(data), 'archive.zip'),
        UpstreamError,
        [('repo-1.0/main.tf', b'variable "x" {}\n')],
        checks_names=False,
        refuses_duplicates=False,
    ),
]


@pytest.mark.parametrize('reader', ZIP_READERS, ids=str)
def test_zip_valid_archive_is_read(reader: ZipReader, tmp_path: Path) -> None:
    reader.read(reader.build(), tmp_path)


@pytest.mark.parametrize('reader', ZIP_READERS, ids=str)
def test_zip_damaged_copies_are_refused_cleanly(reader: ZipReader, tmp_path: Path) -> None:
    """zipfile raises ValueError, NotImplementedError, zlib.error...: each became a 500 somewhere (regression)."""
    for damaged in hostile.mutations(reader.build(), seed=11):
        with contextlib.suppress(reader.refused):
            reader.read(damaged, tmp_path)


def _bombed(reader: ZipReader, declared: int | None) -> bytes:
    name, _ = reader.members[0]
    return hostile.zip_bomb(name, BOMB, reader.members[1:], declared=declared)


@pytest.mark.parametrize('reader', [r for r in ZIP_READERS if r.meta_limit or r.total], ids=str)
@pytest.mark.parametrize('declared', [None, 100], ids=['honest', 'lying'])
def test_zip_bomb_stops_at_the_limit(
    reader: ZipReader, declared: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if reader.total:
        monkeypatch.setattr(*reader.total, MiB)
    data = _bombed(reader, declared)
    error, peak = measured(lambda: reader.read(data, tmp_path))
    assert isinstance(error, reader.refused), repr(error)
    assert peak < PEAK


@pytest.mark.parametrize('reader', [r for r in ZIP_READERS if r.entries], ids=str)
def test_zip_too_many_entries_are_refused(reader: ZipReader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert reader.entries
    monkeypatch.setattr(*reader.entries, 10)
    top = reader.members[0][0].rsplit('/', 1)[0] + '/' if '/' in reader.members[0][0] else ''
    members = [*reader.members, *((f'{top}f{i}', b'') for i in range(11))]
    _refused(reader, hostile.zip_bytes(members, method=zipfile.ZIP_STORED), tmp_path)


def test_zip_with_100k_entries_is_refused_quickly() -> None:
    data = hostile.many_zip_entries(nuget.MAX_ENTRIES + 1)
    started = time.monotonic()
    with pytest.raises(ValidationFailed, match='too many files'):
        nuget.read_nuspec_from(io.BytesIO(data))
    assert time.monotonic() - started < SLOW


@pytest.mark.parametrize('reader', [r for r in ZIP_READERS if r.meta_limit], ids=str)
def test_zip_encrypted_metadata_is_refused(reader: ZipReader, tmp_path: Path) -> None:
    name, _ = reader.members[0]
    _refused(reader, hostile.encrypted(reader.build(), name), tmp_path)


@pytest.mark.parametrize('reader', [r for r in ZIP_READERS if r.checks_names], ids=str)
@pytest.mark.parametrize('name', ['../evil', '/etc/passwd', 'C:/evil', 'a\\..\\..\\evil', 'a/../../evil'])
def test_zip_slip_names_are_refused(reader: ZipReader, name: str, tmp_path: Path) -> None:
    _refused(reader, reader.build([*reader.members, (name, b'x')]), tmp_path)


@pytest.mark.parametrize('reader', [r for r in ZIP_READERS if r.refuses_duplicates], ids=str)
def test_zip_duplicate_metadata_is_refused(reader: ZipReader, tmp_path: Path) -> None:
    """The reader checked one copy and an extracting client keeps the other (composer, pypi sdist: regression)."""
    name, data = reader.members[0]
    with pytest.warns(UserWarning, match='Duplicate name'):
        archive = reader.build([*reader.members, (name, data.replace(b'demo', b'evil'))])
    _refused(reader, archive, tmp_path)


@pytest.mark.parametrize('reader', [r for r in ZIP_READERS if r.meta_limit], ids=str)
def test_zip_oversized_metadata_is_refused(reader: ZipReader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert reader.meta_limit
    monkeypatch.setattr(*reader.meta_limit, len(reader.members[0][1]) - 1)
    _refused(reader, reader.build(), tmp_path)


# ---------------------------------------------------------------------------- documents inside archives
DEEP_JSON = b'[' * 300_000 + b']' * 300_000


def test_deeply_nested_json_is_refused_cleanly(tmp_path: Path) -> None:
    """``json.loads`` raises RecursionError (not ValueError) past its depth limit: a 500 (regression)."""
    with pytest.raises(ValidationFailed, match=r'composer\.json'):
        composer.read_composer_json(io.BytesIO(hostile.zip_bytes([('composer.json', DEEP_JSON)])))
    with pytest.raises(ValidationFailed, match=r'package\.json'):
        npm.inspect_tarball(hostile.tar([('package/package.json', DEEP_JSON)]))
    with pytest.raises(ValidationFailed):
        npm.parse_put(DEEP_JSON, 'demo')
    with pytest.raises(ValidationFailed, match=r'index\.json'):
        conda.parse_index(DEEP_JSON)
    with pytest.raises(cargo.Invalid):
        cargo.parse_metadata(DEEP_JSON)
    members = _galaxy_members()
    members[0] = ('MANIFEST.json', DEEP_JSON)
    with pytest.raises(galaxy.ImportFailed, match=r'MANIFEST\.json'):
        galaxy.read_collection(_file(tmp_path, hostile.tar(members)), GALAXY)


def _quick(call: Callable[[], Any], refused: type[BaseException] | tuple[type[BaseException], ...]) -> None:
    started = time.monotonic()
    error, peak = measured(call)
    assert error is None or isinstance(error, refused), repr(error)
    assert time.monotonic() - started < SLOW
    assert peak < 64 * MiB


def test_yaml_merge_bombs_are_refused(tmp_path: Path) -> None:
    """Explicit ``!!merge`` keys copied the merged mappings: 40 nested levels took 2^40 steps (regression)."""
    doc = hostile.yaml_merge_bomb(40)
    for load in (gems.load_yaml, helm_index.load_yaml, helm_index.load_document):
        with pytest.raises(ValidationFailed, match=r'not (a )?valid YAML'):
            load(doc)
    chart = hostile.tar([('demo/Chart.yaml', CHART_YAML + doc)])
    with pytest.raises(ValidationFailed):
        helm.read_chart(_file(tmp_path, chart))
    gem = hostile.tar([('metadata.gz', gzip.compress(doc)), _gem_members()[1]], '')
    with pytest.raises(ValidationFailed):
        gems.read_gem(io.BytesIO(gem))


def test_yaml_alias_bombs_stay_bounded() -> None:
    doc = hostile.yaml_laughs()
    _quick(lambda: gems.spec_from(gems.load_yaml(doc)), ValidationFailed)
    with pytest.raises(ValidationFailed, match='aliases'):
        helm_index.load_yaml(doc)
    _quick(lambda: gems.load_yaml(hostile.yaml_laughs(levels=40, width=30)), ValidationFailed)


def test_yaml_tags_never_build_objects() -> None:
    for doc in (b'!!python/object/apply:os.system ["true"]', b'--- !ruby/object:Gem::Installer\ni: x\n'):
        _quick(lambda doc=doc: gems.load_yaml(doc), ValidationFailed)
        with pytest.raises(ValidationFailed):
            helm_index.load_yaml(doc)


def test_xml_entity_expansion_is_refused() -> None:
    nuspec = hostile.xml_laughs('package', '<metadata><id>Demo</id><version>1.0.0</version></metadata>')
    with pytest.raises(ValidationFailed, match='not valid XML'):
        nuget.parse_nuspec(nuspec)
    with pytest.raises(ValidationFailed):
        maven_metadata.parse(hostile.xml_laughs('metadata'))
    with pytest.raises(ValidationFailed):
        yum_repodata.parse_repomd(hostile.xml_laughs('repomd'))
    for suffix, kind in (('', ''), ('.gz', 'gz'), ('.xz', 'xz'), ('.zst', 'zst'), ('.bz2', 'bz2')):
        with pytest.raises(ValidationFailed):
            yum_repodata.parse_primary(hostile.compress(hostile.xml_laughs('metadata'), kind), suffix)


def test_rpm_file_list_expansion_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every base name repeats its directory: one 64 KiB directory shared by 2000 names built 128 MiB of paths
    from a 200 KiB header (regression)."""
    long_dir = '/' + 'd' * 65_536 + '/'
    extra: dict[int, ws_rpm.Value] = {
        1116: (ws_rpm.INT32, [0] * 2000),
        1117: (ws_rpm.STRING_ARRAY, [f'f{i}' for i in range(2000)]),
        1118: (ws_rpm.STRING_ARRAY, [long_dir]),
        1030: (ws_rpm.INT16, [0o100644] * 2000),
        1037: (ws_rpm.INT32, [0] * 2000),
    }
    path = _file(tmp_path, ws_rpm.build_rpm('demo', extra=extra))
    error, peak = measured(lambda: rpm.read_header(path))
    assert isinstance(error, ValidationFailed) and 'file list is too large' in str(error), repr(error)
    assert peak < 32 * MiB


def test_rpm_damaged_headers_are_refused_cleanly(tmp_path: Path) -> None:
    for damaged in hostile.mutations(ws_rpm.build_rpm('demo'), flips=200):
        with contextlib.suppress(ValidationFailed):
            rpm.read_header(_file(tmp_path, damaged))


# ---------------------------------------------------------------------------- compressed metadata
STREAMS = [('.gz', 'gz'), ('.xz', 'xz'), ('.bz2', 'bz2'), ('.zst', 'zst')]


@pytest.mark.parametrize(('suffix', 'kind'), STREAMS)
def test_compressed_indexes_stop_at_their_limit(suffix: str, kind: str, tmp_path: Path) -> None:
    data = hostile.bomb(BOMB, kind)
    for call in (
        lambda: deb822.decompress(data, suffix, limit=MiB),
        lambda: list(yum_repodata.decompress_chunks(data, suffix, limit=MiB)),
        lambda: deb.decompress(data, suffix, limit=MiB),
    ):
        error, peak = measured(call)
        assert isinstance(error, ValidationFailed), repr(error)
        assert peak < 8 * MiB


@pytest.mark.parametrize(('suffix', 'kind'), STREAMS)
def test_damaged_compressed_indexes_are_refused_cleanly(suffix: str, kind: str) -> None:
    """deb822 let a zstd error through (a 500 on a proxied Packages.zst; regression)."""
    sample = hostile.compress(b'Package: demo\nVersion: 1\n\n' * 200, kind)
    for damaged in hostile.mutations(sample, cuts=10, flips=40):
        with contextlib.suppress(ValidationFailed):
            deb822.decompress(damaged, suffix)
        with contextlib.suppress(ValidationFailed):
            list(yum_repodata.decompress_chunks(damaged, suffix))
        with contextlib.suppress(ValidationFailed):
            deb.decompress(damaged, suffix)


@pytest.mark.parametrize('compression', ['zst', 'bz2'])
def test_conda_repodata_stops_at_its_limit(compression: str, tmp_path: Path) -> None:
    prefix = b'{"info": {"subdir": "noarch"}, "packages": {"' + b'a' * 10
    path = _file(tmp_path, hostile.bomb(BOMB, compression, prefix), f'repodata.json.{compression}')
    error, peak = measured(
        lambda: list(conda_repodata.read_entries(path, compression, conda_repodata.Scan(), limit=MiB))
    )
    assert isinstance(error, conda_repodata.RepodataError), repr(error)
    assert peak < 8 * MiB


def test_gemspec_bomb_stops_at_its_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gems_plugin, 'MAX_SPEC_INFLATED', MiB)
    data = hostile.bomb(BOMB, 'zlib', b'\x04\x08')
    error, peak = measured(lambda: gems_plugin.check_gemspec(data, None))  # type: ignore[arg-type]
    assert isinstance(error, UpstreamError), repr(error)
    assert peak < 4 * MiB


def test_gem_metadata_bomb_stops_at_its_limit() -> None:
    gem = hostile.tar([('metadata.gz', hostile.bomb(BOMB, 'gz')), _gem_members()[1]], '')
    error, peak = measured(lambda: gems.read_gem(io.BytesIO(gem)))
    assert isinstance(error, ValidationFailed) and 'too large' in str(error), repr(error)
    assert peak < 3 * gems.MAX_METADATA


def test_nested_compression_is_not_unpacked(tmp_path: Path) -> None:
    """An archive inside an archive is an opaque member: nothing recurses into it."""
    inner = hostile.bomb(BOMB, 'gz')
    crate = hostile.tar([('foo-1.0.0/Cargo.toml', CARGO_TOML), ('foo-1.0.0/inner.tar.gz', inner)])
    error, peak = measured(lambda: cargo.inspect_crate(_file(tmp_path, crate), 'foo', '1.0.0'))
    assert error is None and peak < PEAK


# ---------------------------------------------------------------------------- Alpine apk (waystation_common.apk)
def _apk_segment(tar_bytes: bytes) -> bytes:
    return hostile.compress(tar_bytes, 'gz')


def _apk_with_control(control_tar: bytes) -> bytes:
    """A package whose control segment is ``control_tar`` (gzip), followed by a small data segment."""
    return _apk_segment(control_tar) + _apk_segment(hostile.tar([('usr/x', b'x')], ''))


def _read_apk(data: bytes) -> apk.PackageInfo:
    return apk.read_package(io.BytesIO(data), len(data))


def test_apk_valid_package_and_index_are_read() -> None:
    package = ws_apk.build_apk('demo', '1.0-r0')
    assert _read_apk(package).name == 'demo'
    index = apk.parse_index(ws_apk.build_index({'demo-1.0-r0.apk': package}))
    assert [e.name for e in index.entries] == ['demo']


@pytest.mark.parametrize(
    'control',
    [
        hostile.pax_bomb(apk.CONTROL_MAX // 2, ''),
        hostile.pax_bomb(apk.CONTROL_MAX // 2, '', gnu_longname=True),
        hostile.longname_chain(2000, ''),  # RecursionError in tarfile without BoundedTarInfo
        hostile.longname_chain(2000, '', pax=True),
        hostile.sparse_bomb(2048, ''),
    ],
    ids=['pax-bomb', 'gnu-bomb', 'gnu-chain', 'pax-chain', 'sparse'],
)
def test_apk_hostile_control_tar_is_refused(control: bytes) -> None:
    with pytest.raises(apk.ApkError):
        _read_apk(_apk_with_control(control))


@pytest.mark.parametrize('segment', ['first', 'control'])
def test_apk_gzip_segment_bomb_stops_at_the_limit(segment: str) -> None:
    bomb = hostile.bomb(apk.CONTROL_MAX * 8, 'gz')
    data = bomb if segment == 'first' else ws_apk.sign_segment(b'x', ws_apk.make_key(), 'k.rsa.pub') + bomb
    tracemalloc.start()
    try:
        with pytest.raises(apk.ApkError, match=r'too large|not a tar|not valid'):
            _read_apk(data)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < apk.CONTROL_MAX * 3


def test_apk_index_bombs_and_member_counts_are_bounded() -> None:
    with pytest.raises(apk.ApkError, match='too large'):
        apk.decompress_index(hostile.bomb(64 * 1024 * 1024, 'gz'), max_out=1024 * 1024)
    with pytest.raises(apk.ApkError, match='too many gzip members'):
        apk.decompress_index(b''.join(hostile.compress(b'x', 'gz') for _ in range(9)))
    with pytest.raises(apk.ApkError):
        apk.parse_index(hostile.compress(hostile.longname_chain(2000, ''), 'gz'))
    with pytest.raises(apk.ApkError):
        apk.parse_index(hostile.compress(hostile.pax_bomb(4 * 1024 * 1024, ''), 'gz'))


def test_apk_damaged_copies_are_refused_cleanly() -> None:
    package = ws_apk.build_apk('demo', '1.0-r0', key=ws_apk.make_key())
    index = ws_apk.build_index({'demo-1.0-r0.apk': package})
    for damaged in hostile.mutations(package):
        with contextlib.suppress(apk.ApkError):
            _read_apk(damaged)
    for damaged in hostile.mutations(index):
        with contextlib.suppress(apk.ApkError):
            apk.parse_index(damaged)


def test_apk_index_numbers_are_ascii_and_bounded() -> None:
    """``str.isdigit`` accepts ``²`` (``int`` refuses it) and 5000-digit sizes (``int`` refuses past 4300 digits,
    MongoDB past 64 bits): such values are dropped, never a 500."""
    stanza = 'C:Q1' + 'A' * 27 + '=\nP:demo\nV:1.0-r0\nA:x86_64\nS:{size}\nI:{size}\nt:{size}\n\n'
    for size in ('²', '9' * 5000, '1' * 21):
        (entry,) = apk.parse_entries(stanza.format(size=size))
        assert entry.size is None
        details = apk_common.details(entry.fields)
        assert details['installed_size'] is None
        assert details['build_time'] is None


@pytest.mark.parametrize(
    'pkginfo',
    [b'pkgname = demo\n\xff\xfe', b'pkgname = de\x01mo\n', b'no separator\n', b'pkgname = ' + b'x' * 70_000 + b'\n'],
    ids=['not-utf8', 'control-char', 'no-separator', 'long-field'],
)
def test_apk_bad_pkginfo_is_refused(pkginfo: bytes) -> None:
    with pytest.raises(apk.ApkError):
        _read_apk(_apk_with_control(hostile.tar([('.PKGINFO', pkginfo)], '')))
