"""Request coalescing (ARCHITECTURE §6.3, SPECIFICATION §4.1): one upstream fetch per asset across replicas.

The engine calls ``await coalescer.claim(repo_id, asset_key)`` before it contacts the upstream for an asset that is
missing or stale. Exactly one caller cluster-wide gets a ``Lease`` (it owns the fetch); everyone else gets a
``Follower`` of that lease until the owner reports an outcome.

Coordination lives in one Valkey hash per asset, ``fill:<repo_id>:<sha1(asset key)>`` (DATA_MODEL §2), created by a
Lua ``claim`` script (``HSET`` + ``PEXPIRE`` only when no active fill exists, otherwise ``HINCRBY followers``), with:

- ``owner`` (random token), ``status`` (``pending`` → ``streaming`` → ``done`` | ``failed`` | ``aborted``),
  ``host`` (the owner's host id: the kernel boot id, shared by every container on one machine);
- while streaming: ``upload`` (the ``uploads/<id>`` file), ``written`` (bytes in that file), ``synced`` (bytes made
  durable with ``fdatasync``), ``total``, ``content_type``, ``headers`` (JSON, the stored upstream headers);
- at the end: ``sha256``/``size`` (committed), ``outcome`` (the owner's cache state when nothing was filled, e.g.
  ``REVALIDATED``), ``error``/``error_code`` (failed), ``followers`` (attached followers, shown on the owner's live
  transfer).

The owner's ``Lease`` runs a small publisher task: it renews the lease (``lease_s``, renewed every third) and
publishes progress (every 20 ms while followers are attached, else every 250 ms), ``fdatasync``-ing first while
followers are attached so tailers on other hosts can rely on ``synced``. Nothing on the tee path waits on Valkey:
``advance`` is O(1) in memory. Terminal states are kept for ``LINGER_MS`` so late pollers see them. A fill that
makes no progress for ``stall_s`` stops being renewed (it expires and is taken over).

Followers read the state from the local ``Lease`` when the owner is in the same process (no Valkey round trip),
else from one shared poller per key and process (``HGETALL`` every ``poll_s``). Whether a follower may tail the
upload file is ``tail_ok``: ``WAYSTATION_COALESCE_TAIL=local`` always, ``wait`` never, ``auto`` only on the owner's
host when the data dir is not on a network filesystem (``/proc/mounts``); tailers on another host read only up to
``synced``. Valkey being unavailable degrades to no coalescing (every request owns its fetch), never to an error.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from redis.asyncio import Redis

from waystation_common.errors import DigestMismatch, NotFound, WaystationError
from waystation_common.settings import CoalesceTail

__all__ = ['Coalescer', 'FillState', 'Follower', 'Lease', 'Status', 'data_dir_is_network_fs', 'host_id']

log = logging.getLogger('waystation.proxy.coalesce')

PREFIX = 'fill:'
LINGER_MS = 10_000  # terminal states stay readable this long
PUBLISH_FOLLOWED_S = 0.02  # progress publish interval while followers are attached
PUBLISH_IDLE_S = 0.25  # ... and while nobody follows (a new follower sees progress at most this old)
SYNC_INTERVAL_S = 0.25  # at most this often an fdatasync before publishing ``synced``
POLL_S = 0.02
LOG_EVERY_S = 60.0

NETWORK_FS = frozenset(
    {
        'nfs',
        'nfs4',
        'cifs',
        'smb3',
        'smbfs',
        '9p',
        'afs',
        'ncpfs',
        'glusterfs',
        'lustre',
        'gpfs',
        'beegfs',
        'ceph',
    }
)


class Status(StrEnum):
    PENDING = 'pending'  # the owner is asking the upstream
    STREAMING = 'streaming'  # the owner is writing ``uploads/<upload>``
    DONE = 'done'  # committed (``sha256``) or finished without a fill (``outcome``): look in the cache
    FAILED = 'failed'  # the fetch failed: followers answer the same error, nothing was cached
    ABORTED = 'aborted'  # the owner gave up (its client left, shutdown, internal error): a follower takes over


ACTIVE = frozenset({Status.PENDING, Status.STREAMING})


@dataclass(frozen=True)
class FillState:
    owner: str
    status: Status
    host: str = ''
    upload: str | None = None
    written: int = 0
    synced: int = 0
    total: int | None = None
    content_type: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    sha256: str | None = None
    size: int | None = None
    outcome: str | None = None
    error: str | None = None
    error_code: str | None = None
    followers: int = field(default=0, compare=False)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @classmethod
    def parse(cls, raw: Mapping[str, str]) -> FillState | None:
        try:
            headers = json.loads(raw['headers']) if raw.get('headers') else {}
            return cls(
                owner=raw['owner'],
                status=Status(raw['status']),
                host=raw.get('host', ''),
                upload=raw.get('upload') or None,
                written=int(raw.get('written') or 0),
                synced=int(raw.get('synced') or 0),
                total=int(raw['total']) if raw.get('total') else None,
                content_type=raw.get('content_type') or None,
                headers={str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {},
                sha256=raw.get('sha256') or None,
                size=int(raw['size']) if raw.get('size') else None,
                outcome=raw.get('outcome') or None,
                error=raw.get('error') or None,
                error_code=raw.get('error_code') or None,
                followers=max(0, int(raw.get('followers') or 0)),
            )
        except KeyError, ValueError, TypeError:
            return None


# KEYS[1] fill key; ARGV token, lease ms, host. 1 = claimed, 2 = claimed over a finished fill, else the active
# fill's fields (and this caller is counted as a follower).
_CLAIM = """
local status = redis.call('HGET', KEYS[1], 'status')
if status == 'pending' or status == 'streaming' then
  redis.call('HINCRBY', KEYS[1], 'followers', 1)
  return redis.call('HGETALL', KEYS[1])
