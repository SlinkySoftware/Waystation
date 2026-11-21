"""Upstream GETs for worker jobs that refresh a proxy repository's cache (``apt.refresh``), with the proxy engine's
rules (ARCHITECTURE §6.1):

- the repository's network settings: SSRF guard (``waystation_common.ssrf``: the connected address is the checked
  one; through an outbound proxy the target is checked before each hop), ``allow_private_upstream``, extra CA
  certificates (``ca_pem`` + ``WAYSTATION_EXTRA_CA_FILE``), per-repository ``http_proxy`` else the server-wide
  ``WAYSTATION_HTTP_PROXY``/``NO_PROXY``;
- its static credentials (basic or bearer, decrypted with ``WAYSTATION_SECRET_KEYS``), sent only to the upstream's
  origin, never to a redirect target on another host; never logged;
- redirects followed by hand (at most 5, each hop re-checked), connect 10 s / read 60 s idle timeouts, no retries;
- the repository's auto-block breaker (``waystation_common.breaker``, the state the server's proxy engine uses): a
  blocked upstream is not called (one probe per ended period across processes), and every outcome is recorded.

Bodies are read by the caller with a size bound (``read_bounded``, ``copy_bounded``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ssl
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from redis.asyncio import Redis

from waystation_common.breaker import AutoBlockBreaker, BreakerTarget, blocking_status
from waystation_common.crypto import Keyring
from waystation_common.db.client import Document
from waystation_common.errors import PayloadTooLarge, UpstreamError
from waystation_common.events import keys as event_keys
from waystation_common.settings import Settings
from waystation_common.ssrf import GuardedBackend, SsrfGuard, UpstreamBlocked
from waystation_common.storage import BlobWriter

MAX_REDIRECTS = 5
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
CHUNK = 256 * 1024
USER_AGENT = 'Waystation (+https://github.com/SlinkySoftware/Waystation)'
NO_BREAKER = BreakerTarget(repo_id='', name='', enabled=False)  # auto-block does not apply


def secret_aad(repo_id: str) -> bytes:
    """Associated data of a repository's encrypted upstream secret (as the server stores it)."""
    return f'repo:{repo_id}'.encode()


@dataclass(frozen=True)
class Upstream:
    """How to reach one proxy repository's upstream (from its ``repositories`` document)."""

    repo_id: str
    url: str
    auth_kind: str = 'none'
    username: str | None = None
    secret_enc: str | None = None
    allow_private: bool = False
    ca_pem: str | None = None
    http_proxy: str | None = None
    breaker: BreakerTarget = NO_BREAKER

    @classmethod
    def of(cls, repo: Document) -> Upstream:
        proxy: dict[str, Any] = repo.get('proxy') or {}
        auth: dict[str, Any] = proxy.get('auth') or {}
        return cls(
            repo_id=str(repo['_id']),
            url=str(proxy['upstream_url']),
            auth_kind=str(auth.get('kind') or 'none'),
            username=auth.get('username'),
            secret_enc=auth.get('secret_enc'),
            allow_private=bool(proxy.get('allow_private_upstream')),
            ca_pem=proxy.get('ca_pem') or None,
            http_proxy=proxy.get('http_proxy') or None,
            breaker=BreakerTarget.of_document(repo),
        )

    def url_for(self, path: str) -> str:
        base = self.url if self.url.endswith('/') else self.url + '/'
        return urljoin(base, path.lstrip('/'))


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or '').lower(), parts.port or (443 if scheme == 'https' else 80)


