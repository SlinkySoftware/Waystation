"""yum format units: repomd/primary parsing (defused, bounded), chunked decompression, path classification,
options and "Set me up" (T-P2-YUM)."""

import bz2
import gzip
import hashlib
import lzma
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from waystation_common import openpgp
from waystation_common.errors import ValidationFailed
from waystation_server.formats.base import SetupContext
from waystation_server.formats.yum import evr, repodata
from waystation_server.formats.yum.descriptor import DESCRIPTOR, YumOptions, layout
from waystation_server.formats.yum.plugin import Target, classify, expected_of
from waystation_server.proxy.engine import Expected
from waystation_server.repos.model import RepoConfig

H1 = hashlib.sha256(b'one').hexdigest()
H2 = hashlib.sha256(b'two').hexdigest()
S1 = hashlib.sha1(b'one').hexdigest()
S512 = hashlib.sha512(b'one').hexdigest()


def repomd_xml(*data: str, revision: str = '42') -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<repomd xmlns="http://linux.duke.edu/metadata/repo" xmlns:rpm="http://linux.duke.edu/metadata/rpm">\n'
        f'  <revision>{revision}</revision>\n' + ''.join(data) + '</repomd>\n'
    ).encode()


def data_xml(kind: str, href: str, digest: str, *, alg: str = 'sha256', size: int = 3, extra: str = '') -> str:
    return (
        f'<data type="{kind}"><checksum type="{alg}">{digest}</checksum>'
        f'<open-checksum type="sha256">{H2}</open-checksum><location href="{href}"{extra}/>'
        f'<timestamp>1</timestamp><size>{size}</size><open-size>9</open-size></data>\n'
    )


def package_xml(name: str, href: str, digest: str, *, alg: str = 'sha256', size: int = 10, extra: str = '') -> str:
    return (
        f'<package type="rpm"><name>{name}</name><arch>noarch</arch>'
        f'<version epoch="1" ver="2.0" rel="3.el10"/>'
        f'<checksum type="{alg}" pkgid="YES">{digest}</checksum><summary>s &amp; t</summary>'
        f'<size package="{size}" installed="1" archive="1"/><location href="{href}"{extra}/>'
        '<format><rpm:provides><rpm:entry name="x"/></rpm:provides><file>/usr/bin/name</file></format>'
        '</package>\n'
    )


def primary_xml(*packages: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm" '
        f'packages="{len(packages)}">\n' + ''.join(packages) + '</metadata>\n'
    ).encode()


def test_parse_repomd() -> None:
    doc = repomd_xml(
        data_xml('primary', f'repodata/{H1}-primary.xml.gz', H1.upper(), size=6214),
        data_xml('filelists', 'repodata/filelists.xml.zst', S1, alg='sha'),
        data_xml('other', 'repodata/other.xml.xz', S512, alg='sha512'),
        data_xml('updateinfo', 'repodata/updateinfo.xml.bz2', H1, alg='sha384'),  # unsupported: no checksum
        data_xml('group', '../../etc/passwd', H1),  # unsafe: dropped
        data_xml('modules', 'repodata/modules.yaml.gz', H1, extra=' xml:base="https://mirror.example.com/"'),
        data_xml('', 'repodata/untyped.xml', H1),
    )
    repomd = repodata.parse_repomd(doc)
    assert repomd.revision == '42'
    assert repomd.entries[f'repodata/{H1}-primary.xml.gz'] == repodata.RepomdEntry(
        'primary', f'repodata/{H1}-primary.xml.gz', repodata.Checksum('sha256', H1), 6214
    )
    assert repomd.entries['repodata/filelists.xml.zst'].checksum == repodata.Checksum('sha1', S1)
    assert repomd.entries['repodata/other.xml.xz'].checksum == repodata.Checksum('sha512', S512)
    assert repomd.entries['repodata/updateinfo.xml.bz2'].checksum is None
    assert set(repomd.entries) == {
        f'repodata/{H1}-primary.xml.gz',
        'repodata/filelists.xml.zst',
        'repodata/other.xml.xz',
        'repodata/updateinfo.xml.bz2',
    }
    primary = repomd.of_type('primary')
    assert primary is not None
    assert primary.href.endswith('primary.xml.gz')
    assert repomd.of_type('comps') is None


