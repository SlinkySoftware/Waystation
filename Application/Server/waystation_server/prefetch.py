"""The internal prefetch route (T-O12, adr/0012; SPECIFICATION §4.1 "Prefetch").

``POST /_internal/prefetch/<repo id>`` is how the worker's prefetch jobs get proxy work done: the worker may not
import the server (ARCHITECTURE §3) and the format protocol logic lives here, so the worker names *what* to fetch and
the server fetches it **through the format plugin, exactly as for a client**: the proxy engine fetches, verifies,
coalesces (a client asking for the same file at the same moment shares the fill), commits and indexes.

- Authorisation: only a request carrying a valid system grant for this repository (``X-Ws-System-Grant``,
  ``waystation_common.system_grants``; decided by ``security.access.system_grant``) is served; it acts as the
  ``prefetch`` system principal with ``browse``/``read`` on this one online proxy repository. Anything else answers
  404, as for a repository that does not exist. The web tier does not route ``/_internal/`` (only the worker, on
  ``WAYSTATION_INTERNAL_URL``, reaches it).
- Body: ``{"op": "warm", "items": [...]}`` (items in the format's warm syntax, ``FormatDescriptor.warm_item``; the
  plugin's ``warm`` expands each into ``GET`` requests of its own paths) or ``{"op": "refresh", "items": [...]}``
  (cached metadata asset keys; ``FormatDescriptor.refresh_request`` maps each to the client request, which the engine
  then revalidates even before it is stale).
- Answer: ``application/x-ndjson``, one line per file fetched (``file``), one per item (``item``: ``ok``, ``error``,
  files and how many came from the upstream), a ``ping`` every ``PING_S`` while work runs (keeps the worker's idle
  timeout from firing on a large layer) and a final ``done``.
- Bounds: at most ``MAX_ITEMS`` items per call, ``ITEM_CONCURRENCY`` at a time per call, ``SLOTS`` items per server
  process across calls, ``MAX_FILES`` requests per item. Bodies of streamed fills are read and dropped (the fill is
  what matters; nothing is sent on). Prefetch requests are never downloads: the engine records no download counts
  and no client request counters for them (no self-reinforcing popularity); live transfers show the principal
  ``prefetch``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import weakref
from collections.abc import AsyncIterator, Mapping
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response, StreamingResponse

from waystation_common.errors import NotFound, PathRejected, UpstreamError, WaystationError
from waystation_common.paths import normalise
from waystation_common.storage import BlobStore
from waystation_server.dataplane.xaccel import XACCEL_HEADER
from waystation_server.formats.base import (
    Fetched,
    FormatDescriptor,
    FormatPlugin,
    FormatRequest,
    PrefetchMode,
    WarmingPlugin,
)
from waystation_server.repos.model import RepoConfig
from waystation_server.security import access
from waystation_server.security.principal import Principal
from waystation_server.services import Services

log = logging.getLogger('waystation.prefetch')

router = APIRouter(include_in_schema=False)

ROUTE = '/_internal/prefetch/{repo_id}'
MAX_ITEMS = 200
MAX_FILES = 5000
ITEM_CONCURRENCY = 4
SLOTS = 8
PING_S = 10.0
KEEP_BYTES = 8 * 1024 * 1024  # streamed bodies up to this size are kept for ``read`` (manifests by digest)
USER_AGENT = 'waystation-prefetch'
_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _process_slots() -> asyncio.Semaphore:
    """The process-wide bound on prefetch items (one semaphore per event loop)."""
    loop = asyncio.get_running_loop()
    slots = _slots.get(loop)
    if slots is None:
        slots = _slots[loop] = asyncio.Semaphore(SLOTS)
    return slots


class PrefetchCall(BaseModel):
    model_config = ConfigDict(extra='forbid')

    op: Literal['warm', 'refresh']
    items: list[Annotated[str, StringConstraints(min_length=1, max_length=2048)]] = Field(max_length=MAX_ITEMS)


def _scope(services: Services, request: Request, repo: RepoConfig, path: str, headers: Mapping[str, str]) -> Any:
    raw = [(b'host', (request.headers.get('host') or 'localhost').encode('latin-1'))]
    raw.append((b'user-agent', USER_AGENT.encode()))
    raw += [(k.lower().encode('latin-1'), v.encode('latin-1')) for k, v in headers.items()]
    url_path = f'/repository/{repo.name}/{path}'
    return {
        'type': 'http',
        'http_version': '1.1',
        'method': 'GET',
        'scheme': 'http',
        'path': url_path,
        'raw_path': url_path.encode('utf-8'),
        'root_path': '',
        'query_string': b'',
        'headers': raw,
        'client': None,
        'server': None,
        'app': request.scope.get('app'),
        'state': {'log': {}, 'client_ip': '', 'services': services},
    }


def _sha256_of(response: Response) -> str | None:
    """The blob an X-Accel answer points at (``/_blobs/sha256/aa/bb/<hex>``)."""
    target = response.headers.get(XACCEL_HEADER)
    if not target:
        return None
    name = target.rsplit('/', 1)[-1]
    return name if len(name) == 64 and all(c in '0123456789abcdef' for c in name) else None


class _Context:
    """``formats.base.WarmContext`` for one item: ``get`` runs the format's own handler on a ``GET`` of its paths."""

    def __init__(
        self,
        services: Services,
        request: Request,
        repo: RepoConfig,
        plugin: FormatPlugin,
        principal: Principal,
        mode: PrefetchMode,
        emit: asyncio.Queue[dict[str, Any] | None],
        item: str,
    ) -> None:
        self.services = services
        self.request = request
        self.repo = repo
        self.plugin = plugin
        self.principal = principal
        self.mode = mode
        self.emit = emit
        self.item = item
        self.files = 0
        self.fetched = 0

    def _format_request(self, path: str, headers: Mapping[str, str]) -> FormatRequest:
        services, repo, principal = self.services, self.repo, self.principal
        return FormatRequest(
            request=StarletteRequest(_scope(services, self.request, repo, path, headers)),
            repo=repo,
            root='repository',
            path=path,
            principal=principal,
            proxy=services.proxy,
            hosted=services.hosted,
            base_url=services.base_url,
            _require=lambda action: access.require_repo(principal, action, repo),
            _allows=lambda action, other: access.check(principal, action, other),
            _repository=services.catalog.get,
            _allows_member=lambda action, member: access.check(principal, action, member),
            prefetch=self.mode,
        )

    async def get(self, path: str, headers: Mapping[str, str] | None = None) -> Fetched:
        if self.files >= MAX_FILES:
            raise UpstreamError(f'more than {MAX_FILES} files for one item')
        self.files += 1
        try:
            clean = normalise(path)
        except PathRejected:
            raise NotFound(f'invalid path {path!r}') from None
        response = await self.plugin.handle(self._format_request(clean, headers or {}))
        body: bytes | None = None
        if isinstance(response, StreamingResponse):
            # The fill is what matters: read and drop; small bodies (manifests by digest) are kept for ``read``.
            chunks: list[bytes] = []
            kept = 0
            try:
                async for chunk in response.body_iterator:
                    if kept <= KEEP_BYTES:
                        data = chunk.encode() if isinstance(chunk, str) else bytes(chunk)
                        kept += len(data)
                        chunks.append(data)
            except Exception as exc:  # the fill failed (upstream reset, digest mismatch...): nothing was cached
                raise UpstreamError(f'{clean}: the transfer failed ({type(exc).__name__})') from None
            body = b''.join(chunks) if kept <= KEEP_BYTES else None
        elif XACCEL_HEADER not in response.headers:
            body = bytes(response.body)
        cache = response.headers.get('x-ws-cache')
        fetched = Fetched(
            path=clean,
            status=response.status_code,
            cache=cache,
            headers=dict(response.headers),
            sha256=_sha256_of(response),
            body=body,
        )
        if cache in ('FETCHING', 'REVALIDATED'):
            self.fetched += 1
        await self.emit.put(
            {'event': 'file', 'item': self.item, 'path': clean, 'status': response.status_code, 'cache': cache}
        )
        if response.status_code >= 400:
            reason = (body or b'')[:300].decode('utf-8', 'replace').strip()
            if response.status_code == 404:
                raise NotFound(f'{clean}: not found' + (f' ({reason})' if reason else ''))
            raise UpstreamError(f'{clean}: answered {response.status_code}' + (f' ({reason})' if reason else ''))
        return fetched

    async def read(self, fetched: Fetched, *, max_bytes: int) -> bytes:
        if fetched.body is not None:
            if len(fetched.body) > max_bytes:
                raise UpstreamError(f'{fetched.path}: too large to read')
            return fetched.body
        if fetched.sha256 is None:
            raise UpstreamError(f'{fetched.path}: no body')
        blobs: BlobStore = self.services.blobs
        path = blobs.blob_path(fetched.sha256)
        size = (await asyncio.to_thread(path.stat)).st_size
        if size > max_bytes:
            raise UpstreamError(f'{fetched.path}: too large to read')
        return await asyncio.to_thread(path.read_bytes)


