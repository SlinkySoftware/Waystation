"""Auto-block state, network options (CA bundle, outbound proxy) and their validation (T-S07), without services."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ws_testing import make_repo
from ws_tls import make_ca

from waystation_common.settings import Settings
from waystation_server.proxy.breaker import BlockState, UpstreamAutoBlocked, blocking_status
from waystation_server.proxy.http import Network, UpstreamClients, check_ca_pem, check_proxy_url, network_of
from waystation_server.repos.schemas import AutoBlockConfig, ProxyConfig
from waystation_server.repos.service import check_network
from waystation_server.stats.repos import Health, parse_health, status_of


def test_check_ca_pem(tmp_path: Path) -> None:
    pem = make_ca(tmp_path).ca_pem
    assert check_ca_pem(pem) is None
    assert check_ca_pem('comment line\n' + pem + pem) is None
    assert 'PEM certificate' in (check_ca_pem('hello') or '')
    broken = pem.replace(pem.splitlines()[3], 'AAAA')
    assert check_ca_pem(broken) is not None
    assert 'at most' in (check_ca_pem(pem * 101) or '')


@pytest.mark.parametrize(
    ('url', 'ok'),
    [
        ('http://proxy.example.com:3128', True),
        ('https://proxy.example.com/', True),
        ('socks5://proxy.example.com:1080', False),
        ('http://user:secret@proxy.example.com:3128', False),
        ('http://proxy.example.com:3128/path', False),
        ('http://proxy.example.com:3128?x=1', False),
        ('http://proxy.example.com:0', False),
        ('http://[::1', False),
        ('proxy.example.com:3128', False),
    ],
)
def test_check_proxy_url(url: str, ok: bool) -> None:
    assert (check_proxy_url(url) is None) is ok


async def test_check_network_field_errors(tmp_path: Path) -> None:
    good = ProxyConfig(upstream_url='https://upstream.example.com/', ca_pem=make_ca(tmp_path).ca_pem)
    assert await check_network(good) == []
    bad = ProxyConfig(
        upstream_url='https://upstream.example.com/',
        ca_pem='not a certificate',
        http_proxy='ftp://proxy.example.com',
        auto_block=AutoBlockConfig(initial_s=600, max_s=60),
    )
    fields = [e.field for e in await check_network(bad)]
    assert fields == ['proxy.ca_pem', 'proxy.http_proxy', 'proxy.auto_block.max_s']
    assert await check_network(ProxyConfig(upstream_url='https://u.example.com/', ca_pem='  ', http_proxy='')) == []


def test_proxy_selection_and_client_isolation(tmp_path: Path) -> None:
    clients = UpstreamClients(
        Settings(http_proxy='http://proxy.example.com:3128', no_proxy='internal.example.com,.corp.example.com')
    )
    assert clients.proxy_for('https://registry.example.com/v2/') == 'http://proxy.example.com:3128'
    assert clients.proxy_for('https://internal.example.com/x') is None
    assert clients.proxy_for('https://a.corp.example.com/x') is None
    own = Network(http_proxy='http://repo-proxy.example.com:8080')
    assert clients.proxy_for('https://internal.example.com/x', own) == 'http://repo-proxy.example.com:8080'
    direct = UpstreamClients(Settings())
    assert direct.proxy_for('https://registry.example.com/') is None
    url = 'https://registry.example.com/v2/'
    plain = direct.client_for(url, allow_private=False)
    assert direct.client_for(url, allow_private=False) is plain
    assert direct.client_for(url, allow_private=True) is not plain
    assert direct._get(url, own, '') is not plain


async def test_ca_bundle_gets_its_own_client(tmp_path: Path) -> None:
    clients = UpstreamClients(Settings())
    url = 'https://registry.example.com/v2/'
    pem = make_ca(tmp_path).ca_pem
    with_ca = await clients.client(url, Network(ca_pem=pem))
    assert with_ca is await clients.client(url, Network(ca_pem=pem))
    assert with_ca is not await clients.client(url, Network())
    await clients.aclose()


def test_network_of() -> None:
    repo = make_repo('r', proxy={'upstream_url': 'https://u.example.com/', 'http_proxy': 'http://p.example.com:1'})
    assert repo.proxy is not None
    assert network_of(repo.proxy) == Network(allow_private=False, ca_pem=None, http_proxy='http://p.example.com:1')
    assert repo.proxy.auto_block.enabled and repo.proxy.auto_block.failures == 5


def test_blocking_status() -> None:
    assert [blocking_status(s) for s in (200, 304, 401, 404, 429, 500, 503)] == [
        False,
        False,
        False,
        False,
        True,
        True,
        True,
    ]


def test_auto_blocked_error() -> None:
    exc = UpstreamAutoBlocked(1000.0, now=970.2)
    assert exc.status == 503
    assert exc.headers == {'Retry-After': '30'}
    assert 'auto-blocked' in exc.detail
    assert UpstreamAutoBlocked(1000.0, now=1000.0).headers == {'Retry-After': '1'}


def test_block_state_and_status() -> None:
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    until_ms = int((now + timedelta(seconds=60)).timestamp() * 1000)
    raw = {
        'block_failures': '5',
        'blocked_until': str(until_ms),
        'block_level': '2',
        'block_reason': 'upstream answered 503',
        'probe_until': str(until_ms + 1000),
        'consecutive_failures': '5',
        'last_error_at': now.isoformat(),
    }
    state = BlockState.parse(raw, now=now.timestamp())
    assert state.blocked and state.level == 2 and state.failures == 5 and state.probing
    assert state.blocked_until == now + timedelta(seconds=60)
    assert state.reason == 'upstream answered 503'
    assert not BlockState.parse({'blocked_until': 'x', 'block_level': 'y'}).blocked

    repo = make_repo('r')
    health = parse_health(raw, {})
    assert status_of(repo, health, now=now) == 'blocked'
    off = make_repo('r', proxy={'upstream_url': 'https://u.example.com/', 'auto_block': {'enabled': False}})
    assert status_of(off, health, now=now) == 'degraded'
    offline = make_repo('r', proxy={'upstream_url': 'https://u.example.com/', 'offline': True})
    assert status_of(offline, health, now=now) == 'offline'
    assert status_of(repo, Health(), now=now) == 'ok'
