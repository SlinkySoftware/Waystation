"""Docker dedicated port connectors (T-P6-DOCKERC, SPECIFICATION §4.4, ADR 0013): options, "Set me up" snippets,
the trusted connector header, realm and catalog lookups. Routing and the token service end to end are in
``test_registry_auth_integration.py`` and ``test_docker_hosted_integration.py``."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from starlette.requests import Request
from ws_testing import make_repo

from waystation_server import registry_auth
from waystation_server.formats.base import SetupContext
from waystation_server.formats.docker.descriptor import DESCRIPTOR, DockerOptions, connector_host
from waystation_server.repos.model import RepoType
from waystation_server.routing import connector_port
from waystation_server.services import Services

BASE = 'https://registry.example.com:8443'


def test_connector_port_option_range() -> None:
    assert DockerOptions().connector_port is None
    assert DockerOptions(connector_port=5001).connector_port == 5001
    for bad in (80, 1023, 65536):
        with pytest.raises(ValidationError):
            DockerOptions(connector_port=bad)


@pytest.mark.parametrize(
    ('base', 'port', 'expected'),
    [
        (BASE, 5001, 'registry.example.com:5001'),
        ('http://registry.example.com', 5002, 'registry.example.com:5002'),
        ('https://[2001:db8::1]:8443', 5001, '[2001:db8::1]:5001'),
        (BASE, None, None),
        (BASE, True, None),
        (BASE, '5001', None),
    ],
)
def test_connector_host(base: str, port: Any, expected: str | None) -> None:
    assert connector_host(base, port) == expected


def _snippets(repo_type: RepoType, **options: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {'type': repo_type, 'format_options': options}
    if repo_type is RepoType.HOSTED:
        fields |= {'proxy': None, 'hosted': {}}
    elif repo_type is RepoType.GROUP:
        fields |= {'proxy': None, 'group': {'members': ['a', 'b'], 'writable_member': 'a'}}
    repo = make_repo('dock', 'docker', **fields)
    clients = DESCRIPTOR.setup_snippets(SetupContext(repo, BASE, 'registry.example.com:8443', None))
    docker = next(c for c in clients if c.id == 'docker')
    return {s.id: s for s in docker.snippets}


def test_connector_snippets() -> None:
    proxy = _snippets(RepoType.PROXY, docker_hub=True, connector_port=5001)
    assert proxy['docker-connector'].content == 'docker pull registry.example.com:5001/library/nginx:latest'
    assert proxy['docker-connector'].title == 'Pull through the dedicated port 5001'
    assert 'docker login registry.example.com:5001' in (proxy['docker-connector'].description or '')
    hosted = _snippets(RepoType.HOSTED, connector_port=5002)
    assert hosted['docker-connector'].content.splitlines() == [
        'docker login registry.example.com:5002',
        'docker tag myapp:1.0 registry.example.com:5002/myapp:1.0',
        'docker push registry.example.com:5002/myapp:1.0',
    ]
    group = _snippets(RepoType.GROUP, connector_port=5003)
    assert group['docker-connector'].content == 'docker pull registry.example.com:5003/team/app:1.0'
    for repo_type in (RepoType.PROXY, RepoType.HOSTED, RepoType.GROUP):
        assert 'docker-connector' not in _snippets(repo_type)


def _request(headers: dict[str, str], *, internal: bool | None) -> Request:
    scope: dict[str, Any] = {
        'type': 'http',
        'method': 'GET',
        'path': '/v2/',
        'headers': [(k.lower().encode(), v.encode('latin-1')) for k, v in headers.items()],
    }
    if internal is not None:
        scope['state'] = {'internal_hop': internal}
    return Request(scope)


@pytest.mark.parametrize(
    ('value', 'internal', 'expected'),
    [
        ('5001', True, 5001),
        (' 5002 ', True, 5002),
        ('5001', False, None),  # not from the web tier: ignored
        ('5001', None, None),  # no request context at all
        ('abc', True, 0),  # malformed: routes nowhere
        ('123456', True, 0),
        ('500\xb2', True, 0),  # a non-ASCII digit (superscript two)
        ('', True, None),
    ],
)
def test_connector_header_is_trusted_from_internal_hops_only(value: str, internal: bool | None, expected: Any) -> None:
    assert connector_port(_request({'X-Waystation-Connector': value}, internal=internal)) == expected


def test_no_header_no_connector() -> None:
    assert connector_port(_request({}, internal=True)) is None


class _Services:
    base_url = 'https://registry.example.com'


def test_challenge_realm() -> None:
    services: Any = _Services()
    realm = registry_auth.challenge(services, scope='repository:a:pull', connector=5001).headers['www-authenticate']
    assert realm.startswith('Bearer realm="https://registry.example.com/auth/token?connector=5001",')
    # the connector wins over ns (ns is never sent on a connector anyway)
    both = registry_auth.challenge(services, scope=None, ns='quay.io', connector=5001).headers['www-authenticate']
    assert 'ns=' not in both
    plain = registry_auth.challenge(services, scope=None).headers['www-authenticate']
    assert plain.startswith('Bearer realm="https://registry.example.com/auth/token",')


@pytest.mark.parametrize(
    'unit_repos',
    [[make_repo('hub', 'docker', format_options={'connector_port': 5001}),
      make_repo('raw-thing', 'raw', format_options={'connector_port': 5002}),  # not docker: never a connector
      make_repo('plain', 'docker')]],
)  # fmt: skip
async def test_catalog_lookup_by_connector_port(unit_services: Services) -> None:
    hub = await unit_services.catalog.docker_connector(5001)
    assert hub is not None and hub.name == 'hub'
    assert await unit_services.catalog.docker_connector(5002) is None
    assert await unit_services.catalog.docker_connector(5003) is None
