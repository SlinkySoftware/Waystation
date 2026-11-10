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

**Coalescing** (ARCHITECTURE §6.3, ``proxy.coalesce``): before contacting the upstream for a missing or stale asset
the engine claims the asset's single-flight lease in Valkey. The owner fetches as above (its live transfer shows the
follower count); followers of a streaming content fill tail the owner's upload file as it grows (``COALESCED``; last
piece held back until the owner committed), or wait for the commit and are served by X-Accel (metadata, ``HEAD``,
``WAYSTATION_COALESCE_TAIL=wait``, another host or a network filesystem). A failed or mismatched fetch fails its
followers too (nothing unverified is ever completed); an owner that goes away (it died: lease expiry; shutdown;
not its client leaving, see below) is replaced by one of its followers, which continue after the bytes they
already sent once those are confirmed identical. Valkey being down only disables coalescing.

**Fills outlive their client** (``proxy.fills``): a streaming content fill runs as a background task of the engine
(``FillRegistry``), not in the request's task, and the client's response reads it through a ``Feed`` (in memory
while the client keeps up, else from the upload file). A client that goes away mid-transfer does not stop it: the
blob is committed and indexed, followers keep tailing, the next request is a hit. Bounded by the upstream timeouts
and the stall limit; shutdown (``aclose``) cancels the running fills (``aborted``, upload removed).

**Auto-block** (``proxy.breaker``): every upstream request is admitted by the repository's breaker first (an
auto-blocked upstream answers ``UpstreamAutoBlocked`` without a request, so stale-on-error applies) and every answer
or transport failure is recorded; one request per ended block period probes the upstream.

**Network options** per repository (``proxy.http.Network``): custom CA bundle and outbound HTTP proxy; through a
proxy the SSRF guard checks every hop's target before the request.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import logging
import os
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol
from urllib.parse import urljoin, urlsplit

import anyio
import httpx
from redis.asyncio import Redis
from starlette.responses import Response, StreamingResponse

from waystation_common.crypto import Keyring
from waystation_common.db.client import Database
from waystation_common.errors import (
    DigestMismatch,
    InsufficientStorage,
    NotFound,
    UpstreamError,
    ValidationFailed,
    WaystationError,
)
from waystation_common.events import FROM_CACHE, ActivityEvent, CacheState, EventEmitter, EventType, Transfer
from waystation_common.events import keys as event_keys
from waystation_common.ids import new_id
from waystation_common.settings import Settings
from waystation_common.storage import BlobRef, BlobStore, BlobWriter
from waystation_server.dataplane.xaccel import ServedBlob, serve_blob
from waystation_server.db import assets as assets_db
from waystation_server.metrics import Metrics
from waystation_server.problems import ResponseAborted
from waystation_server.proxy.breaker import UpstreamBreaker, blocking_status
from waystation_server.proxy.coalesce import Coalescer, FillState, Follower, Lease, Status
from waystation_server.proxy.fills import Feed, FillRegistry
from waystation_server.proxy.http import UpstreamClients, network_of
from waystation_server.proxy.records import AssetKind, StoredAsset, UpstreamMeta
from waystation_server.proxy.ssrf import UpstreamBlocked
from waystation_server.repos.model import AuthKind, RepoConfig, StoredProxy, secret_aad
from waystation_server.repos.quota import Quotas

if TYPE_CHECKING:
    from waystation_server.formats.base import FormatRequest

