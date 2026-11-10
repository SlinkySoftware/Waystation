"""Cache fills outlive their client (T-S07, ARCHITECTURE §6.3): ``proxy.fills`` and the engine's streaming fill.

The engine tests drive ``ProxyEngine._fill_stream`` without MongoDB or Valkey: a scripted upstream body, a real
``BlobStore`` in a temporary directory, a solo lease (plus a local follower tailing it), and fakes for the asset
record, events and auto-block. The end-to-end behaviour through the app and two replicas is
``test_fill_continue_integration``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from redis.asyncio import Redis
from ws_testing import make_repo, make_settings

from waystation_common.errors import UpstreamError
from waystation_common.settings import CoalesceTail
from waystation_common.storage import BlobStore
from waystation_server.db import assets as assets_db
from waystation_server.metrics import Metrics
from waystation_server.proxy.coalesce import Coalescer, Follower, Lease, Status
from waystation_server.proxy.engine import AssetSpec, ProxyEngine, UpstreamInfo, _Source
from waystation_server.proxy.fills import Feed, FillRegistry
from waystation_server.proxy.http import UpstreamClients
from waystation_server.proxy.records import StoredAsset

MiB = 1024 * 1024
CHUNK = 256 * 1024
DATA = random.Random(5).randbytes(3 * MiB)
SHA = hashlib.sha256(DATA).hexdigest()


async def eventually(check: Any, within: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f'condition not reached within {within} s')
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------- Feed
async def collect(feed: Feed) -> bytes:
    out = bytearray()
    async with contextlib.aclosing(feed.read()) as chunks:
        async for chunk in chunks:
            out += chunk
    return bytes(out)


def store(tmp_path: Path) -> BlobStore:
    blobs = BlobStore(tmp_path)
    blobs.uploads_dir.mkdir(parents=True)
    return blobs


async def test_feed_hands_released_bytes_over_in_order(tmp_path: Path) -> None:
    feed = Feed(store(tmp_path), 'a' * 32)
    reader = asyncio.create_task(collect(feed))
    feed.wrote(0)  # the first chunk is held back
    feed.release(b'one', 3)
    feed.release(b'', 3)
    await asyncio.sleep(0)
    feed.release(b'two', 6)
    feed.end()
    assert await reader == b'onetwo'
    assert not feed.kept


async def test_feed_reads_what_a_slow_client_missed_from_the_file(tmp_path: Path) -> None:
    blobs = store(tmp_path)
    upload = 'b' * 32
    feed = Feed(blobs, upload, limit=CHUNK)
    path = blobs.uploads_dir / upload
    pieces = [DATA[i : i + CHUNK] for i in range(0, len(DATA), CHUNK)]
    with path.open('wb') as file:
        for piece in pieces[:-1]:
            file.write(piece)
            file.flush()
            feed.release(piece, file.tell())  # nobody reads yet: older chunks are dropped (they are in the file)
        file.write(pieces[-1])
    assert len(feed._chunks) == 1  # bounded memory
    reader = asyncio.create_task(collect(feed))
    await asyncio.sleep(0.05)
    committed = blobs.blob_path(SHA)
    committed.parent.mkdir(parents=True)
    path.rename(committed)  # the commit renames the upload into place: the reader follows
    feed.committed(SHA)
    feed.release(pieces[-1], len(DATA))
    feed.end()
    assert await reader == DATA


async def test_feed_reader_opens_the_blob_after_the_commit(tmp_path: Path) -> None:
    blobs = store(tmp_path)
    feed = Feed(blobs, 'c' * 32, limit=1)
    target = blobs.blob_path(SHA)
    target.parent.mkdir(parents=True)
    target.write_bytes(DATA)
    feed.release(DATA[:CHUNK], len(DATA))
    feed.release(DATA[CHUNK:], len(DATA))  # the first chunk was dropped (in the file)
    feed.committed(SHA)
    feed.end()
    assert await collect(feed) == DATA


async def test_feed_waits_while_the_upload_is_being_renamed(tmp_path: Path) -> None:
    blobs = store(tmp_path)
    feed = Feed(blobs, 'd' * 32, limit=1)
    feed.release(b'x' * 10, 10)
    feed.release(b'y' * 10, 20)  # the first chunk is dropped, but no file exists (being renamed)
    reader = asyncio.create_task(collect(feed))
    await asyncio.sleep(0.05)
    assert not reader.done()
    target = blobs.blob_path(SHA)
    target.parent.mkdir(parents=True)
    target.write_bytes(b'x' * 10 + b'y' * 10)
    feed.committed(SHA)
    feed.end()
    assert await reader == b'x' * 10 + b'y' * 10


async def test_feed_errors(tmp_path: Path) -> None:
    blobs = store(tmp_path)
    feed = Feed(blobs, 'e' * 32)
    feed.release(b'a', 1)
    feed.fail(ValueError('boom'))
    feed.fail(ValueError('again'))  # once
    with pytest.raises(ValueError, match='boom'):
        await collect(feed)

    cancelled = Feed(blobs, 'f' * 32)
    cancelled.fail(asyncio.CancelledError())
    with pytest.raises(UpstreamError, match='cancelled'):
        await collect(cancelled)

    short = Feed(blobs, '1' * 32, limit=1)
    (blobs.uploads_dir / short.upload_id).write_bytes(b'')
    short.release(b'a', 1)
    short.release(b'b', 2)
    with pytest.raises(UpstreamError, match='shorter'):
        await collect(short)

    gone = Feed(blobs, '2' * 32, limit=1)
    gone.release(b'a', 1)
    gone.release(b'b', 2)
    gone.end()
    with pytest.raises(UpstreamError, match='disappeared'):
        await collect(gone)


async def test_feed_client_leaving_asks_to_keep_the_fill(tmp_path: Path) -> None:
    feed = Feed(store(tmp_path), '3' * 32)
    asked: list[bool] = []

    def detach() -> bool:
        asked.append(True)
        return True

    feed.on_detach = detach
    feed.release(b'a', 1)
    reader = asyncio.create_task(collect(feed))
    await asyncio.sleep(0.01)
    reader.cancel()  # the client goes away mid-response
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert asked == [True]
    assert feed.kept
    feed.release(b'b' * MiB, 2)  # nothing is buffered for a client that left
    assert feed._buffered == 0
    feed.end()


# ---------------------------------------------------------------------------- FillRegistry
async def test_registry_runs_fills_outside_the_caller_and_bounds_detached_ones() -> None:
    registry = FillRegistry(max_detached=1)
    cleaned: list[str] = []
    gate = asyncio.Event()

    async def fill(name: str) -> None:
        try:
            await gate.wait()
        finally:
            cleaned.append(name)

    async def instant() -> None:
        return None

    assert registry.spawn(instant(), name='instant').done()  # finished while starting: never registered
    a = registry.spawn(fill('a'), name='a')
    b = registry.spawn(fill('b'), name='b')
    assert registry.running == 2
    assert registry.detach(a) and registry.detach(a)
    assert not registry.detach(b)  # one detached fill already
    assert registry.detached == 1
    gate.set()
    await asyncio.gather(a, b)
    await asyncio.sleep(0)
    assert registry.running == 0 and registry.detached == 0
    assert registry.detach(a)  # finished: nothing to keep
    assert sorted(cleaned) == ['a', 'b']


async def test_registry_close_cancels_and_waits_even_for_fills_that_never_ran(caplog: Any) -> None:
    registry = FillRegistry()
    cleaned: list[str] = []

    async def fill() -> None:
        try:
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0.01)  # cleanup that awaits (lease outcome, upload removal)
            cleaned.append('x')

    async def crash() -> None:
        await asyncio.sleep(0)
        raise RuntimeError('bug')

    registry.spawn(fill(), name='x')  # eager: inside its try before anyone can cancel it
    registry.spawn(crash(), name='crash')
    await registry.aclose()
    assert cleaned == ['x']
    assert registry.running == 0

    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await release.wait()

    task = registry.spawn(stubborn(), name='stubborn')
    await registry.aclose(grace_s=0.05)  # bounded: gives up after the timeout
    assert registry.running == 1
    release.set()
    await task


# ---------------------------------------------------------------------------- the engine
class Recorder:
    """Any emitter/breaker call is recorded (never awaited on the tee path)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record

    def named(self, name: str) -> list[dict[str, Any]]:
        return [kwargs | {'args': args} for n, args, kwargs in self.calls if n == name]