end
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[1], 'owner', ARGV[1], 'status', 'pending', 'host', ARGV[3], 'written', '0', 'synced', '0',
  'followers', '0')
redis.call('PEXPIRE', KEYS[1], ARGV[2])
if status then return 2 end
return 1
"""

# KEYS[1]; ARGV token, ttl ms, field/value pairs. The owner's renew/progress/finish: -1 when no longer the owner.
_UPDATE = """
if redis.call('HGET', KEYS[1], 'owner') ~= ARGV[1] then return -1 end
local status = redis.call('HGET', KEYS[1], 'status')
if status ~= 'pending' and status ~= 'streaming' then return -1 end
if #ARGV > 2 then redis.call('HSET', KEYS[1], unpack(ARGV, 3)) end
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return tonumber(redis.call('HGET', KEYS[1], 'followers') or '0')
"""

# KEYS[1]; ARGV owner token. A follower detaches.
_LEAVE = """
if redis.call('HGET', KEYS[1], 'owner') ~= ARGV[1] then return -1 end
local n = redis.call('HINCRBY', KEYS[1], 'followers', -1)
if n < 0 then redis.call('HSET', KEYS[1], 'followers', '0') end
return n
"""


def host_id() -> str:
    """The kernel boot id (identical in every container on one machine, which share a page cache), else the
    hostname. Blocking: call in a thread."""
    try:
        value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        value = ''
    return value or socket.gethostname()


def _unescape(field_value: str) -> str:
    return field_value.encode('latin-1').decode('unicode_escape') if '\\' in field_value else field_value


def data_dir_is_network_fs(data_dir: Path, mounts: str | None = None) -> bool:
    """True when ``data_dir`` sits on a network filesystem (``/proc/mounts``; the longest matching mount point
    wins). Blocking: call in a thread."""
    if mounts is None:
        try:
            mounts = Path('/proc/mounts').read_text()
        except OSError:
            return False
    try:
        target = os.path.realpath(data_dir)
    except OSError:
        target = str(data_dir)
    best, best_type = '', ''
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        point, fstype = _unescape(parts[1]), parts[2]
        inside = target == point or target.startswith(point.rstrip('/') + '/')
        if inside and len(point) >= len(best):
            best, best_type = point, fstype
    base = best_type.split('.', 1)[1] if best_type.startswith('fuse.') else best_type
    return base in NETWORK_FS or best_type.startswith('fuse.sshfs')


def failure_code(exc: BaseException) -> tuple[Status, str | None, str]:
    """How followers see an owner's exception: (status, error code, detail)."""
    if isinstance(exc, NotFound):
        return Status.FAILED, 'not_found', exc.detail
    if isinstance(exc, DigestMismatch):
        return Status.FAILED, 'mismatch', exc.detail
    if isinstance(exc, WaystationError):
        code = 'timeout' if exc.status == 504 else 'upstream'
        return Status.FAILED, code, exc.detail
    return Status.ABORTED, None, 'the owner of the fetch gave up'