@pytest.mark.parametrize(
    'data',
    [
        b'not xml at all',
        b'<metadata></metadata>',  # wrong root
        b'<repomd><data type="primary"></repomd>',  # not well-formed
        b'<!DOCTYPE repomd [<!ENTITY x "y">]><repomd/>',  # DTD
        b'<!DOCTYPE repomd SYSTEM "http://example.com/x.dtd"><repomd/>',  # external DTD
        b'<repomd><revision>&undefined;</revision></repomd>',  # entities without a DTD
        b'<repomd><revision>' + b'x' * (repodata.TEXT_MAX + 1) + b'</revision></repomd>',  # text bound
    ],
)
def test_parse_repomd_rejects(data: bytes) -> None:
    with pytest.raises(ValidationFailed):
        repodata.parse_repomd(data)


def test_parse_repomd_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationFailed, match='too large'):
        repodata.parse_repomd(b' ' * (repodata.REPOMD_MAX_BYTES + 1))
    monkeypatch.setattr(repodata, 'REPOMD_MAX_ENTRIES', 2)
    many = repomd_xml(*(data_xml('x', f'repodata/{i}.xml', H1) for i in range(3)))
    with pytest.raises(ValidationFailed, match='too many'):
        repodata.parse_repomd(many)


def test_billion_laughs_is_refused() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]><metadata>&lol2;</metadata>'
    )
    with pytest.raises(ValidationFailed, match='document type'):
        repodata.parse_primary(bomb, '')


def test_parse_primary() -> None:
    doc = primary_xml(
        package_xml('hello', 'Packages/h/hello-2.0-3.el10.noarch.rpm', H1.upper(), size=1234),
        package_xml('old', 'Packages/o/old.rpm', S1, alg='sha'),
        package_xml('evil', '../../../etc/passwd', H1),  # unsafe href: dropped
        package_xml('elsewhere', 'Packages/e/e.rpm', H1, extra=' xml:base="https://mirror.example.com/"'),
        package_xml('weak', 'Packages/w/w.rpm', 'abc'),  # malformed checksum: dropped
        package_xml('md5', 'Packages/m/m.rpm', hashlib.md5(b'x').hexdigest(), alg='md5'),
    )
    packages = repodata.parse_primary(gzip.compress(doc), '.gz')
    assert packages == [
        repodata.Package(
            href='Packages/h/hello-2.0-3.el10.noarch.rpm',
            checksum=repodata.Checksum('sha256', H1),
            size=1234,
            name='hello',
            epoch='1',
            version='2.0',
            release='3.el10',
            arch='noarch',
        ),
        repodata.Package(
            href='Packages/o/old.rpm',
            checksum=repodata.Checksum('sha1', S1),
            size=10,
            name='old',
            epoch='1',
            version='2.0',
            release='3.el10',
            arch='noarch',
        ),
    ]


def test_parse_primary_skips_incomplete_packages() -> None:
    doc = primary_xml(
        '<package type="rpm"><name>nosize</name>'
        f'<checksum type="sha256">{H1}</checksum><location href="Packages/n.rpm"/></package>',
        f'<package type="rpm"><name>nohref</name><size package="1"/><checksum type="sha256">{H1}</checksum></package>',
    )
    assert repodata.parse_primary(doc, '') == []


@pytest.mark.parametrize(
    'data', [b'<repomd/>', b'<metadata><package>', b'<!DOCTYPE metadata><metadata/>', b'\xff\xfe garbage']
)
def test_parse_primary_rejects(data: bytes) -> None:
    with pytest.raises(ValidationFailed):
        repodata.parse_primary(data, '')


