"""The proxy engine (ARCHITECTURE §6, SPECIFICATION §4.1): what every proxy format plugin uses to serve a file.

A format turns a request into an ``AssetSpec`` (which asset, where upstream, what to expect) and calls
``await req.proxy.serve(req, spec)``; the engine returns the complete response:

- **Hit** (cached and fresh, or the repository is offline): ``X-Accel-Redirect`` to the blob (ARCH §4.5); the
  server answers conditional requests and HEAD itself (``dataplane.xaccel``). Cache state ``HIT``.
- **Stale metadata** (older than ``max_age``): conditional request upstream (``If-None-Match`` /
  ``If-Modified-Since``); ``304`` → ``REVALIDATED`` (only timestamps change); new content → fill; upstream error →
  ``STALE`` (served from cache) when the repository allows stale-on-error, else the error.
- **Miss**: negative cache (upstream 404s remembered in Valkey for ``negative_ttl_s``) → 404; ``HEAD`` on content →
  upstream ``HEAD`` relayed (nothing downloaded or cached); otherwise fetch (``FETCHING``):
  - *content* (``AssetKind.CONTENT``): streamed to the client **while** written to the blob store (tee, 256 KiB
    chunks). The last chunk is held back until the size and every expected digest are verified and the blob is
    committed; a mismatch (or an upstream reset) aborts the connection so the client never receives a complete,
    wrong file, and nothing is cached.
  - *metadata* (``AssetKind.METADATA``): read fully (bounded), verified, optionally ``validate``-d and
    ``transform``-ed by the format (in a thread: rewrite URLs, parse indexes), committed, then served by X-Accel.
    Validation failures answer ``502`` and cache nothing.

Upstream requests go through ``UpstreamClients`` (pooled HTTP/2 clients, SSRF guard at connect time). Redirects are
followed by the engine (max 5), each hop re-checked; credentials are only sent to the repository's upstream origin
(unless a format's ``UpstreamAuthenticator`` decides otherwise). Connect failures are retried once before the first
byte. Errors are raised as ``waystation_common.errors`` exceptions (``NotFound``, ``UpstreamError`` 502,
``UpstreamTimeout`` 504, ``DigestMismatch`` 502) that the router turns into the format's native error.

Every outcome is recorded without blocking (ARCH §8): live transfers and activity via the ``EventEmitter``,
per-minute counters, upstream health, Prometheus metrics and the access-log fields. MongoDB is written on fills
and revalidations only, never on hits.

``relay`` forwards a request that must not be cached (npm audit POSTs) and answers with the upstream's bounded body.

Not yet (later tasks): request coalescing (T-S08), auto-block (T-S07 full), download counters (T-S09).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urljoin, urlsplit

import anyio
import httpx
from redis.asyncio import Redis
from starlette.responses import Response, StreamingResponse

from waystation_common.crypto import Keyring
from waystation_common.db.client import Database
from waystation_common.errors import (
    DigestMismatch,
    NotFound,
    UpstreamError,
    ValidationFailed,
    WaystationError,
)
from waystation_common.events import FROM_CACHE, ActivityEvent, CacheState, EventEmitter, EventType, Transfer
from waystation_common.events import keys as event_keys
from waystation_common.ids import new_id
from waystation_common.settings import Settings
from waystation_common.storage import BlobRef, BlobStore
from waystation_server.dataplane.xaccel import ServedBlob, serve_blob
from waystation_server.db import assets as assets_db
from waystation_server.metrics import Metrics
from waystation_server.problems import ResponseAborted
from waystation_server.proxy.http import UpstreamClients
from waystation_server.proxy.records import AssetKind, StoredAsset, UpstreamMeta
from waystation_server.repos.model import AuthKind, RepoConfig, StoredProxy, secret_aad

if TYPE_CHECKING:
    from waystation_server.formats.base import FormatRequest

__all__ = [
    'AssetKind',
    'AssetSpec',
    'Expected',
    'ProxyEngine',
    'StaticAuth',
    'StoredAsset',
    'UpstreamAuthenticator',
    'UpstreamInfo',
    'UpstreamTimeout',
]

log = logging.getLogger('waystation.proxy')

CHUNK = 256 * 1024
METADATA_MAX_BYTES = 128 * 1024 * 1024
RELAY_MAX_BYTES = 32 * 1024 * 1024
MAX_REDIRECTS = 5
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
GONE_CODES = frozenset({404, 410})
DEFAULT_TYPE = 'application/octet-stream'
HASHES = ('sha1', 'sha256', 'sha512', 'md5')


class UpstreamTimeout(UpstreamError):  # noqa: N818 - reads as a state, like UpstreamError
    code = 'upstream_timeout'
    status = 504


@dataclass(frozen=True)
class Expected:
    """What the protocol promises about the bytes (hex digests, size). Checked before anything is committed."""

    sha256: str | None = None
    sha1: str | None = None
    sha512: str | None = None
    md5: str | None = None
    size: int | None = None

    def digests(self) -> dict[str, str]:
        return {alg: getattr(self, alg).lower() for alg in HASHES if getattr(self, alg)}

    @classmethod
    def from_sri(cls, integrity: str, *, size: int | None = None) -> Expected:
        """From a Subresource Integrity string (npm ``dist.integrity``): ``sha512-<base64>`` (strongest wins)."""
        found: dict[str, str] = {}
        for token in integrity.split():
            alg, _, b64 = token.partition('-')
            if alg in ('sha1', 'sha256', 'sha512'):
                try:
                    found[alg] = base64.b64decode(b64, validate=True).hex()
                except ValueError:
                    raise ValidationFailed('invalid integrity value') from None
        return cls(size=size, **found)


@dataclass(frozen=True)
class UpstreamInfo:
    url: str  # final URL after redirects
    status: int
    headers: Mapping[str, str]  # lower-case names
    latency_ms: float


class UpstreamAuthenticator(Protocol):
    """Upstream credentials for one request. ``headers`` is called for every hop (decide per URL: never send
    credentials to a host they are not for); ``challenge`` is called once on a 401 and returns True to retry (e.g.
    Docker token negotiation)."""

    async def headers(self, url: str) -> Mapping[str, str]: ...

    async def challenge(self, response: httpx.Response, url: str) -> bool: ...


@dataclass(frozen=True)
class StaticAuth:
    """The repository's configured basic/bearer credentials, sent only to the upstream origin."""

    origin: tuple[str, str, int]
    header: str

    async def headers(self, url: str) -> Mapping[str, str]:
        return {'Authorization': self.header} if _origin(url) == self.origin else {}

    async def challenge(self, response: httpx.Response, url: str) -> bool:
        return False


