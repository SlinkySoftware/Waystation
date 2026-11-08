"""Shared upstream HTTP clients (ARCHITECTURE §6.1).

One ``httpx.AsyncClient`` per (scheme, host, port, SSRF mode): HTTP/2 when the upstream offers it, pooled
connections, connect timeout 10 s, read timeout 60 s between bytes, no automatic redirects (the engine follows them
itself so every hop is re-checked and credentials never leak to another host). Connections go through the SSRF
guard's network backend; with an outbound proxy configured (``WAYSTATION_HTTP_PROXY``) the guard checks the target
before the request instead (the proxy resolves it).
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpcore
import httpx

from waystation_common.settings import Settings
from waystation_server.proxy.ssrf import GuardedBackend, SsrfGuard

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 60.0
MAX_CONNECTIONS_PER_HOST = 64
USER_AGENT = 'Waystation (+https://github.com/SlinkySoftware/Waystation)'


@dataclass(frozen=True)
class Timeouts:
    connect: float = CONNECT_TIMEOUT_S
    read: float = READ_TIMEOUT_S

    def httpx(self) -> httpx.Timeout:
        return httpx.Timeout(connect=self.connect, read=self.read, write=self.read, pool=self.connect)


def ssl_context(extra_ca_file: str | None = None) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if extra_ca_file:
        ctx.load_verify_locations(cafile=extra_ca_file)
    return ctx


class UpstreamClients:
    def __init__(self, settings: Settings, *, timeouts: Timeouts | None = None) -> None:
        self.guard = SsrfGuard(settings.ssrf_allow_cidrs)
        self.timeouts = timeouts or Timeouts()
        self.proxy = settings.http_proxy or None
        self.no_proxy = {h.strip().lower() for h in (settings.no_proxy or '').split(',') if h.strip()}
        self._ssl = ssl_context(str(settings.extra_ca_file) if settings.extra_ca_file else None)
        self._clients: dict[tuple[str, str, int, bool], httpx.AsyncClient] = {}

    def _uses_proxy(self, host: str) -> bool:
        return bool(self.proxy) and not any(host == h or host.endswith('.' + h.lstrip('.')) for h in self.no_proxy)

    def client_for(self, url: str, *, allow_private: bool) -> httpx.AsyncClient:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError('upstream URL must be http(s) with a host')
        host = parts.hostname.lower()
        port = parts.port or (443 if scheme == 'https' else 80)
        key = (scheme, host, port, allow_private)
        client = self._clients.get(key)
        if client is None:
            client = self._build(host, allow_private=allow_private)
            self._clients[key] = client
        return client

    def _build(self, host: str, *, allow_private: bool) -> httpx.AsyncClient:
        limits = httpx.Limits(max_connections=MAX_CONNECTIONS_PER_HOST, max_keepalive_connections=16)
        common = {
            'timeout': self.timeouts.httpx(),
            'follow_redirects': False,
            'headers': {'User-Agent': USER_AGENT},
            'trust_env': False,
        }
        if self._uses_proxy(host):
            transport = httpx.AsyncHTTPTransport(http2=True, verify=self._ssl, limits=limits, proxy=self.proxy)
            return httpx.AsyncClient(transport=transport, **common)  # type: ignore[arg-type]
        transport = httpx.AsyncHTTPTransport(http2=True, verify=self._ssl, limits=limits)
        # Swap in a pool whose network backend enforces the SSRF guard at connect time. httpx exposes no public hook
        # for the backend; the pool is the documented httpcore object httpx wraps.
        transport._pool = httpcore.AsyncConnectionPool(
            ssl_context=self._ssl,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=True,
            network_backend=GuardedBackend(self.guard, allow_private=allow_private),
        )
        return httpx.AsyncClient(transport=transport, **common)  # type: ignore[arg-type]

    async def precheck(self, url: str, *, allow_private: bool) -> None:
        """Resolve and check the URL's host now (used before proxied requests and by "test upstream")."""
        parts = urlsplit(url)
        host = parts.hostname or ''
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        await self.guard.resolve(host, port, allow_private=allow_private)

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.aclose()