class Breaker:
    async def success(self, repo: Any) -> None:
        return None

    async def failure(self, repo: Any, reason: str) -> None:
        return None


class Upstream:
    """A scripted upstream body: sends ``DATA`` in 256 KiB chunks, stops at ``pause_at`` until ``gate`` is set."""

    def __init__(self, *, pause_at: int = MiB) -> None:
        self.gate = asyncio.Event()
        self.pause_at = pause_at
        self.closed = False

    async def body(self) -> AsyncIterator[bytes]:
        try:
            for i in range(0, len(DATA), CHUNK):
                if i >= self.pause_at:
                    await self.gate.wait()
                yield DATA[i : i + CHUNK]
        finally:
            self.closed = True

    def response(self) -> tuple[httpx.Response, UpstreamInfo]:
        headers = {'content-type': 'application/x-big', 'content-length': str(len(DATA))}
        response = httpx.Response(200, headers=headers, content=self.body())
        return response, UpstreamInfo(url='https://upstream.example.com/big.bin', status=200, headers=headers,
                                      latency_ms=1.0)  # fmt: skip


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> list[StoredAsset]:
    records: list[StoredAsset] = []

    async def save(db: Any, asset: StoredAsset) -> StoredAsset:
        records.append(asset)
        return asset

    monkeypatch.setattr(assets_db, 'save', save)
    return records


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[ProxyEngine]:
    settings = make_settings(tmp_path)
    valkey = Redis.from_url('redis://127.0.0.1:1/0')  # never used: solo leases and fakes
    clients = UpstreamClients(settings)
    blobs = BlobStore(settings.data_dir)
    proxy = ProxyEngine(
        settings=settings,
        db=cast(Any, None),
        valkey=valkey,
        blobs=blobs,
        emitter=cast(Any, Recorder()),
        clients=clients,
        metrics=Metrics(),
        keyring=None,
        coalescer=Coalescer(valkey, data_dir=settings.data_dir, mode=CoalesceTail.LOCAL),  # forbidden-strings: allow
        breaker=cast(Any, Breaker()),
    )
    try:
        yield proxy
    finally:
        await proxy.aclose()
        await clients.aclose()
        await valkey.aclose()