Transform = Callable[[bytes, UpstreamInfo], bytes]
Validate = Callable[[bytes, UpstreamInfo], None]
OnStored = Callable[[StoredAsset, bytes | None], Awaitable[None]]
Expect = Callable[[], Awaitable[Expected | None]]


@dataclass(frozen=True)
class AssetSpec:
    """One asset of a proxy repository, as a format plugin describes it.

    ``path`` is the asset key: unique per repository, already normalised (it is often the request path, but a format
    may key by digest, e.g. ``by-hash/SHA256/<h>``). The upstream URL is ``upstream_url`` when given (absolute), else
    the repository's upstream URL joined with ``upstream_path`` (default ``path``).
    """

    path: str
    kind: AssetKind = AssetKind.CONTENT
    upstream_path: str | None = None
    upstream_url: str | None = None
    upstream_headers: Mapping[str, str] = field(default_factory=dict)
    expected: Expected | None = None
    # Resolved only when the engine is about to GET the asset from upstream (never on a hit): for formats whose
    # expected digests live in other metadata (npm tarballs -> the packument). Replaces ``expected`` when set.
    expect: Expect | None = None
    content_type: str | None = None  # default: the upstream's Content-Type
    store_headers: tuple[str, ...] = ()  # upstream response headers kept and replayed (e.g. Docker-Content-Digest)
    response_headers: Mapping[str, str] = field(default_factory=dict)
    max_size: int | None = None  # default: metadata 128 MiB, content WAYSTATION_MAX_UPLOAD_GB
    max_age_s: int | None = None  # override the repository's metadata/content max age
    validate: Validate | None = None  # metadata: raise to refuse the upstream bytes (runs in a thread)
    transform: Transform | None = None  # metadata: bytes to store and serve (runs in a thread)
    on_stored: OnStored | None = None  # after commit (metadata: also the original upstream bytes)
    authenticator: UpstreamAuthenticator | None = None  # replaces the repository's static credentials
    head_passthrough: bool | None = None  # HEAD on a miss asks upstream with HEAD (default: content yes)
    artifact: str | None = None  # display name in live transfers (default: last path segment)
    component: str | None = None


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or '').lower(), parts.port or (443 if scheme == 'https' else 80)


def _join(base: str, path: str) -> str:
    return urljoin(base if base.endswith('/') else base + '/', path.lstrip('/'))


def _describe(exc: BaseException) -> str:
    if isinstance(exc, WaystationError):
        return exc.detail
    if isinstance(exc, asyncio.CancelledError | GeneratorExit):
        return 'client disconnected'
    if isinstance(exc, httpx.TimeoutException):
        return 'upstream timed out'
    if isinstance(exc, httpx.HTTPError):
        return f'upstream transfer failed ({type(exc).__name__})'
    return f'internal error ({type(exc).__name__})'


