"""Shared upstream HTTP clients (ARCHITECTURE §6.1).

One ``httpx.AsyncClient`` per (scheme, host, port, SSRF mode, CA bundle, outbound proxy): HTTP/2 when the upstream
offers it, pooled connections, connect timeout 10 s, read timeout 60 s between bytes, no automatic redirects (the
engine follows them itself so every hop is re-checked and credentials never leak to another host).

Per repository (``Network``): ``allow_private`` (SSRF guard exception), ``ca_pem`` (extra trusted CA certificates,
added to the system store and ``WAYSTATION_EXTRA_CA_FILE`` for that repository's clients only) and ``http_proxy``
(an outbound proxy for that repository; null = the server-wide ``WAYSTATION_HTTP_PROXY`` unless the host matches
``WAYSTATION_NO_PROXY``).

SSRF: direct connections go through the guard's network backend (the checked address is the connected address).
Through an outbound proxy the proxy resolves and connects, so the guard checks the target before every request and
every redirect hop instead (``client()`` does it): the same rules apply, with a DNS-rebinding window between the
check and the proxy's own lookup. The proxy itself is configured by an administrator and is not checked. A repository
with ``allow_private_upstream`` behind a proxy may use names only the proxy can resolve.
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpcore
import httpx
from cryptography import x509

from waystation_common.errors import UpstreamError
from waystation_common.settings import Settings
from waystation_server.proxy.ssrf import GuardedBackend, SsrfGuard, UpstreamBlocked
from waystation_server.repos.model import StoredProxy

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 60.0
MAX_CONNECTIONS_PER_HOST = 64
USER_AGENT = 'Waystation (+https://github.com/SlinkySoftware/Waystation)'
CA_PEM_MAX_CERTS = 100


@dataclass(frozen=True)
class Timeouts:
    connect: float = CONNECT_TIMEOUT_S
    read: float = READ_TIMEOUT_S

    def httpx(self) -> httpx.Timeout:
        return httpx.Timeout(connect=self.connect, read=self.read, write=self.read, pool=self.connect)


@dataclass(frozen=True)
class Network:
    """How one repository reaches its upstream."""

    allow_private: bool = False
    ca_pem: str | None = None
    http_proxy: str | None = None


def network_of(proxy: StoredProxy) -> Network:
    return Network(allow_private=proxy.allow_private_upstream, ca_pem=proxy.ca_pem, http_proxy=proxy.http_proxy)


def ssl_context(extra_ca_file: str | None = None, ca_pem: str | None = None) -> ssl.SSLContext:
    """A verifying context: system CAs + ``extra_ca_file`` + ``ca_pem`` (blocking: loads files and parses PEM)."""
    ctx = ssl.create_default_context()
    if extra_ca_file:
        ctx.load_verify_locations(cafile=extra_ca_file)
    if ca_pem:
        ctx.load_verify_locations(cadata=ca_pem)
    return ctx


def check_ca_pem(pem: str) -> str | None:
    """Why ``pem`` is not a usable CA bundle (None when it is). Blocking: run in a thread."""
    if '-----BEGIN CERTIFICATE-----' not in pem:
        return 'must contain at least one PEM certificate (-----BEGIN CERTIFICATE-----)'
    try:
        certs = x509.load_pem_x509_certificates(pem.encode('ascii'))
    except (ValueError, UnicodeEncodeError) as exc:
        return f'not a valid PEM certificate bundle ({exc.__class__.__name__})'
    if len(certs) > CA_PEM_MAX_CERTS:
        return f'at most {CA_PEM_MAX_CERTS} certificates'
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=pem)
    except (ssl.SSLError, ValueError) as exc:
        return f'not usable as a CA bundle ({exc.__class__.__name__})'
    return None


def check_proxy_url(url: str) -> str | None:
    """Why ``url`` is not a usable outbound proxy URL (None when it is)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return 'not a valid URL'
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return 'must be an http:// or https:// URL with a host, e.g. http://proxy.example.com:3128'
    if parts.username or parts.password:
        return 'credentials in the proxy URL are not supported per repository (use WAYSTATION_HTTP_PROXY)'
    if parts.path not in ('', '/') or parts.query or parts.fragment:
        return 'must not have a path, query or fragment'
    if port == 0:
        return 'invalid port'
    return None


def _no_proxy(raw: str | None) -> set[str]:
    return {h.strip().lower() for h in (raw or '').split(',') if h.strip()}


