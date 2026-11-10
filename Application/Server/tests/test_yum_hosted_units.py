"""Hosted yum units (T-P5-YUMH): RPM header reading (real and synthetic packages, refusals) and the repodata entries
rendered from it; hosted "Set me up"."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path
from xml.parsers import expat

import pytest
from ws_rpm import INT32, STRING, STRING_ARRAY, build_rpm

from waystation_common.errors import ValidationFailed
from waystation_server.formats.base import SetupContext
from waystation_server.formats.yum import repodata, rpm
from waystation_server.formats.yum.descriptor import DESCRIPTOR
from waystation_server.repos.model import RepoConfig

ROOT = Path(__file__).resolve().parents[3]
ROCKY_RPM = next((ROOT / 'tests/fixtures/upstreams/yum/dl.rockylinux.org').glob('get-rocky-release-core-*.body'))


def write(tmp_path: Path, data: bytes, name: str = 'p.rpm') -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_reads_a_real_distribution_package_like_createrepo() -> None:
    h = rpm.read_header(ROCKY_RPM)
    assert (h.name, h.epoch, h.version, h.release, h.arch) == ('rocky-release-core', '0', '10', '1.el10', 'noarch')
    # The values createrepo wrote into Rocky's own primary.xml for this file.
    assert (h.header_start, h.header_end, h.installed_size, h.archive_size) == (4504, 7305, 3202, 3796)
    assert [d.name for d in h.deps['requires']] == ['rocky-release']
    assert [(d.name, d.flags & 14, d.version) for d in h.deps['provides']][-1] == ('rocky-release-core', 8, '10-1.el10')
    assert h.sourcerpm == 'rocky-release-core-10-1.el10.src.rpm'
    assert h.filename == 'rocky-release-core-10-1.el10.noarch.rpm'
    assert ('/etc/yum.repos.d/Rocky-SIG-Core-Common.repo', '') in h.files


def test_synthetic_package_fields_files_and_dependencies(tmp_path: Path) -> None:
    data = build_rpm(
        'hello', '2.1', '3.el10', 'x86_64', epoch=2, requires=('/bin/sh', 'libc.so.6()(64bit)', '/usr/bin/hello')
    )
    h = rpm.read_header(write(tmp_path, data))
    assert (h.name, h.epoch, h.version, h.release, h.arch) == ('hello', '2', '2.1', '3.el10', 'x86_64')
    assert h.summary == 'A test package'
    assert h.build_time == 1_700_000_000
    # rpmlib() and what the package contains itself (/usr/bin/hello) are not requirements.
    assert [d.name for d in h.deps['requires']] == ['/bin/sh', 'libc.so.6()(64bit)']
    assert dict(h.files) == {'/usr/bin/hello': '', '/etc/hello.conf': '', '/usr/share/hello': 'dir'}
    assert h.changelog == (('Test Packager <packager@example.com> - 1.0-1', 1_700_000_000, '- First build'),)
    assert h.header_start % 8 == 0 and h.header_end > h.header_start


def test_source_packages_old_file_names_and_ghosts(tmp_path: Path) -> None:
    assert rpm.read_header(write(tmp_path, build_rpm('hello', source=True))).arch == 'src'
    h = rpm.read_header(write(tmp_path, build_rpm('hello', extra={1027: (STRING_ARRAY, ['/a', '/b'])})))
    assert [p for p, _ in h.files] == ['/a', '/b']
    ghost = build_rpm('g', files=('/var/log/g.log',), dirs=(), extra={1037: (INT32, [1 << 6])})
    assert rpm.read_header(write(tmp_path, ghost)).files == (('/var/log/g.log', 'ghost'),)


@pytest.mark.parametrize(
    ('data', 'message'),
    [
        (b'not an rpm at all' * 10, 'not an RPM file'),
        (build_rpm('hello')[:100], 'truncated'),
        (build_rpm('hello', digest='0' * 64), 'SHA-256'),
        (build_rpm('bad name'), 'invalid name'),
        (build_rpm('hello', version='1 0'), 'invalid name'),
        (build_rpm('hello', arch='x86-64!'), 'invalid architecture'),
    ],
)
def test_refuses_malformed_packages(tmp_path: Path, data: bytes, message: str) -> None:
    with pytest.raises(ValidationFailed, match=message):
        rpm.read_header(write(tmp_path, data))


def test_refuses_headers_out_of_bounds(tmp_path: Path) -> None:
    good = build_rpm('hello')
    main = good.index(b'\x8e\xad\xe8\x01', 97)
    huge = good[: main + 8] + (300_000).to_bytes(4, 'big') + good[main + 12 :]
    with pytest.raises(ValidationFailed, match='too large'):
        rpm.read_header(write(tmp_path, huge))
    bad_magic = good[:main] + b'XXXX' + good[main + 4 :]
    with pytest.raises(ValidationFailed, match='not an RPM header'):
        rpm.read_header(write(tmp_path, bad_magic))
    # An entry pointing outside the store.
    bad = build_rpm('hello', extra={1020: (STRING, 'x')}, digest='')
    main = bad.index(b'\x8e\xad\xe8\x01', 97)
    count = int.from_bytes(bad[main + 8 : main + 12], 'big')
    entries = bytearray(bad[main + 16 : main + 16 + count * 16])
    for i in range(count):
        if int.from_bytes(entries[i * 16 : i * 16 + 4], 'big') == 1020:
            entries[i * 16 + 8 : i * 16 + 12] = (10**6).to_bytes(4, 'big')
    broken = bad[: main + 16] + bytes(entries) + bad[main + 16 + count * 16 :]
    with pytest.raises(ValidationFailed, match='outside its store'):
        rpm.read_header(write(tmp_path, broken))


def test_wrong_value_types_and_inconsistent_file_lists(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed, match='unexpected type'):
        rpm.read_header(write(tmp_path, build_rpm('hello', extra={1000: (INT32, [1])}, digest='')))
    with pytest.raises(ValidationFailed, match='unexpected type'):
        rpm.read_header(write(tmp_path, build_rpm('hello', extra={1006: (STRING, 'x')}, digest='')))
    with pytest.raises(ValidationFailed, match='inconsistent'):
        rpm.read_header(write(tmp_path, build_rpm('hello', extra={1116: (INT32, [9, 9, 9])}, digest='')))


def test_split_evr() -> None:
    assert rpm.split_evr('2:1.0-3') == ('2', '1.0', '3')
    assert rpm.split_evr('1.0') == ('0', '1.0', '')
    assert rpm.split_evr('x:1.0-1') == ('0', 'x:1.0', '1')


def _well_formed(fragment: str) -> None:
    doc = f'<root xmlns:rpm="urn:rpm">{fragment}</root>'
    expat.ParserCreate().Parse(doc, True)


def test_entries_are_well_formed_xml_and_parse_back(tmp_path: Path) -> None:
    data = build_rpm('hello', summary='Tricky <&> "quotes" \x01 control')
    h = rpm.read_header(write(tmp_path, data))
    e = rpm.entries(h, href='Packages/h/hello-1.0-1.el10.noarch.rpm', sha256='a' * 64, size=len(data), upload_time=5)
    for fragment in (e.primary, e.filelists, e.other):
        _well_formed(fragment)
    assert '<file>/usr/bin/hello</file>' in e.primary and '/usr/share/hello' not in e.primary
    assert '<file type="dir">/usr/share/hello</file>' in e.filelists
    assert 'pre="1"' not in e.primary
    assert '<rpm:entry name="hello" flags="EQ" epoch="0" ver="1.0" rel="1.el10"/>' in e.primary
    primary = (
        '<?xml version="1.0"?><metadata xmlns="http://linux.duke.edu/metadata/common" '
        f'xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="1">{e.primary}</metadata>'
    )
    (pkg,) = repodata.parse_primary(gzip.compress(primary.encode()), '.gz')
    assert (pkg.name, pkg.href, pkg.checksum.digest, pkg.size) == (
        'hello',
        'Packages/h/hello-1.0-1.el10.noarch.rpm',
        'a' * 64,
        len(data),
    )


def test_dependency_xml() -> None:
    assert rpm.Dependency('a').xml() == '<rpm:entry name="a"/>'
    assert rpm.Dependency('a', 12, '1:2.0').xml() == '<rpm:entry name="a" flags="GE" epoch="1" ver="2.0"/>'
    assert rpm.Dependency('a', 1 << 9).xml() == '<rpm:entry name="a" pre="1"/>'
    assert rpm.Dependency('a', 8, '').xml() == '<rpm:entry name="a"/>'


def test_hosted_setup_snippets() -> None:
    now = datetime.now(UTC)
    repo = RepoConfig.model_validate(
        {'_id': 'r1', 'name': 'mine', 'format': 'yum', 'type': 'hosted', 'created_at': now, 'updated_at': now}
    )
    ctx = SetupContext(
        repo=repo, base_url='https://registry.example.com', registry_host='registry.example.com', username='alice'
    )
    (client,) = DESCRIPTOR.setup_snippets(ctx)
    by_id = {s.id: s for s in client.snippets}
    repo_file = by_id['repo'].content
    assert 'baseurl=https://registry.example.com/repository/mine/\n' in repo_file
    assert 'repo_gpgcheck=1' in repo_file and 'gpgcheck=0' in repo_file
    assert 'gpgkey=https://registry.example.com/repository/mine/RPM-GPG-KEY' in repo_file
    assert 'username={{USERNAME}}' in repo_file and 'password={{TOKEN}}' in repo_file
    assert '--upload-file' in by_id['upload'].content
    assert by_id['delete'].content.endswith(
        '-X DELETE https://registry.example.com/repository/mine/mypackage-1.0-1.el10.x86_64.rpm'
    )
