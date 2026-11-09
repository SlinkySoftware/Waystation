"""Nexus import (T-M01): mapping, the Nexus client and the planner against hand-written Nexus 3 REST fixtures
(``tests/fixtures/nexus3``) served by the fake upstream. No real Nexus is ever contacted."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from ws_fake_upstream import FakeUpstream, Respond
from ws_nexus import API, PASSWORD, SERVER, USER, detail, listing, nexus_routes
from ws_testing import make_repo

from waystation_common.settings import Settings
from waystation_server.migration import nexus_client, nexus_import
from waystation_server.migration.nexus_client import NexusError, NexusRepository, NexusSource
from waystation_server.migration.nexus_import import NexusApplyRequest, NexusImportRequest
from waystation_server.migration.nexus_mapping import map_repository, supports_section
from waystation_server.proxy.http import Network, UpstreamClients
from waystation_server.repos.model import RepoConfig
from waystation_server.security.principal import AuthMethod, Principal, PrincipalKind
from waystation_server.services import Services

ADMIN = Principal(PrincipalKind.USER, 'u-admin', 'admin', ('admin',), AuthMethod.SESSION)
PROXY_ONLY = {f: frozenset({'proxy'}) for f in ('docker', 'npm', 'apt', 'raw', 'yum')}


def nexus_repos() -> dict[str, NexusRepository]:
    return {e['name']: NexusRepository(entry=e, detail=detail(e['name'])) for e in listing()}


@pytest.fixture
async def nexus() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream(nexus_routes()) as upstream:
        yield upstream


def request(nexus: FakeUpstream, **overrides: Any) -> NexusImportRequest:
    values: dict[str, Any] = {'url': nexus.url, 'username': USER, 'password': PASSWORD, 'allow_private': True}
    values.update(overrides)
    return NexusImportRequest(**values)


# ---------------------------------------------------------------------------------------------------- mapping
def test_docker_hub_proxy() -> None:
    m = map_repository(nexus_repos()['dockerhub-proxy'], supported=PROXY_ONLY)
    assert m.action == 'create'
    assert m.docker_hub and not m.connector
    assert m.body == {
        'name': 'dockerhub-proxy',
        'format': 'docker',
        'type': 'proxy',
        'online': True,
        'description': 'Imported from Nexus (docker proxy)',
        'proxy': {
            'upstream_url': 'https://registry-1.docker.io',
            'metadata_max_age_s': 86400,
            'content_max_age_s': None,
            'negative_ttl_s': 86400,
            'auto_block': {'enabled': True},
        },
        'format_options': {'docker_hub': True, 'aliases': ['docker.io']},
    }
    fields = {d.field: d for d in m.differences}
    assert fields['proxy.content_max_age_s'].nexus == '1440 min'
    assert fields['storage.blobStoreName'].nexus == 'docker'
    assert fields['cleanup.policyNames'].nexus == 'docker-unused-90d'


def test_ghcr_proxy_alias_and_credentials_not_imported() -> None:
    m = map_repository(nexus_repos()['ghcr-proxy'], supported=PROXY_ONLY)
    assert m.body is not None
    assert m.body['format_options'] == {'aliases': ['ghcr.io']}
    assert 'auth' not in m.body['proxy']
    auth = next(d for d in m.differences if d.field == 'proxy.auth')
    assert auth.nexus == 'username (nexus-reader)'
    assert auth.waystation == 'none'
    assert 'password' in auth.note.lower()
    assert not m.docker_hub
    without = map_repository(nexus_repos()['ghcr-proxy'], supported=PROXY_ONLY, aliases=False)
    assert without.body is not None and without.body['format_options'] == {}


def test_apt_yum_npm_raw() -> None:
    repos = nexus_repos()
    debian = map_repository(repos['debian-security'], supported=PROXY_ONLY)
    assert debian.body is not None
    assert debian.body['format_options'] == {'distribution': 'trixie-security', 'flat': False}
    assert debian.body['proxy']['upstream_url'] == 'http://security.debian.org/debian-security/'
    assert debian.body['proxy']['metadata_max_age_s'] == 3600
    assert debian.body['proxy']['content_max_age_s'] is None
    epel = map_repository(repos['epel'], supported=PROXY_ONLY)
    assert epel.body is not None
    assert epel.body['proxy']['metadata_max_age_s'] == 1800  # -1 (never) keeps the default
    assert epel.body['proxy']['negative_ttl_s'] == 0  # negative cache disabled
    assert any(d.field == 'proxy.metadata_max_age_s' and d.nexus == 'never (-1)' for d in epel.differences)
    oracle = map_repository(repos['oracle'], supported=PROXY_ONLY)
    assert oracle.body is not None
    assert oracle.body['proxy']['offline'] is True
    assert oracle.body['proxy']['auto_block'] == {'enabled': False}
    raw = map_repository(repos['github-releases'], supported=PROXY_ONLY)
    assert raw.body is not None
    assert raw.body['proxy']['content_max_age_s'] == 10080 * 60  # raw content ages as in Nexus
    assert raw.body['format_options'] == {'content_disposition': 'attachment'}
    npm = map_repository(NexusRepository(entry=repos['npm'].entry, detail_error='no detail'), supported=PROXY_ONLY)
    assert npm.body is not None
    assert npm.body['proxy'] == {'upstream_url': 'https://registry.npmjs.org', 'content_max_age_s': None}
    assert npm.notes == ['no detail']


def test_pypi_content_is_kept_forever() -> None:
    raw = nexus_repos()['github-releases']  # a proxy whose content ages after 10080 minutes in Nexus
    entry = {**raw.entry, 'name': 'pypi-proxy', 'format': 'pypi'}
    nexus = {**(detail('github-releases') or {}), 'name': 'pypi-proxy', 'format': 'pypi'}
    supported = {**PROXY_ONLY, 'pypi': frozenset({'proxy'})}
    pypi = map_repository(NexusRepository(entry=entry, detail=nexus), supported=supported)
    assert pypi.body is not None
    assert pypi.body['proxy']['content_max_age_s'] is None  # files are verified against the page's hashes
    assert any(d.field == 'proxy.content_max_age_s' and d.nexus == '10080 min' for d in pypi.differences)


def test_unsupported_and_invalid() -> None:
    repos = nexus_repos()
    maven = map_repository(repos['maven-central'], supported=PROXY_ONLY)
    assert (maven.action, maven.format, maven.reason) == ('unsupported', 'maven', 'Waystation does not serve maven yet')
    r = map_repository(repos['r-hosted'], supported=PROXY_ONLY)
    assert (r.action, r.reason) == ('unsupported', 'Waystation has no r format')
    files = map_repository(repos['files'], supported=PROXY_ONLY)
    assert (files.action, files.reason) == ('unsupported', 'Waystation has no raw hosted repositories yet')
    group = map_repository(repos['docker-group'], supported=PROXY_ONLY)
    assert group.action == 'unsupported'
    assert group.connector and group.members == ['docker-hosted', 'dockerhub-proxy', 'ghcr-proxy']
    odd = NexusRepository(entry={'name': 'x', 'format': 'raw', 'type': 'proxy'})
    assert map_repository(odd, supported=PROXY_ONLY).action == 'invalid'  # one-character name
    unknown = NexusRepository(entry={'name': 'bower-proxy', 'format': 'bower2', 'type': 'proxy'})
    assert map_repository(unknown, supported=PROXY_ONLY).action == 'unsupported'
    no_dist = NexusRepository(
        entry={'name': 'apt-x', 'format': 'apt', 'type': 'proxy'},
        detail={'name': 'apt-x', 'proxy': {'remoteUrl': 'http://deb.debian.org/debian/'}},
    )
    assert map_repository(no_dist, supported=PROXY_ONLY).reason == 'the Nexus apt repository has no distribution'
    no_remote = NexusRepository(entry={'name': 'raw-x', 'format': 'raw', 'type': 'proxy'})
    assert map_repository(no_remote, supported=PROXY_ONLY).action == 'invalid'


@pytest.mark.skipif(not supports_section('group'), reason='group repositories are not in this build yet')
def test_hosted_and_group_sections() -> None:
    every = frozenset({'proxy', 'hosted', 'group'})
    supported = {**PROXY_ONLY, 'docker': every, 'raw': every}
    repos = nexus_repos()
    files = map_repository(repos['files'], supported=supported)
    assert files.body is not None and files.body['hosted'] == {'write_policy': 'disable_redeploy'}
    assert files.body['format_options'] == {'content_disposition': 'attachment'}
    raw_group = map_repository(repos['raw-group'], supported=supported)
    assert raw_group.body is not None and raw_group.body['group'] == {'members': ['files', 'github-releases']}
    group = map_repository(repos['docker-group'], supported=supported)
    assert group.body is not None
    assert group.body['group'] == {
        'members': ['docker-hosted', 'dockerhub-proxy', 'ghcr-proxy'],
        'writable_member': 'docker-hosted',
    }
    assert any(d.field == 'docker connector' and d.nexus == 'httpPort 8082' for d in group.differences)


# ---------------------------------------------------------------------------------------------------- client
def clients(settings: Settings) -> UpstreamClients:
    return UpstreamClients(settings)


async def test_read_lists_and_details(unit_settings: Settings, nexus: FakeUpstream) -> None:
    source = NexusSource(nexus.url, USER, PASSWORD, Network(allow_private=True))
    snapshot = await nexus_client.read(clients(unit_settings), source)
    assert snapshot.version == SERVER
    by_name = {r.name: r for r in snapshot.repositories}
    assert len(by_name) == 19
    assert by_name['debian'].detail is not None and by_name['debian'].detail['apt']['distribution'] == 'trixie'
    assert by_name['maven-central'].detail is not None  # the detail path of maven2 is maven
    assert by_name['npm'].detail is None
    assert by_name['npm'].detail_error and 'list' in by_name['npm'].detail_error
    assert by_name['npm'].config['proxy'] == {'remoteUrl': 'https://registry.npmjs.org'}
    assert all(s.headers.get('authorization', '').startswith('Basic ') for s in nexus.seen)


async def test_read_errors(unit_settings: Settings, nexus: FakeUpstream) -> None:
    upstream = clients(unit_settings)
    with pytest.raises(NexusError, match='refused the credentials'):
        await nexus_client.read(upstream, NexusSource(nexus.url, USER, 'wrong', Network(allow_private=True)))
    with pytest.raises(NexusError, match='private or reserved'):
        await nexus_client.read(upstream, NexusSource(nexus.url, USER, PASSWORD, Network()))
    with pytest.raises(NexusError, match='no Nexus 3 REST API'):
        await nexus_client.read(
            upstream, NexusSource(nexus.url + 'nexus/', USER, PASSWORD, Network(allow_private=True))
        )
    nexus.routes[API] = Respond(200, {'content-type': 'text/html'}, b'<html>sign in</html>')
    with pytest.raises(NexusError, match='not JSON'):
        await nexus_client.read(upstream, NexusSource(nexus.url, USER, PASSWORD, Network(allow_private=True)))
    nexus.routes[API] = Respond(302, {'location': 'https://nexus.example.com/'}, b'')
    with pytest.raises(NexusError, match='redirect'):
        await nexus_client.read(upstream, NexusSource(nexus.url, USER, PASSWORD, Network(allow_private=True)))
    nexus.routes[API] = Respond(200, {}, b'[' + b'{"name":"a"},' * 200_000 + b'{}]')
    with pytest.raises(NexusError, match='more than'):
        await nexus_client.read(upstream, NexusSource(nexus.url, USER, PASSWORD, Network(allow_private=True)))


def test_url_problems() -> None:
    assert nexus_client.url_problem('https://nexus.example.com/nexus/') is None
    assert nexus_client.url_problem('ftp://nexus.example.com') is not None
    assert 'credentials' in (nexus_client.url_problem('https://admin:pw@nexus.example.com') or '')
    assert nexus_client.url_problem('https://nexus.example.com/?a=1') is not None
    assert nexus_client.url_problem('https://nexus.example.com:99999') is not None


def test_password_never_in_repr() -> None:
    source = NexusSource('https://nexus.example.com', USER, PASSWORD)
    assert PASSWORD not in repr(source)
    body = NexusImportRequest(url='https://nexus.example.com', username=USER, password=PASSWORD)
    assert PASSWORD not in repr(body) and PASSWORD not in body.model_dump_json()


# ---------------------------------------------------------------------------------------------------- planner
@pytest.fixture
def unit_repos() -> list[RepoConfig]:
    return []


async def test_preview_plans_every_repository(unit_services: Services, nexus: FakeUpstream) -> None:
    report = await nexus_import.preview(unit_services, ADMIN, request(nexus))
    items = {i.name: i for i in report.items}
    assert report.applied is False
    assert report.nexus_version == SERVER
    proxies = [
        'dockerhub-proxy', 'ghcr-proxy', 'debian', 'debian-security', 'ubuntu', 'ubuntu-security', 'raspberrypi',
        'rocky', 'almalinux', 'epel', 'oracle', 'npm', 'github-releases',
    ]  # fmt: skip
    for name in proxies:
        assert items[name].action == 'create', (name, items[name].reason)
        assert items[name].repository is not None
    assert items['maven-central'].action == 'unsupported'
    assert items['r-hosted'].action == 'unsupported'
    for name in ('files', 'raw-group'):  # raw hosted and group (T-P2-RAW)
        assert items[name].action == 'create', (name, items[name].reason)
    assert report.summary.total == 19
    assert report.summary.create + report.summary.unsupported + report.summary.exists + report.summary.invalid == 19
    # unprefixed pulls: the Docker Hub proxy (behind the group's connector, or the only one)
    assert report.default_docker_repo is not None and report.default_docker_repo.name == 'dockerhub-proxy'
    hub = items['dockerhub-proxy'].repository
    assert hub is not None and hub.format_options == {
        'docker_hub': True,
        'aliases': ['docker.io'],
        'default_repo': True,
    }
    assert items['ghcr-proxy'].repository is not None
    assert items['ghcr-proxy'].repository.format_options == {
        'aliases': ['ghcr.io'],
        'docker_hub': False,
        'default_repo': False,
    }
    assert items['rocky'].repository is not None
    assert items['rocky'].repository.format_options == {'repo_path': '', 'gpg_key': None}
    assert items['raspberrypi'].repository is not None
    assert items['raspberrypi'].repository.format_options == {'distribution': 'trixie', 'flat': False}


@pytest.mark.parametrize(
    'unit_repos',
    [[make_repo('debian', 'apt', format_options={'distribution': 'bookworm', 'flat': False}),
      make_repo('quay', 'docker', format_options={'aliases': ['ghcr.io'], 'docker_hub': False, 'default_repo': True})]],
)  # fmt: skip
async def test_preview_against_existing_repositories(unit_services: Services, nexus: FakeUpstream) -> None:
    report = await nexus_import.preview(unit_services, ADMIN, request(nexus))
    items = {i.name: i for i in report.items}
    debian = items['debian']
    assert debian.action == 'exists'
    diffs = {d.field: d for d in debian.differences}
    assert diffs['format_options.distribution'].nexus == 'trixie'
    assert diffs['format_options.distribution'].waystation == 'bookworm'
    assert diffs['proxy.upstream_url'].nexus == 'http://deb.debian.org/debian/'
    # ghcr.io is already routed to quay: dropped with a difference
    ghcr = items['ghcr-proxy']
    assert ghcr.repository is not None and ghcr.repository.format_options['aliases'] == []
    assert any(d.field == 'format_options.aliases' and 'quay' in d.note for d in ghcr.differences)
    # an existing default docker repository is kept
    assert report.default_docker_repo is not None and report.default_docker_repo.name == 'quay'
    hub = items['dockerhub-proxy'].repository
    assert hub is not None and hub.format_options is not None and hub.format_options['default_repo'] is False


async def test_default_docker_requested_and_undetermined(unit_services: Services, nexus: FakeUpstream) -> None:
    chosen = await nexus_import.preview(unit_services, ADMIN, request(nexus, default_docker_repo='ghcr-proxy'))
    assert chosen.default_docker_repo is not None and chosen.default_docker_repo.name == 'ghcr-proxy'
    bad = await nexus_import.preview(unit_services, ADMIN, request(nexus, default_docker_repo='debian'))
    assert bad.default_docker_repo is not None and bad.default_docker_repo.name is None


async def test_apply_selected_without_database_fails_cleanly(unit_services: Services, nexus: FakeUpstream) -> None:
    """The selection is honoured before anything is written (the write itself is covered by the integration test)."""
    body = NexusApplyRequest(url=nexus.url, username=USER, password=PASSWORD, allow_private=True, repositories=[])
    report = await nexus_import.apply(unit_services, ADMIN, body)
    assert report.applied is True
    assert {i.outcome for i in report.items if i.action == 'create'} == {'not_selected'}
    assert report.summary.created == 0
