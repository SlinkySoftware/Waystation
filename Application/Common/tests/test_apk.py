"""Alpine apk: version order, file names, package reading, index parsing and signing (``waystation_common.apk``)."""

from __future__ import annotations

import io
import itertools

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from ws_apk import build_apk, build_index, checksum, make_key

from waystation_common import apk
from waystation_common.cleanup.plan import coordinates
from waystation_common.cleanup.versions import is_prerelease, sort_key
from waystation_common.components import key_for
from waystation_common.versionsort import version_sort

# Ascending apk order (apk version -t), including apk-tools' own test vectors' shapes.
ORDERED = [
    '0.1',
    '1.0_alpha',
    '1.0_alpha1',
    '1.0_beta2',
    '1.0_pre1',
    '1.0_rc1',
    '1.0_rc2',
    '1.0',
    '1.0-r1',
    '1.0-r2',
    '1.0-r10',
    '1.0_cvs1',
    '1.0_git20240101',
    '1.0_p1',
    '1.0_p2-r3',
    '1.0a',
    '1.0b',
    '1.0.1',
    '1.0.2_rc1',
    '1.0.2',
    '1.1',
    '1.2',
    '1.10',
    '2.0',
    '2.39.5-r0',
    '2.40.0-r0',
    '10.0',
    '20240101',
]


def test_compare_matches_known_order() -> None:
    for i, a in enumerate(ORDERED):
        assert apk.compare(a, a) == 0
        for b in ORDERED[i + 1 :]:
            assert apk.compare(a, b) == -1, (a, b)
            assert apk.compare(b, a) == 1, (b, a)


def test_leading_zeros_follow_apk() -> None:
    # apk compares a dotted part with leading zeros by the number of zeros first (fractional style)
    assert apk.compare('1.05', '1.5') == -1
    assert apk.compare('1.0', '1.00') == 1
    assert apk.compare('1.001', '1.01') == -1


def test_sort_string_agrees_with_compare() -> None:
    corpus = [*ORDERED, '1.05', '1.00', '1.001', '1.01', '1.0_rc', '1.0_p', '1.2a3', '3.3_pre20240101-r1', '0']
    for a, b in itertools.product(corpus, repeat=2):
        want = apk.compare(a, b)
        sa, sb = apk.sort_string(a), apk.sort_string(b)
        got = (sa > sb) - (sa < sb)
        assert got == want, (a, b, sa, sb)
    assert version_sort('apk', '1.0-r1') == apk.sort_string('1.0-r1')
    assert sorted(ORDERED, key=lambda v: sort_key('apk', v)) == ORDERED


def test_invalid_versions() -> None:
    for bad in ('', 'abc', '1.0-1', '1..0', '1.0_foo', '1.0a.1', '-r1', '1.0-r'):
        assert not apk.valid_version(bad), bad
    assert apk.sort_string('1.0_foo').startswith('!') and apk.sort_string('abc').startswith('!')
    assert apk.valid_version('1.37.0-r19')
    assert apk.is_prerelease('1.0_rc1') and is_prerelease('apk', '2.0_beta3')
    assert not apk.is_prerelease('1.0_p1')
    assert apk.newest(['1.0', '1.0-r1', '1.0_rc1']) == '1.0-r1'


def test_file_names() -> None:
    assert apk.split_file_name('busybox-1.37.0-r19.apk') == ('busybox', '1.37.0-r19')
    assert apk.split_file_name('py3-foo-bar-2.0-r0.apk') == ('py3-foo-bar', '2.0-r0')
    assert apk.split_file_name('libstdc++-14.2.0-r6.apk') == ('libstdc++', '14.2.0-r6')
    assert apk.split_file_name('font-name-1-2.0-r0.apk') == ('font-name-1', '2.0-r0')
    assert apk.split_file_name('nodash.apk') is None
    assert apk.split_file_name('a-1.0.tar') is None
    path = 'v3.22/main/x86_64/busybox-1.37.0-r19.apk'
    coord = coordinates('apk', path)
    assert (coord.name, coord.version, coord.group) == ('busybox', '1.37.0-r19', 'v3.22/main/x86_64/busybox')
    key = key_for('apk', path)
    assert key is not None and key.display == 'busybox-1.37.0-r19'
    assert key_for('apk', 'v3.22/main/x86_64/APKINDEX.tar.gz') is None
    directory = apk.Directory('v3.22/main/x86_64')
    assert (directory.branch, directory.repository, directory.arch) == ('v3.22', 'main', 'x86_64')