async def _one(
    services: Services,
    request: Request,
    repo: RepoConfig,
    descriptor: FormatDescriptor,
    plugin: FormatPlugin,
    principal: Principal,
    op: PrefetchMode,
    item: str,
    emit: asyncio.Queue[dict[str, Any] | None],
) -> None:
    ctx = _Context(services, request, repo, plugin, principal, op, emit, item)
    error: str | None = None
    outcome = 'ok'
    try:
        async with _process_slots():
            if op == 'warm':
                problem = descriptor.warm_item(item) if descriptor.warm_item is not None else 'not supported'
                if problem is not None or not isinstance(plugin, WarmingPlugin):
                    outcome, error = 'invalid', problem or 'not supported'
                else:
                    await plugin.warm(ctx, item)
            else:
                mapped = descriptor.refresh_request(item) if descriptor.refresh_request is not None else None
                if mapped is None:
                    outcome = 'skipped'
                else:
                    await ctx.get(mapped.path, mapped.headers)
    except WaystationError as exc:
        outcome, error = 'failed', exc.detail
    except Exception as exc:  # a bug in a format must not end the whole call
        log.exception('prefetch item failed', extra={'repo': repo.name, 'item': item[:200]})
        outcome, error = 'failed', type(exc).__name__
    line: dict[str, Any] = {
        'event': 'item',
        'item': item,
        'outcome': outcome,
        'files': ctx.files,
        'fetched': ctx.fetched,
    }
    if error:
        line['error'] = error[:500]
    await emit.put(line)


