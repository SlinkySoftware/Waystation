"""Set me up OS variants (T-U04): a snippet with ``os`` replaces the snippet of the same id on that operating system,
so every variant needs a base snippet (no ``os``) in the same client, and (id, os) is unique per client."""

from __future__ import annotations

from typing import Any

import pytest
from ws_testing import make_repo

from waystation_server.formats.base import FormatDescriptor, SetupContext
from waystation_server.plugins import default_registry
from waystation_server.repos.model import RepoType

DESCRIPTORS = default_registry().descriptors()
# Options a format needs before it can describe a repository.
OPTIONS: dict[str, dict[str, Any]] = {'apt': {'distribution': 'trixie'}}


def _repos(descriptor: FormatDescriptor) -> list[Any]:
    repos = []
    for repo_type in sorted(descriptor.types, key=str):
        fields: dict[str, Any] = {'type': repo_type, 'format_options': OPTIONS.get(descriptor.name, {})}
        if repo_type is RepoType.HOSTED:
            fields |= {'proxy': None, 'hosted': {}}
        elif repo_type is RepoType.GROUP:
            fields |= {'proxy': None, 'group': {'members': ['a', 'b']}}
        try:
            repos.append(make_repo(f'{descriptor.name}-{repo_type}', descriptor.name, **fields))
        except ValueError:  # a type whose model needs more than this test gives it
            continue
    return repos


@pytest.mark.parametrize('descriptor', DESCRIPTORS, ids=[d.name for d in DESCRIPTORS])
def test_os_variants_replace_a_base_snippet(descriptor: FormatDescriptor) -> None:
    for repo in _repos(descriptor):
        for base_url in ('https://registry.example.com', 'http://registry.example.com'):
            for client in descriptor.setup_snippets(SetupContext(repo, base_url, 'registry.example.com', None)):
                keys = [(s.id, s.os) for s in client.snippets]
                assert len(keys) == len(set(keys)), f'{repo.name}/{client.id}: duplicate snippet'
                bases = {s.id for s in client.snippets if s.os is None}
                for s in client.snippets:
                    assert s.os is None or s.id in bases, f'{repo.name}/{client.id}/{s.id}: {s.os} variant without a base'


def test_variants_where_the_os_matters() -> None:
    by_name = {d.name: d for d in DESCRIPTORS}

    def variants(fmt: str, **fields: Any) -> dict[tuple[str, str | None], Any]:
        repo = make_repo('r', fmt, **fields)
        clients = by_name[fmt].setup_snippets(SetupContext(repo, 'https://registry.example.com', 'registry.example.com', None))
        return {(s.id, s.os): s for c in clients for s in c.snippets}

    docker = variants('docker', format_options={'default_repo': True, 'docker_hub': True})
    assert docker[('docker-mirror', None)].filename == '/etc/docker/daemon.json'
    assert {os for (sid, os) in docker if sid == 'docker-mirror'} == {None, 'macos', 'windows'}
    pypi = variants('pypi')
    assert pypi[('pip-conf', 'windows')].filename == '%APPDATA%\\pip\\pip.ini'
    assert pypi[('uv-auth', 'windows')].content.startswith('$env:UV_INDEX_WAYSTATION_USERNAME = ')
    go = variants('go')
    assert go[('netrc', 'windows')].filename == '%USERPROFILE%\\_netrc'
    terraform = variants('terraform')
    assert terraform[('terraform-cli-config', 'windows')].filename == '%APPDATA%\\terraform.rc'
    assert terraform[('terraform-token-env', 'windows')].language == 'powershell'