class UpstreamClients:
    def __init__(self, settings: Settings, *, timeouts: Timeouts | None = None) -> None:
        self.guard = SsrfGuard(settings.ssrf_allow_cidrs)
        self.timeouts = timeouts or Timeouts()
        self.proxy = settings.http_proxy or None
        self.no_proxy = _no_proxy(settings.no_proxy)
        self._extra_ca = str(settings.extra_ca_file) if settings.extra_ca_file else None
        self._ssl = ssl_context(self._extra_ca)
        self._ssl_by_ca: dict[str, ssl.SSLContext] = {}
        self._clients: dict[tuple[str, str, int, bool, str, str], httpx.AsyncClient] = {}

    def _uses_global_proxy(self, host: str) -> bool:
        return bool(self.proxy) and not any(host == h or host.endswith('.' + h.lstrip('.')) for h in self.no_proxy)

    def proxy_for(self, url: str, network: Network | None = None) -> str | None:
        """The outbound proxy a request to ``url`` goes through (None: direct)."""
        if network is not None and network.http_proxy:
            return network.http_proxy
        return self.proxy if self._uses_global_proxy((urlsplit(url).hostname or '').lower()) else None

    async def client(self, url: str, network: Network) -> httpx.AsyncClient:
        """The client for one request to ``url`` (redirect hops included). Through an outbound proxy the SSRF guard
        checks the target here. Raises ``ValueError`` for unsupported URLs and the guard's ``UpstreamError``s."""
        ca_key = await self._ensure_ca(network.ca_pem)
        client = self._get(url, network, ca_key)
        if self.proxy_for(url, network):
            await self.check_target(url, network)
        return client

    async def check_target(self, url: str, network: Network) -> None:
        """The SSRF check of ``url`` before a request. Through an outbound proxy with ``allow_private`` a name this
        server cannot resolve is left to the proxy (proxy-only DNS); every other failure raises."""
        try:
            await self.precheck(url, allow_private=network.allow_private)
        except UpstreamBlocked:
            raise
        except UpstreamError:
            if not (network.allow_private and self.proxy_for(url, network)):
                raise

    def client_for(self, url: str, *, allow_private: bool) -> httpx.AsyncClient:
        """The client for ``url`` with the server-wide settings (callers check ``precheck`` when proxied)."""
        return self._get(url, Network(allow_private=allow_private), '')

    async def _ensure_ca(self, pem: str | None) -> str:
        if not pem:
            return ''
        key = hashlib.sha256(pem.encode()).hexdigest()
        if key not in self._ssl_by_ca:
            ctx = await asyncio.to_thread(ssl_context, self._extra_ca, pem)
            self._ssl_by_ca.setdefault(key, ctx)
        return key

    def _get(self, url: str, network: Network, ca_key: str) -> httpx.AsyncClient:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError('upstream URL must be http(s) with a host')
        host = parts.hostname.lower()
        port = parts.port or (443 if scheme == 'https' else 80)
        proxy = self.proxy_for(url, network) or ''
        key = (scheme, host, port, network.allow_private, ca_key, proxy)
        client = self._clients.get(key)
        if client is None:
            ctx = self._ssl_by_ca[ca_key] if ca_key else self._ssl
            client = self._build(ctx, proxy=proxy or None, allow_private=network.allow_private)
            self._clients[key] = client
        return client

    def _build(self, ctx: ssl.SSLContext, *, proxy: str | None, allow_private: bool) -> httpx.AsyncClient:
        limits = httpx.Limits(max_connections=MAX_CONNECTIONS_PER_HOST, max_keepalive_connections=16)
        common = {
            'timeout': self.timeouts.httpx(),
            'follow_redirects': False,
            'headers': {'User-Agent': USER_AGENT},
            'trust_env': False,
        }
        if proxy:
            transport = httpx.AsyncHTTPTransport(http2=True, verify=ctx, limits=limits, proxy=proxy)
            return httpx.AsyncClient(transport=transport, **common)  # type: ignore[arg-type]
        transport = httpx.AsyncHTTPTransport(http2=True, verify=ctx, limits=limits)
        # Swap in a pool whose network backend enforces the SSRF guard at connect time. httpx exposes no public hook
        # for the backend; the pool is the documented httpcore object httpx wraps.
        transport._pool = httpcore.AsyncConnectionPool(
            ssl_context=ctx,
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
