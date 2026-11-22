"""Hosted apt (T-P5-APTH) without databases: reading uploaded .deb files safely, the Packages paragraph and pool
path, repository options and "Set me up", the worker's Release rendering, and the Nexus mapping."""

from __future__ import annotations

import hashlib
import io
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ws_deb import ar_member, build_deb, control_text, tar_of

from waystation_common.errors import ValidationFailed
from waystation_server.formats.apt import deb
from waystation_server.formats.apt.descriptor import DESCRIPTOR, AptOptions
from waystation_server.formats.base import SetupContext
from waystation_server.migration.nexus_client import NexusRepository
from waystation_server.migration.nexus_mapping import map_repository
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_worker import apt_index as worker_apt


def read(tmp_path: Path, data: bytes) -> deb.Deb:
    path = tmp_path / 'p.deb'
    path.write_bytes(data)
    return deb.read_deb(path, sha256=hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize('compression', ['gz', 'xz', 'zst', 'none'])
def test_reads_packages_with_every_control_compression(tmp_path: Path, compression: str) -> None:
    data = build_deb('hello', '1:2.0-1', 'amd64', compression=compression, Source='hello-src (2.0-1)')
    info = read(tmp_path, data)
    assert (info.package, info.version, info.architecture, info.source, info.section) == (
        'hello',
        '1:2.0-1',
        'amd64',
        'hello-src',
        'misc',
    )
    assert info.size == len(data)
    assert info.md5 == hashlib.md5(data, usedforsecurity=False).hexdigest()
    assert info.sha512 == hashlib.sha512(data).hexdigest()
    assert info.filename == 'hello_2.0-1_amd64.deb'  # no epoch in file names
    assert info.pool_path('main') == 'pool/main/h/hello-src/hello_2.0-1_amd64.deb'


def test_packages_paragraph_and_lib_prefix(tmp_path: Path) -> None:
    info = read(tmp_path, build_deb('libfoo1', '0.9', 'all', Depends='libc6 (>= 2.36)'))
    assert info.pool_path('contrib') == 'pool/contrib/libf/libfoo1/libfoo1_0.9_all.deb'
    stanza = info.stanza('pool/contrib/libf/libfoo1/libfoo1_0.9_all.deb')
    lines = stanza.split('\n')
    assert lines[0] == 'Package: libfoo1'
    assert 'Depends: libc6 (>= 2.36)' in lines
    assert lines.index('Filename: pool/contrib/libf/libfoo1/libfoo1_0.9_all.deb') < lines.index(
        'Description: libfoo1 for tests'
    )
    assert f'SHA256: {info.sha256}' in lines and f'Size: {info.size}' in lines and f'MD5sum: {info.md5}' in lines
    assert stanza.endswith(' -dash line kept.\n')  # continuation lines kept, Description last
    assert '\n\n' not in stanza


def test_control_fields_waystation_writes_cannot_be_uploaded(tmp_path: Path) -> None:
    text = control_text('x1', '1.0', 'all') + 'Filename: ../../etc/passwd\nSHA256: 00\n'
    stanza = read(tmp_path, build_deb('x1', '1.0', control=text)).stanza('pool/main/x/x1/x1_1.0_all.deb')
    assert 'etc/passwd' not in stanza and 'SHA256: 00\n' not in stanza


def _tar_with(name: str, data: bytes = b'', *, kind: bytes = tarfile.REGTYPE) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w') as tar:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.size = len(data) if kind == tarfile.REGTYPE else 0
        tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return raw.getvalue()


GOOD_CONTROL = control_text('ok', '1.0', 'all').encode()
DATA = ('data.tar.gz', tar_of({'./x': b'x'}))


@pytest.mark.parametrize(
    ('members', 'message'),
    [
        ([('control.tar.gz', tar_of({'./control': GOOD_CONTROL})), DATA], 'debian-binary'),
        ([('debian-binary', b'3.0\n'), ('control.tar.gz', tar_of({'./control': GOOD_CONTROL})), DATA], '2.x'),
        ([('debian-binary', b'2.0\n'), DATA, ('control.tar.gz', tar_of({'./control': GOOD_CONTROL}))], 'order'),
        ([('debian-binary', b'2.0\n'), ('control.tar.gz', tar_of({'./control': GOOD_CONTROL}))], 'order'),
        ([('debian-binary', b'2.0\n'), ('control.tar.bz2', b'x'), DATA], 'compression'),
        (
            [
                ('debian-binary', b'2.0\n'),
                ('control.tar.gz', tar_of({'./control': GOOD_CONTROL})),
                ('data.tar.7z', b''),
            ],
            'compression',
        ),
        ([('debian-binary', b'2.0\n'), ('control.tar.gz', b'not gzip'), DATA], 'decompress'),
        (
            [('debian-binary', b'2.0\n'), ('control.tar.gz', tar_of({'./control': GOOD_CONTROL})[:-9]), DATA],
            'truncated',
        ),
        ([('debian-binary', b'2.0\n'), ('control.tar', b'\x00' * 10 + b'junk'), DATA], 'tar'),
        ([('debian-binary', b'2.0\n'), ('control.tar', _tar_with('./../control', GOOD_CONTROL)), DATA], 'unsafe'),
        ([('debian-binary', b'2.0\n'), ('control.tar', _tar_with('/control', GOOD_CONTROL)), DATA], 'unsafe'),
        ([('debian-binary', b'2.0\n'), ('control.tar', _tar_with('./control', kind=tarfile.SYMTYPE)), DATA], 'regular'),
        ([('debian-binary', b'2.0\n'), ('control.tar', _tar_with('./postinst', b'x')), DATA], 'no control'),
        ([('debian-binary', b'2.0\n')] * 17, 'too many'),
    ],
)
def test_refuses_malformed_archives(tmp_path: Path, members: list[tuple[str, bytes]], message: str) -> None:
    with pytest.raises(ValidationFailed, match=message):
        read(tmp_path, build_deb('x', '1', members=members))


def test_refuses_damaged_ar_files(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed, match='ar archive'):
        read(tmp_path, b'PK\x03\x04' + b'\x00' * 100)
    good = build_deb('ok', '1.0')
    with pytest.raises(ValidationFailed, match='truncated'):
        read(tmp_path, good[:-200])
    damaged = bytearray(good)
    damaged[8 + 58 : 8 + 60] = b'xx'  # the first member header's terminator
    with pytest.raises(ValidationFailed, match='header'):
        read(tmp_path, bytes(damaged))
    bad_size = b'!<arch>\n' + ar_member('debian-binary', b'2.0\n').replace(b'4         `', b'4x        `')
    with pytest.raises(ValidationFailed, match='header'):
        read(tmp_path, bad_size)


def test_bounds_on_decompression() -> None:
    bomb = tar_of({'./control': b'A' * (2 * 1024 * 1024)}, compression='xz')
    with pytest.raises(ValidationFailed, match='size limit'):
        deb.decompress(bomb, '.xz', limit=1024 * 1024)
    with pytest.raises(ValidationFailed, match='trailing'):
        deb.decompress(tar_of({'./c': b'c'}) + b'junk', '.gz')
    with pytest.raises(ValidationFailed, match='unsupported'):
        deb.decompress(b'', '.rar')
    big = _tar_with('./control', b'A' * (deb.CONTROL_MAX + 1))
    with pytest.raises(ValidationFailed, match='1 MiB'):
        deb.control_file(big)


@pytest.mark.parametrize(
    ('text', 'message'),
    [
        (b'\xff\xfe', 'UTF-8'),
        (b'Package: a\n\nPackage: b\n', 'more than one paragraph'),
        (b' continued\nPackage: a\n', 'continuation'),
        (b'Package: a\nPackage: b\n', 'repeats'),
        (b'Package a\n', 'invalid line'),
        (b'Package: a\x1b[31m\n', 'control characters'),
        (b'Version: 1\nArchitecture: all\n', 'Package'),
        (b'Package: Upper\nVersion: 1\nArchitecture: all\n', 'Package'),
        (b'Package: ok\nVersion: 1 2\nArchitecture: all\n', 'Version'),
        (b'Package: ok\nVersion: 1:2:3\nArchitecture: all\n', 'Version'),
        (b'Package: ok\nVersion: 1.0\nArchitecture: amd64 arm64\n', 'Architecture'),
        (b'Package: ok\nVersion: 1.0\nArchitecture: all\nSource: ../x\n', 'Source'),
    ],
)
def test_refuses_bad_control_files(text: bytes, message: str) -> None:
    with pytest.raises(ValidationFailed, match=message):
        deb.describe(deb.parse_control(text))


def test_control_comments_and_crlf() -> None:
    fields = deb.parse_control(
        b'# comment\r\nPackage: ok\r\nVersion: 1.0\r\nArchitecture: all\r\nDepends:\r\n a,\r\n b\r\n\r\n'
    )
    assert deb.describe(fields)[:3] == ('ok', '1.0', 'all')
    assert dict(fields)['Depends'] == '\n a,\n b'


# ---------------------------------------------------------------------------- options and "Set me up"
def repo(**options: object) -> RepoConfig:
    now = datetime.now(UTC)
    return RepoConfig.model_validate(
        {
            '_id': 'r1',
            'name': 'debs',
            'format': 'apt',
            'type': 'hosted',
            'format_options': {'distribution': 'stable', **options},
            'created_at': now,
            'updated_at': now,
        }
    )


def test_options_and_hosted_checks() -> None:
    opts = AptOptions.model_validate({'distribution': 'trixie'})
    assert (opts.components, opts.architectures) == (['main'], ['amd64', 'arm64'])
    assert DESCRIPTOR.check_options(opts, RepoType.HOSTED) == []
    assert DESCRIPTOR.check_options(AptOptions(distribution='a/b', flat=True), RepoType.HOSTED) == [
        ('distribution', 'a hosted distribution is one name (letters, digits, `._-`), no `/`'),
        ('flat', 'hosted repositories publish dists/<distribution>/ (no flat layout)'),
    ]
    assert DESCRIPTOR.check_options(AptOptions(distribution='a/b'), RepoType.PROXY) == []
    for bad in ({'components': ['Main']}, {'components': ['a', 'a']}, {'architectures': ['all']}, {'components': []}):
        with pytest.raises(ValueError):
            AptOptions.model_validate({'distribution': 'x', **bad})
    with pytest.raises(ValueError):
        AptOptions.model_validate({'distribution': 'x', 'architectures': ['amd64', 'amd64']})
    assert RepoType.HOSTED in DESCRIPTOR.types


def test_hosted_setup_snippets() -> None:
    ctx = SetupContext(
        repo=repo(components=['main', 'contrib']),
        base_url='https://registry.example.com',
        registry_host='registry.example.com',
        username='alice',
    )
    (client,) = DESCRIPTOR.setup_snippets(ctx)
    by_id = {s.id: s for s in client.snippets}
    sources = by_id['apt-sources'].content
    assert 'URIs: https://registry.example.com/repository/debs/\n' in sources
    assert 'Suites: stable\nComponents: main contrib\nSigned-By: /etc/apt/keyrings/debs.asc\n' in sources
    assert 'https://registry.example.com/repository/debs/public.asc' in by_id['apt-key'].content
    assert by_id['apt-auth'].content.startswith('machine registry.example.com/repository/debs/\nlogin {{USERNAME}}')
    assert by_id['upload'].content == (
        'curl -fS -u {{USERNAME}}:{{TOKEN}} --upload-file mypackage_1.0-1_amd64.deb '
        'https://registry.example.com/repository/debs/'
    )
    assert '-X DELETE https://registry.example.com/repository/debs/pool/main/m/mypackage/' in by_id['delete'].content


# ---------------------------------------------------------------------------- worker rendering
def test_release_text_and_by_hash_paths() -> None:
    opts = worker_apt.Options.of({'distribution': 'stable', 'components': ['main'], 'architectures': ['amd64']})
    assert opts is not None
    files = [
        worker_apt.IndexFile('main/binary-amd64/Packages', 'a' * 64, 1234, 'b' * 32, 'text/plain'),
        worker_apt.IndexFile('main/binary-amd64/Packages.xz', 'c' * 64, 99, 'd' * 32, 'application/x-xz'),
    ]
    now = datetime(2026, 11, 22, 10, 0, 0, tzinfo=UTC)
    text = worker_apt.release_text('debs', opts, ['main'], ['amd64'], files, now)
    assert text.startswith('Origin: Waystation\nLabel: debs\nSuite: stable\nCodename: stable\n')
    assert 'Date: Sun, 22 Nov 2026 10:00:00 UTC\n' in text
    assert 'Acquire-By-Hash: yes\n' in text
    assert (
        f'SHA256:\n {"a" * 64} 1234 main/binary-amd64/Packages\n {"c" * 64}   99 main/binary-amd64/Packages.xz\n'
        in text
    )
    assert worker_apt.by_hash_paths(text, 'stable') == {
        f'dists/stable/main/binary-amd64/by-hash/SHA256/{"a" * 64}',
        f'dists/stable/main/binary-amd64/by-hash/SHA256/{"c" * 64}',
    }
    assert files[0].by_hash('stable') == f'dists/stable/main/binary-amd64/by-hash/SHA256/{"a" * 64}'


def test_worker_options_are_checked() -> None:
    assert worker_apt.Options.of({'distribution': 'a/b'}) is None
    assert worker_apt.Options.of(None) is None
    opts = worker_apt.Options.of({'distribution': 'x', 'components': ['BAD'], 'architectures': ['all']})
    assert opts == worker_apt.Options('x', ('main',), ('amd64', 'arm64'))


# ---------------------------------------------------------------------------- Nexus import
def test_nexus_apt_hosted_maps_with_its_distribution() -> None:
    every = frozenset({'proxy', 'hosted'})
    nexus = NexusRepository(
        entry={'name': 'apt-internal', 'format': 'apt', 'type': 'hosted'},
        detail={
            'name': 'apt-internal',
            'online': True,
            'storage': {'writePolicy': 'ALLOW_ONCE'},
            'apt': {'distribution': 'bookworm'},
            'aptSigning': {'keypair': '<redacted>'},
        },
    )
    m = map_repository(nexus, supported={'apt': every})
    assert m.action == 'create' and m.body is not None
    assert m.body['format_options'] == {'distribution': 'bookworm', 'components': ['main']}
    assert m.body['hosted'] == {'write_policy': 'disable_redeploy'}
    assert any(d.field == 'signing' for d in m.differences)
    proxy_only = map_repository(nexus, supported={'apt': frozenset({'proxy'})})
    assert proxy_only.action == 'unsupported'