def test_primary_package_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repodata, 'PRIMARY_MAX_PACKAGES', 1)
    doc = primary_xml(package_xml('a', 'a.rpm', H1), package_xml('b', 'b.rpm', H1))
    with pytest.raises(ValidationFailed, match='too many'):
        repodata.parse_primary(doc, '')


def _zstd(data: bytes) -> bytes:
    from compression import zstd

    return zstd.compress(data)


PACKERS: list[tuple[str, Any]] = [
    ('.gz', gzip.compress),
    ('.xz', lzma.compress),
    ('.bz2', bz2.compress),
    ('.zst', _zstd),
    ('', lambda d: d),
]


@pytest.mark.parametrize(('suffix', 'pack'), PACKERS)
def test_decompress_round_trip_in_chunks(suffix: str, pack: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repodata, 'CHUNK', 1000)
    data = bytes(range(256)) * 50
    chunks = list(repodata.decompress_chunks(pack(data), suffix))
    assert b''.join(chunks) == data
    assert max(len(c) for c in chunks) <= 1000
    doc = primary_xml(*(package_xml(f'p{i}', f'Packages/p{i}.rpm', H1) for i in range(40)))
    assert len(repodata.parse_primary(pack(doc), suffix)) == 40


@pytest.mark.parametrize(('suffix', 'pack'), PACKERS)
def test_decompress_is_bounded(suffix: str, pack: Any) -> None:
    with pytest.raises(ValidationFailed, match=r'size limit|too large'):
        list(repodata.decompress_chunks(pack(b'\0' * 10_000), suffix, limit=1000))


