"""Swift Package Registry units (T-P5-SWIFT): naming rules and tag versions, routes, API version negotiation, problem
details, release documents and links, release metadata, source repository URLs, the Git ref advertisement, the
deterministic SCM archive build and the published-archive checks, signer recording, options and "Set me up"."""

import datetime as dt
import hashlib
import io
import json
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from waystation_common import swift
from waystation_common.cleanup.model import Criteria
from waystation_common.cleanup.plan import AssetRow, plan_assets
from waystation_common.components import key_for
from waystation_common.versionsort import version_sort
from waystation_server.formats.base import SetupContext
from waystation_server.formats.swift import api, gitrefs
from waystation_server.formats.swift.archive import (
    ZIP_EPOCH,
    ArchiveRefused,
    build_from_tarball,
    inspect_zip,
    tools_version,
)
from waystation_server.formats.swift.descriptor import DESCRIPTOR, SwiftOptions
from waystation_server.formats.swift.hosted import signer_of
from waystation_server.formats.swift.plugin import Route, map_tags, parse_route
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.security.actions import Action

MANIFEST = b'// swift-tools-version:5.9\nimport PackageDescription\n\nlet package = Package(name: "Demo")\n'
URLS = api.Urls('https://ws.example.com/repository/swift/')


# ---------------------------------------------------------------------------- naming rules
def test_scopes_names_versions_and_tags() -> None:
    assert swift.valid_scope('mona') and swift.valid_scope('a-b-c') and swift.valid_scope('A' * 39)
    for bad in ('-a', 'a-', 'a--b', 'a_b', 'a.b', '', 'a' * 40, '_scm'):
        assert not swift.valid_scope(bad)
    assert swift.valid_name('LinkedList') and swift.valid_name('swift-argument_parser')
    for bad in ('a__b', '_a', 'a-', 'a.b', 'a/b', 'x' * 101):
        assert not swift.valid_name(bad)
    assert swift.valid_version('1.0.0-beta.1+build.2') and not swift.valid_version('1.0')
    assert swift.version_of_tag('v1.2.3') == '1.2.3' and swift.version_of_tag('1.2') == '1.2.0'
    assert swift.version_of_tag('2.0.0-rc.1') == '2.0.0-rc.1' and swift.version_of_tag('v4') is None
    assert swift.version_of_tag('release-1.0') is None and swift.version_of_tag('01.0.0') is None
    pkg = swift.PackageId('Mona', 'LinkedList')
    assert pkg.id == 'Mona.LinkedList' and pkg.prefix == 'mona/linkedlist'
    assert pkg.archive_path('1.0.0') == 'mona/linkedlist/1.0.0.zip'
    assert pkg.manifest_path('1.0.0', 'Package@swift-5.7.swift') == 'mona/linkedlist/1.0.0/Package@swift-5.7.swift'
    assert swift.parse_id('mona.LinkedList') == pkg.__class__('mona', 'LinkedList')
    assert swift.parse_id('mona') is None and swift.parse_id('mo_na.x') is None
    assert swift.parse_archive_path('mona/linkedlist/1.0.0.zip') == ('mona', 'linkedlist', '1.0.0')
    for other in ('_scm/mona/x/1.0.0.tar.gz', 'mona/linkedlist/1.0.0/Package.swift', 'mona/x/1.0.zip'):
        assert swift.parse_archive_path(other) is None


def test_versions_sort_by_semver_and_components() -> None:
    ordered = ['1.0.0-alpha', '1.0.0-beta.2', '1.0.0-beta.10', '1.0.0', '1.2.0', '10.0.0']
    shuffled = [ordered[i] for i in (3, 5, 0, 4, 2, 1)]
    assert sorted(shuffled, key=lambda v: version_sort('swift', v)) == ordered
    key = key_for('swift', 'mona/linkedlist/1.2.0.zip')
    assert key is not None and (key.namespace, key.name, key.version) == ('mona', 'mona.linkedlist', '1.2.0')
    assert key_for('swift', 'mona/linkedlist/1.2.0/Package.swift') is None
    assert key_for('swift', '_scm/mona/linkedlist/1.2.0.tar.gz') is None
    assert key_for('swift', 'mona/linkedlist/1.2.0.zip', content=False) is None