def request() -> Any:
    return SimpleNamespace(
        repo=make_repo('files'),
        is_head=False,
        method='GET',
        principal=SimpleNamespace(display='anonymous'),
        client_ip='192.0.2.10',
        request=SimpleNamespace(headers={}),
        log=lambda **fields: None,
    )


async def start(engine: ProxyEngine, upstream: Upstream) -> tuple[Lease, asyncio.Task[bytes], list[int]]:
    """The owner's request: a streaming response whose client reads it in a task (``received``: bytes so far)."""
    lease = engine.coalescer.solo('id-files', 'big.bin')
    response, info = upstream.response()
    served = await engine._fill_stream(request(), AssetSpec(path='big.bin'), response, info, lease)
    received = [0]

    async def client() -> bytes:
        out = bytearray()
        async for chunk in cast(Any, served).body_iterator:
            out += chunk
            received[0] = len(out)
        return bytes(out)

    return lease, asyncio.create_task(client()), received


async def gone(client: asyncio.Task[bytes]) -> None:
    client.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await client


def emitter(engine: ProxyEngine) -> Recorder:
    return cast(Recorder, engine.emitter)


async def test_a_client_that_stays_gets_the_file(engine: ProxyEngine, saved: list[StoredAsset]) -> None:
    upstream = Upstream()
    upstream.gate.set()
    lease, client, _ = await start(engine, upstream)
    assert await client == DATA
    await eventually(lambda: engine.fills.running == 0)
    assert lease.status is Status.DONE
    assert [a.blob_sha256 for a in saved] == [SHA]
    assert [f['status'] for f in emitter(engine).named('transfer_finished')] == [200]


