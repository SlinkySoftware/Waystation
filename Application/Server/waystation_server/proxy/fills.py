"""Cache fills that outlive their client (ARCHITECTURE §6.3, SPECIFICATION §4.1).

A streaming content fill (upstream → ``BlobWriter``, verified, committed, indexed) runs as a background task owned by
the engine, not in the request's task: when the client that triggered it goes away (an aborted ``docker pull``, a
client timeout on a multi-GB layer) the fill still finishes, its followers keep tailing it, and the next request is a
cache hit. Two pieces:

- ``FillRegistry``: the engine's in-flight fills. ``spawn`` starts one eagerly (its cleanup is always reached, even
  when it is cancelled at once); ``detach`` is asked when a fill's client left and refuses past ``max_detached``
  fills without a client (the caller then cancels the fill: it is aborted and a follower, if any, takes it over);
  ``aclose`` (shutdown) cancels every fill and waits for them to clean up.
- ``Feed``: the bytes a fill releases to the client that started it (everything but the held-back last chunk until
  the commit). In memory while the client keeps up; once more than ``limit`` bytes wait, the oldest chunks that are
  already in the upload file are dropped and the client reads them from the file instead (a slow client never slows
  the fill, and memory stays bounded). The fill never waits for its client.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import deque
from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import Any

from waystation_common.errors import UpstreamError
from waystation_common.storage import BlobStore

__all__ = ['FEED_BUFFER', 'MAX_DETACHED_FILLS', 'Feed', 'FillRegistry']

log = logging.getLogger('waystation.proxy.fills')

FEED_BUFFER = 8 * 1024 * 1024  # bytes a fill keeps in memory for its client before it reads from the file
FILE_READ = 1024 * 1024
MAX_DETACHED_FILLS = 64  # fills per process that may keep running after their client left
CLOSE_TIMEOUT_S = 10.0


class Feed:
    """Released bytes of one background fill for the client that started it (see the module docstring)."""

    def __init__(self, blobs: BlobStore, upload_id: str, *, limit: int = FEED_BUFFER) -> None:
        self.blobs = blobs
        self.upload_id = upload_id
        self.limit = limit
        self.released = 0  # bytes the client may have
        self.flushed = 0  # bytes in the upload file (readable through the filesystem)
        self.sha256: str | None = None  # set once committed (the upload file was renamed to this blob)
        self.error: Exception | None = None
        self.ended = False
        self.reader_gone = False
        self.kept = False  # the client left and the fill went on without it (the fill finishes its live transfer)
        self.on_detach: Callable[[], bool] | None = None  # asked when the client leaves early: keep the fill going?
        self._chunks: deque[tuple[int, bytes]] = deque()  # (offset, bytes) not yet taken by the client
        self._buffered = 0
        self._event = asyncio.Event()

    # ---- the fill's side
    def _fire(self) -> None:
        event, self._event = self._event, asyncio.Event()
        event.set()

    def wrote(self, flushed: int) -> None:
        """Bytes now in the upload file (nothing new for the client)."""
        self.flushed = flushed

    def release(self, data: bytes, flushed: int) -> None:
        """The client may have ``data`` (the next bytes); ``flushed``: bytes now in the upload file."""
        self.flushed = flushed
        offset = self.released
        self.released += len(data)
        if self.reader_gone or not data:
            return
        self._chunks.append((offset, data))
        self._buffered += len(data)
        while self._buffered > self.limit and self._chunks:
            first, chunk = self._chunks[0]
            if first + len(chunk) > flushed:
                break  # not in the file yet: keep it
            self._chunks.popleft()
            self._buffered -= len(chunk)
        self._fire()

    def committed(self, sha256: str) -> None:
        self.sha256 = sha256
        self._fire()

    def end(self) -> None:
        self.ended = True
        self._fire()

    def fail(self, exc: BaseException) -> None:
        if self.ended:
            return
        self.error = exc if isinstance(exc, Exception) else UpstreamError('the cache fill was cancelled')
        self.ended = True
        self._chunks.clear()
        self._buffered = 0
        self._fire()

    # ---- the client's side
    async def read(self) -> AsyncGenerator[bytes]:
        """The released bytes, in order; returns at the end of a successful fill, raises the fill's error."""
        pos = 0
        fd: int | None = None
        try:
            while True:
                event = self._event
                if self._chunks and self._chunks[0][0] <= pos:
                    offset, data = self._chunks.popleft()
                    self._buffered -= len(data)
                    if offset + len(data) <= pos:
                        continue
                    if offset < pos:
                        data = data[pos - offset :]
                    pos += len(data)
                    yield data
                    continue
                if self.error is not None:
                    raise self.error
                limit = self._chunks[0][0] if self._chunks else self.released
                if pos < limit:  # dropped from memory: it is in the file
                    if fd is None:
                        fd = await self._open()
                        if fd is None:
                            await event.wait()  # being renamed into place: retry once the commit is announced
                            continue
                    data = await asyncio.to_thread(os.pread, fd, min(FILE_READ, limit - pos), pos)
                    if not data:
                        raise UpstreamError('the upload file is shorter than recorded')
                    pos += len(data)
                    yield data
                    continue
                if self.ended:
                    return
                await event.wait()
        finally:
            self.reader_gone = True
            self._chunks.clear()
            self._buffered = 0
            if not self.ended and self.on_detach is not None:
                self.kept = self.on_detach()
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)

    async def _open(self) -> int | None:
        """A read-only descriptor on the fill's file (the upload, or the blob once committed: the same inode)."""
        sha256 = self.sha256
        path = self.blobs.blob_path(sha256) if sha256 else self.blobs.uploads_dir / self.upload_id
        try:
            return await asyncio.to_thread(os.open, path, os.O_RDONLY | os.O_CLOEXEC)
        except FileNotFoundError:
            if sha256 is None and not self.ended:
                return None
            raise UpstreamError('the upload file disappeared') from None


