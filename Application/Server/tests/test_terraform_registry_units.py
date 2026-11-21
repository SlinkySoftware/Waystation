"""Terraform module registry proxy and hosted repository (T-P5-TFM) units: registry routes, module documents, archive
sources, hosted archive checks, signed download URLs, cleanup/component coordinates and "Set me up"."""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from ws_testing import make_repo

from waystation_common.cleanup.plan import coordinates
from waystation_common.components import key_for
from waystation_common.errors import UpstreamError, ValidationFailed
from waystation_server.formats.base import SetupContext
from waystation_server.formats.terraform import modules, paths
from waystation_server.formats.terraform.descriptor import DESCRIPTOR, TerraformOptions
from waystation_server.formats.terraform.paths import HostedProvider, Module, RegistryOp
from waystation_server.repos.model import RepoType
from waystation_server.security.download_urls import TTL_S, DownloadUrls

GITHUB = frozenset({'github.com'})
VPC = Module('terraform-aws-modules', 'vpc', 'aws')


def tarball(files: dict[str, bytes], *, links: dict[str, str] | None = None, top: str = '') -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        if top:
            info = tarfile.TarInfo(top)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(f'{top}/{name}' if top else name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return buffer.getvalue()


# ---------------------------------------------------------------------------- routes
def test_module_routes() -> None:
    versions = paths.parse_registry_route('v1/modules/Terraform-AWS-Modules/VPC/AWS/versions')
    assert versions is not None and versions.op is RegistryOp.MODULE_VERSIONS and versions.module == VPC
    download = paths.parse_registry_route('v1/modules/terraform-aws-modules/vpc/aws/5.1.2/download')
    assert download is not None and download.op is RegistryOp.MODULE_DOWNLOAD and download.version == '5.1.2'
    archive = paths.parse_registry_route('v1/modules/terraform-aws-modules/vpc/aws/5.1.2/archive.zip')
    assert archive is not None and archive.op is RegistryOp.MODULE_ARCHIVE and archive.filename == 'archive.zip'
    upload = paths.parse_registry_route('v1/modules/acme/net_work/aws/1.0.0-rc.1')
    assert (
        upload is not None
        and upload.op is RegistryOp.MODULE_VERSION
        and upload.module == Module('acme', 'net_work', 'aws')
    )
    for bad in (
        'v1/modules/acme/vpc/aws',
        'v1/modules/acme/vpc/aws/latest',
        'v1/modules/acme/vpc/aws/1.0/download',
        'v1/modules/acme/vpc/aws-x/versions',
        'v1/modules/-acme/vpc/aws/versions',
        'v1/modules/acme/vpc/aws/1.0.0/other.tar.gz',
        'v1/modules/acme/vpc/aws/1.0.0/download/x',
        'v1/modules//vpc/aws/versions',
        'v1/other/acme/vpc/aws/versions',
        'v2/modules/acme/vpc/aws/versions',
    ):
        assert paths.parse_registry_route(bad) is None, bad


def test_provider_routes() -> None:
    provider = HostedProvider('acme', 'thing')
    versions = paths.parse_registry_route('v1/providers/ACME/thing/versions')
    assert versions is not None and versions.op is RegistryOp.PROVIDER_VERSIONS and versions.provider == provider
    download = paths.parse_registry_route('v1/providers/acme/thing/1.2.3/download/linux/arm64')
    assert download is not None and download.op is RegistryOp.PROVIDER_DOWNLOAD
    assert (download.os, download.arch) == ('linux', 'arm64')
    zip_route = paths.parse_registry_route(
        'v1/providers/acme/thing/1.2.3/terraform-provider-thing_1.2.3_linux_arm64.zip'
    )
    assert zip_route is not None and zip_route.op is RegistryOp.PROVIDER_FILE and zip_route.os == 'linux'
    sums = paths.parse_registry_route('v1/providers/acme/thing/1.2.3/terraform-provider-thing_1.2.3_SHA256SUMS.sig')
    assert sums is not None and sums.op is RegistryOp.PROVIDER_FILE and sums.os == ''
    release = paths.parse_registry_route('v1/providers/acme/thing/1.2.3')
    assert release is not None and release.op is RegistryOp.PROVIDER_VERSION
    for bad in (
        'v1/providers/acme/thing/1.2.3/terraform-provider-other_1.2.3_linux_arm64.zip',
        'v1/providers/acme/thing/1.2.3/terraform-provider-thing_1.2.4_linux_arm64.zip',
        'v1/providers/acme/thing/1.2.3/download/linux',
        'v1/providers/acme/thing/one/download/linux/amd64',
        'v1/providers/acme/versions',
        'v1/providers/acme/thing/1.2.3/README',
    ):
        assert paths.parse_registry_route(bad) is None, bad
    assert (
        paths.provider_filename(provider, '1.2.3', 'linux', 'amd64') == 'terraform-provider-thing_1.2.3_linux_amd64.zip'
    )
    assert paths.provider_filename(provider, '1.2.3', sums='SHA256SUMS') == 'terraform-provider-thing_1.2.3_SHA256SUMS'
    assert paths.parse_hosted_provider_address('acme/thing') == provider
    assert paths.parse_hosted_provider_address('example.com/acme/thing') is None
    assert paths.parse_module_address('terraform-aws-modules/vpc/aws') == VPC
    assert paths.parse_module_address('vpc/aws') is None


def test_keys_and_coordinates() -> None:
    assert paths.module_archive_key(VPC, '5.1.2') == 'v1/modules/terraform-aws-modules/vpc/aws/5.1.2/archive.tar.gz'
    assert paths.module_versions_key('registry.terraform.io', VPC) == (
        'registry.terraform.io/v1/modules/terraform-aws-modules/vpc/aws/versions'
    )
    coord = coordinates('terraform', 'v1/modules/acme/net/aws/1.2.0/archive.zip')
    assert (coord.name, coord.version, coord.ordered_by_version) == ('acme/net/aws', '1.2.0', True)
    coord = coordinates('terraform', 'v1/providers/acme/thing/2.0.0/terraform-provider-thing_2.0.0_linux_amd64.zip')
    assert (coord.name, coord.version, coord.ordered_by_version) == ('acme/thing', '2.0.0', True)
    assert not coordinates(
        'terraform', 'v1/providers/acme/thing/2.0.0/terraform-provider-thing_2.0.0_SHA256SUMS'
    ).ordered_by_version
    assert not coordinates('terraform', 'registry.terraform.io/v1/modules/acme/net/aws/versions').ordered_by_version
    key = key_for('terraform', 'v1/modules/acme/net/aws/1.2.0/archive.tar.gz')
    assert key is not None and (key.name, key.version, key.display) == ('acme/net/aws', '1.2.0', 'acme/net/aws 1.2.0')
    assert key_for('terraform', 'v1/providers/acme/thing/2.0.0/terraform-provider-thing_2.0.0_SHA256SUMS') is None
    # the provider network mirror keeps its layout
    mirror = coordinates('terraform', 'registry.terraform.io/hashicorp/null/3.1.0/linux/amd64/x.zip')
    assert mirror.name == 'registry.terraform.io/hashicorp/null'


# ---------------------------------------------------------------------------- module documents
def test_module_versions_document() -> None:
    doc = {'modules': [{'versions': [{'version': '1.0.0'}, {'version': '1.10.0'}, {'version': 'v2'}, 'x', {}]}]}
    assert modules.parse_module_versions(json.dumps(doc).encode()) == ['1.10.0', '1.0.0']
    rendered = json.loads(modules.render_module_versions(['1.0.0', '2.0.0-rc.1', '1.10.0']))
    assert rendered == {
        'modules': [{'versions': [{'version': '2.0.0-rc.1'}, {'version': '1.10.0'}, {'version': '1.0.0'}]}]
    }
    for bad in (b'nope', b'[]', b'{"modules": 1}', b'{"modules": [{"versions": 3}]}'):
        with pytest.raises(UpstreamError):
            modules.parse_module_versions(bad)
    with pytest.raises(UpstreamError):
        modules.parse_module_versions(b' ' * (modules.MAX_MODULE_VERSIONS + 1))
    assert modules.parse_module_versions(b'{"modules": []}') == []


def test_download_answer() -> None:
    url = 'https://registry.example.com/v1/modules/acme/net/aws/1.0.0/download'
    header = {'x-terraform-get': 'git::https://github.com/acme/net?ref=v1.0.0'}
    assert modules.parse_download(b'', header, url) == 'git::https://github.com/acme/net?ref=v1.0.0'
    body = json.dumps({'location': './archive.tgz'}).encode()
    assert (
        modules.parse_download(body, {}, url)
        == 'https://registry.example.com/v1/modules/acme/net/aws/1.0.0/archive.tgz'
    )
    assert (
        modules.parse_download(b'', {'x-terraform-get': '/files/a.zip'}, url)
        == 'https://registry.example.com/files/a.zip'
    )
    for data, headers in ((b'', {}), (b'{}', {}), (b'not json', {}), (b'', {'x-terraform-get': 'a\nb'})):
        with pytest.raises(UpstreamError):
            modules.parse_download(data, headers, url)


def test_archive_sources() -> None:
    src = modules.archive_source('git::https://github.com/hashicorp/terraform-cidr-subnets?ref=v1.0.0', GITHUB)
    assert src == modules.ArchiveSource(
        'https://github.com/hashicorp/terraform-cidr-subnets/archive/v1.0.0.tar.gz', 'archive.tar.gz', '', True
    )
    sub = modules.archive_source('github.com/acme/mono.git//modules/vpc?ref=main&depth=1', GITHUB)
    assert sub is not None and sub.subdir == 'modules/vpc' and sub.url.endswith('/acme/mono/archive/main.tar.gz')
    shorthand = modules.archive_source('git::github.com/acme/net?ref=v2', GITHUB)
    assert shorthand is not None and shorthand.url == 'https://github.com/acme/net/archive/v2.tar.gz'
    hosts = frozenset({'github.com', 'downloads.example.com', 's3.example.com'})
    zipped = modules.archive_source('https://downloads.example.com/net-1.0.0.zip//sub', hosts)
    assert zipped == modules.ArchiveSource('https://downloads.example.com/net-1.0.0.zip', 'archive.zip', 'sub', False)
    forced = modules.archive_source('https://downloads.example.com/get?id=7&archive=tar.gz', hosts)
    assert (
        forced is not None
        and forced.url == 'https://downloads.example.com/get?id=7'
        and forced.filename == 'archive.tar.gz'
    )
    s3 = modules.archive_source('s3::https://s3.example.com/bucket/net.tgz', hosts)
    assert s3 is not None and s3.url == 'https://s3.example.com/bucket/net.tgz'
    for passthrough in (
        'git::https://github.com/acme/net',  # no ref: a moving branch
        'git::https://github.com/acme/net?ref=v1&sshkey=abc',
        'git::https://gitlab.example.com/acme/net?ref=v1',  # not an allowed host
        'git::ssh://git@github.com/acme/net?ref=v1',
        'git@github.com:acme/net.git?ref=v1',
        'https://github.com/acme/net/tree/main?ref=v1',
        'https://downloads.example.com/page',  # no archive extension
        'https://other.example.com/net.zip',  # not an allowed host
        'hg::https://github.com/acme/net?ref=v1',
        'git::https://github.com/acme/net//../escape?ref=v1',
        'git::https://github.com/acme/net?ref=../../x',
        'https://user:secret@downloads.example.com/net.zip',
        'git::http://github.com/acme/net?ref=v1',
    ):
        assert modules.archive_source(passthrough, hosts) is None, passthrough


def test_split_subdir_and_client_location() -> None:
    assert modules.split_subdir('https://example.com/a.zip//dir/sub?x=1') == (
        'https://example.com/a.zip?x=1',
        'dir/sub',
    )
    assert modules.split_subdir('example.com/a//b') == ('example.com/a', 'b')
    assert modules.split_subdir('https://example.com/a.zip?u=https://x//y') == (
        'https://example.com/a.zip?u=https://x//y',
        '',
    )
    src = modules.ArchiveSource('https://github.com/a/b/archive/v1.tar.gz', 'archive.tar.gz', 'modules/vpc', True)
    assert modules.client_location('./archive.tar.gz?ws_grant=1.x', src, 'b-1') == (
        './archive.tar.gz//b-1/modules/vpc?ws_grant=1.x'
    )
    plain = modules.ArchiveSource('https://example.com/a.zip', 'archive.zip', '', False)
    assert modules.client_location('./archive.zip', plain, None) == './archive.zip'


def test_archive_top() -> None:
    data = tarball({'main.tf': b'# vpc\n'}, top='net-1.0.0')
    assert modules.archive_top(io.BytesIO(data), 'archive.tar.gz') == 'net-1.0.0'
    modules.check_fetched_archive(data, 'archive.tar.gz')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('net/main.tf', b'')
    assert modules.archive_top(io.BytesIO(buffer.getvalue()), 'archive.zip') == 'net'
    for bad, name in (
        (b'not an archive', 'archive.tar.gz'),
        (b'PK nope', 'archive.zip'),
        (tarball({}), 'archive.tar.gz'),
    ):
        with pytest.raises(UpstreamError):
            modules.archive_top(io.BytesIO(bad), name)


def test_hosted_module_archive(tmp_path: Path) -> None:
    def check(data: bytes) -> modules.ModuleArchive:
        path = tmp_path / 'module.tar.gz'
        path.write_bytes(data)
        return modules.check_module_archive(path)

    info = check(tarball({'main.tf': b'output "a" { value = 1 }\n', 'variables.tf.json': b'{}', 'sub/x.tf': b''}))
    assert (info.files, info.root_files) == (3, ('main.tf', 'variables.tf.json'))
    assert check(tarball({'./main.tofu': b''})).root_files == ('main.tofu',)
    for bad in (
        tarball({'README.md': b'hi', 'sub/main.tf': b''}),  # no .tf at the root
        tarball({'main.tf': b''}, top='module'),  # archived with its directory
        tarball({'main.tf': b'', '../escape.tf': b''}),
        tarball({'main.tf': b'', '/etc/passwd': b''}),
        tarball({'main.tf': b''}, links={'link.tf': '/etc/passwd'}),
        b'not gzip at all',
    ):
        with pytest.raises(ValidationFailed):
            check(bad)


# ---------------------------------------------------------------------------- signed download URLs
def test_download_urls() -> None:
    urls = DownloadUrls([b'k' * 32])
    grant = urls.sign('repo1', 'v1/modules/a/b/c/1.0.0/archive.tar.gz', now=1000)
    assert urls.verify(grant, 'repo1', 'v1/modules/a/b/c/1.0.0/archive.tar.gz', now=1000 + TTL_S)
    assert not urls.verify(grant, 'repo1', 'v1/modules/a/b/c/1.0.0/archive.tar.gz', now=1001 + TTL_S)  # expired
    assert not urls.verify(grant, 'repo2', 'v1/modules/a/b/c/1.0.0/archive.tar.gz', now=1000)  # another repository
    assert not urls.verify(grant, 'repo1', 'v1/modules/a/b/c/1.0.1/archive.tar.gz', now=1000)  # another file
    assert not urls.verify(grant, 'repo1', 'v1/modules/a/b/c/1.0.0/archive.tar.gz', now=0)  # too far ahead
    expiry, _, mac = grant.partition('.')
    assert not urls.verify(f'{int(expiry) + 1}.{mac}', 'repo1', 'v1/modules/a/b/c/1.0.0/archive.tar.gz', now=1000)
    for junk in ('', 'x', 'x.y', '1.' + 'a' * 80):
        assert not urls.verify(junk, 'repo1', 'p', now=1000)
    rotated = DownloadUrls([b'n' * 32, b'k' * 32])  # an older key still verifies
    assert rotated.verify(grant, 'repo1', 'v1/modules/a/b/c/1.0.0/archive.tar.gz', now=1000)
    assert not DownloadUrls([b'n' * 32]).verify(grant, 'repo1', 'v1/modules/a/b/c/1.0.0/archive.tar.gz', now=1000)
    assert DownloadUrls([]).sign('r', 'p') != DownloadUrls([]).sign('r', 'p')  # a random key per process


# ---------------------------------------------------------------------------- options and "Set me up"
def test_options_and_types() -> None:
    assert RepoType.HOSTED in DESCRIPTOR.types and RepoType.PROXY in DESCRIPTOR.types
    assert TerraformOptions().module_archive_hosts == ['github.com']
    opts = TerraformOptions(module_archive_hosts=['GitHub.com', 'downloads.example.com', 'github.com'])
    assert opts.module_archive_hosts == ['github.com', 'downloads.example.com']
    with pytest.raises(ValueError, match='invalid host name'):
        TerraformOptions(module_archive_hosts=['https://github.com'])


def _texts(repo_type: RepoType) -> dict[str, str]:
    if repo_type is RepoType.HOSTED:
        repo = make_repo('tf', fmt='terraform', type=RepoType.HOSTED, proxy=None, hosted={})
    else:
        repo = make_repo('tf', fmt='terraform', proxy={'upstream_url': 'https://registry.terraform.io/'})
    ctx = SetupContext(
        repo=repo, base_url='https://registry.example.com', registry_host='registry.example.com', username='alice'
    )
    return {f'{c.id}:{s.id}:{s.os}': s.content for c in DESCRIPTOR.setup_snippets(ctx) for s in c.snippets}


def test_setup_snippets() -> None:
    proxy = _texts(RepoType.PROXY)
    config = proxy['terraform:terraform-cli-config:None']
    assert 'network_mirror' in config and 'host "registry.terraform.io"' in config
    assert '"modules.v1" = "https://registry.example.com/repository/tf/v1/modules/"' in config
    assert 'credentials "registry.terraform.io"' in config and 'credentials "registry.example.com"' in config
    hosted = _texts(RepoType.HOSTED)
    assert (
        hosted['terraform:terraform-cli-config:None']
        == 'credentials "registry.example.com" {\n  token = "{{TOKEN}}"\n}\n'
    )
    usage = hosted['terraform:terraform-sources:None']
    assert 'source  = "registry.example.com/my-namespace/network/aws"' in usage
    assert 'source  = "registry.example.com/my-namespace/example"' in usage
    assert (
        'repository/tf/v1/modules/my-namespace/network/aws/1.0.0' in hosted['terraform:terraform-publish-module:None']
    )
    assert '-F signing_key=@signing-key.asc' in hosted['terraform:terraform-publish-provider:None']
    assert 'tofu:tofu-cli-config:None' in hosted and 'tofu:terraform-sources:None' in hosted