__all__ = [
    'AssetKind',
    'AssetSpec',
    'CheckedExpected',
    'Expected',
    'OnUpstream',
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
MAX_ROUNDS = 4  # claims per request when owners keep going away (ARCHITECTURE §6.3)
WAIT_S = 1.0  # a follower re-checks at least this often (stall detection); state changes wake it at once
TAIL_READ = 1024 * 1024
STALE_CODES = frozenset({'upstream', 'timeout', 'mismatch'})  # followers may serve a stale copy on these
_UPLOAD_ID = re.compile(r'^[0-9a-f]{32}$')
# Lease outcome of a fill served without being stored (hard quota, low disk): followers fetch on their own.
UNSTORED = 'UNSTORED'


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
class CheckedExpected(Expected):
    """``Expected`` plus a format's own check of the complete bytes (e.g. Go's ``h1:`` hash of a module zip): called
    in a thread with a seekable binary file before anything is committed (and before the last chunk is sent); raises
    ``DigestMismatch`` to refuse them. Not run when a fill is passed through without being stored (hard quota, low
    disk)."""

    check: Callable[[BinaryIO], None] | None = field(default=None, compare=False)


def _check_of(expected: Expected | None) -> Callable[[BinaryIO], None] | None:
    return expected.check if isinstance(expected, CheckedExpected) else None


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
OnUpstream = Callable[[UpstreamInfo], None]
SameAsCached = Callable[[StoredAsset, UpstreamInfo], bool]


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
    # Stale metadata: ask upstream with HEAD first and keep the cached copy when this says it is unchanged (e.g. same
    # Docker-Content-Digest; Docker Hub does not count HEADs against its pull limit). False/errors: normal refresh.
    revalidate_head: SameAsCached | None = None
    on_upstream: OnUpstream | None = None  # sees every final upstream response's headers (rate limits); sync, cheap
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
    check = _check_of(expected)
    if check is not None:
        check(io.BytesIO(data))
    return computed


def _check_file(check: Callable[[BinaryIO], None], path: Path) -> None:
    """Run a format's ``Expected.check`` on a written file (blocking: call in a thread)."""
    with path.open('rb') as file:
        check(file)


class _Abandoned(Exception):  # noqa: N818 - control flow: the followed owner went away
    pass


@dataclass
class _Fill:
    """A streaming fill in progress (owner side). ``writer`` None: served without being stored (``reason``)."""

    writer: BlobWriter | None
    total: int | None
    content_type: str
    stored: dict[str, str]
    extra: dict[str, Any]  # hashlib objects for expected digests (other than sha256 when there is a writer)
    done: int = 0
    reason: str | None = None  # 'quota' (hard repository quota) or 'low_disk' (free-space guard)


class _Source:
    """A follower's read-only descriptor on the followed fill's file (``uploads/<id>``, or the blob once committed:
    the same inode, renamed)."""

    def __init__(self) -> None:
        self.fd: int | None = None
        self.fill: str | None = None  # owner token of the fill the descriptor belongs to

    async def open(self, blobs: BlobStore, state: FillState) -> bool:
        if state.status is Status.DONE and state.sha256:
            path = blobs.blob_path(state.sha256)
        elif state.upload and _UPLOAD_ID.fullmatch(state.upload):
            path = blobs.uploads_dir / state.upload
        else:
            return False
        try:
            fd = await asyncio.to_thread(os.open, path, os.O_RDONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            return False
        self.close()
        self.fd, self.fill = fd, state.owner
        return True

    def close(self) -> None:
        if self.fd is not None:
            fd, self.fd, self.fill = self.fd, None, None
            with contextlib.suppress(OSError):
                os.close(fd)


def _hash_prefix(fd: int, length: int) -> str:
    """sha256 of the first ``length`` bytes of ``fd`` (blocking: call in a thread)."""
    digest = hashlib.sha256()
    pos = 0
    while pos < length:
        data = os.pread(fd, min(TAIL_READ, length - pos), pos)
        if not data:
            break
        digest.update(data)
        pos += len(data)
    return digest.hexdigest()


def _failure(state: FillState) -> WaystationError:
    """The error a follower answers with when the fetch it followed failed."""
    detail = state.error or 'upstream transfer failed'
    if state.error_code == 'not_found':
        return NotFound('Not Found')
    if state.error_code == 'mismatch':
        return DigestMismatch(detail)
    if state.error_code == 'timeout':
        return UpstreamTimeout(detail)
    return UpstreamError(detail)


def _as_error(exc: BaseException) -> BaseException:
    """Upstream transport errors as the errors followers should see."""
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamTimeout('upstream timed out')
    if isinstance(exc, httpx.HTTPError):
        return UpstreamError('upstream transfer failed')
    return exc


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
        coalescer: Coalescer | None = None,
        breaker: UpstreamBreaker | None = None,
        quotas: Quotas | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.valkey = valkey
        self.blobs = blobs
        self.emitter = emitter
        self.clients = clients
        self.metrics = metrics
        self.keyring = keyring
        self.coalescer = coalescer or Coalescer(
            valkey,
            data_dir=settings.data_dir,
            mode=settings.coalesce_tail,
            lease_s=settings.coalesce_lease_s,
            stall_s=self._stall_s,
        )
        self.breaker = breaker or UpstreamBreaker(valkey, emitter, metrics)
        self.quotas = quotas
        self.fills = FillRegistry()

    async def aclose(self) -> None:
        """Shutdown: cancel the background cache fills and wait until they have cleaned up (each is published as
        ``aborted``, so a follower on another replica takes it over, and its upload file is removed)."""
        await self.fills.aclose()

    def _stall_s(self) -> float:
        """How long a fill may show no progress before it counts as abandoned: the owner's upstream timeouts (one
        connect retry, a read) plus a lease."""
        timeouts = self.clients.timeouts
        return 2 * timeouts.connect + timeouts.read + self.settings.coalesce_lease_s

    # ------------------------------------------------------------------ public API
    async def serve(self, req: FormatRequest, spec: AssetSpec) -> Response:
        """Serve ``spec`` from the cache or the upstream (see the module docstring)."""
        started = time.perf_counter()
        proxy = self._proxy(req.repo)
        asset = await self._cached(req.repo, spec)
        if asset is not None and (proxy.offline or self._fresh(asset, spec, proxy)):
            return self._hit(req, asset, CacheState.HIT, started, spec.artifact)
        if asset is None:
            if proxy.offline or await self._negative(req.repo, spec):
                self._account(req, CacheState.HIT, 404, 0, started)
                raise NotFound('Not Found')
            if req.is_head and self._head_passthrough(spec):
                return await self._head_miss(req, spec, proxy, started)
        for _ in range(MAX_ROUNDS):
            flight = await self.coalescer.claim(req.repo.id, spec.path)
            if isinstance(flight, Lease):
                return await self._serve_owner(req, spec, proxy, asset, flight, started)
            response = await self._serve_follower(req, spec, proxy, flight, started)
            if response is not None:
                return response
            asset = await self._cached(req.repo, spec)  # the owner went away: maybe it got that far
            if asset is not None and self._fresh(asset, spec, proxy):
                return self._hit(req, asset, CacheState.COALESCED, started, spec.artifact)
        self._account_error(req, started)
        raise UpstreamError('the upstream fetch was abandoned repeatedly')

    async def ensure(self, req: FormatRequest, spec: AssetSpec) -> StoredAsset:
        """Make sure a fresh copy of a metadata asset is cached (fetch or revalidate as needed) and return it,
        without producing a response (e.g. npm needs the packument to validate a tarball). Stale-on-error applies."""
        proxy = self._proxy(req.repo)
        asset = await self._cached(req.repo, spec)
        if asset is not None and (proxy.offline or self._fresh(asset, spec, proxy)):
            return asset
        if asset is None and proxy.offline:
            raise NotFound('Not Found')
        for _ in range(MAX_ROUNDS):
            flight = await self.coalescer.claim(req.repo.id, spec.path)
            if isinstance(flight, Lease):
                try:
                    if flight.replaced:
                        asset = await self._cached(req.repo, spec)
                        if asset is not None and self._fresh(asset, spec, proxy):
                            await flight.finish(outcome=CacheState.HIT.value)
                            return asset
                    asset = await self._ensure_owned(req, spec, proxy, asset, flight)
                except BaseException as exc:
                    with anyio.CancelScope(shield=True):
                        await flight.fail(exc)
                    raise
                await flight.finish()
                return asset
            try:
                state = await self._await_outcome(flight, stream_ok=False)
            finally:
                await flight.leave()
            if state is not None and state.status is Status.FAILED:
                cached = await self._cached(req.repo, spec)
                if cached is not None and proxy.stale_on_error and state.error_code in STALE_CODES:
                    return cached
                raise _failure(state)
            asset = await self._cached(req.repo, spec)
            if asset is not None and (state is not None or self._fresh(asset, spec, proxy)):
                return asset
        raise UpstreamError('the upstream fetch was abandoned repeatedly')

    async def _ensure_owned(
        self, req: FormatRequest, spec: AssetSpec, proxy: StoredProxy, asset: StoredAsset | None, lease: Lease
    ) -> StoredAsset:
        if asset is not None and await self._unchanged_by_head(req, spec, proxy, asset):
            return asset
        started = time.perf_counter()
        try:
            spec = await self._expect(spec)
            response, info = await self._open(req, spec, proxy, conditional=asset)
        except (UpstreamError, DigestMismatch) as exc:
            if asset is not None and proxy.stale_on_error:
                await lease.fail(exc)
                return asset
            raise
        if info.status == 304 and asset is not None:
            await response.aclose()
            await self._revalidated(asset, info)
            return asset
        if info.status != 200:
            await response.aclose()
            error = UpstreamError(f'upstream answered {info.status}')
            if asset is not None and proxy.stale_on_error:
                await lease.fail(error)
                return asset
            if info.status in GONE_CODES:
                await self._remember_missing(req.repo, spec, proxy)
                raise NotFound('Not Found')
            raise error
        filled = await self._fill_buffered(req, spec, response, info, started, lease)
        if isinstance(filled, Response):  # pragma: no cover - only when unstored answers are allowed
            raise InsufficientStorage('not enough free space in the blob store')
        return filled

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
        await self.breaker.admit(req.repo)
        try:
            client = await self.clients.client(url, network_of(proxy))
        except ValueError:
            raise UpstreamError('unsupported upstream URL') from None
        except UpstreamError as exc:
            await self._failed(req, exc.detail, blocking=not isinstance(exc, UpstreamBlocked))
            self._account_error(req, started)
            raise
        auth = self._auth(req.repo, proxy, AssetSpec(path=upstream_path))
        send = {'Accept-Encoding': 'gzip, deflate', **(headers or {})}
        if auth is not None:
            send.update(await auth.headers(url))
        try:
            request = client.build_request(req.method, url, headers=send, content=body or None)
            response = await client.send(request, stream=True)
        except httpx.TimeoutException:
            await self._failed(req, 'upstream timed out')
            self._account_error(req, started)
            raise UpstreamTimeout('upstream timed out') from None
        except httpx.HTTPError as exc:
            await self._failed(req, _describe(exc))
            self._account_error(req, started)
            raise UpstreamError('upstream connection failed') from None
        except UpstreamError as exc:
            await self._failed(req, exc.detail, blocking=not isinstance(exc, UpstreamBlocked))
            self._account_error(req, started)
            raise
        try:
            status = response.status_code
            if blocking_status(status):
                await self._answered(req, status)
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
            await self._failed(req, _describe(exc), after_response=True)
            self._account_error(req, started)
            raise UpstreamError('upstream transfer failed') from None
        finally:
            with anyio.CancelScope(shield=True):
                await response.aclose()
        if not blocking_status(status):
            await self._answered(req, status)  # success once the whole body arrived
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
    async def _unchanged_by_head(
        self, req: FormatRequest, spec: AssetSpec, proxy: StoredProxy, asset: StoredAsset
    ) -> bool:
        """``spec.revalidate_head``: a stale asset confirmed unchanged by an upstream HEAD (timestamps updated).
        Any failure or a changed answer returns False and the caller refreshes as usual."""
        if spec.revalidate_head is None:
            return False
        try:
            response, info = await self._open(req, spec, proxy, method='HEAD')
        except UpstreamError, DigestMismatch:
            return False
        await response.aclose()
        if info.status != 200 or not spec.revalidate_head(asset, info):
            return False
        await self._revalidated(asset, info)
        return True

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
        network = network_of(proxy)
        redirects = retries = 0
        challenged = False
        await self.breaker.admit(req.repo)
        started = time.perf_counter()
        while True:
            try:
                client = await self.clients.client(url, network)
            except ValueError:
                raise UpstreamError('upstream redirected to an unsupported URL') from None
            except UpstreamError as exc:  # SSRF guard refusals, DNS failures (outbound proxy: checked here)
                await self._failed(req, exc.detail, blocking=not isinstance(exc, UpstreamBlocked))
                raise
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
                await self._failed(req, _describe(exc))
                raise UpstreamError('upstream unreachable') from None
            except httpx.TimeoutException:
                await self._failed(req, 'upstream timed out')
                raise UpstreamTimeout('upstream timed out') from None
            except httpx.HTTPError as exc:
                await self._failed(req, _describe(exc))
                raise UpstreamError('upstream connection failed') from None
            except UpstreamError as exc:  # SSRF guard refusals, DNS failures
                await self._failed(req, exc.detail, blocking=not isinstance(exc, UpstreamBlocked))
                raise
            status = response.status_code
            if status in REDIRECT_CODES and 'location' in response.headers:
                await response.aclose()
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    await self._failed(req, 'too many redirects', blocking=False)
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
            if not (status == 200 and method == 'GET'):
                await self._answered(req, status)  # a 200 GET succeeds once its body arrived (fills, below)
            self.metrics.upstream_latency.labels(req.repo.name).observe(latency / 1000)
            info = UpstreamInfo(
                url=url,
                status=status,
                headers={k.lower(): v for k, v in response.headers.items()},
                latency_ms=latency,
            )
            if spec.on_upstream is not None:
                spec.on_upstream(info)
            return response, info

    def _upstream_failed(self, req: FormatRequest, error: str, *, after_response: bool = False) -> None:
        log.warning('upstream request failed', extra={'error': error})
        self.emitter.upstream(
            repo_id=req.repo.id, format=req.repo.format, ok=False, error=error, count_request=not after_response
        )

    async def _failed(
        self, req: FormatRequest, error: str, *, after_response: bool = False, blocking: bool = True
    ) -> None:
        """An upstream request failed: health, counters and (``blocking``: the upstream is down) auto-block."""
        self._upstream_failed(req, error, after_response=after_response)
        if blocking:
            with anyio.CancelScope(shield=True):
                await self.breaker.failure(req.repo, error)

    async def _answered(self, req: FormatRequest, status: int) -> None:
        """The upstream answered ``status``: auto-block counts 5xx and 429 as failures, anything else as success."""
        with anyio.CancelScope(shield=True):
            if blocking_status(status):
                await self.breaker.failure(req.repo, f'upstream answered {status}')
            else:
                await self.breaker.success(req.repo)

    # ------------------------------------------------------------------ coalescing: the owner
    async def _serve_owner(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        proxy: StoredProxy,
        asset: StoredAsset | None,
        lease: Lease,
        started: float,
    ) -> Response:
        self.metrics.coalesce(repo=req.repo.name, event='solo' if lease.solo else 'owner')
        try:
            if lease.replaced:  # a fill just finished: it may have cached what we need
                asset = await self._cached(req.repo, spec)
                if asset is not None and self._fresh(asset, spec, proxy):
                    await lease.finish(outcome=CacheState.HIT.value)
                    return self._hit(req, asset, CacheState.HIT, started, spec.artifact)
            response = await self._fetch(req, spec, proxy, asset, lease, started)
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                await lease.fail(exc)
            raise
        if not lease.handed_off:
            await lease.finish()
        return response

    async def _fetch(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        proxy: StoredProxy,
        asset: StoredAsset | None,
        lease: Lease,
        started: float,
    ) -> Response:
        if asset is not None:
            if await self._unchanged_by_head(req, spec, proxy, asset):
                await lease.finish(outcome=CacheState.REVALIDATED.value)
                return self._hit(req, asset, CacheState.REVALIDATED, started, spec.artifact)
            try:
                spec = await self._expect(spec)
                response, info = await self._open(req, spec, proxy, conditional=asset)
            except (UpstreamError, DigestMismatch) as exc:
                if proxy.stale_on_error:
                    log.info('serving stale copy', extra={'path': spec.path, 'error': exc.detail})
                    await lease.fail(exc)
                    return self._hit(req, asset, CacheState.STALE, started, spec.artifact)
                self._account_error(req, started)
                raise
            if info.status == 304:
                await response.aclose()
                await self._revalidated(asset, info)
                await lease.finish(outcome=CacheState.REVALIDATED.value)
                return self._hit(req, asset, CacheState.REVALIDATED, started, spec.artifact)
            if info.status != 200:
                await response.aclose()
                error = UpstreamError(f'upstream answered {info.status}')
                if proxy.stale_on_error:
                    await lease.fail(error)
                    return self._hit(req, asset, CacheState.STALE, started, spec.artifact)
                self._account_error(req, started)
                raise error
            return await self._fill(req, spec, proxy, response, info, started, lease)

        try:
            spec = await self._expect(spec)
            response, info = await self._open(req, spec, proxy)
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
        return await self._fill(req, spec, proxy, response, info, started, lease)

    @staticmethod
    def _head_passthrough(spec: AssetSpec) -> bool:
        return spec.head_passthrough if spec.head_passthrough is not None else spec.kind is AssetKind.CONTENT

    async def _head_miss(self, req: FormatRequest, spec: AssetSpec, proxy: StoredProxy, started: float) -> Response:
        """``HEAD`` for content that is not cached: an upstream ``HEAD`` (nothing downloaded, nothing coalesced)."""
        try:
            response, info = await self._open(req, spec, proxy, method='HEAD')
        except UpstreamError, DigestMismatch:
            self._account_error(req, started)
            raise
        await response.aclose()
        if info.status in GONE_CODES:
            await self._remember_missing(req.repo, spec, proxy)
            self._account(req, CacheState.FETCHING, 404, 0, started)
            raise NotFound('Not Found')
        if info.status != 200:
            self._account_error(req, started)
            raise UpstreamError(f'upstream answered {info.status}')
        self._account(req, CacheState.FETCHING, 200, 0, started)
        return self._head_response(spec, info)

    # ------------------------------------------------------------------ coalescing: followers
    async def _await_outcome(self, follower: Follower, *, stream_ok: bool) -> FillState | None:
        """Wait until the followed fill is finished (or, with ``stream_ok``, streaming and tailable here). None: the
        owner went away (aborted, lease expired, stalled) and the caller should claim again."""
        while True:
            state = follower.state()
            if state is None or not follower.mine(state) or state.status is Status.ABORTED or follower.stalled():
                return None
            if state.status in (Status.DONE, Status.FAILED):
                return state
            if stream_ok and state.status is Status.STREAMING and self.coalescer.tail_ok(state):
                return state
            await follower.wait(WAIT_S)

    async def _serve_follower(
        self, req: FormatRequest, spec: AssetSpec, proxy: StoredProxy, follower: Follower, started: float
    ) -> Response | None:
        """Answer from another request's fetch: tail its upload file, or wait for the commit and serve the blob.
        None: the owner went away; the caller claims again (one of the followers becomes the owner)."""
        tail = spec.kind is AssetKind.CONTENT and not req.is_head
        handed_off = False
        try:
            state = await self._await_outcome(follower, stream_ok=tail)
            if state is None:
                self.metrics.coalesce(repo=req.repo.name, event='takeover')
                return None
            if state.status is Status.STREAMING:
                handed_off = True
                self.metrics.coalesce(repo=req.repo.name, event='tail')
                return self._tail_response(req, spec, proxy, follower, state)
            if state.status is Status.DONE and state.outcome == UNSTORED:
                # The owner's copy was not stored (quota, low disk): nothing to follow, fetch on our own.
                cached = await self._cached(req.repo, spec)
                if cached is not None and self._fresh(cached, spec, proxy):
                    return self._hit(req, cached, CacheState.COALESCED, started, spec.artifact)
                lease = self.coalescer.solo(req.repo.id, spec.path)
                return await self._serve_owner(req, spec, proxy, cached, lease, started)
            if state.status is Status.FAILED:
                cached = await self._cached(req.repo, spec)
                if cached is not None and proxy.stale_on_error and state.error_code in STALE_CODES:
                    return self._hit(req, cached, CacheState.STALE, started, spec.artifact)
                error = _failure(state)
                if isinstance(error, NotFound):
                    self._account(req, CacheState.COALESCED, 404, 0, started)
                else:
                    self._account_error(req, started)
                raise error
            asset = await self._cached(req.repo, spec)
            if asset is None:
                return None
            self.metrics.coalesce(repo=req.repo.name, event='wait')
            if state.outcome in (CacheState.REVALIDATED.value, CacheState.HIT.value):
                return self._hit(req, asset, CacheState(state.outcome), started, spec.artifact)
            return self._hit(req, asset, CacheState.COALESCED, started, spec.artifact)
        finally:
            if not handed_off:
                with anyio.CancelScope(shield=True):
                    await follower.leave()

    def _tail_response(
        self, req: FormatRequest, spec: AssetSpec, proxy: StoredProxy, follower: Follower, state: FillState
    ) -> Response:
        headers: dict[str, str] = {
            'Content-Type': state.content_type or DEFAULT_TYPE,
            'X-Ws-Cache': CacheState.COALESCED.value,
        }
        headers.update(state.headers)
        headers.update(spec.response_headers)
        if state.total is not None:
            headers['Content-Length'] = str(state.total)
        tid = self._start_transfer(req, spec, state.total, None, cache=CacheState.COALESCED)
        req.log(cache=CacheState.COALESCED.value)
        body = self._tail_body(req, spec, proxy, follower, state.total, tid)
        return StreamingResponse(body, status_code=200, headers=headers)

    async def _tail_body(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        proxy: StoredProxy,
        follower: Follower,
        total: int | None,
        tid: str,
    ) -> AsyncGenerator[bytes]:
        """A follower's response body: the owner's bytes as they land (last piece held back until the owner has
        verified and committed them). If the owner goes away, claim again: either follow the new owner or fetch
        upstream ourselves, in both cases checking that the bytes already sent are a prefix of the new fill."""
        flight: Follower | Lease = follower
        source = _Source()
        sent = 0
        prefix: str | None = None
        cache = CacheState.COALESCED
        try:
            for _ in range(MAX_ROUNDS):
                held: bytes | None = None
                try:
                    if isinstance(flight, Follower):
                        chunks = self._read_fill(flight, source, sent, prefix, total)
                    else:
                        cache = CacheState.FETCHING
                        self.emitter.transfer_progress(tid, sent, cache=cache)
                        chunks = self._takeover(req, spec, proxy, flight, sent, prefix, total)
                    async with contextlib.aclosing(chunks):
                        async for chunk in chunks:
                            if held is not None:
                                yield held
                                sent += len(held)
                                self.emitter.transfer_progress(tid, sent, bytes_total=total)
                            held = chunk
                    if held is not None:
                        yield held
                        sent += len(held)
                    break
                except _Abandoned:
                    self.metrics.coalesce(repo=req.repo.name, event='takeover')
                if isinstance(flight, Follower):
                    await flight.leave()
                prefix = None
                if sent:
                    if source.fd is None:
                        raise UpstreamError('cannot resume the transfer')
                    prefix = await asyncio.to_thread(_hash_prefix, source.fd, sent)
                source.close()
                log.info('coalesced fill abandoned by its owner; claiming it', extra={'path': spec.path})
                flight = await self.coalescer.claim(req.repo.id, spec.path)
            else:
                raise UpstreamError('the upstream fetch was abandoned repeatedly')
        except BaseException as exc:
            error = _describe(exc)
            self.emitter.transfer_finished(tid, status=502, bytes_done=sent, error=error)
            self.metrics.request(repo=req.repo.name, format=req.repo.format, status=502, cache='ERROR')
            if isinstance(exc, Exception):
                log.warning('coalesced transfer aborted', extra={'path': spec.path, 'error': error, 'bytes': sent})
                raise ResponseAborted(error) from exc
            raise
        finally:
            source.close()
            if isinstance(flight, Follower):
                with anyio.CancelScope(shield=True):
                    await flight.leave()
        self.emitter.transfer_finished(tid, status=200, bytes_done=sent)
        self.metrics.request(repo=req.repo.name, format=req.repo.format, status=200, cache=cache.value)
        self.metrics.add_bytes(repo=req.repo.name, direction='out_upstream', count=sent)

    async def _read_fill(
        self, follower: Follower, source: _Source, start: int, prefix: str | None, total: int | None
    ) -> AsyncGenerator[bytes]:
        """The followed fill's bytes from ``start``, as they become readable; returns once the owner committed.
        Raises ``_Abandoned`` when the owner went away, the owner's error when the fetch failed. Once the commit was
        seen it is final: a slow follower may still be reading long after the fill record expired (``LINGER_MS``)."""
        pos = start
        checked = prefix is None
        final: FillState | None = None
        while True:
            state = final or follower.state()
            if final is None and (
                state is None or not follower.mine(state) or state.status is Status.ABORTED or follower.stalled()
            ):
                raise _Abandoned
            assert state is not None
            if state.status is Status.FAILED:
                raise _failure(state)
            if state.status is Status.PENDING or (
                state.status is Status.STREAMING and not self.coalescer.tail_ok(state)
            ):
                await follower.wait(WAIT_S)
                continue
            done = state.status is Status.DONE
            if done and state.sha256 is not None and state.size is not None:
                final = state
            if done and state.outcome == UNSTORED:
                raise _Abandoned  # served without being stored: claim again and fetch on our own
            if done and (state.sha256 is None or state.size is None):
                raise UpstreamError('the fill finished without content')
            if total is not None and state.total is not None and state.total != total:
                raise UpstreamError('the upstream size changed during the transfer')
            if source.fill != state.owner and not await source.open(self.blobs, state):
                if done:
                    raise UpstreamError('the cached file disappeared')
                await follower.wait(WAIT_S)  # renamed into place or aborted: the state will say which
                continue
            fd = source.fd
            assert fd is not None
            readable = self.coalescer.readable(state)
            if not checked:
                if readable < start and not done:
                    await follower.wait(WAIT_S)
                    continue
                if readable < start or await asyncio.to_thread(_hash_prefix, fd, start) != prefix:
                    raise DigestMismatch('the upstream content changed during the transfer')
                checked = True
            if pos < readable:
                data = await asyncio.to_thread(os.pread, fd, min(TAIL_READ, readable - pos), pos)
                if not data:
                    if done:
                        raise UpstreamError('the cached file is shorter than recorded')
                    await follower.wait(WAIT_S)
                    continue
                pos += len(data)
                yield data
                continue
            if done:
                return
            await follower.wait(WAIT_S)

    async def _takeover(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        proxy: StoredProxy,
        lease: Lease,
        skip: int,
        prefix: str | None,
        total: int | None,
    ) -> AsyncGenerator[bytes]:
        """A follower that became the owner mid-response: fetch upstream into the cache (a normal tee, with followers
        of its own) and continue its client's response after the ``skip`` bytes it already sent, once those are
        confirmed identical (``prefix``: their sha256)."""
        self.metrics.coalesce(repo=req.repo.name, event='solo' if lease.solo else 'owner')
        try:
            spec = await self._expect(spec)
            response, info = await self._open(req, spec, proxy)
            if info.status != 200:
                await response.aclose()
                if info.status in GONE_CODES:
                    raise NotFound('Not Found')
                raise UpstreamError(f'upstream answered {info.status}')
            fill = await self._open_fill(req, spec, response, info)
            if total is not None and fill.total is not None and fill.total != total:
                with anyio.CancelScope(shield=True):
                    await response.aclose()
                    if fill.writer is not None:
                        await fill.writer.abort()
                raise UpstreamError('the upstream size changed during the transfer')
        except BaseException as exc:
            with anyio.CancelScope(shield=True):
                await lease.fail(exc)
            raise
        if fill.writer is None:
            await lease.finish(outcome=UNSTORED)
            self._not_stored(req, spec, fill.reason or 'low_disk')
            tee = self._pass_through(req, spec, response, info, fill)
        else:
            self._streaming(lease, fill, None)
            tee = self._background_fill(req, spec, response, info, fill, lease, None).read()
        hasher = hashlib.sha256()
        skipped = 0
        async with contextlib.aclosing(tee):
            async for chunk in tee:
                if skipped < skip:
                    take = min(len(chunk), skip - skipped)
                    await asyncio.to_thread(hasher.update, memoryview(chunk)[:take])
                    skipped += take
                    if skipped == skip and hasher.hexdigest() != prefix:
                        raise DigestMismatch('the upstream content changed during the transfer')
                    chunk = chunk[take:]
                    if not chunk:
                        continue
                yield chunk
        if skipped < skip:
            raise DigestMismatch('the upstream content changed during the transfer')

    # ------------------------------------------------------------------ fills
    async def _fill(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        proxy: StoredProxy,
        response: httpx.Response,
        info: UpstreamInfo,
        started: float,
        lease: Lease,
    ) -> Response:
        if spec.kind is AssetKind.METADATA or req.is_head:
            filled = await self._fill_buffered(req, spec, response, info, started, lease, unstored_ok=True)
            if isinstance(filled, Response):
                return filled
            return self._serve(req, filled, CacheState.FETCHING)
        return await self._fill_stream(req, spec, response, info, lease)

    def _length(self, spec: AssetSpec, info: UpstreamInfo, max_size: int) -> int | None:
        raw = info.headers.get('content-length')
        total = int(raw) if raw and raw.isdigit() and not info.headers.get('content-encoding') else None
        expected = spec.expected.size if spec.expected else None
        if total is not None and expected is not None and total != expected:
            raise DigestMismatch(f'upstream length {total} does not match the expected size {expected}')
        if total is not None and total > max_size:
            raise UpstreamError('upstream file exceeds the size limit')
        return total

    def _start_transfer(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        total: int | None,
        latency: float | None,
        *,
        cache: CacheState = CacheState.FETCHING,
    ) -> str:
        now = datetime.now(UTC)
        tid = new_id()
        self.emitter.transfer_started(
            Transfer(
                id=tid,
                repo_id=req.repo.id,
                repo=req.repo.name,
                format=req.repo.format,
                path=spec.path,
                cache=cache,
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
        # The blobs document was written by the commit's GC claim (``BlobStore.guard``), before the rename.
        saved = await assets_db.save(self.db, asset)  # also counts the repository's usage (quotas)
        if self.quotas is not None:
            await self.quotas.written(req.repo)
        return saved

    def _stored_headers(self, spec: AssetSpec, info: UpstreamInfo) -> dict[str, str]:
        return {name: info.headers[name.lower()] for name in spec.store_headers if name.lower() in info.headers}

    def _followed(self, lease: Lease, tid: str) -> None:
        """Show the lease's follower count on the owner's live transfer."""

        def update(followers: int) -> None:
            self.emitter.transfer_followers(tid, followers)

        lease.on_followers = update

    async def _fill_buffered(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        response: httpx.Response,
        info: UpstreamInfo,
        started: float,
        lease: Lease,
        *,
        unstored_ok: bool = False,
    ) -> StoredAsset | Response:
        """Read the whole body, verify, validate, transform and store it. With ``unstored_ok``, a body the blob store
        refuses (low-disk guard; content: a hard quota) is answered from memory without being stored (the returned
        ``Response``); metadata is never refused by a quota (small, and the repository needs it to work)."""
        max_size = spec.max_size or (
            METADATA_MAX_BYTES if spec.kind is AssetKind.METADATA else self.settings.max_upload_bytes
        )
        tid: str | None = None
        done = 0
        unstored: bytes | None = None
        reason: str | None = None
        try:
            total = self._length(spec, info, max_size)
            tid = self._start_transfer(req, spec, total, info.latency_ms)
            self._followed(lease, tid)
            chunks: list[bytes] = []
            async for chunk in response.aiter_bytes(CHUNK):
                done += len(chunk)
                if done > max_size:
                    raise UpstreamError('upstream file exceeds the size limit')
                chunks.append(chunk)
                self.emitter.transfer_progress(tid, done, bytes_total=total)
            await self._answered(req, info.status)  # the whole body arrived (auto-block success)
            data = b''.join(chunks)
            checksums = await asyncio.to_thread(_verify, spec.expected, data)
            if spec.validate is not None:
                await asyncio.to_thread(spec.validate, data, info)
            body = await asyncio.to_thread(spec.transform, data, info) if spec.transform is not None else data
            content_type = spec.content_type or info.headers.get('content-type') or DEFAULT_TYPE
            writer: BlobWriter | None = None
            if unstored_ok and spec.kind is AssetKind.CONTENT and await self._quota_refuses(req, len(body)):
                reason = 'quota'
            else:
                try:
                    writer = await self.blobs.open_writer(max_size=max(max_size, len(body)))
                except InsufficientStorage:
                    if not unstored_ok:
                        raise
                    reason = 'low_disk'
            if writer is None:
                unstored = body
                await lease.finish(outcome=UNSTORED)
            else:
                async with writer:
                    await writer.write(body)
                    ref = await self.blobs.commit(writer)
                if spec.transform is not None:
                    checksums = {'sha256': ref.sha256}
                asset = await self._record(
                    req, spec, ref, content_type, self._stored_headers(spec, info), info, checksums
                )
                await lease.finish(sha256=ref.sha256, size=ref.size)
                if spec.on_stored is not None:
                    await spec.on_stored(asset, data)
        except BaseException as exc:
            error = _describe(exc)
            if tid is not None:
                self.emitter.transfer_finished(tid, status=502, bytes_done=done, error=error)
            else:
                self._account_error(req, started)
            if isinstance(exc, DigestMismatch):
                self._upstream_failed(req, error, after_response=True)
            elif isinstance(exc, httpx.HTTPError):
                await self._failed(req, error, after_response=True)
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
        if unstored is not None:
            self._not_stored(req, spec, reason or 'low_disk')
            req.log(cache=CacheState.FETCHING.value, bytes=len(unstored), not_stored=reason)
            return self._unstored_response(req, spec, info, unstored, content_type, reason or 'low_disk')
        req.log(cache=CacheState.FETCHING.value, bytes=asset.size)
        return asset

    async def _quota_refuses(self, req: FormatRequest, size: int | None) -> bool:
        """Does the repository's hard quota refuse to cache ``size`` more bytes (content only)?"""
        if self.quotas is None:
            return False
        return await self.quotas.refusal(req.repo, size, kind='fill') is not None

    def _not_stored(self, req: FormatRequest, spec: AssetSpec, reason: str) -> None:
        self.metrics.fill_not_stored(repo=req.repo.name, reason=reason)
        log.info('serving without caching', extra={'path': spec.path, 'reason': reason})

    def _unstored_response(
        self, req: FormatRequest, spec: AssetSpec, info: UpstreamInfo, body: bytes, content_type: str, reason: str
    ) -> Response:
        headers = {'Content-Type': content_type, 'X-Ws-Cache': CacheState.FETCHING.value, 'X-Ws-Not-Stored': reason}
        headers.update(self._stored_headers(spec, info))
        headers.update(spec.response_headers)
        if req.is_head:
            headers['Content-Length'] = str(len(body))
            return Response(status_code=200, headers=headers)
        return Response(body, status_code=200, headers=headers)

    async def _open_fill(
        self, req: FormatRequest, spec: AssetSpec, response: httpx.Response, info: UpstreamInfo
    ) -> _Fill:
        """Check the announced length and open the ``BlobWriter`` of a streaming fill (closes ``response`` when
        that fails). A hard repository quota or the low-disk guard refusing the write gives a fill without a writer:
        the content is passed through to the client, verified, and not stored."""
        max_size = spec.max_size or self.settings.max_upload_bytes
        expected = spec.expected
        reason: str | None = None
        writer: BlobWriter | None = None
        try:
            total = self._length(spec, info, max_size)
            if await self._quota_refuses(req, total if total is not None else expected.size if expected else None):
                reason = 'quota'
            else:
                try:
                    writer = await self.blobs.open_writer(
                        max_size=max_size,
                        expected_size=expected.size if expected else None,
                        expected_sha256=expected.sha256.lower() if expected and expected.sha256 else None,
                    )
                except InsufficientStorage:
                    reason = 'low_disk'
        except BaseException:
            with anyio.CancelScope(shield=True):
                await response.aclose()
            raise
        digests = expected.digests() if expected else {}
        return _Fill(
            writer=writer,
            total=total,
            content_type=spec.content_type or info.headers.get('content-type') or DEFAULT_TYPE,
            stored=self._stored_headers(spec, info),
            extra={alg: hashlib.new(alg) for alg in digests if alg != 'sha256' or writer is None},
            reason=reason,
        )

    def _streaming(self, lease: Lease, fill: _Fill, tid: str | None) -> None:
        """Publish the fill to followers; from here on the response body finishes the lease."""
        assert fill.writer is not None
        lease.streaming(
            upload=fill.writer.upload_id,
            total=fill.total,
            content_type=fill.content_type,
            headers=fill.stored,
            fd=fill.writer.fileno(),
        )
        lease.handed_off = True
        if tid is not None:
            self._followed(lease, tid)

    async def _fill_stream(
        self, req: FormatRequest, spec: AssetSpec, response: httpx.Response, info: UpstreamInfo, lease: Lease
    ) -> Response:
        fill = await self._open_fill(req, spec, response, info)
        tid = self._start_transfer(req, spec, fill.total, info.latency_ms)
        feed: Feed | None = None
        if fill.writer is None:
            await lease.finish(outcome=UNSTORED)  # followers fetch on their own
            self._not_stored(req, spec, fill.reason or 'low_disk')
        else:
            self._streaming(lease, fill, tid)
            feed = self._background_fill(req, spec, response, info, fill, lease, tid)
        headers: dict[str, str] = {'Content-Type': fill.content_type, 'X-Ws-Cache': CacheState.FETCHING.value}
        if fill.reason:
            headers['X-Ws-Not-Stored'] = fill.reason
        headers.update(fill.stored)
        headers.update(spec.response_headers)
        if fill.total is not None:
            headers['Content-Length'] = str(fill.total)
        req.log(cache=CacheState.FETCHING.value)
        engine = self

        async def body() -> AsyncIterator[bytes]:
            source = engine._pass_through(req, spec, response, info, fill, tid) if feed is None else feed.read()
            try:
                async with contextlib.aclosing(source) as chunks:
                    async for chunk in chunks:
                        yield chunk
            except BaseException as exc:
                error = _describe(exc)
                if feed is None or not feed.kept:  # a fill that went on without its client ends the transfer itself
                    engine.emitter.transfer_finished(tid, status=502, bytes_done=fill.done, error=error)
                engine.metrics.request(repo=req.repo.name, format=req.repo.format, status=502, cache='ERROR')
                if isinstance(exc, Exception):
                    raise ResponseAborted(error) from exc
                raise
            engine.emitter.transfer_finished(tid, status=200, bytes_done=fill.done)
            engine.metrics.request(repo=req.repo.name, format=req.repo.format, status=200, cache='FETCHING')
            engine.metrics.add_bytes(repo=req.repo.name, direction='out_upstream', count=fill.done)

        return StreamingResponse(body(), status_code=200, headers=headers)

    def _background_fill(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        response: httpx.Response,
        info: UpstreamInfo,
        fill: _Fill,
        lease: Lease,
        tid: str | None,
    ) -> Feed:
        """Start the streaming fill as a background task of the engine (``FillRegistry``), not of the request: if
        the client goes away the fill still finishes (commit, index, followers, the next request hits). Returns the
        feed the client's response reads."""
        assert fill.writer is not None
        feed = Feed(self.blobs, fill.writer.upload_id)
        task = self.fills.spawn(
            self._run_fill(req, spec, response, info, fill, lease, tid, feed), name=f'fill-{lease.key}'
        )

        def detach() -> bool:
            if self.fills.detach(task):
                log.info('client went away; finishing the cache fill', extra={'path': spec.path, 'bytes': fill.done})
                return True
            log.warning(
                'too many cache fills without a client; aborting this one',
                extra={'path': spec.path, 'fills': self.fills.detached},
            )
            task.cancel()
            return False

        feed.on_detach = detach
        return feed

    async def _run_fill(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        response: httpx.Response,
        info: UpstreamInfo,
        fill: _Fill,
        lease: Lease,
        tid: str | None,
        feed: Feed,
    ) -> None:
        """Upstream → ``BlobWriter`` (256 KiB chunks), each chunk released to ``feed`` (the client) once the next
        one arrived: the last chunk is held back until the size and every expected digest are verified and the blob
        committed, so a mismatch (or a reset) leaves the client with an incomplete transfer, never a complete wrong
        file, and nothing is cached. Followers see every flushed byte (``lease.advance``, O(1)) and the outcome
        (``finish`` / ``fail``). Runs to the end whether or not the client is still there; bounded by the upstream
        timeouts and by the stall limit (no upstream bytes for ``_stall_s``). Cancelled only by shutdown (or the
        ``FillRegistry`` bound): then ``aborted`` (followers take over) and the upload file is removed."""
        writer = fill.writer
        assert writer is not None
        expected = spec.expected
        held: bytes | None = None
        committed = False
        error: str | None = None
        failure: BaseException | None = None
        stall = self._stall_s()
        loop = asyncio.get_running_loop()
        try:
            try:
                async with asyncio.timeout(stall) as idle:
                    async for chunk in response.aiter_bytes(CHUNK):
                        idle.reschedule(loop.time() + stall)
                        for hasher in fill.extra.values():
                            hasher.update(chunk)
                        await writer.write(chunk)
                        fill.done += len(chunk)
                        lease.advance(writer.flushed)
                        if tid is not None:
                            self.emitter.transfer_progress(tid, fill.done, bytes_total=fill.total)
                        if held is not None:
                            feed.release(held, writer.flushed)
                        else:
                            feed.wrote(writer.flushed)
                        held = chunk
            except TimeoutError:
                raise UpstreamTimeout('upstream stalled') from None
            await self._answered(req, info.status)  # the whole body arrived (auto-block success)
            if expected is not None:
                for alg, hasher in fill.extra.items():
                    if hasher.hexdigest() != expected.digests()[alg]:
                        raise DigestMismatch(f'{alg} does not match the expected digest')
                check = _check_of(expected)
                if check is not None:
                    await writer.flush()
                    await asyncio.to_thread(_check_file, check, writer.path)
            ref = await self.blobs.commit(writer)
            committed = True
            feed.committed(ref.sha256)
            checksums = {'sha256': ref.sha256, **{alg: h.hexdigest() for alg, h in fill.extra.items()}}
            asset = await self._record(req, spec, ref, fill.content_type, fill.stored, info, checksums)
            self.emitter.asset_downloaded(asset.id)
            await lease.finish(sha256=ref.sha256, size=ref.size)
            if spec.on_stored is not None:
                await spec.on_stored(asset, None)
            if held is not None:
                feed.release(held, ref.size)
            feed.end()
        except BaseException as exc:
            error = 'cache fill cancelled' if isinstance(exc, asyncio.CancelledError) else _describe(exc)
            failure = exc
            if isinstance(exc, DigestMismatch):
                self._upstream_failed(req, error, after_response=True)
            elif isinstance(exc, httpx.HTTPError | UpstreamTimeout):
                await self._failed(req, error, after_response=True)
            log.warning('cache fill aborted', extra={'path': spec.path, 'error': error, 'bytes': fill.done})
            with anyio.CancelScope(shield=True):
                await lease.fail(_as_error(exc))
            if not isinstance(exc, Exception):
                raise
            return  # the client (if still there) gets the error through the feed
        finally:
            with anyio.CancelScope(shield=True):
                await response.aclose()
                if not committed:
                    await writer.abort()
            if failure is not None:  # only now, as before: nothing of the failed fill is left when the client hears
                feed.fail(failure)
            if feed.kept and tid is not None:
                self.emitter.transfer_finished(tid, status=502 if error else 200, bytes_done=fill.done, error=error)
        self.metrics.add_bytes(repo=req.repo.name, direction='in_upstream', count=fill.done)

    async def _pass_through(
        self,
        req: FormatRequest,
        spec: AssetSpec,
        response: httpx.Response,
        info: UpstreamInfo,
        fill: _Fill,
        tid: str | None = None,
    ) -> AsyncGenerator[bytes]:
        """Upstream → the caller only (the write was refused: hard quota, low disk). Verified like a tee: the last
        chunk is held back until the size and every expected digest match, so the client never receives a complete
        wrong file. Nothing is stored and nobody follows."""
        expected = spec.expected
        held: bytes | None = None
        try:
            async for chunk in response.aiter_bytes(CHUNK):
                for hasher in fill.extra.values():
                    hasher.update(chunk)
                fill.done += len(chunk)
                if tid is not None:
                    self.emitter.transfer_progress(tid, fill.done, bytes_total=fill.total)
                if held is not None:
                    yield held
                held = chunk
            await self._answered(req, info.status)  # the whole body arrived (auto-block success)
            if expected is not None:
                if expected.size is not None and fill.done != expected.size:
                    raise DigestMismatch(f'size {fill.done} does not match the expected {expected.size}')
                for alg, hasher in fill.extra.items():
                    if hasher.hexdigest() != expected.digests()[alg]:
                        raise DigestMismatch(f'{alg} does not match the expected digest')
            if held is not None:
                yield held
        except BaseException as exc:
            error = _describe(exc)
            if isinstance(exc, DigestMismatch):
                self._upstream_failed(req, error, after_response=True)
            elif isinstance(exc, httpx.HTTPError):
                await self._failed(req, error, after_response=True)
            log.warning('pass-through aborted', extra={'path': spec.path, 'error': error, 'bytes': fill.done})
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await response.aclose()
        self.metrics.add_bytes(repo=req.repo.name, direction='in_upstream', count=fill.done)

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

    def _hit(
        self, req: FormatRequest, asset: StoredAsset, cache: CacheState, started: float, artifact: str | None = None
    ) -> Response:
        response = self._serve(req, asset, cache)
        sent = asset.size if response.status_code == 200 and req.method == 'GET' else 0
        self._account(req, cache, response.status_code, sent, started)
        if sent:
            self.emitter.asset_downloaded(asset.id)  # batched; flushed by the stats rollup (cleanup criteria)
        if sent and asset.kind is AssetKind.CONTENT:
            self.emitter.activity(
                ActivityEvent(
                    type=EventType.CACHE_STALE if cache is CacheState.STALE else EventType.CACHE_HIT,
                    ts=datetime.now(UTC),
                    repo_id=req.repo.id,
                    repo=req.repo.name,
                    format=req.repo.format,
                    path=asset.path,
                    artifact=artifact,
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
