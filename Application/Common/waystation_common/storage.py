"""Content-addressed blob store on a local or network filesystem (ARCHITECTURE §5). The only API to blob files.

Layout under ``root`` (``WAYSTATION_DATA_DIR``): ``blobs/sha256/<aa>/<bb>/<hex>`` (immutable, 0640, dirs 2750)
and ``uploads/<id>`` (in-progress writes, same filesystem so commit is an atomic rename).

Writing::

    store = BlobStore.from_settings(settings)
    async with await store.open_writer(expected_sha256=digest, expected_size=size) as writer:
        async for chunk in upstream:
            await writer.write(chunk)          # hashed incrementally; raises PayloadTooLarge past max_size
        ref = await store.commit(writer)       # verify, fsync, rename into place, fsync dir
    # leaving the block without commit (or on an exception) deletes the upload file

Guarantees:

- Nothing appears under ``blobs/`` until ``commit`` has verified size/digest and fsynced the file, and the rename
  is atomic, so a process killed mid-write leaves at most an orphan in ``uploads/`` (``sweep_uploads`` removes
  orphans older than a cutoff; GC runs it with 24 h).
- Identical content committed concurrently in one process is put in place once (``BlobRef.created`` is True for
  exactly one writer); across processes the race is benign: both renames carry identical bytes atomically.
- No file I/O on the event loop: buffered writes (``chunk_size``, default 1 MiB) are hashed and written in a worker
  thread; every other filesystem call also runs in ``asyncio.to_thread``.
- ``open_writer`` refuses to start when free space would drop below ``min_free_bytes`` (``InsufficientStorage``).

``path_for_xaccel(sha256)`` is the internal URI the web tier serves (``/_blobs/...``, ARCHITECTURE §4.5).
``delete`` is for GC only. Not done yet: the NFS-backed CI scenario (T-F07 full task).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self

from waystation_common.datadir import DIR_MODE, FILE_MODE
from waystation_common.errors import DigestMismatch, InsufficientStorage, PayloadTooLarge, ValidationFailed
from waystation_common.settings import Settings

XACCEL_PREFIX = '/_blobs/'
DEFAULT_CHUNK = 1024 * 1024
ORPHAN_AGE_S = 24 * 3600
_HEX = re.compile(r'^[0-9a-f]{64}$')


@dataclass(frozen=True)
class BlobRef:
    sha256: str
    size: int
    created: bool = False  # True when this commit put the file in place


@dataclass
class _DigestLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


def check_sha256(sha256: str) -> str:
    if not _HEX.fullmatch(sha256):
        raise ValidationFailed('not a lowercase hex sha256 digest')
    return sha256


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class BlobWriter:
    """A streaming write into ``uploads/``. Create with ``BlobStore.open_writer``; finish with ``BlobStore.commit``."""

    def __init__(
        self,
        store: BlobStore,
        upload_id: str,
        fd: int,
        *,
        max_size: int | None,
        expected_size: int | None,
        expected_sha256: str | None,
    ) -> None:
        self.store = store
        self.upload_id = upload_id
        self.path = store.uploads_dir / upload_id
        self.max_size = max_size
        self.expected_size = expected_size
        self.expected_sha256 = expected_sha256
        self.size = 0  # bytes accepted (buffered or written)
        limits = [n for n in (max_size, expected_size) if n is not None]
        self._limit = min(limits) if limits else None
        self._fd: int | None = fd
        self._hash = hashlib.sha256()
        self._buffer = bytearray()
        self._done = False

    @property
    def closed(self) -> bool:
        return self._done

    @property
    def flushed(self) -> int:
        """Bytes written to the upload file so far (what a reader of ``path`` can see; the rest is buffered)."""
        return self.size - len(self._buffer)

    def fileno(self) -> int:
        """The upload file's descriptor (e.g. to ``os.dup`` for an ``fdatasync`` from another task)."""
        return self._require_fd()

    async def write(self, data: bytes | bytearray | memoryview) -> None:
        if self._done:
            raise ValueError('writer is closed')
        if not data:
            return
        new_size = self.size + len(data)
        if self._limit is not None and new_size > self._limit:
            await self.abort()
            raise PayloadTooLarge(f'content exceeds {self._limit} bytes')
        self.size = new_size
        self._buffer += data
        if len(self._buffer) >= self.store.chunk_size:
            await self.flush()

    async def flush(self) -> None:
        """Hash and write buffered bytes (in a thread)."""
        if self._buffer:
            data, self._buffer = bytes(self._buffer), bytearray()
            await asyncio.to_thread(self._write_sync, data)

    async def sync(self) -> None:
        """Flush and ``fdatasync``: readers on other hosts may rely on the bytes so far (ARCHITECTURE §6.3)."""
        await self.flush()
        await asyncio.to_thread(os.fdatasync, self._require_fd())

    def _require_fd(self) -> int:
        if self._fd is None:
            raise ValueError('writer is closed')
        return self._fd

    def _write_sync(self, data: bytes) -> None:
        fd = self._require_fd()
        self._hash.update(data)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]

    def hexdigest(self) -> str:
        return self._hash.hexdigest()

    def _close_sync(self, *, unlink: bool) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if unlink:
            self.path.unlink(missing_ok=True)

    async def abort(self) -> None:
        """Discard the upload (idempotent)."""
        if self._done:
            return
        self._done = True
        self._buffer = bytearray()
        self.store._active.discard(self.upload_id)
        await asyncio.to_thread(self._close_sync, unlink=True)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.abort()  # no-op after a successful commit