@pytest.mark.parametrize(('suffix', 'pack'), PACKERS[:4])
def test_decompress_rejects_truncated_and_corrupt(suffix: str, pack: Any) -> None:
    packed = pack(bytes(range(256)) * 100)
    with pytest.raises(ValidationFailed):
        list(repodata.decompress_chunks(packed[: len(packed) // 2], suffix))
    with pytest.raises(ValidationFailed):
        list(repodata.decompress_chunks(b'garbage' * 10, suffix))


def test_decompress_unknown() -> None:
    with pytest.raises(ValidationFailed, match='unsupported'):
        list(repodata.decompress_chunks(b'x', '.lzma'))


def test_compression_of() -> None:
    assert repodata.compression_of('repodata/x-primary.xml.gz') == '.gz'
    assert repodata.compression_of('repodata/x-primary.xml.zst') == '.zst'
    assert repodata.compression_of('repodata/comps.xml') == ''
    assert repodata.compression_of('repodata/x-primary.sqlite.bz2') == '.bz2'
    assert repodata.compression_of('repodata/modules.yaml') is None


def test_checksums() -> None:
    assert repodata.checksum('SHA256', f' {H1.upper()} ') == repodata.Checksum('sha256', H1)
    assert repodata.checksum('sha', S1) == repodata.Checksum('sha1', S1)
    assert repodata.checksum('sha256', S1) is None  # wrong length
    assert repodata.checksum('md5', 'a' * 32) is None
    assert repodata.checksum(None, None) is None
    assert repodata.checksum('sha256', 'g' * 64) is None
    assert repodata.checksum_prefix(f'{H1}-primary.xml.gz') == repodata.Checksum('sha256', H1)
    assert repodata.checksum_prefix(f'{S1}-other.xml.gz') == repodata.Checksum('sha1', S1)
    assert repodata.checksum_prefix(f'{S512}-filelists.xml.zst') == repodata.Checksum('sha512', S512)
    assert repodata.checksum_prefix('primary.xml.gz') is None
    assert repodata.checksum_prefix('abc-primary.xml.gz') is None
    assert repodata.checksum_prefix(f'{"z" * 64}-primary.xml.gz') is None
    assert expected_of('sha1', S1, 3) == Expected(sha1=S1, size=3)
    assert expected_of('sha512', S512) == Expected(sha512=S512)
    assert expected_of('sha256', H1) == Expected(sha256=H1)


@pytest.mark.parametrize(
    ('path', 'expected'),
    [
        ('10/BaseOS/x86_64/os/repodata/repomd.xml', Target('repomd', '10/BaseOS/x86_64/os', 'repomd.xml')),
        ('repodata/repomd.xml', Target('repomd', '', 'repomd.xml')),
        ('os/repodata/repomd.xml.asc', Target('signature', 'os', 'repomd.xml.asc')),
        ('os/repodata/repomd.xml.key', Target('signature', 'os', 'repomd.xml.key')),
        (f'os/repodata/{H1}-primary.xml.gz', Target('repodata', 'os', f'{H1}-primary.xml.gz')),
        ('os/Packages/h/hello-1-1.noarch.rpm', Target('package')),
        ('hello.drpm', Target('package')),
        ('os/.treeinfo', Target('other')),
        ('RPM-GPG-KEY-Rocky-10', Target('other')),
        ('repodata', Target('other')),
    ],
)
def test_classify(path: str, expected: Target) -> None:
    assert classify(path) == expected


def _repo(url: str, **options: Any) -> RepoConfig:
    now = datetime.now(UTC)
    return RepoConfig.model_validate(
        {
            '_id': 'r1',
            'name': 'rocky',
            'format': 'yum',
            'type': 'proxy',
            'proxy': {'upstream_url': url},
            'format_options': options,
            'created_at': now,
            'updated_at': now,
        }
    )


def _setup(url: str, **options: Any) -> dict[str, str]:
    ctx = SetupContext(
        repo=_repo(url, **options), base_url='https://waystation.example.com', registry_host='', username=None
    )
    [client] = DESCRIPTOR.setup_snippets(ctx)
    return {s.id: s.content for s in client.snippets}


def test_setup_snippets_follow_well_known_layouts() -> None:
    rocky = _setup('https://dl.rockylinux.org/pub/rocky/')
    assert '[rocky-baseos]\nname=Rocky Linux $releasever - BaseOS (Waystation)\n' in rocky['repo']
    assert 'baseurl=https://waystation.example.com/repository/rocky/$releasever/BaseOS/$basearch/os/\n' in rocky['repo']
    assert 'baseurl=https://waystation.example.com/repository/rocky/$releasever/extras/$basearch/os/\n' in rocky['repo']
    assert 'gpgcheck=1\n' in rocky['repo']
    assert 'gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-$releasever\n' in rocky['repo']
    assert '#password={{TOKEN}}' in rocky['repo']
    assert rocky['disable'] == 'sudo dnf config-manager --set-disabled baseos appstream extras'
    alma = _setup('https://repo.almalinux.org/almalinux/')
    assert 'RPM-GPG-KEY-AlmaLinux-$releasever' in alma['repo']
    oracle = _setup('https://yum.oracle.com/')
    assert '/repository/rocky/repo/OracleLinux/OL$releasever/baseos/latest/$basearch/\n' in oracle['repo']
    assert '/repository/rocky/OL$releasever/' in _setup('https://yum.oracle.com/repo/OracleLinux/')['repo']
    epel = _setup('https://dl.fedoraproject.org/pub/epel/')
    assert '/repository/rocky/$releasever/Everything/$basearch/\n' in epel['repo']
    assert 'RPM-GPG-KEY-EPEL-$releasever' in epel['repo']
    fedora = _setup('https://dl.fedoraproject.org/pub/fedora/linux/')
    assert '/repository/rocky/updates/$releasever/Everything/$basearch/\n' in fedora['repo']
    assert layout('https://rpm.example.com/el10/') is None


def test_setup_snippets_for_other_upstreams_and_overrides() -> None:
    generic = _setup('https://rpm.example.com/el10/x86_64/')
    assert generic['repo'].startswith(
        '[rocky]\nname=rocky (Waystation)\nbaseurl=https://waystation.example.com/repository/rocky/\n'
    )
    assert 'gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-<vendor>\n' in generic['repo']
    assert 'disable' not in generic
    custom = _setup(
        'https://dl.rockylinux.org/pub/rocky/',
        repo_path='/10/extras/$basearch/os',
        gpg_key='https://rpm.example.com/key',
    )
    assert 'baseurl=https://waystation.example.com/repository/rocky/10/extras/$basearch/os/\n' in custom['repo']
    assert 'gpgkey=https://rpm.example.com/key\n' in custom['repo']


def test_options_and_probe() -> None:
    assert YumOptions().repo_path == ''
    assert YumOptions(repo_path='./a//b/').repo_path == 'a/b/'
    for bad in ('a/../b', 'a b', 'x' * 201):
        with pytest.raises(ValidationError):
            YumOptions(repo_path=bad)
    with pytest.raises(ValidationError):
        YumOptions(gpg_key='not a url')
    with pytest.raises(ValidationError):
        YumOptions.model_validate({'flat': True})
    assert DESCRIPTOR.probe_path(YumOptions()) == ''
    assert DESCRIPTOR.probe_path(YumOptions(repo_path='$releasever/BaseOS/$basearch/os')) == ''
    assert (
        DESCRIPTOR.probe_path(YumOptions(repo_path='10/extras/x86_64/os')) == '10/extras/x86_64/os/repodata/repomd.xml'
    )
    assert DESCRIPTOR.probe_path(DESCRIPTOR.parse_options({'repo_path': 'os'})) == 'os/repodata/repomd.xml'


# ---------------------------------------------------------------------------- RPM version order
@pytest.mark.parametrize(
    ('older', 'newer'),
    [
        ('1.0', '1.0.1'),
        ('1.0~rc1', '1.0'),
        ('1.0~rc1', '1.0~rc2'),
        ('1.0', '1.0^git1'),
        ('1.0^git1', '1.0.1'),
        ('1.0a', '1.0.1'),
        ('1.0', '1.0a'),
        ('a', 'ab'),
        ('a1', 'ab'),
        ('2', '10'),
        ('1.010', '1.11'),
        ('1_0', '1.1'),
        ('5.14', '5.14.0'),
        ('beta', '1'),
    ],
)
def test_version_key_follows_rpmvercmp(older: str, newer: str) -> None:
    assert evr.version_key(older) < evr.version_key(newer)


def test_equal_versions_and_evr_order() -> None:
    assert evr.version_key('1.01') == evr.version_key('1.1')
    assert evr.version_key('1.0') == evr.version_key('1_0')
    assert evr.evr_key('1', '1.0', '1') > evr.evr_key('0', '9.9', '9')
    assert evr.evr_key(None, '1.0', '2.el10') > evr.evr_key('0', '1.0', '1.el10')
    assert evr.evr_key('0', '1.0', '10.el10') > evr.evr_key('0', '1.0', '9.el10')
    assert evr.evr_key('x', '1', '1') == evr.evr_key('0', '1', '1')
    assert evr.version_key('1' + '0' * 120) > evr.version_key('9')
    assert evr.evr_string('0', '1.0', '1.el10') == '1.0-1.el10'
    assert evr.evr_string('2', '1.0', '1') == '2:1.0-1'
    assert evr.evr_string(None, '1.0', '') == '1.0'


# ---------------------------------------------------------------------------- repomd_keys option
def test_repomd_keys_option_takes_only_valid_public_keys() -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = openpgp.parse_public_key(openpgp.rsa_key_body(private.public_key(), 1_700_000_000))
    assert key is not None
    cert = openpgp.certificate(private, key, 'test')
    assert YumOptions(repomd_keys=f'  {cert}  ').repomd_keys == cert.strip() + '\n'
    assert YumOptions(repomd_keys='   ').repomd_keys is None
    with pytest.raises(ValidationError, match='OpenPGP'):
        YumOptions(repomd_keys='not a key')
