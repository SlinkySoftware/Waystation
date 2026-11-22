"""SSRF corpus (T-R01, Documents/Security/ASVS_L2.md V12.6 / V5.2.6) against ``waystation_common.ssrf``.

Every address and URL below must be refused unless the repository allows private upstreams, and the refusal must
happen before any connection: the guard resolves once and connects to the very address it checked (no second
lookup, so DNS rebinding between the check and the connection cannot reach a private address).
Lines with non-documentation addresses carry the forbidden-strings pragma (Build/ci/forbidden_strings.sh).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
import pytest

from waystation_common.errors import UpstreamError
from waystation_common.ssrf import GuardedBackend, SsrfGuard, UpstreamBlocked, embedded_ipv4

BLOCKED = [
    '127.0.0.1',
    '127.255.255.254',
    '0.0.0.0',
    '10.0.0.1',  # forbidden-strings: allow
    '172.16.0.1',  # forbidden-strings: allow
    '192.168.1.1',  # forbidden-strings: allow
    '100.64.0.1',  # carrier-grade NAT; forbidden-strings: allow
    '169.254.169.254',  # cloud metadata; forbidden-strings: allow
    '192.0.0.170',  # IETF protocol assignments; forbidden-strings: allow
    '198.18.0.1',  # benchmarking; forbidden-strings: allow
    '192.0.2.1',  # documentation
    '224.0.0.1',  # multicast; forbidden-strings: allow
    '240.0.0.1',  # reserved; forbidden-strings: allow
    '255.255.255.255',  # forbidden-strings: allow
    '::',
    '::1',
    'fe80::1',  # forbidden-strings: allow
    'fc00::1',  # forbidden-strings: allow
    'fd12:3456::1',  # forbidden-strings: allow
    'ff02::1',  # forbidden-strings: allow
    'fec0::1',  # deprecated site-local (Python calls it global); forbidden-strings: allow
    '2001:db8::1',
    '::ffff:127.0.0.1',  # IPv4-mapped loopback; forbidden-strings: allow
    '::ffff:7f00:1',  # the same, hex; forbidden-strings: allow
    '::ffff:169.254.169.254',  # forbidden-strings: allow
    '::127.0.0.1',  # IPv4-compatible loopback (Python calls it global); forbidden-strings: allow
    '::a9fe:a9fe',  # IPv4-compatible metadata address; forbidden-strings: allow
    '64:ff9b::7f00:1',  # NAT64 loopback (Python calls it global); forbidden-strings: allow
    '64:ff9b::a9fe:a9fe',  # NAT64 metadata address; forbidden-strings: allow
    '64:ff9b::a00:1',  # NAT64 private; forbidden-strings: allow
    '64:ff9b:1::7f00:1',  # local-use NAT64; forbidden-strings: allow
    '2002:7f00:1::1',  # 6to4 of loopback; forbidden-strings: allow
    '2002:a9fe:a9fe::1',  # 6to4 of the metadata address; forbidden-strings: allow
    '2001:0:4136:e378:8000:63bf:80ff:fffe',  # Teredo (client 127.0.0.1); forbidden-strings: allow
]
ALLOWED = [
    '8.8.8.8',  # forbidden-strings: allow
    '2606:4700::1111',  # forbidden-strings: allow
    '::ffff:8.8.8.8',  # forbidden-strings: allow
    '64:ff9b::808:808',  # NAT64 of a public address; forbidden-strings: allow
]


@pytest.mark.parametrize('ip', BLOCKED)
def test_private_and_special_addresses_are_refused(ip: str) -> None:
    assert not SsrfGuard().allowed(ipaddress.ip_address(ip), allow_private=False)


@pytest.mark.parametrize('ip', ALLOWED)
def test_public_addresses_pass(ip: str) -> None:
    assert SsrfGuard().allowed(ipaddress.ip_address(ip), allow_private=False)


NEVER = [
    '0.0.0.0',
    '::',
    '::ffff:0.0.0.0',  # forbidden-strings: allow
    '224.0.0.1',  # forbidden-strings: allow
    'ff02::1',  # forbidden-strings: allow
    '64:ff9b::e000:1',  # NAT64 multicast; forbidden-strings: allow
]


@pytest.mark.parametrize('ip', NEVER)
def test_unspecified_and_multicast_stay_refused_with_allow_private(ip: str) -> None:
    assert not SsrfGuard().allowed(ipaddress.ip_address(ip), allow_private=True)


def test_allow_private_and_the_allow_list() -> None:
    assert SsrfGuard().allowed(ipaddress.ip_address('10.0.0.1'), allow_private=True)  # forbidden-strings: allow
    guard = SsrfGuard(['198.51.100.0/24', '2001:db8:1::/48'])
    assert guard.allowed(ipaddress.ip_address('198.51.100.7'), allow_private=False)
    assert guard.allowed(ipaddress.ip_address('::ffff:198.51.100.7'), allow_private=False)  # forbidden-strings: allow
    assert guard.allowed(ipaddress.ip_address('2001:db8:1::7'), allow_private=False)
    assert not guard.allowed(ipaddress.ip_address('192.0.2.7'), allow_private=False)


EMBEDDED = [
    ('::ffff:127.0.0.1', '127.0.0.1'),  # forbidden-strings: allow
    ('::7f00:1', '127.0.0.1'),  # forbidden-strings: allow
    ('64:ff9b::7f00:1', '127.0.0.1'),  # forbidden-strings: allow
    ('::1', '::1'),  # not 0.0.0.1; forbidden-strings: allow
]


def test_embedded_ipv4() -> None:
    for ip, embedded in EMBEDDED:
        assert embedded_ipv4(ipaddress.ip_address(ip)) == ipaddress.ip_address(embedded)
    assert embedded_ipv4(ipaddress.ip_address('2001:db8::1')) == ipaddress.ip_address('2001:db8::1')
    assert embedded_ipv4(ipaddress.ip_address('192.0.2.1')) == ipaddress.ip_address('192.0.2.1')


# Host spellings the resolver turns into loopback (inet_aton forms) or that are loopback by name.
LOOPBACK_SPELLINGS = [
    'localhost',
    'LOCALHOST',
    'localhost.',
    '2130706433',
    '0x7f000001',
    '017700000001',  # forbidden-strings: allow
    '127.1',
    '0177.0.0.1',
    '0x7f.0.0.1',
    '[::1]',
    '[::ffff:127.0.0.1]',  # forbidden-strings: allow
    '0',
]  # forbidden-strings: allow


@pytest.mark.parametrize('host', LOOPBACK_SPELLINGS)
async def test_numeric_and_named_loopback_spellings_are_refused(host: str) -> None:
    with pytest.raises(UpstreamError):  # UpstreamBlocked, or not resolvable at all: never a connection
        await SsrfGuard().resolve(host, 80, allow_private=False)


class _Resolver:
    """A scripted ``getaddrinfo``: answers each call with the next list of addresses (DNS rebinding)."""

    def __init__(self, *answers: Iterable[str]) -> None:
        self.answers = [list(a) for a in answers]
        self.calls = 0

    async def __call__(self, host: str, port: int, **_: Any) -> list[tuple[Any, ...]]:
        addrs = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        family = socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, '', (a, port)) for a in addrs]


class _Recorder(httpcore.AsyncNetworkBackend):
    """The inner network backend: records what the guard asks it to connect to and refuses."""

    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def connect_tcp(self, host: str, port: int, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        self.hosts.append(host)
        raise httpcore.ConnectError('recorder: no network in tests')

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        raise AssertionError('never')

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


async def test_any_private_answer_refuses_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = _Resolver(['192.0.2.1', '8.8.8.8'])  # forbidden-strings: allow
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolver)
    with pytest.raises(UpstreamBlocked):
        await SsrfGuard(['8.8.8.8/32']).resolve(  # forbidden-strings: allow
            'mixed.example.com', 443, allow_private=False
        )  # forbidden-strings: allow


async def test_dns_rebinding_cannot_reach_a_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """First lookup public, every later one loopback: the guard connects to the address it checked, by number,
    and resolves exactly once per connection attempt."""
    resolver = _Resolver(['198.51.100.7'], ['127.0.0.1'])
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolver)
    inner = _Recorder()
    backend = GuardedBackend(SsrfGuard(['198.51.100.0/24']), allow_private=False, inner=inner)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp('rebind.example.com', 443)
    assert inner.hosts == ['198.51.100.7']
    assert resolver.calls == 1
    with pytest.raises(UpstreamBlocked):  # the next connection sees the rebound answer and is refused
        await backend.connect_tcp('rebind.example.com', 443)
    assert inner.hosts == ['198.51.100.7']


async def test_unix_sockets_are_refused() -> None:
    with pytest.raises(httpcore.ConnectError):
        await GuardedBackend(SsrfGuard(), allow_private=True).connect_unix_socket('/var/run/docker.sock')


HOSTILE_URLS = [
    'http://example.com@127.0.0.1:9/',  # userinfo: the host is what follows the @
    'http://user:pass@[::1]:9/',
    'http://127.0.0.1:9#@example.com/',
    'http://[::ffff:127.0.0.1]:9/',  # forbidden-strings: allow
    'http://[::ffff:7f00:1]:9/',  # forbidden-strings: allow
    'http://[::127.0.0.1]:9/',  # forbidden-strings: allow
    'http://[64:ff9b::a9fe:a9fe]/latest/meta-data/',  # forbidden-strings: allow
    'http://169.254.169.254/latest/meta-data/',  # forbidden-strings: allow
    'http://[fe80::1%25eth0]:9/',  # forbidden-strings: allow
    'http://2130706433:9/',
    'http://0x7f000001:9/',
    'http://0177.0.0.1:9/',
    'http://127.1:9/',
    'http://0:9/',
    'http://[::]:9/',
    'http://localhost:9/',
    'http://LOCALHOST.:9/',
]


@pytest.mark.parametrize('url', HOSTILE_URLS)
async def test_hostile_urls_never_connect(url: str) -> None:
    inner = _Recorder()
    transport = httpx.AsyncHTTPTransport()
    transport._pool = httpcore.AsyncConnectionPool(
        network_backend=GuardedBackend(SsrfGuard(), allow_private=False, inner=inner)
    )
    async with httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False) as client:
        with pytest.raises((UpstreamError, httpx.HTTPError, httpx.InvalidURL, ValueError)):
            await client.get(url)
    assert inner.hosts == [], f'{url} reached the network backend: {inner.hosts}'