def test_cleanup_never_removes_release_files() -> None:
    old = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    rows = [
        AssetRow('1', 'mona/linkedlist/1.0.0.zip', 'content', 10, 'a' * 64, old),
        AssetRow('2', 'mona/linkedlist/1.0.0/Package.swift', 'content', 1, 'b' * 64, old),
        AssetRow('3', '_scm/mona/linkedlist/1.0.0.tar.gz', 'content', 10, 'c' * 64, old),
    ]
    found = plan_assets('swift', Criteria(published_older_than_days=1), rows, dt.datetime.now(dt.UTC))
    assert [c.path for c in found] == ['_scm/mona/linkedlist/1.0.0.tar.gz']


# ---------------------------------------------------------------------------- routes
@pytest.mark.parametrize(
    ('path', 'route'),
    [
        ('', Route('root')),
        ('login', Route('login')),
        ('identifiers', Route('identifiers')),
        ('mona/LinkedList', Route('releases', 'mona', 'LinkedList')),
        ('mona/LinkedList.json', Route('releases', 'mona', 'LinkedList')),
        ('mona/LinkedList/1.1.1', Route('release', 'mona', 'LinkedList', '1.1.1')),
        ('mona/LinkedList/1.1.1.json', Route('release', 'mona', 'LinkedList', '1.1.1')),
        ('mona/LinkedList/1.1.1-rc.json', Route('release', 'mona', 'LinkedList', '1.1.1-rc.json')),
        ('mona/LinkedList/1.1.1.zip', Route('archive', 'mona', 'LinkedList', '1.1.1')),
        ('mona/LinkedList/1.1.1/Package.swift', Route('manifest', 'mona', 'LinkedList', '1.1.1')),
    ],
)
def test_routes(path: str, route: Route) -> None:
    assert parse_route(path) == route


@pytest.mark.parametrize(
    'path',
    ['mona', 'mo_na/x', 'mona/x.y', 'mona/x/1.0', 'mona/x/1.0.0/Package@swift-5.swift', 'mona/x/1.0.0/a/b', 'a/b/c/d'],
)
def test_routes_refused(path: str) -> None:
    assert parse_route(path) is None


def test_tags_map_to_versions() -> None:
    tags = {'1.0.0': 'a' * 40, 'v1.0.0': 'b' * 40, 'v2.0': 'c' * 40, '2.0': 'd' * 40, 'nightly': 'e' * 40}
    assert map_tags(tags) == {'1.0.0': ('1.0.0', 'a' * 40), '2.0.0': ('2.0', 'd' * 40)}


# ---------------------------------------------------------------------------- wire format
def test_accept_negotiation() -> None:
    for ok in (
        None,
        '',
        '*/*',
        'application/json',
        'application/vnd.swift.registry.v1+json',
        'application/vnd.swift.registry+zip',
        'application/vnd.swift.registry.v1+swift; q=0.9',
    ):
        api.check_accept(ok)
    with pytest.raises(api.Problem) as unsupported:
        api.check_accept('application/vnd.swift.registry.v2+json')
    assert unsupported.value.status == 415
    for bad in ('application/vnd.swift.registry.vx+json', 'application/vnd.swift.registry.v1+xml'):
        with pytest.raises(api.Problem) as invalid:
            api.check_accept(bad)
        assert invalid.value.status == 400


def test_problem_details() -> None:
    response = api.problem(404, 'release not found')
    assert response.status_code == 404 and response.media_type == 'application/problem+json'
    assert response.headers['content-version'] == '1' and response.headers['content-language'] == 'en'
    assert json.loads(bytes(response.body)) == {
        'type': 'about:blank',
        'title': 'Not Found',
        'status': 404,
        'detail': 'release not found',
    }