async def _run(
    services: Services,
    request: Request,
    repo: RepoConfig,
    principal: Principal,
    call: PrefetchCall,
    emit: asyncio.Queue[dict[str, Any] | None],
) -> None:
    descriptor = services.formats.descriptor(repo.format)
    plugin = services.formats.plugin(repo.format)
    try:
        if descriptor is None or plugin is None:
            for item in call.items:
                await emit.put({'event': 'item', 'item': item, 'outcome': 'invalid', 'error': 'not supported'})
            return
        limit = asyncio.Semaphore(ITEM_CONCURRENCY)

        async def bounded(item: str) -> None:
            async with limit:
                await _one(services, request, repo, descriptor, plugin, principal, call.op, item, emit)

        async with asyncio.TaskGroup() as group:
            for item in dict.fromkeys(call.items):
                group.create_task(bounded(item))
    finally:
        await emit.put(None)


async def _lines(work: asyncio.Task[None], emit: asyncio.Queue[dict[str, Any] | None]) -> AsyncIterator[bytes]:
    try:
        while True:
            try:
                line = await asyncio.wait_for(emit.get(), PING_S)
            except TimeoutError:
                yield b'{"event":"ping"}\n'
                continue
            if line is None:
                break
            yield json.dumps(line, separators=(',', ':')).encode() + b'\n'
        await work
        yield b'{"event":"done"}\n'
    finally:
        if not work.done():  # the worker went away: stop (running fills carry on, ARCH §6.3)
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await work


@router.post(ROUTE)
@access.handler_checked
async def prefetch(request: Request, repo_id: str) -> Response:
    """Run prefetch work for the worker (see the module notes). 404 unless the system grant is valid."""
    services: Services = request.app.state.services
    repo = await services.catalog.by_id(repo_id)
    principal = access.system_grant(request, repo, services.system_grants) if repo is not None else None
    if repo is None or principal is None:
        raise NotFound('Not Found')
    request.state.log.update(repo=repo.name, format=repo.format, op='prefetch')
    try:
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            raise ValueError('body too large')
        call = PrefetchCall.model_validate_json(raw)
    except ValueError, ValidationError:
        return Response('{"error":"invalid prefetch call"}', status_code=400, media_type='application/json')
    emit: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    work = asyncio.create_task(_run(services, request, repo, principal, call, emit), name=f'prefetch-{repo.name}')
    return StreamingResponse(_lines(work, emit), media_type='application/x-ndjson')
