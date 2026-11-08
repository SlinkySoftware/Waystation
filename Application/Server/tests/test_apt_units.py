"""apt format units: Release/Packages parsing, bounded decompression, path classification (T-P2-APT)."""

import bz2
import gzip
import hashlib
import lzma
from datetime import UTC, datetime

import pytest

from waystation_common.errors import ValidationFailed
from waystation_server.formats.apt import deb822
from waystation_server.formats.apt.descriptor import DESCRIPTOR
from waystation_server.formats.apt.plugin import Target, classify, packages_target
from waystation_server.formats.base import SetupContext
from waystation_server.repos.model import RepoConfig

H1 = hashlib.sha256(b'one').hexdigest()
H2 = hashlib.sha256(b'two').hexdigest()
H512 = hashlib.sha512(b'one').hexdigest()

RELEASE = f"""Origin: Debian
Suite: trixie-security
Acquire-By-Hash: yes
Description: Debian x.y Security Updates
 continued description line
MD5Sum:
 0123456789abcdef0123456789abcdef 3 main/binary-amd64/Packages.xz
SHA256:
 {H1} 3 main/binary-amd64/Packages.xz
 {H2.upper()} 3 main/i18n/Translation-en.bz2
 {H1} 3 ../../etc/passwd
SHA512:
 {H512} 3 main/binary-amd64/Packages.xz
""".encode()


def test_parse_release() -> None:
    release = deb822.parse_release(RELEASE)
    assert release.by_hash is True
    assert release.files == {
        'main/binary-amd64/Packages.xz': deb822.IndexEntry(H1, 3),
        'main/i18n/Translation-en.bz2': deb822.IndexEntry(H2, 3),  # hex normalised to lower case
    }
    assert release.sha512 == {'main/binary-amd64/Packages.xz': H512}
    assert release.path_for('sha256', H2) == 'main/i18n/Translation-en.bz2'
    assert release.path_for('sha512', H512) == 'main/binary-amd64/Packages.xz'
    assert release.path_for('sha256', '0' * 64) is None
    assert release.path_for('md5', H1) is None


def test_parse_inrelease_strips_the_clearsign_armor() -> None:
    signed = (
        b'-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n'
        + RELEASE.replace(b'Origin: Debian', b'- Origin: Debian')
        + b'-----BEGIN PGP SIGNATURE-----\n\nabc\n-----END PGP SIGNATURE-----\n'
    )
    assert deb822.strip_clearsign(signed).startswith(b'Origin: Debian')
    assert deb822.parse_release(signed).files == deb822.parse_release(RELEASE).files
    assert deb822.strip_clearsign(b'plain') == b'plain'


def test_parse_release_without_by_hash_and_trailing_paragraphs() -> None:
    release = deb822.parse_release(f'\nSuite: x\nSHA256:\n {H1} 3 main/Contents-amd64.gz\n\nJunk: here\n'.encode())
    assert release.by_hash is False
    assert list(release.files) == ['main/Contents-amd64.gz']


@pytest.mark.parametrize(
    'data',
    [
        b'-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nSuite: x\n',  # no signature block
        b'SHA256:\n nothex 3 main/Packages\n',
        b'SHA256:\n ' + H1.encode() + b' x main/Packages\n',
        b'SHA256:\n ' + H1.encode() + b' 3\n',
        b'Suite x\n',
        b'x' * (deb822.RELEASE_MAX_BYTES + 1),
    ],
)
def test_parse_release_rejects_garbage(data: bytes) -> None:
    with pytest.raises(ValidationFailed):
        deb822.parse_release(data)


def test_release_entry_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deb822, 'RELEASE_MAX_ENTRIES', 1)
    with pytest.raises(ValidationFailed, match='too many'):
        deb822.parse_release(f'SHA256:\n {H1} 3 a\n {H1} 3 b\n'.encode())


