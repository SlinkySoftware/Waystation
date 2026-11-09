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


class SsrfGuard:
    def __init__(self, allow_cidrs: Iterable[str] = ()) -> None:
        self.allow: tuple[IPNetwork, ...] = tuple(ipaddress.ip_network(c, strict=False) for c in allow_cidrs)

    def allowed(self, ip: IPAddress, *, allow_private: bool) -> bool:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if allow_private:
            return not ip.is_unspecified and not ip.is_multicast
        if any(ip.version == net.version and ip in net for net in self.allow):
            return True
        return ip.is_global and not ip.is_multicast

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