class _Signal:
    """A broadcast "something changed": waiters capture ``event`` before reading state, then wait on it."""

    def __init__(self) -> None:
        self.event = asyncio.Event()

    def fire(self) -> None:
        event, self.event = self.event, asyncio.Event()
        event.set()


class Lease:
    """This request owns the fetch. ``advance`` from the tee; ``finish``/``fail`` exactly once (later calls are
    no-ops). ``solo`` leases (Valkey unavailable) coordinate nothing."""

    owner = True

    def __init__(self, coalescer: Coalescer, key: str, token: str, *, solo: bool = False, replaced: bool = False):
        self.coalescer = coalescer
        self.key = key
        self.token = token
        self.solo = solo
        self.replaced = replaced  # claimed over a recently finished fill: look in the cache again first
        self.status = Status.PENDING
        self.upload: str | None = None
        self.written = 0
        self.synced = 0
        self.total: int | None = None
        self.content_type: str | None = None
        self.headers: dict[str, str] = {}
        self.sha256: str | None = None
        self.size: int | None = None
        self.outcome: str | None = None
        self.error: str | None = None
        self.error_code: str | None = None
        self.followers = 0
        self.handed_off = False  # a streaming response finishes the lease
        self.lost = False
        self.on_followers: Callable[[int], None] | None = None
        self.signal = _Signal()
        self._wake = asyncio.Event()
        self._sync_fd: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_change = time.monotonic()
        self._closed = False

    @property
    def terminal(self) -> bool:
        return self.status not in ACTIVE

    def state(self) -> FillState:
        return FillState(
            owner=self.token,
            status=self.status,
            host=self.coalescer.host,
            upload=self.upload,
            written=self.written,
            synced=self.written,  # same process: everything written is visible
            total=self.total,
            content_type=self.content_type,
            headers=dict(self.headers),
            sha256=self.sha256,
            size=self.size,
            outcome=self.outcome,
            error=self.error,
            error_code=self.error_code,
            followers=self.followers,
        )

    def start(self) -> None:
        if not self.solo:
            self._task = asyncio.create_task(self._publish(), name=f'coalesce-{self.key}')

    def _changed(self) -> None:
        self._last_change = time.monotonic()
        self.signal.fire()
        self._wake.set()

    def streaming(
        self, *, upload: str, total: int | None, content_type: str, headers: Mapping[str, str], fd: int | None
    ) -> None:
        """The owner opened ``uploads/<upload>`` and starts writing (``fd``: its descriptor, dup'ed for syncs)."""
        if self.terminal:
            return
        self.status = Status.STREAMING
        self.upload, self.total, self.content_type, self.headers = upload, total, content_type, dict(headers)
        if fd is not None and not self.solo:
            with contextlib.suppress(OSError):
                self._sync_fd = os.dup(fd)
        self._changed()

    def advance(self, written: int) -> None:
        """Bytes now in the upload file (O(1); published by the background task)."""
        if written != self.written:
            self.written = written
            self._changed()

    async def finish(self, *, sha256: str | None = None, size: int | None = None, outcome: str | None = None) -> None:
        if self.terminal:
            return
        self.status = Status.DONE
        self.sha256, self.size, self.outcome = sha256, size, outcome
        if size is not None:
            self.written = size
        await self._close()

    async def fail(self, exc: BaseException) -> None:
        if self.terminal:
            return
        self.status, self.error_code, self.error = failure_code(exc)
        await self._close()

    def _fields(self) -> list[str]:
        values: dict[str, Any] = {
            'status': self.status.value,
            'written': self.written,
            'synced': self.synced,
        }
        if self.upload:
            values.update(
                upload=self.upload,
                total='' if self.total is None else self.total,
                content_type=self.content_type or '',
                headers=json.dumps(self.headers),
            )
        if self.status is Status.DONE:
            values.update(sha256=self.sha256 or '', size='' if self.size is None else self.size)
            values['outcome'] = self.outcome or ''
        elif self.status in (Status.FAILED, Status.ABORTED):
            values.update(error=(self.error or '')[:300], error_code=self.error_code or '')
        out: list[str] = []
        for name, value in values.items():
            out += [name, str(value)]
        return out

    async def _send(self, ttl_ms: int) -> int:
        result = await self.coalescer._update(keys=[self.key], args=[self.token, ttl_ms, *self._fields()])
        return int(result)

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.coalescer._leases.pop(self.key, None)
        task, self._task = self._task, None
        self.signal.fire()
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        if self._sync_fd is not None:
            fd, self._sync_fd = self._sync_fd, None
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.close, fd)  # may flush on a network filesystem
        if self.solo or self.lost:
            return
        try:
            await self._send(LINGER_MS)
        except Exception as exc:  # Valkey down: followers notice through lease expiry
            self.coalescer._warn('could not publish the fill outcome', exc)

    async def _publish(self) -> None:
        c = self.coalescer
        last_sync = 0.0
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), c.renew_s)
                self._wake.clear()
                if time.monotonic() - self._last_change > c.stall_s():
                    log.warning('fill made no progress; letting its lease expire', extra={'key': self.key})
                    return
                if (
                    self.followers
                    and self._sync_fd is not None
                    and self.written > self.synced
                    and time.monotonic() - last_sync >= SYNC_INTERVAL_S
                ):
                    target = self.written
                    try:
                        await asyncio.to_thread(os.fdatasync, self._sync_fd)
                        self.synced = target
                    except OSError, TypeError:  # closed under us at commit: the next update is terminal
                        pass
                    last_sync = time.monotonic()
                try:
                    followers = await self._send(c.lease_ms)
                except Exception as exc:
                    c._warn('could not renew a fill lease', exc)
                    await asyncio.sleep(PUBLISH_IDLE_S)
                    continue
                if followers < 0:
                    self.lost = True
                    log.warning('fill lease lost to another owner', extra={'key': self.key})
                    return
                if followers != self.followers:
                    self.followers = followers
                    if self.on_followers is not None:
                        self.on_followers(followers)
                await asyncio.sleep(PUBLISH_FOLLOWED_S if self.followers else PUBLISH_IDLE_S)
        except asyncio.CancelledError:
            return