PACKAGES = f"""Package: hello
Version: 2.10-5
Filename: pool/main/h/hello/hello_2.10-5_amd64.deb
Size: 53000
SHA256: {H1.upper()}
Description: hello
 Filename: pool/not/a/field.deb

Package: nohash
Filename: pool/main/n/nohash.deb
Size: 1

Package: bad
Filename: ../../escape.deb
Size: 1
SHA256: {H1}

Package: badhash
Filename: pool/main/b/badhash.deb
Size: 1
SHA256: zz

Package: last
Filename: pool/main/l/last_1_all.deb
Size: 7
SHA256: {H2}""".encode()


def test_parse_packages() -> None:
    assert deb822.parse_packages(PACKAGES) == [
        deb822.PoolEntry('pool/main/h/hello/hello_2.10-5_amd64.deb', H1, 53000),
        deb822.PoolEntry('pool/main/l/last_1_all.deb', H2, 7),
    ]


def test_packages_entry_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deb822, 'PACKAGES_MAX_ENTRIES', 0)
    with pytest.raises(ValidationFailed, match='too many'):
        deb822.parse_packages(PACKAGES)


@pytest.mark.parametrize(
    ('suffix', 'pack'),
    [
        ('', lambda b: b),
        ('.gz', gzip.compress),
        ('.xz', lzma.compress),
        ('.lzma', lambda b: lzma.compress(b, format=lzma.FORMAT_ALONE)),
        ('.bz2', bz2.compress),
    ],
)
def test_decompress_round_trip(suffix: str, pack: object) -> None:
    assert callable(pack)
    assert deb822.parse_packages_file(pack(PACKAGES), suffix) == deb822.parse_packages(PACKAGES)


def test_decompress_zstd() -> None:
    from compression import zstd

    assert deb822.decompress(zstd.compress(PACKAGES), '.zst') == PACKAGES


@pytest.mark.parametrize('suffix', ['.gz', '.xz', '.bz2'])
def test_decompress_is_bounded(suffix: str) -> None:
    bomb = {'.gz': gzip.compress, '.xz': lzma.compress, '.bz2': bz2.compress}[suffix](b'\0' * 100_000)
    with pytest.raises(ValidationFailed, match='size limit'):
        deb822.decompress(bomb, suffix, limit=50_000)
    assert len(deb822.decompress(bomb, suffix, limit=100_000)) == 100_000


