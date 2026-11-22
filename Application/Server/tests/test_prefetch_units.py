"""Prefetch units (T-O12, adr/0012): warm item syntax and refresh maps per format, the system-grant decision, the
internal route's refusals, and the worker's selection of due metadata."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from ws_testing import TEST_KEY, make_repo

from waystation_common.system_grants import HEADER, PREFETCH, SystemGrants
from waystation_server.formats.base import RefreshRequest
from waystation_server.formats.common import prefetch as common
from waystation_server.formats.docker import prefetch as docker
from waystation_server.formats.go import prefetch as go
from waystation_server.formats.maven import prefetch as maven
from waystation_server.formats.npm import prefetch as npm
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.security import access
from waystation_server.security.actions import Action
from waystation_server.security.principal import AuthMethod, PrincipalKind
from waystation_server.services import Services
from waystation_worker import prefetch as worker

GRANTS = SystemGrants.from_secret_keys([TEST_KEY])


# ---------------------------------------------------------------------------- item syntax
@pytest.mark.parametrize(
    ('item', 'expected'),
    [
        ('nginx', ('nginx', 'latest', ('linux/amd64',))),
        ('library/nginx:1.27', ('library/nginx', '1.27', ('linux/amd64',))),
        ('nginx:1.27 linux/amd64,linux/arm64/v8', ('nginx', '1.27', ('linux/amd64', 'linux/arm64/v8'))),
        (f'nginx@sha256:{"a" * 64} all', ('nginx', f'sha256:{"a" * 64}', None)),
        ('  ghcr-mirror/org/app:v1.2.3  ', ('ghcr-mirror/org/app', 'v1.2.3', ('linux/amd64',))),
    ],
)
def test_docker_items(item: str, expected: tuple[str, str, tuple[str, ...] | None]) -> None:
    assert docker.parse_item(item) == expected
    assert docker.check_item(item) is None


@pytest.mark.parametrize(
    'item', ['', 'Nginx', 'nginx:', 'nginx@sha256:abc', 'nginx:1 linux', 'nginx 1 2', 'nginx:bad tag', '../x']
)
def test_docker_items_refused(item: str) -> None:
    assert docker.check_item(item)


def test_docker_refresh_map() -> None:
    accept = ', '.join(docker.p.MANIFEST_TYPES)
    assert docker.refresh_request('library/nginx/manifests/1.27') == RefreshRequest(
        'v2/library/nginx/manifests/1.27', {'Accept': accept}
    )
    narrow = docker.refresh_request('library/nginx/manifests/latest@accept=om+dm')
    assert narrow is not None
    assert narrow.headers['Accept'] == f'{docker.p.OCI_MANIFEST}, {docker.p.DOCKER_MANIFEST}'
    assert docker.refresh_request('library/nginx/tags/list') == RefreshRequest('v2/library/nginx/tags/list')
    assert docker.refresh_request(f'library/nginx/manifests/sha256:{"a" * 64}') is None  # content
    assert docker.refresh_request('library/nginx/manifests/latest@accept=zz') is None
    assert docker.refresh_request(f'library/nginx/referrers/sha256:{"a" * 64}') is None
    assert docker.refresh_request('blobs/sha256:00') is None


@pytest.mark.parametrize(
    ('item', 'expected'),
    [
        ('lodash', ('lodash', None)),
        ('lodash@4.17.21', ('lodash', '4.17.21')),
        ('lodash@next', ('lodash', 'next')),
        ('@types/node@22.10.1', ('@types/node', '22.10.1')),
        ('@types/node', ('@types/node', None)),
    ],
)
def test_npm_items(item: str, expected: tuple[str, str | None]) -> None:
    assert npm.parse_item(item) == expected


@pytest.mark.parametrize('item', ['', '@types', 'lodash@^4.0.0', 'lodash@>=1', 'a b', '@/x'])
def test_npm_items_refused(item: str) -> None:
    assert npm.check_item(item)


def test_npm_refresh_map() -> None:
    abbreviated = npm.refresh_request('@types/node/-/packument.install-v1.json')
    assert abbreviated is not None and abbreviated.path == '@types/node'
    assert 'install-v1' in abbreviated.headers['Accept']
    assert npm.refresh_request('lodash/-/packument.json') == RefreshRequest('lodash', {'Accept': 'application/json'})
    assert npm.refresh_request('lodash/-/lodash-4.17.21.tgz') is None


def test_npm_tarball_resolution() -> None:
    doc: dict[str, Any] = {
        'dist-tags': {'latest': '2.0.0', 'next': '3.0.0-rc.1'},
        'versions': {
            v: {'dist': {'tarball': f'https://registry.example.com/x/-/x-{v}.tgz'}}
            for v in ('1.0.0', '2.0.0', '3.0.0-rc.1')
        },
    }
    assert npm._tarball(doc, 'x', None) == 'x-2.0.0.tgz'
    assert npm._tarball(doc, 'x', 'next') == 'x-3.0.0-rc.1.tgz'
    assert npm._tarball(doc, 'x', '1.0.0') == 'x-1.0.0.tgz'
    with pytest.raises(Exception, match='no version'):
        npm._tarball(doc, 'x', '9.9.9')


@pytest.mark.parametrize(
    ('item', 'ok'),
    [
        ('org.apache.commons:commons-lang3:3.17.0', True),
        ('io.netty:netty-transport-native-epoll:4.1.115.Final:jar:linux-x86_64', True),
        ('g:a:1.0:pom', True),
        ('g:a', False),
        ('g:a:1.0-SNAPSHOT', False),
        ('g..x:a:1', False),
        ('g:a:1:JAR', False),
        ('g:a:1:jar:c:extra', False),
    ],
)
def test_maven_items(item: str, ok: bool) -> None:
    assert (maven.check_item(item) is None) is ok


def test_maven_packaging() -> None:
    assert maven.extension_for(b'<project><packaging>pom</packaging></project>') is None
    assert maven.extension_for(b'<project><packaging> war </packaging></project>') == 'war'
    assert maven.extension_for(b'<project><packaging>bundle</packaging></project>') == 'jar'
    assert maven.extension_for(b'<project></project>') == 'jar'


@pytest.mark.parametrize(
    ('item', 'expected'),
    [
        ('golang.org/x/text@v0.21.0', ('golang.org/x/text', 'v0.21.0')),
        ('golang.org/x/text', ('golang.org/x/text', 'latest')),
        ('github.com/Azure/go-autorest@latest', ('github.com/Azure/go-autorest', 'latest')),
    ],
)
def test_go_items(item: str, expected: tuple[str, str]) -> None:
    assert go.parse_item(item) == expected


@pytest.mark.parametrize('item', ['text', 'golang.org/x/text@master', 'golang.org/x/text@1.0.0', '@v1.0.0'])
def test_go_items_refused(item: str) -> None:
    assert go.check_item(item)


def test_path_items_and_same_path() -> None:
    assert common.check_path('tools/tool-1.0.tar.gz') is None
    for bad in ('', 'tools/', '../etc/passwd', '/abs', 'a?b=1', 'x' * 600):
        assert common.check_path(bad), bad
    assert common.same_path('a/maven-metadata.xml') == RefreshRequest('a/maven-metadata.xml')
    assert common.same_path('') is None


# ---------------------------------------------------------------------------- the decision
def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({'type': 'http', 'method': 'POST', 'path': '/_internal/prefetch/x', 'headers': raw})


def test_system_grant_reads_one_proxy_repository_only() -> None:
    repo = make_repo('hub', 'docker')
    other = make_repo('other', 'docker')
    principal = access.system_grant(_request({HEADER: GRANTS.sign(PREFETCH, repo.id)}), repo, GRANTS)
    assert principal is not None
    assert (principal.kind, principal.method, principal.display) == (
        PrincipalKind.SYSTEM,
        AuthMethod.SYSTEM_GRANT,
        'prefetch',
    )
    assert access.check(principal, Action.READ, repo) and access.check(principal, Action.BROWSE, repo)
    for action in (Action.WRITE, Action.DELETE, Action.MANAGE):
        assert not access.check(principal, action, repo)
    assert not access.can_see(principal, other)
    assert not access.check(principal, Action.ADMIN)
    assert not access.check(principal, Action.STATS_READ)


def test_system_grant_refusals() -> None:
    repo = make_repo('hub', 'docker')
    good = {HEADER: GRANTS.sign(PREFETCH, repo.id)}
    assert access.system_grant(_request({}), repo, GRANTS) is None
    assert access.system_grant(_request({HEADER: GRANTS.sign(PREFETCH, 'id-other')}), repo, GRANTS) is None
    assert access.system_grant(_request({HEADER: GRANTS.sign('download', repo.id)}), repo, GRANTS) is None
    assert access.system_grant(_request(good), repo, SystemGrants([])) is None
    assert access.system_grant(_request(good), repo, None) is None
    offline = repo.model_copy(update={'online': False})
    assert access.system_grant(_request(good), offline, GRANTS) is None
    hosted: RepoConfig = repo.model_copy(update={'type': RepoType.HOSTED, 'proxy': None})
    assert access.system_grant(_request(good), hosted, GRANTS) is None


@pytest.fixture
def unit_repos() -> list[RepoConfig]:
    return [make_repo('files'), make_repo('local', type=RepoType.HOSTED, proxy=None)]


async def test_route_answers_404_without_a_valid_grant(unit_client: httpx.AsyncClient, unit_services: Services) -> None:
    body = {'op': 'warm', 'items': ['a.bin']}
    assert (await unit_client.post('/_internal/prefetch/id-files', json=body)).status_code == 404
    other = {HEADER: GRANTS.sign(PREFETCH, 'id-local')}
    assert (await unit_client.post('/_internal/prefetch/id-files', json=body, headers=other)).status_code == 404
    hosted = {HEADER: GRANTS.sign(PREFETCH, 'id-local')}
    assert (await unit_client.post('/_internal/prefetch/id-local', json=body, headers=hosted)).status_code == 404
    missing = {HEADER: GRANTS.sign(PREFETCH, 'id-none')}
    assert (await unit_client.post('/_internal/prefetch/id-none', json=body, headers=missing)).status_code == 404
    # Read-only: the route only takes POST; other methods are not routed.
    assert (await unit_client.get('/_internal/prefetch/id-files')).status_code == 405


# ---------------------------------------------------------------------------- worker selection
def test_due_selects_stale_and_soon_stale_metadata() -> None:
    now = datetime(2026, 11, 22, 12, tzinfo=UTC)

    def doc(path: str, age_s: int | None) -> dict[str, Any]:
        fetched = None if age_s is None else now - timedelta(seconds=age_s)
        return {'path': path, 'upstream': {'fetched_at': fetched}}

    docs = [doc('old', 4000), doc('soon', 1000), doc('fresh', 100), doc('unknown', None)]
    assert worker.due(docs, max_age_s=1800, ahead_s=900, now=now) == ['old', 'soon', 'unknown']
    assert worker.due(docs, max_age_s=1800, ahead_s=0, now=now) == ['old', 'unknown']
    assert worker.due(docs, max_age_s=0, ahead_s=900, now=now) == ['old', 'soon', 'fresh', 'unknown']


def test_tally_counts_outcomes() -> None:
    tally = worker.Tally()
    tally.add({'item': 'a', 'outcome': 'ok', 'files': 3, 'fetched': 2})
    tally.add({'item': 'b', 'outcome': 'failed', 'error': 'b: not found', 'files': 1})
    tally.add({'item': 'c', 'outcome': 'skipped'})
    tally.add({'item': 'd', 'outcome': 'invalid', 'error': 'not supported'})
    assert tally.as_dict() == {
        'items': 4,
        'ok': 1,
        'failed': 2,
        'skipped': 1,
        'files': 4,
        'fetched': 2,
        'errors': ['b: b: not found', 'd: not supported'],
    }


def test_server_client_needs_secret_keys() -> None:
    with pytest.raises(worker.PrefetchUnavailableError):
        worker.ServerClient('http://server.example.com:8000', SystemGrants([]))