def test_release_documents_and_links() -> None:
    doc = api.releases_doc(URLS, 'mona', 'LinkedList', [('1.1.0', False), ('1.0.0', True)])
    assert doc['releases']['1.1.0'] == {'url': 'https://ws.example.com/repository/swift/mona/LinkedList/1.1.0'}
    assert doc['releases']['1.0.0']['problem']['status'] == 410
    record = {
        'scope': 'mona',
        'name': 'LinkedList',
        'version': '1.1.0',
        'checksum': 'ab' * 32,
        'metadata': {'description': 'x'},
        'published_at': dt.datetime(2026, 11, 22, 1, 2, 3, tzinfo=dt.UTC),
        'signature': {'format': 'cms-1.0.0', 'base64': 'c2ln'},
    }
    info = api.release_doc(record)
    assert info['id'] == 'mona.LinkedList' and info['publishedAt'] == '2026-11-22T01:02:03.000Z'
    assert info['resources'] == [
        {
            'name': 'source-archive',
            'type': 'application/zip',
            'checksum': 'ab' * 32,
            'signing': {'signatureBase64Encoded': 'c2ln', 'signatureFormat': 'cms-1.0.0'},
        }
    ]
    links = api.neighbours(URLS, 'mona', 'LinkedList', ['2.0.0', '1.1.0', '1.0.0'], '1.1.0')
    header = api.link_header(links)
    assert '/2.0.0>; rel="latest-version"' in header and '/2.0.0>; rel="successor-version"' in header
    assert '/1.0.0>; rel="predecessor-version"' in header
    assert api.digest_header(hashlib.sha256(b'x').hexdigest()).startswith('sha-256=')
    assert URLS.manifest('mona', 'LinkedList', '1.0.0', '5.7').endswith('/1.0.0/Package.swift?swift-version=5.7')


def test_release_metadata() -> None:
    assert api.check_metadata('') == {}
    good = {'description': 'd', 'repositoryURLs': ['https://github.com/mona/LinkedList'], 'author': {'name': 'M'}}
    assert api.check_metadata(json.dumps(good)) == good
    for bad in (
        '{',
        '[]',
        '{"description": 1}',
        '{"repositoryURLs": "x"}',
        '{"author": {}}',
        '{"author": {"name": "a", "organization": {}}}',
    ):
        with pytest.raises(api.Problem) as refused:
            api.check_metadata(bad)
        assert refused.value.status == 422
    with pytest.raises(api.Problem) as large:
        api.check_metadata(json.dumps({'description': 'x' * (api.MAX_METADATA + 1)}))
    assert large.value.status == 413


def test_source_repository_urls() -> None:
    same = (
        'https://github.com/Mona/LinkedList.git',
        'https://github.com/mona/linkedlist/',
        'git@github.com:mona/LinkedList.git',
        'ssh://git@github.com/mona/LinkedList',
        'https://user@GitHub.com:443/mona/LinkedList',
    )
    assert {api.url_key(u) for u in same} == {'github.com/mona/linkedlist'}
    assert api.url_parts('https://github.com/Mona/LinkedList.git') == ('github.com', 'Mona/LinkedList')
    for bad in ('', 'file:///etc/passwd', 'https://github.com/', 'https://github.com/a/../b', 'not a url'):
        assert api.url_key(bad) is None


# ---------------------------------------------------------------------------- git refs
def pkt(line: str) -> bytes:
    data = line.encode()
    return f'{len(data) + 4:04x}'.encode() + data


def advertisement(refs: list[tuple[str, str]]) -> bytes:
    out = pkt('# service=git-upload-pack\n') + b'0000'
    for i, (oid, ref) in enumerate(refs):
        out += pkt(f'{oid} {ref}' + ('\x00multi_ack side-band-64k\n' if i == 0 else '\n'))
    return out + b'0000'


def test_ref_advertisement() -> None:
    data = advertisement(
        [
            ('1' * 40, 'HEAD'),
            ('1' * 40, 'refs/heads/main'),
            ('2' * 40, 'refs/tags/1.0.0'),
            ('3' * 40, 'refs/tags/v1.1.0'),
            ('4' * 40, 'refs/tags/v1.1.0^{}'),
            ('5' * 40, 'refs/pull/1/head'),
            ('6' * 40, 'refs/tags/bad name'),
        ]
    )
    tags = gitrefs.parse_advertisement(data)
    assert tags == {'1.0.0': '2' * 40, 'v1.1.0': '4' * 40}
    assert gitrefs.load_compact(gitrefs.compact(tags)) == tags
    empty = pkt('# service=git-upload-pack\n') + b'0000' + pkt('0' * 40 + ' capabilities^{}\x00agent=x\n') + b'0000'
    assert gitrefs.parse_advertisement(empty) == {}
    for bad in (b'', b'zzzz', b'00ffabc', pkt('ERR access denied\n'), pkt('nonsense line\n'), b'<html>'):
        with pytest.raises(ValueError):
            gitrefs.parse_advertisement(bad)