def _verify(expected: Expected | None, data: bytes) -> dict[str, str]:
    """Hash ``data`` (sha256 plus every algorithm ``expected`` names) and compare. Returns the computed digests."""
    computed = {'sha256': hashlib.sha256(data).hexdigest()}
    if expected is None:
        return computed
    if expected.size is not None and len(data) != expected.size:
        raise DigestMismatch(f'size {len(data)} does not match the expected {expected.size}')
    for alg, want in expected.digests().items():
        got = computed.get(alg) or hashlib.new(alg, data).hexdigest()
        computed[alg] = got
        if got != want:
            raise DigestMismatch(f'{alg} does not match the expected digest')
    return computed


class ProxyEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        valkey: Redis,
        blobs: BlobStore,
        emitter: EventEmitter,
        clients: UpstreamClients,
        metrics: Metrics,
        keyring: Keyring | None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.valkey = valkey
        self.blobs = blobs
        self.emitter = emitter
        self.clients = clients
        self.metrics = metrics
        self.keyring = keyring

    # ------------------------------------------------------------------ public API
    async def serve(self, req: FormatRequest, spec: AssetSpec) -> Response:
        """Serve ``spec`` from the cache or the upstream (see the module docstring)."""
        started = time.perf_counter()
        proxy = self._proxy(req.repo)
        asset = await self._cached(req.repo, spec)
        if asset is not None:
            if proxy.offline or self._fresh(asset, spec, proxy):
                return self._hit(req, asset, CacheState.HIT, started)
            try:
                spec = await self._expect(spec)
                response, info = await self._open(req, spec, proxy, conditional=asset)
            except (UpstreamError, DigestMismatch) as exc:
                if proxy.stale_on_error:
                    log.info('serving stale copy', extra={'path': spec.path, 'error': exc.detail})
                    return self._hit(req, asset, CacheState.STALE, started)
                self._account_error(req, started)
                raise
            if info.status == 304:
                await response.aclose()
                await self._revalidated(asset, info)
                return self._hit(req, asset, CacheState.REVALIDATED, started)
            if info.status != 200:
                await response.aclose()
                if proxy.stale_on_error:
                    return self._hit(req, asset, CacheState.STALE, started)
                self._account_error(req, started)
                raise UpstreamError(f'upstream answered {info.status}')
            return await self._fill(req, spec, proxy, response, info, started)

        if proxy.offline:
            self._account(req, CacheState.HIT, 404, 0, started)
            raise NotFound('Not Found')
        if await self._negative(req.repo, spec):
            self._account(req, CacheState.HIT, 404, 0, started)
            raise NotFound('Not Found')
        head_passthrough = (
            spec.head_passthrough if spec.head_passthrough is not None else spec.kind is AssetKind.CONTENT
        )
        method = 'HEAD' if req.is_head and head_passthrough else 'GET'
        try:
            if method == 'GET':
                spec = await self._expect(spec)
            response, info = await self._open(req, spec, proxy, method=method)
        except UpstreamError, DigestMismatch:
            self._account_error(req, started)
            raise
        if info.status in GONE_CODES:
            await response.aclose()
            await self._remember_missing(req.repo, spec, proxy)
            self._account(req, CacheState.FETCHING, 404, 0, started)
            raise NotFound('Not Found')
        if info.status != 200:
            await response.aclose()
            self._account_error(req, started)
            raise UpstreamError(f'upstream answered {info.status}')
        if method == 'HEAD':
            await response.aclose()
            self._account(req, CacheState.FETCHING, 200, 0, started)
            return self._head_response(spec, info)
        return await self._fill(req, spec, proxy, response, info, started)

    async def ensure(self, req: FormatRequest, spec: AssetSpec) -> StoredAsset:
        """Make sure a fresh copy of a metadata asset is cached (fetch or revalidate as needed) and return it,
        without producing a response (e.g. npm needs the packument to validate a tarball). Stale-on-error applies."""
        proxy = self._proxy(req.repo)
        asset = await self._cached(req.repo, spec)
        if asset is not None and (proxy.offline or self._fresh(asset, spec, proxy)):
            return asset
        if asset is None and proxy.offline:
            raise NotFound('Not Found')
        started = time.perf_counter()
        try:
            spec = await self._expect(spec)
            response, info = await self._open(req, spec, proxy, conditional=asset)
        except UpstreamError, DigestMismatch:
            if asset is not None and proxy.stale_on_error:
                return asset
            raise
        if info.status == 304 and asset is not None:
            await response.aclose()
            await self._revalidated(asset, info)
            return asset
        if info.status != 200:
            await response.aclose()
            if asset is not None and proxy.stale_on_error:
                return asset
            if info.status in GONE_CODES:
                await self._remember_missing(req.repo, spec, proxy)
                raise NotFound('Not Found')
            raise UpstreamError(f'upstream answered {info.status}')
        return await self._fill_buffered(req, spec, response, info, started)

    async def read(self, asset: StoredAsset, *, max_bytes: int = METADATA_MAX_BYTES) -> bytes:
        """The bytes of a cached (metadata) asset, read in a thread. Bounded: larger assets raise."""
        if asset.size > max_bytes:
            raise UpstreamError('cached metadata is too large to parse')
        path = self.blobs.blob_path(asset.blob_sha256)
        return await asyncio.to_thread(path.read_bytes)

    async def relay(
        self,
        req: FormatRequest,
        *,
        upstream_path: str,
        body: bytes = b'',
        headers: Mapping[str, str] | None = None,
        max_size: int = RELAY_MAX_BYTES,
    ) -> Response:
        """Forward one request (the client's method, ``body`` and ``headers``) to the repository's upstream and
        answer with the upstream's status and body: nothing is cached and redirects are not followed (e.g. npm
        audit, a POST that only queries). The response is bounded by ``max_size``; upstream 5xx answer 502."""
        started = time.perf_counter()
        proxy = self._proxy(req.repo)
        if proxy.offline:
            raise NotFound('Not Found')
        url = _join(proxy.upstream_url, upstream_path)
        try:
            client = self.clients.client_for(url, allow_private=proxy.allow_private_upstream)
        except ValueError:
            raise UpstreamError('unsupported upstream URL') from None
        if self.clients.proxy:
            await self.clients.precheck(url, allow_private=proxy.allow_private_upstream)
        auth = self._auth(req.repo, proxy, AssetSpec(path=upstream_path))
        send = {'Accept-Encoding': 'gzip, deflate', **(headers or {})}
        if auth is not None:
            send.update(await auth.headers(url))
        try:
            request = client.build_request(req.method, url, headers=send, content=body or None)
            response = await client.send(request, stream=True)
        except httpx.TimeoutException:
            self._upstream_failed(req, 'upstream timed out')
            self._account_error(req, started)
            raise UpstreamTimeout('upstream timed out') from None
        except httpx.HTTPError as exc:
            self._upstream_failed(req, _describe(exc))
            self._account_error(req, started)
            raise UpstreamError('upstream connection failed') from None
        except UpstreamError as exc:
            self._upstream_failed(req, exc.detail)
            self._account_error(req, started)
            raise
        try:
            status = response.status_code
            ok = status < 500 and status not in (401, 403, 429)
            latency = round((time.perf_counter() - started) * 1000, 1)
            self.emitter.upstream(
                repo_id=req.repo.id,
                format=req.repo.format,
                ok=ok,
                error=None if ok else f'upstream answered {status}',
                latency_ms=latency,
            )
            if status >= 500 or status in REDIRECT_CODES:
                self._account_error(req, started)
                raise UpstreamError(f'upstream answered {status}')
            chunks: list[bytes] = []
            done = 0
            async for chunk in response.aiter_bytes(CHUNK):
                done += len(chunk)
                if done > max_size:
                    self._account_error(req, started)
                    raise UpstreamError('upstream response exceeds the size limit')
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            self._upstream_failed(req, _describe(exc), after_response=True)
            self._account_error(req, started)
            raise UpstreamError('upstream transfer failed') from None
        finally:
            with anyio.CancelScope(shield=True):
                await response.aclose()
        content = b''.join(chunks)
        self.metrics.add_bytes(repo=req.repo.name, direction='in_upstream', count=done)
        self._account(req, CacheState.FETCHING, status, len(content), started)
        out = {'Content-Type': response.headers.get('content-type', DEFAULT_TYPE), 'X-Ws-Cache': 'BYPASS'}
        return Response(content, status_code=status, headers=out)

    def record_rate_limit(
        self,
        repo: RepoConfig,
        *,
        limit: int,
        remaining: int,
        window_s: int | None = None,
        reset_at: datetime | None = None,
        source: str | None = None,
    ) -> None:
        """Remember the upstream's latest rate-limit headers (shown in upstream health; Docker Hub)."""
        fields: dict[Any, Any] = {
            'limit': str(limit),
            'remaining': str(remaining),
            'window_s': '' if window_s is None else str(window_s),
            'reset_at': '' if reset_at is None else reset_at.isoformat(),
            'source': source or '',
            'seen_at': datetime.now(UTC).isoformat(),
        }
        name = event_keys.upstream_ratelimit_key(repo.id)
        self.emitter.call(lambda pipe: pipe.hset(name, mapping=fields))

    # ------------------------------------------------------------------ lookups
    @staticmethod
    def _proxy(repo: RepoConfig) -> StoredProxy:
        if repo.proxy is None:
            raise NotFound('Not Found')
        return repo.proxy

    async def _cached(self, repo: RepoConfig, spec: AssetSpec) -> StoredAsset | None:
        asset = await assets_db.get(self.db, repo.id, spec.path)
        if asset is None:
            return None
        if not await self.blobs.exists(asset.blob_sha256):
            log.warning('asset blob missing; refetching', extra={'path': spec.path})
            return None
        return asset

    @staticmethod
    def _fresh(asset: StoredAsset, spec: AssetSpec, proxy: StoredProxy) -> bool:
        if spec.max_age_s is not None:
            max_age: int | None = spec.max_age_s
        elif asset.kind is AssetKind.METADATA:
            max_age = proxy.metadata_max_age_s
        else:
            max_age = proxy.content_max_age_s
        if max_age is None:
            return True
        return (datetime.now(UTC) - asset.upstream.fetched_at).total_seconds() < max_age

    @staticmethod
    def _neg_key(repo: RepoConfig, spec: AssetSpec) -> str:
        return f'neg:{repo.id}:{hashlib.sha1(spec.path.encode(), usedforsecurity=False).hexdigest()}'

    async def _negative(self, repo: RepoConfig, spec: AssetSpec) -> bool:
        try:
            return bool(await self.valkey.exists(self._neg_key(repo, spec)))
        except Exception as exc:  # Valkey down: no negative cache, never an error
            log.warning('negative cache lookup failed', extra={'error': type(exc).__name__})
            return False

    async def _remember_missing(self, repo: RepoConfig, spec: AssetSpec, proxy: StoredProxy) -> None:
        if proxy.negative_ttl_s <= 0:
            return
        key = self._neg_key(repo, spec)
        try:
            await self.valkey.set(key, '1', ex=proxy.negative_ttl_s)
        except Exception as exc:  # Valkey down: no negative cache
            log.warning('negative cache update failed', extra={'error': type(exc).__name__})

    # ------------------------------------------------------------------ upstream requests
    def _auth(self, repo: RepoConfig, proxy: StoredProxy, spec: AssetSpec) -> UpstreamAuthenticator | None:
        if spec.authenticator is not None:
            return spec.authenticator
        auth = proxy.auth
        if auth.kind is AuthKind.NONE or not auth.secret_enc or self.keyring is None:
            return None
        secret = self.keyring.decrypt_str(auth.secret_enc, aad=secret_aad(repo.id))
        if auth.kind is AuthKind.BASIC:
            raw = base64.b64encode(f'{auth.username or ""}:{secret}'.encode()).decode('ascii')
            header = f'Basic {raw}'
        else:
            header = f'Bearer {secret}'
        return StaticAuth(origin=_origin(proxy.upstream_url), header=header)

    @staticmethod
    async def _expect(spec: AssetSpec) -> AssetSpec:
        if spec.expect is None:
            return spec
        return replace(spec, expected=await spec.expect(), expect=None)

    def _url(self, proxy: StoredProxy, spec: AssetSpec) -> str:
        if spec.upstream_url:
            return spec.upstream_url
        return _join(proxy.upstream_url, spec.upstream_path if spec.upstream_path is not None else spec.path)

    async def _open(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        proxy: StoredProxy,
        *,
        method: str = 'GET',
        conditional: StoredAsset | None = None,
    ) -> tuple[httpx.Response, UpstreamInfo]:
        """Send the request (following redirects, answering one auth challenge, retrying one connect failure)
        and return the streaming response with its headers read."""
        url = self._url(proxy, spec)
        auth = self._auth(req.repo, proxy, spec)
        headers: dict[str, str] = {
            'Accept-Encoding': 'gzip, deflate' if spec.kind is AssetKind.METADATA else 'identity',
            **spec.upstream_headers,
        }
        if conditional is not None:
            if conditional.upstream.etag:
                headers['If-None-Match'] = conditional.upstream.etag
            if conditional.upstream.last_modified:
                headers['If-Modified-Since'] = conditional.upstream.last_modified
        allow_private = proxy.allow_private_upstream
        redirects = retries = 0
        challenged = False
        started = time.perf_counter()
        while True:
            try:
                client = self.clients.client_for(url, allow_private=allow_private)
            except ValueError:
                raise UpstreamError('upstream redirected to an unsupported URL') from None
            if self.clients.proxy:
                await self.clients.precheck(url, allow_private=allow_private)
            hop_headers = dict(headers)
            if auth is not None:
                hop_headers.update(await auth.headers(url))
            try:
                response = await client.send(client.build_request(method, url, headers=hop_headers), stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if retries == 0:
                    retries += 1
                    await asyncio.sleep(0.2)
                    continue
                self._upstream_failed(req, _describe(exc))
                raise UpstreamError('upstream unreachable') from None
            except httpx.TimeoutException:
                self._upstream_failed(req, 'upstream timed out')
                raise UpstreamTimeout('upstream timed out') from None
            except httpx.HTTPError as exc:
                self._upstream_failed(req, _describe(exc))
                raise UpstreamError('upstream connection failed') from None
            except UpstreamError as exc:  # SSRF guard refusals, DNS failures
                self._upstream_failed(req, exc.detail)
                raise
            status = response.status_code
            if status in REDIRECT_CODES and 'location' in response.headers:
                await response.aclose()
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    self._upstream_failed(req, 'too many redirects')
                    raise UpstreamError('upstream redirected too many times')
                url = urljoin(url, response.headers['location'])
                if urlsplit(url).scheme not in ('http', 'https'):
                    raise UpstreamError('upstream redirected to an unsupported URL')
                continue
            if status == 401 and auth is not None and not challenged:
                challenged = True
                if await auth.challenge(response, url):
                    await response.aclose()
                    continue
            latency = round((time.perf_counter() - started) * 1000, 1)
            ok = status < 500 and status not in (401, 403, 429)
            self.emitter.upstream(
                repo_id=req.repo.id,
                format=req.repo.format,
                ok=ok,
                error=None if ok else f'upstream answered {status}',
                latency_ms=latency,
            )
            self.metrics.upstream_latency.labels(req.repo.name).observe(latency / 1000)
            info = UpstreamInfo(
                url=url,
                status=status,
                headers={k.lower(): v for k, v in response.headers.items()},
                latency_ms=latency,
            )
            return response, info

    def _upstream_failed(self, req: FormatRequest, error: str, *, after_response: bool = False) -> None:
        log.warning('upstream request failed', extra={'error': error})
        self.emitter.upstream(
            repo_id=req.repo.id, format=req.repo.format, ok=False, error=error, count_request=not after_response
        )

    # ------------------------------------------------------------------ fills
    async def _fill(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        proxy: StoredProxy,
        response: httpx.Response,
        info: UpstreamInfo,
        started: float,
    ) -> Response:
        if spec.kind is AssetKind.METADATA or req.is_head:
            asset = await self._fill_buffered(req, spec, response, info, started)
            return self._serve(req, asset, CacheState.FETCHING)
        return await self._fill_stream(req, spec, response, info)

    def _length(self, spec: AssetSpec, info: UpstreamInfo, max_size: int) -> int | None:
        raw = info.headers.get('content-length')
        total = int(raw) if raw and raw.isdigit() and not info.headers.get('content-encoding') else None
        expected = spec.expected.size if spec.expected else None
        if total is not None and expected is not None and total != expected:
            raise DigestMismatch(f'upstream length {total} does not match the expected size {expected}')
        if total is not None and total > max_size:
            raise UpstreamError('upstream file exceeds the size limit')
        return total

    def _start_transfer(self, req: FormatRequest, spec: AssetSpec, total: int | None, latency: float) -> str:
        now = datetime.now(UTC)
        tid = new_id()
        self.emitter.transfer_started(
            Transfer(
                id=tid,
                repo_id=req.repo.id,
                repo=req.repo.name,
                format=req.repo.format,
                path=spec.path,
                cache=CacheState.FETCHING,
                principal=req.principal.display,
                client_ip=req.client_ip or None,
                client_agent=(req.request.headers.get('user-agent') or '')[:120] or None,
                artifact=spec.artifact or spec.path.rsplit('/', 1)[-1] or spec.path,
                component=spec.component,
                bytes_total=total,
                upstream_latency_ms=latency,
                started_at=now,
                updated_at=now,
            )
        )
        return tid

    async def _record(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        ref: BlobRef,
        content_type: str,
        stored_headers: dict[str, str],
        info: UpstreamInfo,
        checksums: dict[str, str],
    ) -> StoredAsset:
        now = datetime.now(UTC)
        asset = StoredAsset(
            id=new_id(),
            repo_id=req.repo.id,
            path=spec.path,
            kind=spec.kind,
            blob_sha256=ref.sha256,
            size=ref.size,
            content_type=content_type,
            checksums=checksums,
            headers=stored_headers,
            upstream=UpstreamMeta(
                url=info.url,
                etag=info.headers.get('etag'),
                last_modified=info.headers.get('last-modified'),
                fetched_at=now,
                status=info.status,
            ),
            created_at=now,
            updated_at=now,
        )
        saved = await assets_db.save(self.db, asset)
        await assets_db.record_blob(self.db, ref.sha256, ref.size, now)
        return saved

    def _stored_headers(self, spec: AssetSpec, info: UpstreamInfo) -> dict[str, str]:
        return {name: info.headers[name.lower()] for name in spec.store_headers if name.lower() in info.headers}

    async def _fill_buffered(
        self, req: FormatRequest, spec: AssetSpec, response: httpx.Response, info: UpstreamInfo, started: float
    ) -> StoredAsset:
        max_size = spec.max_size or (
            METADATA_MAX_BYTES if spec.kind is AssetKind.METADATA else self.settings.max_upload_bytes
        )
        tid: str | None = None
        done = 0
        try:
            total = self._length(spec, info, max_size)
            tid = self._start_transfer(req, spec, total, info.latency_ms)
            chunks: list[bytes] = []
            async for chunk in response.aiter_bytes(CHUNK):
                done += len(chunk)
                if done > max_size:
                    raise UpstreamError('upstream file exceeds the size limit')
                chunks.append(chunk)
                self.emitter.transfer_progress(tid, done, bytes_total=total)
            data = b''.join(chunks)
            checksums = await asyncio.to_thread(_verify, spec.expected, data)
            if spec.validate is not None:
                await asyncio.to_thread(spec.validate, data, info)
            body = await asyncio.to_thread(spec.transform, data, info) if spec.transform is not None else data
            async with await self.blobs.open_writer(max_size=max(max_size, len(body))) as writer:
                await writer.write(body)
                ref = await self.blobs.commit(writer)
            if spec.transform is not None:
                checksums = {'sha256': ref.sha256}
            content_type = spec.content_type or info.headers.get('content-type') or DEFAULT_TYPE
            asset = await self._record(req, spec, ref, content_type, self._stored_headers(spec, info), info, checksums)
            if spec.on_stored is not None:
                await spec.on_stored(asset, data)
        except BaseException as exc:
            error = _describe(exc)
            if tid is not None:
                self.emitter.transfer_finished(tid, status=502, bytes_done=done, error=error)
            else:
                self._account_error(req, started)
            if isinstance(exc, DigestMismatch | httpx.HTTPError):
                self._upstream_failed(req, error, after_response=True)
            if isinstance(exc, httpx.TimeoutException):
                raise UpstreamTimeout('upstream timed out') from None
            if isinstance(exc, httpx.HTTPError):
                raise UpstreamError('upstream transfer failed') from None
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await response.aclose()
        self.emitter.transfer_finished(tid, status=200, bytes_done=done)
        self.metrics.request(repo=req.repo.name, format=req.repo.format, status=200, cache=CacheState.FETCHING)
        self.metrics.add_bytes(repo=req.repo.name, direction='in_upstream', count=done)
        req.log(cache=CacheState.FETCHING.value, bytes=asset.size)
        return asset

    async def _fill_stream(
        self, req: FormatRequest, spec: AssetSpec, response: httpx.Response, info: UpstreamInfo
    ) -> Response:
        max_size = spec.max_size or self.settings.max_upload_bytes
        expected = spec.expected
        try:
            total = self._length(spec, info, max_size)
            writer = await self.blobs.open_writer(
                max_size=max_size,
                expected_size=expected.size if expected else None,
                expected_sha256=expected.sha256.lower() if expected and expected.sha256 else None,
            )
        except BaseException:
            with anyio.CancelScope(shield=True):
                await response.aclose()
            raise
        extra = {alg: hashlib.new(alg) for alg in (expected.digests() if expected else {}) if alg != 'sha256'}
        tid = self._start_transfer(req, spec, total, info.latency_ms)
        content_type = spec.content_type or info.headers.get('content-type') or DEFAULT_TYPE
        stored = self._stored_headers(spec, info)
        headers: dict[str, str] = {'Content-Type': content_type, 'X-Ws-Cache': CacheState.FETCHING.value}
        headers.update(stored)
        headers.update(spec.response_headers)
        if total is not None:
            headers['Content-Length'] = str(total)
        req.log(cache=CacheState.FETCHING.value)
        engine = self

        async def body() -> AsyncIterator[bytes]:
            done = 0
            held: bytes | None = None
            committed = False
            try:
                async for chunk in response.aiter_bytes(CHUNK):
                    for hasher in extra.values():
                        hasher.update(chunk)
                    await writer.write(chunk)
                    done += len(chunk)
                    engine.emitter.transfer_progress(tid, done, bytes_total=total)
                    if held is not None:
                        yield held
                    held = chunk
                if expected is not None:
                    for alg, hasher in extra.items():
                        if hasher.hexdigest() != expected.digests()[alg]:
                            raise DigestMismatch(f'{alg} does not match the expected digest')
                ref = await engine.blobs.commit(writer)
                committed = True
                checksums = {'sha256': ref.sha256, **{alg: h.hexdigest() for alg, h in extra.items()}}
                asset = await engine._record(req, spec, ref, content_type, stored, info, checksums)
                if spec.on_stored is not None:
                    await spec.on_stored(asset, None)
                if held is not None:
                    yield held
            except BaseException as exc:
                error = _describe(exc)
                engine.emitter.transfer_finished(tid, status=502, bytes_done=done, error=error)
                engine.metrics.request(repo=req.repo.name, format=req.repo.format, status=502, cache='ERROR')
                if isinstance(exc, DigestMismatch | httpx.HTTPError):
                    engine._upstream_failed(req, error, after_response=True)
                log.warning('cache fill aborted', extra={'path': spec.path, 'error': error, 'bytes': done})
                if isinstance(exc, Exception):
                    raise ResponseAborted(error) from exc
                raise
            finally:
                with anyio.CancelScope(shield=True):
                    await response.aclose()
                    if not committed:
                        await writer.abort()
            engine.emitter.transfer_finished(tid, status=200, bytes_done=done)
            engine.metrics.request(repo=req.repo.name, format=req.repo.format, status=200, cache='FETCHING')
            engine.metrics.add_bytes(repo=req.repo.name, direction='out_upstream', count=done)
            engine.metrics.add_bytes(repo=req.repo.name, direction='in_upstream', count=done)

        return StreamingResponse(body(), status_code=200, headers=headers)

    # ------------------------------------------------------------------ responses and accounting
    async def _revalidated(self, asset: StoredAsset, info: UpstreamInfo) -> None:
        upstream = asset.upstream.model_copy(
            update={
                'fetched_at': datetime.now(UTC),
                'etag': info.headers.get('etag') or asset.upstream.etag,
                'last_modified': info.headers.get('last-modified') or asset.upstream.last_modified,
            }
        )
        await assets_db.mark_revalidated(self.db, asset.id, upstream, datetime.now(UTC))

    def _serve(self, req: FormatRequest, asset: StoredAsset, cache: CacheState) -> Response:
        blob = ServedBlob(
            sha256=asset.blob_sha256,
            size=asset.size,
            content_type=asset.content_type,
            last_modified=asset.upstream.fetched_at if asset.kind is AssetKind.CONTENT else asset.updated_at,
            headers=asset.headers,
        )
        return serve_blob(req.request, blob, cache=cache.value)

    def _hit(self, req: FormatRequest, asset: StoredAsset, cache: CacheState, started: float) -> Response:
        response = self._serve(req, asset, cache)
        sent = asset.size if response.status_code == 200 and req.method == 'GET' else 0
        self._account(req, cache, response.status_code, sent, started)
        if sent and asset.kind is AssetKind.CONTENT:
            self.emitter.activity(
                ActivityEvent(
                    type=EventType.CACHE_STALE if cache is CacheState.STALE else EventType.CACHE_HIT,
                    ts=datetime.now(UTC),
                    repo_id=req.repo.id,
                    repo=req.repo.name,
                    format=req.repo.format,
                    path=asset.path,
                    principal=req.principal.display,
                    client_ip=req.client_ip or None,
                    cache=cache,
                    status=200,
                    bytes=asset.size,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                )
            )
        return response

    def _head_response(self, spec: AssetSpec, info: UpstreamInfo) -> Response:
        headers = {
            'Content-Type': spec.content_type or info.headers.get('content-type') or DEFAULT_TYPE,
            'X-Ws-Cache': CacheState.FETCHING.value,
        }
        if 'content-length' in info.headers:
            headers['Content-Length'] = info.headers['content-length']
        headers.update(self._stored_headers(spec, info))
        headers.update(spec.response_headers)
        return Response(status_code=200, headers=headers)

    def _account(self, req: FormatRequest, cache: CacheState, status: int, sent: int, started: float) -> None:
        duration_ms = (time.perf_counter() - started) * 1000
        self.emitter.request(
            repo_id=req.repo.id, format=req.repo.format, cache=cache, duration_ms=duration_ms, bytes_out=sent
        )
        self.metrics.request(repo=req.repo.name, format=req.repo.format, status=status, cache=cache.value)
        direction = 'out_cache' if cache in FROM_CACHE else 'out_upstream'
        self.metrics.add_bytes(repo=req.repo.name, direction=direction, count=sent)
        req.log(cache=cache.value, bytes=sent)

    def _account_error(self, req: FormatRequest, started: float) -> None:
        self._account(req, CacheState.ERROR, 502, 0, started)