async def test_the_fill_commits_after_the_client_disconnects(engine: ProxyEngine, saved: list[StoredAsset]) -> None:
    upstream = Upstream()
    lease, client, received = await start(engine, upstream)
    await eventually(lambda: received[0] >= MiB - CHUNK)
    follower = Follower(engine.coalescer, lease.key, lease.state(), lease)  # tails the owner's upload file
    tail = engine._read_fill(follower, _Source(), 0, None, len(DATA))

    async def follow() -> bytes:
        out = bytearray()
        async with contextlib.aclosing(tail) as chunks:
            async for chunk in chunks:
                out += chunk
        return bytes(out)

    followed = asyncio.create_task(follow())
    await gone(client)  # the client goes away mid-download: the fill must not
    assert engine.fills.running == 1 and engine.fills.detached == 1
    assert lease.status is Status.STREAMING and not upstream.closed
    upstream.gate.set()
    assert await asyncio.wait_for(followed, 5) == DATA  # the follower succeeds from the same fetch
    await eventually(lambda: engine.fills.running == 0)
    assert lease.status is Status.DONE and lease.sha256 == SHA  # committed, then the lease was released
    assert lease._closed and lease.key not in engine.coalescer._leases
    assert [a.blob_sha256 for a in saved] == [SHA]
    assert engine.blobs.blob_path(SHA).read_bytes() == DATA
    assert list(engine.blobs.uploads_dir.iterdir()) == []
    assert upstream.closed
    finished = emitter(engine).named('transfer_finished')
    assert [(f['status'], f['bytes_done']) for f in finished] == [(200, len(DATA))]  # the fill ended the transfer
    await follower.leave()


async def test_shutdown_cancels_a_fill_without_a_client(engine: ProxyEngine, saved: list[StoredAsset]) -> None:
    upstream = Upstream()
    lease, client, received = await start(engine, upstream)
    await eventually(lambda: received[0] >= MiB - CHUNK)
    await gone(client)
    assert engine.fills.running == 1
    await asyncio.wait_for(engine.aclose(), 5)
    assert engine.fills.running == 0
    assert lease.status is Status.ABORTED  # followers elsewhere take it over
    assert saved == []
    assert list(engine.blobs.uploads_dir.iterdir()) == []
    assert upstream.closed
    finished = emitter(engine).named('transfer_finished')
    assert [(f['status'], f['error']) for f in finished] == [(502, 'cache fill cancelled')]


async def test_shutdown_aborts_the_response_of_a_client_still_there(
    engine: ProxyEngine, saved: list[StoredAsset]
) -> None:
    upstream = Upstream()
    lease, client, received = await start(engine, upstream)
    await eventually(lambda: received[0] >= MiB - CHUNK)
    await engine.aclose()
    with pytest.raises(Exception, match='cancelled'):
        await client
    assert lease.status is Status.ABORTED
    assert [f['status'] for f in emitter(engine).named('transfer_finished')] == [502]


async def test_a_dead_upstream_cannot_hold_a_fill_forever(
    engine: ProxyEngine, saved: list[StoredAsset], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, '_stall_s', lambda: 0.2)
    upstream = Upstream()  # never sends past 1 MiB
    lease, client, received = await start(engine, upstream)
    await eventually(lambda: received[0] >= MiB - CHUNK)
    await gone(client)
    await eventually(lambda: engine.fills.running == 0)
    assert lease.status is Status.FAILED and lease.error_code == 'timeout'
    assert saved == []
    assert list(engine.blobs.uploads_dir.iterdir()) == []


async def test_past_the_bound_a_fill_without_a_client_is_aborted(engine: ProxyEngine, saved: list[StoredAsset]) -> None:
    engine.fills.max_detached = 0
    upstream = Upstream()
    lease, client, received = await start(engine, upstream)
    await eventually(lambda: received[0] >= MiB - CHUNK)
    await gone(client)
    await eventually(lambda: engine.fills.running == 0)
    assert lease.status is Status.ABORTED  # as before T-S07: a follower would take it over
    assert saved == []
    assert list(engine.blobs.uploads_dir.iterdir()) == []


async def test_a_slow_follower_finishes_after_the_fill_record_expired(
    engine: ProxyEngine, saved: list[StoredAsset]
) -> None:
    """Fills no longer wait for their client, so they can end long before a slow follower on another replica has read
    the file; the fill record lingers only ``LINGER_MS``. A commit, once seen, is final."""
    upstream = Upstream()
    upstream.gate.set()
    lease, client, _ = await start(engine, upstream)
    assert await client == DATA
    await eventually(lambda: engine.fills.running == 0)
    follower = Follower(engine.coalescer, lease.key, lease.state(), None)  # an owner elsewhere: read via the poller
    poller = follower._poller
    assert poller is not None
    out = bytearray()
    async with contextlib.aclosing(engine._read_fill(follower, _Source(), 0, None, len(DATA))) as chunks:
        async for chunk in chunks:
            out += chunk
            poller.state = None  # the record expired while this follower was still reading
    assert bytes(out) == DATA
    await follower.leave()