class FillRegistry:
    """The engine's background fills (bounded by ``max_detached`` fills without a client)."""

    def __init__(self, *, max_detached: int = MAX_DETACHED_FILLS) -> None:
        self.max_detached = max_detached
        self._tasks: set[asyncio.Task[None]] = set()
        self._detached: set[asyncio.Task[None]] = set()

    @property
    def running(self) -> int:
        return len(self._tasks)

    @property
    def detached(self) -> int:
        return len(self._detached)

    def spawn(self, coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        """Run ``coro`` as a fill owned by the registry, outside the caller's task and cancellation scope. Started
        eagerly, so it is inside its own ``try`` before anyone can cancel it."""
        task = asyncio.get_running_loop().create_task(coro, name=name, eager_start=True)
        if not task.done():
            self._tasks.add(task)
            task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        self._detached.discard(task)
        if not task.cancelled() and task.exception() is not None:  # fills handle their own errors
            log.error('cache fill crashed', exc_info=task.exception())

    def detach(self, task: asyncio.Task[None]) -> bool:
        """``task``'s client went away: keep it running (True), or refuse because too many fills already run without
        a client (False; the caller cancels it)."""
        if task.done() or task in self._detached:
            return True
        if len(self._detached) >= self.max_detached:
            return False
        self._detached.add(task)
        return True

    def cancel(self) -> list[asyncio.Task[None]]:
        """Cancel the fills running now (each cleans up in its own task); returns them."""
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        return pending

    async def aclose(self, grace_s: float = CLOSE_TIMEOUT_S) -> None:
        """Shutdown: cancel every fill and wait (bounded) until they have cleaned up."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_s
        while self._tasks:
            pending = self.cancel()
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.warning('cache fills still running at shutdown', extra={'fills': len(pending)})
                return
            await asyncio.wait(pending, timeout=remaining)