class BlobStore:
    def __init__(
        self,
        root: Path,
        *,
        min_free_bytes: int = 0,
        max_size: int | None = None,
        chunk_size: int = DEFAULT_CHUNK,
    ) -> None:
        self.root = root
        self.blobs_dir = root / 'blobs'
        self.uploads_dir = root / 'uploads'
        self.min_free_bytes = min_free_bytes
        self.max_size = max_size
        self.chunk_size = chunk_size
        self._active: set[str] = set()
        self._locks: dict[str, _DigestLock] = {}
        self.on_refused: Callable[[], None] | None = None  # called when the free-space guard refuses a write

    @classmethod
    def from_settings(cls, settings: Settings) -> BlobStore:
        return cls(settings.data_dir, min_free_bytes=settings.min_free_bytes, max_size=settings.max_upload_bytes)

    # ---- addressing
    def blob_path(self, sha256: str) -> Path:
        check_sha256(sha256)
        return self.blobs_dir / 'sha256' / sha256[:2] / sha256[2:4] / sha256

    @staticmethod
    def path_for_xaccel(sha256: str) -> str:
        check_sha256(sha256)
        return f'{XACCEL_PREFIX}sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}'

    # ---- space
    def _free_bytes_sync(self) -> int:
        st = os.statvfs(self.root)
        return st.f_bavail * st.f_frsize

    async def free_bytes(self) -> int:
        return await asyncio.to_thread(self._free_bytes_sync)

    async def check_free_space(self, needed: int = 0) -> None:
        free = await self.free_bytes()
        if free - needed < self.min_free_bytes:
            if self.on_refused is not None:
                self.on_refused()
            raise InsufficientStorage('not enough free space in the blob store')

    # ---- writing
    def _open_sync(self, upload_id: str) -> int:
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.uploads_dir / upload_id, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, FILE_MODE)
        os.fchmod(fd, FILE_MODE)
        return fd

    async def open_writer(
        self,
        *,
        max_size: int | None = None,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> BlobWriter:
        if expected_sha256 is not None:
            check_sha256(expected_sha256)
        limit = max_size if max_size is not None else self.max_size
        if expected_size is not None and limit is not None and expected_size > limit:
            raise PayloadTooLarge(f'content exceeds {limit} bytes')
        await self.check_free_space(expected_size or 0)
        upload_id = uuid.uuid4().hex
        fd = await asyncio.to_thread(self._open_sync, upload_id)
        self._active.add(upload_id)
        return BlobWriter(
            self, upload_id, fd, max_size=limit, expected_size=expected_size, expected_sha256=expected_sha256
        )

    def _mkdirs_sync(self, directory: Path) -> list[Path]:
        """Create missing parents with DIR_MODE; returns the created ones (outermost first)."""
        created: list[Path] = []
        for parent in [*reversed(directory.relative_to(self.root).parents), directory.relative_to(self.root)]:
            path = self.root / parent
            if path == self.root:
                continue
            try:
                path.mkdir(mode=DIR_MODE)
            except FileExistsError:
                continue
            path.chmod(DIR_MODE)
            created.append(path)
        return created

    def _finish_sync(self, writer: BlobWriter) -> int:
        fd = writer._require_fd()
        os.fsync(fd)
        writer._close_sync(unlink=False)
        return writer.size

    def _place_sync(self, writer: BlobWriter, target: Path) -> bool:
        if target.exists():
            writer.path.unlink(missing_ok=True)
            return False
        created = self._mkdirs_sync(target.parent)
        writer.path.rename(target)
        _fsync_dir(target.parent)
        for directory in created:
            _fsync_dir(directory.parent)
        return True

    async def commit(self, writer: BlobWriter) -> BlobRef:
        """Verify, make durable and move the upload into place. The writer is closed afterwards."""
        if writer.closed:
            raise ValueError('writer is closed')
        try:
            await writer.flush()
            size = await asyncio.to_thread(self._finish_sync, writer)
            digest = writer.hexdigest()
            if writer.expected_size is not None and size != writer.expected_size:
                raise DigestMismatch(f'size {size} does not match the expected {writer.expected_size}')
            if writer.expected_sha256 is not None and digest != writer.expected_sha256:
                raise DigestMismatch('sha256 does not match the expected digest')
            target = self.blob_path(digest)
            entry = self._locks.setdefault(digest, _DigestLock())
            entry.users += 1
            try:
                async with entry.lock:
                    created = await asyncio.to_thread(self._place_sync, writer, target)
            finally:
                entry.users -= 1
                if not entry.users:
                    del self._locks[digest]
        except BaseException:
            await writer.abort()
            raise
        writer._done = True
        self._active.discard(writer.upload_id)
        return BlobRef(digest, size, created)

    # ---- reading and GC
    def _stat_sync(self, sha256: str) -> BlobRef | None:
        try:
            st = self.blob_path(sha256).stat()
        except FileNotFoundError:
            return None
        return BlobRef(sha256, st.st_size)

    async def stat(self, sha256: str) -> BlobRef | None:
        return await asyncio.to_thread(self._stat_sync, sha256)

    async def exists(self, sha256: str) -> bool:
        return await self.stat(sha256) is not None

    def _delete_sync(self, sha256: str) -> bool:
        try:
            self.blob_path(sha256).unlink()
        except FileNotFoundError:
            return False
        return True

    async def delete(self, sha256: str) -> bool:
        """Remove a blob (GC only, after mark-and-sweep). True when a file was removed."""
        return await asyncio.to_thread(self._delete_sync, sha256)

    def _sweep_sync(self, older_than_s: float, active: frozenset[str]) -> int:
        cutoff = time.time() - older_than_s
        removed = 0
        try:
            entries = list(os.scandir(self.uploads_dir))
        except FileNotFoundError:
            return 0
        for entry in entries:
            if entry.name in active or not entry.is_file(follow_symlinks=False):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime <= cutoff:
                    Path(entry.path).unlink()
                    removed += 1
            except FileNotFoundError:  # pragma: no cover - raced with its owner
                continue
        return removed

    async def sweep_uploads(self, older_than_s: float = ORPHAN_AGE_S) -> int:
        """Delete upload files untouched for ``older_than_s`` (never this process's open writers)."""
        return await asyncio.to_thread(self._sweep_sync, older_than_s, frozenset(self._active))