class _Poller:
    """One per key and process: reads the fill hash every ``poll_s`` for every local follower of a remote owner."""

    def __init__(self, coalescer: Coalescer, key: str, state: FillState | None) -> None:
        self.coalescer = coalescer
        self.key = key
        self.state = state
        self.signal = _Signal()
        self.users = 0
        self.task = asyncio.create_task(self._run(), name=f'coalesce-poll-{key}')

    async def _run(self) -> None:
        c = self.coalescer
        while True:
            await asyncio.sleep(c.poll_s)
            try:
                raw = cast(dict[str, str], await c.valkey.hgetall(self.key))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                c._warn('could not read a fill', exc)
                continue
            new = FillState.parse(raw) if raw else None
            if new != self.state:
                self.state = new
                self.signal.fire()
            elif new is not None and self.state is not None and new.followers != self.state.followers:
                self.state = new


class Follower:
    """Another request owns the fetch. ``state()`` is the latest known state (None: the fill hash is gone); ``wait``
    returns when it changes (or after ``timeout``). ``mine(state)``: still the fill this follower joined."""

    owner = False

    def __init__(self, coalescer: Coalescer, key: str, state: FillState, local: Lease | None) -> None:
        self.coalescer = coalescer
        self.key = key
        self.token = state.owner
        self.joined = state
        self._local = local
        self._poller: _Poller | None = None
        self._left = False
        if local is None:
            self._poller = coalescer._poller(key, state)
        self._event = self._source_event()
        self.last_change = time.monotonic()
        self._last_seen: FillState | None = state

    def _source_event(self) -> asyncio.Event:
        if self._local is not None:
            return self._local.signal.event
        assert self._poller is not None
        return self._poller.signal.event

    def state(self) -> FillState | None:
        self._event = self._source_event()
        if self._local is not None:
            current: FillState | None = self._local.state()
        else:
            assert self._poller is not None
            current = self._poller.state
        if current != self._last_seen:
            self._last_seen = current
            self.last_change = time.monotonic()
        return current

    def mine(self, state: FillState | None) -> bool:
        return state is not None and state.owner == self.token

    async def wait(self, seconds: float) -> None:
        """Until the state changes (since the last ``state()``) or ``seconds`` passed."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._event.wait(), seconds)

    def stalled(self) -> bool:
        return time.monotonic() - self.last_change > self.coalescer.stall_s()

    async def leave(self) -> None:
        if self._left:
            return
        self._left = True
        if self._poller is not None:
            self.coalescer._release_poller(self._poller)
            self._poller = None
        try:
            await self.coalescer._leave(keys=[self.key], args=[self.token])
        except Exception as exc:
            self.coalescer._warn('could not detach a follower', exc)


Flight = Lease | Follower


class Coalescer:
    def __init__(
        self,
        valkey: Redis,
        *,
        data_dir: Path,
        mode: CoalesceTail = CoalesceTail.AUTO,
        lease_s: float = 15.0,
        stall_s: Callable[[], float] = lambda: 120.0,
        poll_s: float = POLL_S,
    ) -> None:
        self.valkey = valkey
        self.data_dir = data_dir
        self.mode = mode
        self.lease_ms = max(1000, int(lease_s * 1000))
        self.renew_s = lease_s / 3
        self.stall_s = stall_s
        self.poll_s = poll_s
        self.host = ''
        self.network_fs = False
        self._ready = False
        self._leases: dict[str, Lease] = {}
        self._pollers: dict[str, _Poller] = {}
        self._claim = valkey.register_script(_CLAIM)
        self._update = valkey.register_script(_UPDATE)
        self._leave = valkey.register_script(_LEAVE)
        self._last_warning = 0.0

    async def start(self) -> None:
        if self._ready:
            return
        self.host = await asyncio.to_thread(host_id)
        self.network_fs = await asyncio.to_thread(data_dir_is_network_fs, self.data_dir)
        self._ready = True
        if self.network_fs and self.mode is CoalesceTail.AUTO:
            log.info('data dir is on a network filesystem: coalescing followers wait for the commit')

    async def aclose(self) -> None:
        for poller in list(self._pollers.values()):
            poller.task.cancel()
        self._pollers.clear()
        for lease in list(self._leases.values()):
            if lease._task is not None:
                lease._task.cancel()

    @staticmethod
    def key(repo_id: str, asset_key: str) -> str:
        digest = hashlib.sha1(asset_key.encode(), usedforsecurity=False).hexdigest()
        return f'{PREFIX}{repo_id}:{digest}'

    def _warn(self, message: str, exc: BaseException) -> None:
        now = time.monotonic()
        if now - self._last_warning > LOG_EVERY_S:
            self._last_warning = now
            log.warning(message, extra={'error': type(exc).__name__})

    async def claim(self, repo_id: str, asset_key: str) -> Flight:
        """Own the fetch of ``asset_key`` or follow whoever does."""
        if not self._ready:
            await self.start()
        key = self.key(repo_id, asset_key)
        token = uuid.uuid4().hex
        try:
            result = await self._claim(keys=[key], args=[token, self.lease_ms, self.host])
        except Exception as exc:  # Valkey unavailable: no coalescing, never an error
            self._warn('coalescing unavailable', exc)
            return Lease(self, key, token, solo=True)
        if isinstance(result, int):
            lease = Lease(self, key, token, replaced=result == 2)
            self._leases[key] = lease
            lease.start()
            return lease
        items = list(result)
        pairs = zip(items[::2], items[1::2], strict=True)  # forbidden-strings: allow (slices, not addresses)
        state = FillState.parse(dict(pairs))
        if state is None:  # unreadable record: fetch alone rather than wait on it
            return Lease(self, key, token, solo=True)
        local = self._leases.get(key)
        return Follower(self, key, state, local if local is not None and local.token == state.owner else None)

    def tail_ok(self, state: FillState) -> bool:
        """May a follower stream the owner's upload file as it grows (else it waits for the commit)?"""
        if self.mode is CoalesceTail.WAIT:
            return False
        if self.mode is CoalesceTail.LOCAL:
            return True
        return state.host == self.host and not self.network_fs

    def readable(self, state: FillState) -> int:
        """Bytes of the upload file this process may read: all written bytes on the owner's host, else the synced."""
        if state.status is Status.DONE and state.size is not None:
            return state.size
        return state.written if state.host == self.host else state.synced

    def _poller(self, key: str, state: FillState) -> _Poller:
        poller = self._pollers.get(key)
        if poller is None or poller.task.done():
            poller = _Poller(self, key, state)
            self._pollers[key] = poller
        elif poller.state is None or poller.state.owner != state.owner:
            poller.state = state  # the claim just read a newer fill than the last poll
            poller.signal.fire()
        poller.users += 1
        return poller

    def _release_poller(self, poller: _Poller) -> None:
        poller.users -= 1
        if poller.users <= 0:
            poller.task.cancel()
            if self._pollers.get(poller.key) is poller:
                del self._pollers[poller.key]