# ---------------------------------------------------------------------------- archives
def tarball(entries: list[tuple[str, bytes | None, int, str]], *, mtime: int = 1_700_000_000) -> bytes:
    """``(name, data or None for a directory, mode, symlink target)`` entries as a .tar.gz."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, data, mode, link in entries:
            info = tarfile.TarInfo(name)
            info.mtime = mtime
            info.mode = mode
            if link:
                info.type = tarfile.SYMTYPE
                info.linkname = link
                tar.addfile(info)
            elif data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


PACKAGE = [
    ('Demo-1.0.0/', None, 0o755, ''),
    ('Demo-1.0.0/Package.swift', MANIFEST, 0o644, ''),
    ('Demo-1.0.0/Package@swift-5.7.swift', b'// swift-tools-version: 5.7\n', 0o644, ''),
    ('Demo-1.0.0/Sources/Demo/Demo.swift', b'public let x = 1\n', 0o644, ''),
    ('Demo-1.0.0/scripts/run.sh', b'#!/bin/sh\n', 0o755, ''),
    ('Demo-1.0.0/link.swift', None, 0o777, 'Sources/Demo/Demo.swift'),
    ('Demo-1.0.0/escape', None, 0o777, '../../etc/passwd'),
]


def build(tmp_path: Path, data: bytes, name: str = 'out.zip') -> tuple[bytes, list[str]]:
    src = tmp_path / f'{name}.tar.gz'
    src.write_bytes(data)
    out = tmp_path / name
    summary = build_from_tarball(src, out, 'Demo', tmp_path)
    return out.read_bytes(), [m.filename for m in summary.manifests]


def test_scm_archive_is_deterministic(tmp_path: Path) -> None:
    first, manifests = build(tmp_path, tarball(PACKAGE), 'a.zip')
    shuffled = [PACKAGE[0], *reversed(PACKAGE[1:])]
    second, _ = build(tmp_path, tarball(shuffled, mtime=1), 'b.zip')
    assert first == second  # order and timestamps of the tarball do not matter
    assert manifests == ['Package.swift', 'Package@swift-5.7.swift']
    with zipfile.ZipFile(io.BytesIO(first)) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        assert names == sorted(names) and all(n.startswith('Demo/') for n in names)
        assert 'Demo/escape' not in names  # a symlink leaving the package is dropped
        assert all(i.date_time == ZIP_EPOCH for i in infos)
        modes = {i.filename: i.external_attr >> 16 for i in infos}
        assert modes['Demo/scripts/run.sh'] == stat.S_IFREG | 0o755
        assert modes['Demo/Package.swift'] == stat.S_IFREG | 0o644
        assert stat.S_ISLNK(modes['Demo/link.swift'])
        assert zf.read('Demo/link.swift') == b'Sources/Demo/Demo.swift'
    summary = inspect_zip(tmp_path / 'a.zip')  # the proxy's archives pass the publish checks too
    assert summary.top == 'Demo' and summary.manifest() is not None


def test_scm_archive_refusals(tmp_path: Path) -> None:
    for entries, message in (
        ([('a/Package.swift', MANIFEST, 0o644, ''), ('b/x', b'', 0o644, '')], 'more than one top-level'),
        ([('a/../../x', b'', 0o644, '')], 'unsafe path'),
        ([('/abs/x', b'', 0o644, '')], 'unsafe path'),
        ([('a/README.md', b'hi', 0o644, '')], 'no Package.swift'),
    ):
        with pytest.raises(ArchiveRefused, match=message):
            build(tmp_path, tarball(entries))
    with pytest.raises(ArchiveRefused, match='not a readable'):
        build(tmp_path, b'not a tarball')
    assert not list(tmp_path.glob('swift-build-*'))  # work directories are removed


def zip_bytes(entries: list[tuple[str, bytes]], *, symlinks: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, data in entries:
            zf.writestr(name, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return buf.getvalue()


def test_published_archive_checks(tmp_path: Path) -> None:
    path = tmp_path / 'a.zip'
    path.write_bytes(zip_bytes([('LinkedList/Package.swift', MANIFEST), ('LinkedList/Sources/a.swift', b'')]))
    summary = inspect_zip(path)
    assert summary.top == 'LinkedList' and summary.manifest() is not None
    assert summary.manifest().tools_version == '5.9'  # type: ignore[union-attr]
    for entries, symlinks, message in (
        ([('LinkedList/Sources/a.swift', b'')], None, 'valid manifest'),
        ([('LinkedList/Package.swift', b'import PackageDescription\n')], None, 'swift-tools-version'),
        ([('A/Package.swift', MANIFEST), ('B/x', b'')], None, 'one top-level folder'),
        ([('A/Package.swift', MANIFEST), ('A/../x', b'')], None, 'unsafe path'),
        ([('A/Package.swift', MANIFEST), ('A/x', b''), ('A/x', b'')], None, 'duplicate entry'),
        ([('A/Package.swift', MANIFEST)], {'A/evil': '../../../etc/passwd'}, 'leaves the package'),
    ):
        path.write_bytes(zip_bytes(entries, symlinks=symlinks))
        with pytest.raises(ArchiveRefused, match=message):
            inspect_zip(path)
    path.write_bytes(b'PK\x03\x04 truncated')
    with pytest.raises(ArchiveRefused, match='not a readable zip'):
        inspect_zip(path)


def test_tools_version() -> None:
    assert tools_version(b'\xef\xbb\xbf// swift-tools-version:5.9\n') == '5.9'
    assert tools_version(b'\n\n//swift-tools-version: 6.0.1\nimport X') == '6.0.1'
    assert tools_version(b'// SWIFT-TOOLS-VERSION:4.2;') == '4.2'
    assert tools_version(b'import PackageDescription\n// swift-tools-version:5.9') is None
    assert tools_version(b'') is None


def test_signer_is_recorded() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Mona Lisa Octocat')])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    signature = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(b'archive')
        .add_signer(cert, key, hashes.SHA256())
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature])
    )
    assert signer_of(signature) == 'CN=Mona Lisa Octocat'
    with pytest.raises(ArchiveRefused, match='CMS'):
        signer_of(b'not a signature')


# ---------------------------------------------------------------------------- options and "Set me up"
def test_options() -> None:
    options = SwiftOptions.model_validate({'scopes': {'PointFree': 'pointfreeco', 'grp': '/group/sub/'}})
    assert options.scopes == {'pointfree': 'pointfreeco', 'grp': 'group/sub'}
    for bad in (
        {'scopes': {'a_b': 'x'}},
        {'scopes': {'a': '../x'}},
        {'scopes': {'a': 'x', 'A': 'y'}},
        {'archive_layout': 'svn'},
    ):
        with pytest.raises(ValidationError):
            SwiftOptions.model_validate(bad)
    proxy_only = SwiftOptions(archive_layout='gitea', scopes={'a': 'b'}, mapped_scopes_only=True)
    assert len(DESCRIPTOR.check_options(proxy_only, RepoType.HOSTED)) == 3
    assert DESCRIPTOR.check_options(proxy_only, RepoType.PROXY) == []
    assert DESCRIPTOR.types == frozenset({RepoType.PROXY, RepoType.HOSTED})
    assert DESCRIPTOR.request_action('POST', 'login') is Action.READ
    assert DESCRIPTOR.request_action('POST', '') is Action.READ
    assert DESCRIPTOR.request_action('PUT', 'a/b/1.0.0') is None


def repo(kind: RepoType) -> RepoConfig:
    return RepoConfig.model_validate(
        {
            '_id': 'r1',
            'name': 'swift',
            'format': 'swift',
            'type': kind.value,
            'online': True,
            'created_at': dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            'updated_at': dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            **({'proxy': {'upstream_url': 'https://github.com/'}} if kind is RepoType.PROXY else {}),
        }
    )


def test_setup_snippets() -> None:
    hosted = DESCRIPTOR.setup_snippets(SetupContext(repo(RepoType.HOSTED), 'https://ws.example.com', 'ws', None))
    ids = [s.id for s in hosted[0].snippets]
    assert ids == ['swift-set', 'swift-login', 'swift-dependency', 'swift-resolve', 'swift-publish', 'swift-unpublish']
    text = '\n'.join(s.content for s in hosted[0].snippets)
    assert 'swift package-registry set https://ws.example.com/repository/swift/' in text
    assert '--token {{TOKEN}}' in text and 'swift package-registry publish' in text
    proxy = DESCRIPTOR.setup_snippets(SetupContext(repo(RepoType.PROXY), 'https://ws.example.com', 'ws', None))
    assert 'swift-publish' not in [s.id for s in proxy[0].snippets]
