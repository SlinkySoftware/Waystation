"""Alpine apk plugin units: options, prefetch items, refresh map, paths, "Set me up" snippets."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from waystation_common.errors import ValidationFailed
from waystation_server.formats.apk import prefetch
from waystation_server.formats.apk.common import details
from waystation_server.formats.apk.descriptor import DESCRIPTOR, ApkOptions, options_of
from waystation_server.formats.apk.hosted import upload_dir
from waystation_server.formats.apk.plugin import directory_of
from waystation_server.formats.base import SetupContext
from waystation_server.repos.model import RepoConfig, RepoType


def test_options() -> None:
    opts = ApkOptions()
    assert (opts.branch, opts.repositories, opts.architectures) == ('latest-stable', ['main', 'community'], ['x86_64'])
    assert options_of({'repositories': ['main', 'main', 'testing']}).repositories == ['main', 'testing']
    for bad in ({'branch': '..'}, {'branch': 'a/b'}, {'repositories': []}, {'architectures': ['X86']}, {'x': 1}):
        with pytest.raises(ValidationError):
            ApkOptions.model_validate(bad)
    assert DESCRIPTOR.probe_path(opts) == 'latest-stable/main/x86_64/APKINDEX.tar.gz'
    assert RepoType.GROUP not in DESCRIPTOR.types


def test_warm_items() -> None:
    assert prefetch.parse_item('busybox') == (None, 'busybox', None)
    assert prefetch.parse_item(' libstdc++=14.2.0-r6 ') == (None, 'libstdc++', '14.2.0-r6')
    assert prefetch.parse_item('v3.22/main/x86_64/musl=1.2.5-r10') == ('v3.22/main/x86_64', 'musl', '1.2.5-r10')
    for bad in ('', 'a b', 'busybox=one', 'main/x86_64/busybox', 'v3.22/main/X86/busybox', 'v3.22/../x86_64/a'):
        assert prefetch.check_item(bad) is not None, bad
    assert DESCRIPTOR.refresh_request is not None
    assert DESCRIPTOR.refresh_request('v3.22/main/x86_64/APKINDEX.tar.gz') is not None
    assert DESCRIPTOR.refresh_request('v3.22/main/x86_64/busybox-1.0-r0.apk') is None


def test_paths() -> None:
    assert directory_of('v3.22/main/x86_64/APKINDEX.tar.gz') == 'v3.22/main/x86_64'
    assert directory_of('APKINDEX.tar.gz') is None
    assert directory_of('/'.join(['a'] * 10) + '/x.apk') is None
    assert upload_dir('v3.22/main/x86_64/x.apk') == 'v3.22/main/x86_64'
    assert upload_dir('team/v1/main/aarch64/x.apk') == 'team/v1/main/aarch64'
    for bad in ('main/x86_64/x.apk', 'v3.22/main/noarch/x.apk', 'v3.22/ma in/x86_64/x.apk', 'v/m/X86/x.apk'):
        with pytest.raises(ValidationFailed):
            upload_dir(bad)


def test_details() -> None:
    out = details({'T': 'A tool', 'D': 'so:libc.musl-x86_64.so.1 busybox', 'I': '4096', 't': '1700000000'})
    assert out['depends'] == ['so:libc.musl-x86_64.so.1', 'busybox'] and out['installed_size'] == 4096
    assert out['build_time'] == datetime.fromtimestamp(1_700_000_000, UTC) and out['license'] is None


def _repo(kind: RepoType) -> RepoConfig:
    return RepoConfig.model_validate(
        {
            '_id': '01JABCDEFGHJKMNPQRSTVWXYZ0',
            'created_at': datetime(2026, 1, 1, tzinfo=UTC),
            'updated_at': datetime(2026, 1, 1, tzinfo=UTC),
            'name': 'alpine',
            'format': 'apk',
            'type': kind.value,
            'online': True,
            'format_options': {'branch': 'v3.22'},
            **({'proxy': {'upstream_url': 'https://dl-cdn.alpinelinux.org/alpine/'}} if kind is RepoType.PROXY else {}),
        }
    )


def test_setup_snippets() -> None:
    ctx = SetupContext(_repo(RepoType.PROXY), 'https://registry.example.com', 'registry.example.com', None)
    (client,) = DESCRIPTOR.setup_snippets(ctx)
    text = '\n'.join(s.content for s in client.snippets)
    assert 'https://registry.example.com/repository/alpine/v3.22/main\n' in text
    assert 'https://registry.example.com/repository/alpine/v3.22/community' in text
    hosted = SetupContext(_repo(RepoType.HOSTED), 'https://registry.example.com', 'registry.example.com', None)
    (client,) = DESCRIPTOR.setup_snippets(hosted)
    text = '\n'.join(s.content for s in client.snippets)
    assert 'keys/alpine-stvwxyz0.rsa.pub' in text and '/etc/apk/keys/alpine-stvwxyz0.rsa.pub' in text
    assert '--upload-file' in text