def _ssl_context(extra_ca: str | None, ca_pem: str | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if extra_ca:
        ctx.load_verify_locations(cafile=extra_ca)
    if ca_pem:
        ctx.load_verify_locations(cadata=ca_pem)
    return ctx


async def blocked(valkey: Redis | None, repo_id: str, *, now_ms: int | None = None) -> bool:
    """Whether the repository's auto-block breaker blocks its upstream now (Valkey down: not blocked)."""
    if valkey is None:
        return False
    try:
        raw = await valkey.hget(event_keys.upstream_health_key(repo_id), 'blocked_until')
    except Exception:
        return False
    if not raw:
        return False
    try:
        until = int(float(raw))
    except ValueError:
        return False
    return until > (now_ms if now_ms is not None else int(time.time() * 1000))


class Fetcher:
    def __init__(
        self, settings: Settings | None, keyring: Keyring | None, *, breaker: AutoBlockBreaker | None = None
    ) -> None:
        self.breaker = breaker  # None: no auto-block (tests, no Valkey)
        self.guard = SsrfGuard(settings.ssrf_allow_cidrs if settings else ())
        self.proxy = (settings.http_proxy or None) if settings else None
        raw_no_proxy = (settings.no_proxy or '') if settings else ''
        self.no_proxy = {h.strip().lower() for h in raw_no_proxy.split(',') if h.strip()}
        self.extra_ca = str(settings.extra_ca_file) if settings and settings.extra_ca_file else None
        self.keyring = keyring
        self.timeout = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
        self._ssl: dict[str, ssl.SSLContext] = {}
        self._clients: dict[tuple[bool, str, str], httpx.AsyncClient] = {}

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.aclose()

    def proxy_for(self, url: str, upstream: Upstream) -> str | None:
        if upstream.http_proxy:
            return upstream.http_proxy
        host = (urlsplit(url).hostname or '').lower()
        if not self.proxy or any(host == h or host.endswith('.' + h.lstrip('.')) for h in self.no_proxy):
            return None
        return self.proxy

    async def _client(self, upstream: Upstream, proxy: str | None) -> httpx.AsyncClient:
        ca_key = hashlib.sha256(upstream.ca_pem.encode()).hexdigest() if upstream.ca_pem else ''
        key = (upstream.allow_private, ca_key, proxy or '')
        client = self._clients.get(key)
        if client is not None:
            return client
        ctx = self._ssl.get(ca_key)
        if ctx is None:
            ctx = await asyncio.to_thread(_ssl_context, self.extra_ca, upstream.ca_pem)
            self._ssl[ca_key] = ctx
        common: dict[str, Any] = {
            'timeout': self.timeout,
            'follow_redirects': False,
            'trust_env': False,
            'headers': {'User-Agent': USER_AGENT},
        }
        if proxy:
            transport = httpx.AsyncHTTPTransport(verify=ctx, proxy=proxy)
        else:
            transport = httpx.AsyncHTTPTransport(verify=ctx)
            # The SSRF guard as the network backend (as the proxy engine does, ARCH §6.1).
            transport._pool = httpcore.AsyncConnectionPool(
                ssl_context=ctx,
                max_connections=8,
                http1=True,
                http2=False,
                network_backend=GuardedBackend(self.guard, allow_private=upstream.allow_private),
            )
        client = httpx.AsyncClient(transport=transport, **common)
        self._clients[key] = client
        return client

    def _auth_headers(self, upstream: Upstream) -> dict[str, str]:
        if upstream.auth_kind not in ('basic', 'bearer') or not upstream.secret_enc:
            return {}
        if self.keyring is None:
            raise UpstreamError('the upstream credentials cannot be decrypted (WAYSTATION_SECRET_KEYS not set)')
        try:
            secret = self.keyring.decrypt_str(upstream.secret_enc, aad=secret_aad(upstream.repo_id))
        except Exception:
            raise UpstreamError('the upstream credentials cannot be decrypted') from None
        if upstream.auth_kind == 'bearer':
            return {'Authorization': f'Bearer {secret}'}
        token = base64.b64encode(f'{upstream.username or ""}:{secret}'.encode()).decode('ascii')
        return {'Authorization': f'Basic {token}'}

    async def _report(self, upstream: Upstream, *, ok: bool, reason: str = '') -> None:
        """Feed the auto-block breaker (ARCH §6.1) like the proxy engine does."""
        if self.breaker is not None and upstream.breaker.enabled:
            await self.breaker.record(upstream.breaker, ok=ok, reason=reason)

    @contextlib.asynccontextmanager
    async def get(
        self, upstream: Upstream, path: str, *, headers: dict[str, str] | None = None
    ) -> AsyncIterator[httpx.Response]:
        """``GET`` ``path`` below the upstream URL; yields the final (non-redirect) streaming response. The request
        first passes the repository's auto-block breaker (``UpstreamAutoBlocked`` when blocked; one probe per ended
        period across processes) and its outcome is recorded there: connection failures, timeouts, 5xx and 429 (and
        a body that breaks off) as failures, other answers as successes; SSRF refusals and bad credentials not at
        all."""
        url = upstream.url_for(path)
        origin = _origin(upstream.url)
        auth = self._auth_headers(upstream)
        base_headers = {'Accept-Encoding': 'identity', **(headers or {})}
        if self.breaker is not None and upstream.breaker.enabled:
            await self.breaker.check(upstream.breaker)
        for _ in range(MAX_REDIRECTS + 1):
            if urlsplit(url).scheme not in ('http', 'https'):
                raise UpstreamError('upstream redirected to an unsupported URL')
            proxy = self.proxy_for(url, upstream)
            try:
                if proxy:  # the proxy connects: check the target here (ARCH §6.1)
                    _, host, port = _origin(url)
                    await self.guard.resolve(host, port, allow_private=upstream.allow_private)
                client = await self._client(upstream, proxy)
                hop_headers = dict(base_headers)
                if _origin(url) == origin:
                    hop_headers.update(auth)  # credentials never go to another host
                response = await client.send(client.build_request('GET', url, headers=hop_headers), stream=True)
            except UpstreamBlocked:
                raise  # an SSRF refusal says nothing about the upstream's health
            except UpstreamError as exc:  # DNS failures
                await self._report(upstream, ok=False, reason=exc.detail)
                raise
            except httpx.TimeoutException:
                await self._report(upstream, ok=False, reason='upstream timed out')
                raise UpstreamError('upstream timed out') from None
            except httpx.HTTPError:
                await self._report(upstream, ok=False, reason='upstream unreachable')
                raise UpstreamError('upstream unreachable') from None
            status = response.status_code
            if status in REDIRECT_CODES and 'location' in response.headers:
                await response.aclose()
                url = urljoin(url, response.headers['location'])
                continue
            if blocking_status(status):
                await self._report(upstream, ok=False, reason=f'upstream answered {status}')
            elif status != 200:
                await self._report(upstream, ok=True)
            try:
                yield response
            except BodyFailed as exc:  # a 200 succeeds once its body arrived (as in the proxy engine)
                await self._report(upstream, ok=False, reason=exc.detail)
                raise
            finally:
                await response.aclose()
            if status == 200:
                await self._report(upstream, ok=True)
            return
        raise UpstreamError('upstream redirected too many times')


class BodyFailed(UpstreamError):  # noqa: N818 - reads as an event, like the other upstream errors
    """The upstream's body broke off or timed out mid-transfer."""


def _identity(response: httpx.Response) -> None:
    """The bytes must be the file itself (``Accept-Encoding: identity`` was sent): never decode on the fly."""
    encoding = response.headers.get('content-encoding', '').strip().lower()
    if encoding not in ('', 'identity'):
        raise UpstreamError(f'upstream sent a {encoding} content encoding although identity was asked for')


async def read_bounded(response: httpx.Response, max_bytes: int) -> bytes:
    """The body, refused past ``max_bytes`` (``PayloadTooLarge``)."""
    _identity(response)
    declared = response.headers.get('content-length')
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise PayloadTooLarge(f'upstream body exceeds {max_bytes} bytes')
    out = bytearray()
    try:
        async for chunk in response.aiter_raw(CHUNK):
            out += chunk
            if len(out) > max_bytes:
                raise PayloadTooLarge(f'upstream body exceeds {max_bytes} bytes')
    except httpx.HTTPError:
        raise BodyFailed('upstream connection failed') from None
    return bytes(out)


async def copy_bounded(response: httpx.Response, writer: BlobWriter) -> None:
    """Stream the body into ``writer`` (its size limit and expected size/digest apply at write and commit)."""
    _identity(response)
    try:
        async for chunk in response.aiter_raw(CHUNK):
            await writer.write(chunk)
    except httpx.HTTPError:
        raise BodyFailed('upstream connection failed') from None
