"""Terraform provider network mirror (T-P5-TFP) units: paths, origin documents, rendered mirror documents, the h1
hash, the signature check, options and "Set me up"."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile

import pytest
from pydantic import ValidationError
from ws_openpgp import PgpKey, Sub, armor, cert, detached
from ws_testing import make_repo

from waystation_common.cleanup.plan import coordinates
from waystation_common.cleanup.versions import is_prerelease, sort_key
from waystation_common.errors import DigestMismatch, UpstreamError
from waystation_server.formats.base import SetupContext
from waystation_server.formats.terraform import paths, registry, ziphash
from waystation_server.formats.terraform.descriptor import (
    DESCRIPTOR,
    TerraformOptions,
    allowed_origins,
    origin_scheme,
)
from waystation_server.formats.terraform.paths import Op, Provider
from waystation_server.formats.terraform.plugin import signature_check
from waystation_server.repos.model import RepoType

NULL = Provider('registry.terraform.io', 'hashicorp', 'null')


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_routes() -> None:
    index = paths.parse_route('registry.terraform.io/hashicorp/null/index.json')
    assert index is not None and index.op is Op.INDEX and index.provider == NULL
    upper = paths.parse_route('Registry.Terraform.IO/HashiCorp/Null/index.json')
    assert upper is not None and upper.provider == NULL  # host names, namespaces and types are lower-cased
    version = paths.parse_route('registry.terraform.io/hashicorp/null/3.2.0-rc.1+build.5.json')
    assert version is not None and version.op is Op.VERSION and version.version == '3.2.0-rc.1+build.5'
    archive = paths.parse_route('example.com:8443/acme/thing/1.0.0/linux/amd64/terraform-provider-thing_1.0.0.zip')
    assert archive is not None and archive.op is Op.ARCHIVE
    assert (archive.provider.hostname, archive.os, archive.arch) == ('example.com:8443', 'linux', 'amd64')
    for bad in (
        'registry.terraform.io/hashicorp/null',
        'registry.terraform.io/hashicorp/null/versions',
        'registry.terraform.io/hashicorp/null/1.0.json.json',
        'registry.terraform.io/hashicorp/null/01.0.0.json',
        'registry.terraform.io/hashicorp/-null/index.json',
        'registry.terraform.io/hashi_corp/null/index.json',
        'registry..terraform.io/hashicorp/null/index.json',
        'example.com:99999/acme/thing/index.json',
        'example.com:0/acme/thing/index.json',
        'registry.terraform.io/hashicorp/null/1.0.0/linux/amd64/file.tar.gz',
        'registry.terraform.io/hashicorp/null/1.0.0/linux_x/amd64/file.zip',
        'registry.terraform.io/hashicorp/null/x/linux/amd64/file.zip',
        'registry.terraform.io/hashicorp/null/1.0.0/download/linux/amd64',
        'registry.terraform.io/hashicorp/null/1.0.0/SHA256SUMS',
    ):
        assert paths.parse_route(bad) is None, bad
    assert paths.parse_address(' registry.terraform.io/hashicorp/null ') == NULL
    assert paths.parse_address('hashicorp/null') is None
    assert paths.is_hostname('a' * 63 + '.example.com') and not paths.is_hostname('a' * 254)


def test_asset_keys() -> None:
    assert paths.discovery_key('registry.terraform.io') == 'registry.terraform.io/.well-known/terraform.json'
    assert paths.versions_key(NULL) == 'registry.terraform.io/hashicorp/null/versions'
    assert paths.download_key(NULL, '1.0.0', 'linux', 'amd64') == 'registry.terraform.io/hashicorp/null/1.0.0/download/linux/amd64'
    assert paths.sums_key(NULL, '1.0.0').endswith('/1.0.0/SHA256SUMS')
    assert paths.signature_key(NULL, '1.0.0').endswith('/1.0.0/SHA256SUMS.sig')
    key = paths.archive_key(NULL, '1.0.0', 'linux', 'amd64', 'p.zip')
    route = paths.parse_route(key)  # an archive's asset key is its client path
    assert route is not None and route.op is Op.ARCHIVE
    assert paths.archive_url('1.0.0', 'linux', 'amd64', 'p.zip') == '1.0.0/linux/amd64/p.zip'


def test_discovery() -> None:
    assert registry.discovery_url('registry.terraform.io') == 'https://registry.terraform.io/.well-known/terraform.json'
    doc = json.dumps({'modules.v1': '/v1/modules/', 'providers.v1': '/v1/providers/'}).encode()
    assert registry.parse_discovery(doc, 'registry.terraform.io') == 'https://registry.terraform.io/v1/providers/'
    absolute = json.dumps({'providers.v1': 'https://api.example.com/tf/providers'}).encode()
    assert registry.parse_discovery(absolute, 'example.com') == 'https://api.example.com/tf/providers/'
    assert registry.parse_discovery(doc, '127.0.0.1:8080', 'http') == 'http://127.0.0.1:8080/v1/providers/'
    for bad in (b'nope', b'[]', b'{"modules.v1": "/m/"}', b'{"providers.v1": "ftp://x/"}', b'{"providers.v1": "/p?x=1"}'):
        with pytest.raises(UpstreamError):
            registry.parse_discovery(bad, 'example.com')
    with pytest.raises(UpstreamError, match='too large'):
        registry.parse_discovery(b' ' * (registry.MAX_DISCOVERY + 1), 'example.com')
    with pytest.raises(UpstreamError):
        registry.parse_discovery(b'{"providers.v1": "https://user:pw@example.com/p/"}', 'example.com')


def test_versions_and_index() -> None:
    doc = {
        'versions': [
            {'version': '3.2.0', 'protocols': ['5.0'], 'platforms': [{'os': 'linux', 'arch': 'amd64'}, {'os': 'darwin', 'arch': 'arm64'}]},
            {'version': '3.10.0-beta.1', 'protocols': ['5.0', 6], 'platforms': [{'os': 'linux', 'arch': 'amd64'}, 'x']},
            {'version': '3.9.1', 'platforms': None},
            {'version': 'not-a-version'},
            {'version': '1.0.0', 'platforms': [{'os': 'Linux', 'arch': 'amd64'}]},
            'garbage',
        ]
    }
    parsed = registry.parse_versions(json.dumps(doc).encode())
    assert set(parsed) == {'3.2.0', '3.10.0-beta.1', '3.9.1', '1.0.0'}
    assert parsed['3.2.0'].platforms == (('darwin', 'arm64'), ('linux', 'amd64'))
    assert parsed['3.10.0-beta.1'].protocols == ('5.0',)
    assert parsed['1.0.0'].platforms == ()
    assert registry.newest_first(list(parsed)) == ['3.10.0-beta.1', '3.9.1', '3.2.0', '1.0.0']
    assert registry.latest_of(list(parsed)) == '3.9.1'
    assert registry.latest_of(['1.0.0-rc.1']) == '1.0.0-rc.1'
    assert registry.latest_of([]) is None
    index = json.loads(registry.render_index(parsed))
    assert list(index['versions']) == ['3.10.0-beta.1', '3.9.1', '3.2.0', '1.0.0']
    assert index['versions']['3.2.0'] == {}
    for bad in (b'{}', b'{"versions": {}}', b'x'):
        with pytest.raises(UpstreamError):
            registry.parse_versions(bad)
    many = json.dumps({'versions': [{'version': '1.0.0'}] * (registry.MAX_VERSION_COUNT + 1)}).encode()
    with pytest.raises(UpstreamError, match='too long'):
        registry.parse_versions(many)


def download_doc(**changes: object) -> bytes:
    doc: dict[str, object] = {
        'protocols': ['5.0'],
        'os': 'linux',
        'arch': 'amd64',
        'filename': 'terraform-provider-null_3.2.0_linux_amd64.zip',
        'download_url': 'https://releases.example.com/null/3.2.0/terraform-provider-null_3.2.0_linux_amd64.zip',
        'shasums_url': 'https://releases.example.com/null/3.2.0/terraform-provider-null_3.2.0_SHA256SUMS',
        'shasums_signature_url': 'https://releases.example.com/null/3.2.0/terraform-provider-null_3.2.0_SHA256SUMS.sig',
        'shasum': 'A' * 64,
        'signing_keys': {'gpg_public_keys': [{'key_id': 'X', 'ascii_armor': 'KEY'}, {'ascii_armor': 5}, 'x']},
    }
    doc.update(changes)
    return json.dumps(doc).encode()


def test_download_document() -> None:
    known = registry.parse_download(download_doc(), 'linux', 'amd64')
    assert known.shasum == 'a' * 64 and known.keys == ('KEY',)
    assert known.filename == 'terraform-provider-null_3.2.0_linux_amd64.zip'
    no_keys = registry.parse_download(download_doc(signing_keys=None), 'linux', 'amd64')
    assert no_keys.keys == ()
    for changes in (
        {'os': 'darwin'},
        {'filename': '../evil.zip'},
        {'shasum': 'x'},
        {'download_url': 'file:///etc/passwd'},
        {'shasums_url': None},
        {'shasums_signature_url': 'https://' + 'a' * 5000},
    ):
        with pytest.raises(UpstreamError):
            registry.parse_download(download_doc(**changes), 'linux', 'amd64')
    with pytest.raises(UpstreamError):
        registry.parse_download(b'[]', 'linux', 'amd64')


SUMS = (
    f'{"1" * 64}  terraform-provider-null_3.2.0_darwin_arm64.zip\n'
    f'{"a" * 64}  terraform-provider-null_3.2.0_linux_amd64.zip\n'
    f'{"2" * 64} *terraform-provider-null_3.2.0_windows_386.zip\n'
    f'{"3" * 64}  terraform-provider-null_3.2.0_manifest.json\n'
    f'{"4" * 64}  other_3.2.0_linux_arm64.zip\n'
).encode()


def test_sums_archives_and_version_document() -> None:
    listed = registry.parse_sums(SUMS)
    assert listed['terraform-provider-null_3.2.0_windows_386.zip'] == '2' * 64
    known = registry.parse_download(download_doc(), 'linux', 'amd64')
    items = registry.archives(listed, known, ())
    assert [(a.os, a.arch) for a in items] == [('darwin', 'arm64'), ('linux', 'amd64'), ('windows', '386')]
    limited = registry.archives(listed, known, (('linux', 'amd64'), ('windows', '386')))
    assert [(a.os, a.arch) for a in limited] == [('linux', 'amd64'), ('windows', '386')]
    doc = json.loads(registry.render_version('3.2.0', items, {'linux_amd64': 'h1:abc='}))
    assert doc['archives']['linux_amd64'] == {
        'url': '3.2.0/linux/amd64/terraform-provider-null_3.2.0_linux_amd64.zip',
        'hashes': ['h1:abc=', f'zh:{"a" * 64}'],
    }
    assert doc['archives']['darwin_arm64']['hashes'] == [f'zh:{"1" * 64}']
    odd = registry.parse_download(download_doc(filename='provider.zip'), 'linux', 'amd64')
    only = registry.archives({'provider.zip': 'a' * 64}, odd, ())
    assert [(a.os, a.arch, a.filename) for a in only] == [('linux', 'amd64', 'provider.zip')]
    with pytest.raises(UpstreamError, match='shasum'):
        registry.archives({'provider.zip': 'b' * 64}, odd, ())
    for bad in (b'', b'zz  file\n', b'\xff', b'a' * (registry.MAX_SUMS + 1), f'{"1" * 64}  x\n{"2" * 64}  x\n'.encode()):
        with pytest.raises(UpstreamError):
            registry.parse_sums(bad)


def test_zip_h1() -> None:
    files = {'terraform-provider-null_v3.2.0_x5': b'\x7fELF' + b'binary' * 1000, 'LICENSE': b'MPL'}
    summary = ''.join(f'{hashlib.sha256(files[n]).hexdigest()}  {n}\n' for n in sorted(files))
    expected = 'h1:' + base64.b64encode(hashlib.sha256(summary.encode()).digest()).decode()
    assert ziphash.zip_h1(io.BytesIO(make_zip(files))) == expected
    with pytest.raises(ziphash.NotHashable):
        ziphash.zip_h1(io.BytesIO(b'not a zip'))
    with pytest.raises(ziphash.NotHashable):
        ziphash.zip_h1(io.BytesIO(make_zip({'a\nb': b'x'})))


def test_zip_h1_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    data = make_zip({'a': b'x' * 100, 'b': b'y'})
    monkeypatch.setattr(ziphash, 'MAX_FILES', 1)
    with pytest.raises(ziphash.NotHashable, match='too many'):
        ziphash.zip_h1(io.BytesIO(data))
    monkeypatch.setattr(ziphash, 'MAX_FILES', 10)
    monkeypatch.setattr(ziphash, 'MAX_UNCOMPRESSED', 50)
    with pytest.raises(ziphash.NotHashable, match='too large'):
        ziphash.zip_h1(io.BytesIO(data))
    corrupt = bytearray(make_zip({'a': b'hello world' * 100}))
    corrupt[40:60] = b'\x00' * 20
    with pytest.raises(ziphash.NotHashable):
        ziphash.zip_h1(io.BytesIO(bytes(corrupt)))


def test_signature_check() -> None:
    primary, signing = PgpKey(), PgpKey('rsa')
    keys = (armor(cert(primary, subkeys=(Sub(signing),))).decode(),)
    signature_check(detached(signing, SUMS), keys)(io.BytesIO(SUMS))
    with pytest.raises(DigestMismatch, match='signature'):
        signature_check(detached(signing, SUMS + b'x'), keys)(io.BytesIO(SUMS))
    with pytest.raises(DigestMismatch, match='no signing key'):
        signature_check(detached(signing, SUMS), ())(io.BytesIO(SUMS))
    with pytest.raises(DigestMismatch, match='malformed'):
        signature_check(detached(signing, SUMS), ('-----BEGIN PGP PUBLIC KEY BLOCK-----\nxx',))(io.BytesIO(SUMS))
    other = (armor(cert(PgpKey())).decode(),)
    with pytest.raises(DigestMismatch, match='signature'):
        signature_check(detached(signing, SUMS), other)(io.BytesIO(SUMS))


def test_cleanup_coordinates_and_order() -> None:
    coord = coordinates('terraform', 'registry.terraform.io/hashicorp/null/3.2.0/linux/amd64/p_3.2.0_linux_amd64.zip')
    assert (coord.name, coord.version, coord.group, coord.ordered_by_version) == (
        'registry.terraform.io/hashicorp/null',
        '3.2.0',
        'registry.terraform.io/hashicorp/null',
        True,
    )
    other = coordinates('terraform', 'registry.terraform.io/hashicorp/null/3.2.0/SHA256SUMS')
    assert other.ordered_by_version is False
    assert sort_key('terraform', '3.10.0') > sort_key('terraform', '3.9.0') > sort_key('terraform', '3.9.0-rc.1')
    assert is_prerelease('terraform', '1.0.0-beta.2') and not is_prerelease('terraform', '1.0.0')


def test_options_and_origins() -> None:
    options = TerraformOptions()
    assert options.origins == ['registry.terraform.io', 'registry.opentofu.org'] and options.verify_signatures
    custom = TerraformOptions(origins=['Registry.Example.com', 'registry.example.com', 'tf.example.com:8443'])
    assert custom.origins == ['registry.example.com', 'tf.example.com:8443']
    with pytest.raises(ValidationError):
        TerraformOptions(origins=['https://registry.example.com'])
    with pytest.raises(ValidationError):
        TerraformOptions(unknown=True)  # type: ignore[call-arg]
    assert allowed_origins(options, 'https://registry.example.com:443/tf/') == {
        'registry.terraform.io',
        'registry.opentofu.org',
        'registry.example.com',
    }
    assert 'tf.example.com:8080' in allowed_origins(options, 'http://tf.example.com:8080/')
    assert allowed_origins(options, None) == {'registry.terraform.io', 'registry.opentofu.org'}
    assert allowed_origins(options, 'not a url') == {'registry.terraform.io', 'registry.opentofu.org'}
    assert origin_scheme('tf.example.com:8080', 'http://tf.example.com:8080/') == 'http'
    assert origin_scheme('registry.terraform.io', 'http://tf.example.com:8080/') == 'https'
    assert origin_scheme('registry.terraform.io', None) == 'https'


def test_descriptor_and_setup_snippets() -> None:
    assert DESCRIPTOR.types == frozenset({RepoType.PROXY})
    assert DESCRIPTOR.probe_path(TerraformOptions()) == '.well-known/terraform.json'
    repo = make_repo('tf-mirror', 'terraform')
    ctx = SetupContext(repo=repo, base_url='https://ws.example.com', registry_host='ws.example.com', username='me')
    clients = DESCRIPTOR.setup_snippets(ctx)
    assert [c.id for c in clients] == ['terraform', 'tofu']
    config = clients[0].snippets[0].content
    assert 'url = "https://ws.example.com/repository/tf-mirror/"' in config
    assert 'credentials "ws.example.com"' in config and '{{TOKEN}}' in config
    assert 'TF_TOKEN_ws_example_com={{TOKEN}}' in clients[0].snippets[2].content
    plain = SetupContext(repo=repo, base_url='http://ws.example.com', registry_host='ws.example.com', username=None)
    assert 'only https://' in (DESCRIPTOR.setup_snippets(plain)[1].snippets[0].description or '')