@pytest.mark.parametrize('suffix', ['.gz', '.xz', '.bz2'])
def test_decompress_rejects_truncated_and_corrupt(suffix: str) -> None:
    packed = {'.gz': gzip.compress, '.xz': lzma.compress, '.bz2': bz2.compress}[suffix](PACKAGES)
    with pytest.raises(ValidationFailed):
        deb822.decompress(packed[: len(packed) // 2], suffix)
    with pytest.raises(ValidationFailed):
        deb822.decompress(b'not compressed at all', suffix)


def test_decompress_limits_and_unknown() -> None:
    with pytest.raises(ValidationFailed, match='too large'):
        deb822.decompress(b'abc', '', limit=2)
    with pytest.raises(ValidationFailed, match='unsupported'):
        deb822.decompress(b'abc', '.lz4')


def test_compression_of() -> None:
    assert deb822.compression_of('main/binary-amd64/Packages') == ''
    assert deb822.compression_of('main/binary-amd64/Packages.xz') == '.xz'
    assert deb822.compression_of('main/binary-amd64/Packages.lz4') is None


@pytest.mark.parametrize(
    ('path', 'flat', 'expected'),
    [
        ('dists/trixie/InRelease', False, Target('release', 'dists/trixie', 'InRelease')),
        ('dists/trixie/Release.gpg', False, Target('release', 'dists/trixie', 'Release.gpg')),
        (
            'dists/trixie/main/binary-amd64/Release',
            False,
            Target('index', 'dists/trixie', 'main/binary-amd64/Release'),
        ),
        (
            'dists/trixie-security/main/binary-amd64/Packages.xz',
            False,
            Target('index', 'dists/trixie-security', 'main/binary-amd64/Packages.xz'),
        ),
        (
            f'dists/trixie/main/binary-amd64/by-hash/SHA256/{H1}',
            False,
            Target('by-hash', 'dists/trixie', algorithm='sha256', digest=H1),
        ),
        (
            f'dists/trixie/main/by-hash/SHA512/{H512}',
            False,
            Target('by-hash', 'dists/trixie', algorithm='sha512', digest=H512),
        ),
        (f'dists/trixie/main/by-hash/MD5Sum/{"a" * 32}', False, Target('invalid')),
        ('dists/trixie/main/by-hash/SHA256/abc', False, Target('invalid')),
        (f'pool/by-hash/SHA256/{H1}', False, Target('invalid')),
        ('pool/main/h/hello/hello_2.10-5_amd64.deb', False, Target('pool')),
        ('project/trace/master', False, Target('other')),
        ('dists/trixie', False, Target('other')),
        ('Release', True, Target('release', '', 'Release')),
        ('sub/InRelease', True, Target('release', 'sub', 'InRelease')),
        ('Packages.gz', True, Target('index', '', 'Packages.gz')),
        (f'by-hash/SHA256/{H1}', True, Target('by-hash', '', algorithm='sha256', digest=H1)),
        ('hello_1_amd64.deb', True, Target('pool')),
        ('README', True, Target('other')),
    ],
)
def test_classify(path: str, flat: bool, expected: Target) -> None:
    assert classify(path, flat=flat) == expected


def test_packages_target() -> None:
    target = packages_target('dists/trixie-security', 'main/binary-amd64/Packages.xz')
    assert target is not None
    assert (target.suite, target.component, target.arch, target.suffix) == ('trixie-security', 'main', 'amd64', '.xz')
    nested = packages_target('dists/trixie', 'main/debian-installer/binary-arm64/Packages')
    assert nested is not None
    assert (nested.component, nested.arch, nested.suffix) == ('main/debian-installer', 'arm64', '')
    flat = packages_target('', 'Packages.gz')
    assert flat is not None
    assert (flat.suite, flat.component, flat.arch) == ('', '', '')
    assert packages_target('dists/trixie', 'main/binary-amd64/Packages.lz4') is None
    assert packages_target('dists/trixie', 'main/binary-amd64/Packages.diff/Index') is None
    assert packages_target('dists/trixie', 'main/i18n/Translation-en.xz') is None
    assert packages_target('dists/trixie', 'main/source/Packages') is None


def _setup_text(upstream: str, suite: str) -> dict[str, str]:
    now = datetime.now(UTC)
    repo = RepoConfig.model_validate(
        {
            '_id': 'r1',
            'name': 'apt-proxy',
            'format': 'apt',
            'type': 'proxy',
            'proxy': {'upstream_url': upstream},
            'format_options': {'distribution': suite},
            'created_at': now,
            'updated_at': now,
        }
    )
    ctx = SetupContext(repo=repo, base_url='https://waystation.example.com', registry_host='', username=None)
    [client] = DESCRIPTOR.setup_snippets(ctx)
    return {s.id: s.content for s in client.snippets}


def test_setup_snippets_use_the_distribution_keyring() -> None:
    debian = _setup_text('https://deb.debian.org/debian/', 'trixie')
    assert 'Suites: trixie trixie-updates\nComponents: main\n' in debian['apt-sources']
    assert 'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' in debian['apt-sources']
    assert debian['apt-list'].startswith('deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://')
    ubuntu = _setup_text('https://security.ubuntu.com/ubuntu/', 'resolute-security')
    assert 'Suites: resolute-security\nComponents: main restricted universe multiverse\n' in ubuntu['apt-sources']
    assert 'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' in ubuntu['apt-sources']
