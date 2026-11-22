"""SSRF guard (SPECIFICATION §4.1, CLAUDE.md "never trust upstream content").

Upstream connections may only go to globally routable addresses, unless the repository sets
``allow_private_upstream`` or the address is in ``WAYSTATION_SSRF_ALLOW_CIDRS``. The check runs **at connect time**
inside the HTTP client's network backend (``GuardedBackend``): the address that was checked is the address that is
connected to, so DNS rebinding between a check and the connection is impossible, and every redirect hop to a new
host goes through the same check. TLS still verifies the original host name (SNI and certificate).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import Any

import httpcore

from waystation_common.errors import UpstreamError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class UpstreamBlocked(UpstreamError):  # noqa: N818 - reads as a state, like the other upstream errors
    """The upstream resolves to an address the SSRF guard refuses."""

    code = 'upstream_blocked'


_NAT64 = ipaddress.IPv6Network('64:ff9b::/96')  # RFC 6052 well-known prefix (Python counts it as global)
_IPV4_COMPATIBLE = ipaddress.IPv6Network('::/96')  # deprecated IPv4-compatible form (RFC 4291); also holds :: and ::1
# Deprecated site-local addresses (RFC 3879): never global, but Python calls them global.
_SITE_LOCAL = ipaddress.IPv6Network('fec0::/10')  # forbidden-strings: allow


def embedded_ipv4(ip: IPAddress) -> IPAddress:
    """The IPv4 address an IPv6 address stands for when the kernel or a NAT64 gateway turns the connection into an
    IPv4 one (IPv4-mapped, IPv4-compatible, NAT64 well-known prefix); else ``ip``. 6to4 and Teredo addresses are not
    global, so the guard refuses them as they are."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return ip
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if (ip in _IPV4_COMPATIBLE and int(ip) > 1) or ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


class SsrfGuard:
    def __init__(self, allow_cidrs: Iterable[str] = ()) -> None:
        self.allow: tuple[IPNetwork, ...] = tuple(ipaddress.ip_network(c, strict=False) for c in allow_cidrs)

    def allowed(self, ip: IPAddress, *, allow_private: bool) -> bool:
        ip = embedded_ipv4(ip)
        if allow_private:
            return not ip.is_unspecified and not ip.is_multicast
        if any(ip.version == net.version and ip in net for net in self.allow):
            return True
        site_local = isinstance(ip, ipaddress.IPv6Address) and ip in _SITE_LOCAL
        return ip.is_global and not ip.is_multicast and not site_local

    async def resolve(self, host: str, port: int, *, allow_private: bool) -> list[IPAddress]:
        """Resolve ``host`` and return its addresses if **all** of them are allowed, else raise ``UpstreamBlocked``."""
        try:
            literal = ipaddress.ip_address(host.strip('[]'))
        except ValueError:
            literal = None
        if literal is not None:
            ips: list[IPAddress] = [literal]
        else:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise UpstreamError(f'cannot resolve upstream host {host}') from exc
            ips = list(dict.fromkeys(ipaddress.ip_address(str(info[4][0]).split('%')[0]) for info in infos))
        if not ips:
            raise UpstreamError(f'cannot resolve upstream host {host}')
        blocked = [ip for ip in ips if not self.allowed(ip, allow_private=allow_private)]
        if blocked:
            raise UpstreamBlocked(f'upstream host {host} resolves to a private or reserved address')
        return ips


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that connects only to addresses the guard allows."""

    def __init__(
        self, guard: SsrfGuard, *, allow_private: bool, inner: httpcore.AsyncNetworkBackend | None = None
    ) -> None:
        self.guard = guard
        self.allow_private = allow_private
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        ips = await self.guard.resolve(host, port, allow_private=self.allow_private)
        error: Exception | None = None
        for ip in ips:
            try:
                return await self.inner.connect_tcp(
                    str(ip), port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
        assert error is not None
        raise error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore interface
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError('unix sockets are not allowed for upstreams')

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)