def test_read_signed_package() -> None:
    key = make_key()
    data = build_apk('hello', '1.0-r0', depends=('so:libc.musl-x86_64.so.1', 'busybox'), key=key)
    info = apk.read_package(io.BytesIO(data), len(data))
    assert (info.name, info.version, info.arch, info.size) == ('hello', '1.0-r0', 'x86_64', len(data))
    assert info.signed_by == ('test.rsa.pub',)
    assert apk.CHECKSUM.match(info.checksum)
    assert info.fields['depend'] == ['so:libc.musl-x86_64.so.1', 'busybox']
    apk.package_check(info.checksum)(io.BytesIO(data))
    with pytest.raises(apk.ApkError, match='APKINDEX checksum'):
        apk.package_check('Q1' + 'A' * 27 + '=')(io.BytesIO(data))
    unsigned = build_apk('hello', '1.0-r0')
    assert apk.read_package(io.BytesIO(unsigned), len(unsigned)).signed_by == ()


def test_package_errors() -> None:
    good = build_apk('hello', '1.0-r0')
    tampered = good[:-20] + bytes(20)
    with pytest.raises(apk.ApkError):
        apk.read_package(io.BytesIO(tampered), len(tampered))
    with pytest.raises(apk.ApkError, match='v2'):
        apk.read_package(io.BytesIO(b'ADBd....'), 8)
    with pytest.raises(apk.ApkError, match='truncated'):
        apk.read_package(io.BytesIO(good[:30]), 30)
    bad_version = build_apk('hello', 'one')
    with pytest.raises(apk.ApkError, match='pkgver'):
        apk.read_package(io.BytesIO(bad_version), len(bad_version))
    no_hash = build_apk('hello', '1.0-r0', datahash=False)
    assert apk.read_package(io.BytesIO(no_hash), len(no_hash)).name == 'hello'
    with pytest.raises(apk.ApkError, match='control characters'):
        apk.parse_pkginfo('pkgdesc = a\x01b\n')
    with pytest.raises(apk.ApkError):
        apk.parse_pkginfo('no separator\n')


def test_gzip_bomb_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apk, 'CONTROL_MAX', 1024)
    monkeypatch.setattr(apk, 'SIGNATURE_MAX', 1024)
    big = build_apk('hello', '1.0-r0', extra='pkgdesc = ' + 'x' * 5000 + '\n')
    with pytest.raises(apk.ApkError, match='too large'):
        apk.read_package(io.BytesIO(big), len(big))


def test_index_parse_and_build() -> None:
    upstream_key = make_key()
    packages = {'a-1.0-r0.apk': build_apk('a', '1.0-r0'), 'b-2.0_rc1-r1.apk': build_apk('b', '2.0_rc1-r1')}
    index = apk.parse_index(build_index(packages, key=upstream_key))
    assert [(e.name, e.version, e.size) for e in index.entries] == [
        ('a', '1.0-r0', len(packages['a-1.0-r0.apk'])),
        ('b', '2.0_rc1-r1', len(packages['b-2.0_rc1-r1.apk'])),
    ]
    assert index.entries[0].checksum == checksum(packages['a-1.0-r0.apk'])
    assert index.signed_by == ('test.rsa.pub',)
    assert index.description == 'test index'

    key = make_key()
    stanzas = [apk.stanza(apk.read_package(io.BytesIO(p), len(p)), arch='x86_64') for p in packages.values()]
    built = apk.build_index(
        stanzas,
        'hosted',
        key_name='repo-abc.rsa.pub',
        sign=lambda data: key.sign(data, padding.PKCS1v15(), hashes.SHA256()),
        mtime=1_700_000_000,
    )
    parsed = apk.parse_index(built)
    assert [e.name for e in parsed.entries] == ['a', 'b']
    assert parsed.signed_by == ('repo-abc.rsa.pub',)
    assert parsed.entries[1].fields['T'] == 'test package'
    # The signature covers the second gzip member exactly, as apk verifies it.
    first = apk._gzip_member(io.BytesIO(built), 0, 1 << 20, 'sig')
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(first.data), mode='r:') as tar:
        member = tar.next()
        assert member is not None and member.name == '.SIGN.RSA256.repo-abc.rsa.pub'
        handle = tar.extractfile(member)
        assert handle is not None
        signature = handle.read()
    public = serialization.load_pem_public_key(
        key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    public.verify(signature, built[first.end :], padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr,call-arg]


def test_index_errors() -> None:
    with pytest.raises(apk.ApkError, match='gzip'):
        apk.check_index(b'not gzip')
    with pytest.raises(apk.ApkError, match='no APKINDEX'):
        apk.check_index(build_apk('a', '1.0-r0')[:0] + apk._gzip(b'\0' * 1024))
    entries = apk.parse_entries('P:ok\nV:1.0-r0\nC:Q1' + 'A' * 27 + '=\nS:10\n\nP:../bad\nV:1\nC:x\n\n')
    assert [e.name for e in entries] == ['ok']
