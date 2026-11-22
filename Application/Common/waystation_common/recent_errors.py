"""Recent warnings and errors for the support bundle (T-O10; DATA_MODEL ``errors:recent``).

Logs are JSON lines on stdout and are not stored. ``RecentErrors`` is a logging handler that the server and the
workers attach to the root logger for the life of their Valkey connection: it keeps WARNING and ERROR records (the
same fields as the log line, already redacted by ``waystation_common.logs``; credentials in URLs masked; every field
bounded) in a small in-process buffer, and a background task appends them to the Valkey stream ``errors:recent``
(``XADD MAXLEN ~1000``, key TTL 7 days refreshed on every write). The support bundle reads the newest entries.

It never blocks or fails the caller: ``emit`` only appends to a bounded buffer (full = the record is dropped and
counted), a flood of the same message (logger, level and message template) is cut to ``per_key_limit`` records per
``window_s`` with one summary entry for the rest, and when Valkey is down the batch is dropped and counted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final

from redis.asyncio import Redis

from waystation_common.logs import mask_userinfo, record_fields

KEY: Final = 'errors:recent'
FIELD: Final = 'e'
MAXLEN: Final = 1000
TTL_S: Final = 7 * 86400

MAX_QUEUE: Final = 500  # records waiting for the next flush
FLUSH_INTERVAL_S: Final = 2.0
WINDOW_S: Final = 60.0
PER_KEY_LIMIT: Final = 10  # records of one message per window; the rest are summarised
MAX_KEYS: Final = 2000  # distinct messages tracked per window

MAX_MESSAGE: Final = 2000
MAX_EXCEPTION: Final = 4000  # the end of the traceback (where the error is)
MAX_FIELDS: Final = 30
MAX_VALUE: Final = 500

_BASE: Final = ('ts', 'level', 'logger', 'msg', 'component', 'host', 'pid')


def _clip(text: str, limit: int, *, tail: bool = False) -> str:
    if len(text) <= limit:
        return text
    return '…' + text[-(limit - 1) :] if tail else text[: limit - 1] + '…'


def _bounded(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return _clip(mask_userinfo(text), MAX_VALUE)


class RecentErrors(logging.Handler):
    """Logging handler → bounded buffer → Valkey stream (see the module docstring)."""

    def __init__(
        self,
        component: str,
        *,
        level: int = logging.WARNING,
        max_queue: int = MAX_QUEUE,
        window_s: float = WINDOW_S,
        per_key_limit: int = PER_KEY_LIMIT,
        flush_interval_s: float = FLUSH_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(level)
        self.component = component
        self.host = socket.gethostname()
        self.pid = os.getpid()
        self._max_queue = max_queue
        self._window_s = window_s
        self._per_key_limit = per_key_limit
        self._flush_interval_s = flush_interval_s
        self._clock = clock
        self._buffer: deque[str] = deque()
        # (logger, level, template) → [window start, records seen, suppressed, last suppressed entry]
        self._windows: dict[tuple[str, int, str], list[Any]] = {}
        self._valkey: Redis | None = None
        self._task: asyncio.Task[None] | None = None
        self.written = 0
        self.dropped = 0
        self.suppressed = 0

    # ------------------------------------------------------------------ capture (any thread, never raises)
    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._accept(record)
        except Exception:  # a record that cannot be rendered: never fail (or print from) the caller
            self.dropped += 1

    def entry(self, record: logging.LogRecord) -> dict[str, Any]:
        """The stored fields of ``record``: bounded, redacted, URL credentials masked."""
        fields = record_fields(record)
        out: dict[str, Any] = {
            'ts': fields.pop('ts'),
            'level': fields.pop('level'),
            'logger': fields.pop('logger'),
            'msg': _clip(mask_userinfo(str(fields.pop('msg'))), MAX_MESSAGE),
            'component': self.component,
            'host': self.host,
            'pid': self.pid,
        }
        exc = fields.pop('exc', None)
        for i, (key, value) in enumerate(fields.items()):
            if i >= MAX_FIELDS:
                out['more_fields'] = len(fields) - MAX_FIELDS
                break
            if key not in _BASE:
                out[_clip(key, 64)] = _bounded(value)
        if exc:
            out['exc'] = _clip(mask_userinfo(str(exc)), MAX_EXCEPTION, tail=True)
        return out

    def _accept(self, record: logging.LogRecord) -> None:
        now = self._clock()
        key = (record.name, record.levelno, str(record.msg)[:200])
        window = self._windows.get(key)
        if window is None or now - window[0] >= self._window_s:
            if window is not None:
                self._summarise(window)
            elif len(self._windows) >= MAX_KEYS:
                self._sweep(now, force=True)
            window = [now, 0, 0, None]
            self._windows[key] = window
        window[1] += 1
        if window[1] > self._per_key_limit:
            window[2] += 1
            self.suppressed += 1
            window[3] = (record.levelname, record.name, record.getMessage())
            return
        self._push(json.dumps(self.entry(record), default=str, ensure_ascii=False))

    def _push(self, line: str) -> None:
        if len(self._buffer) >= self._max_queue:
            self.dropped += 1
            return
        self._buffer.append(line)

    def _summarise(self, window: list[Any]) -> None:
        if not window[2] or window[3] is None:
            return
        level, logger, message = window[3]
        self._push(
            json.dumps(
                {
                    'ts': datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                    'level': level,
                    'logger': logger,
                    'msg': _clip(mask_userinfo(message), MAX_MESSAGE),
                    'component': self.component,
                    'host': self.host,
                    'pid': self.pid,
                    'suppressed': window[2],
                    'note': f'{window[2]} more records like this one within {self._window_s:.0f} s were not kept',
                },
                ensure_ascii=False,
            )
        )
        window[2] = 0

    def _sweep(self, now: float, *, force: bool = False) -> None:
        """Summarise and forget the windows that ended (all of them with ``force``)."""
        for key, window in list(self._windows.items()):
            if force or now - window[0] >= self._window_s:
                self._summarise(window)
                del self._windows[key]

    @property
    def queued(self) -> int:
        return len(self._buffer)

    # ------------------------------------------------------------------ lifecycle and flushing (event loop)
    def install(self, logger: logging.Logger | None = None) -> None:
        (logger or logging.getLogger()).addHandler(self)

    def uninstall(self, logger: logging.Logger | None = None) -> None:
        (logger or logging.getLogger()).removeHandler(self)

    def start(self, valkey: Redis) -> None:
        """Attach to the root logger and flush to ``valkey`` in the background."""
        self._valkey = valkey
        self.install()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name='recent-errors')

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval_s)
            await self.write_pending()

    def _take(self) -> list[str]:
        self.acquire()  # the handler lock: emit may run in another thread
        try:
            self._sweep(self._clock())
            batch = list(self._buffer)
            self._buffer.clear()
            return batch
        finally:
            self.release()

    async def write_pending(self, timeout_s: float = 5.0) -> None:
        """Write what is buffered; on failure the batch is dropped and counted (never raises)."""
        batch = self._take()
        if not batch:
            return
        if self._valkey is None:
            self.dropped += len(batch)
            return
        try:
            async with asyncio.timeout(timeout_s):
                async with self._valkey.pipeline(transaction=False) as pipe:
                    for line in batch:
                        pipe.xadd(KEY, {FIELD: line}, maxlen=MAXLEN, approximate=True)
                    pipe.expire(KEY, TTL_S)
                    await pipe.execute()
            self.written += len(batch)
        except asyncio.CancelledError:
            self.dropped += len(batch)
            raise
        except Exception:  # Valkey down or slow: drop, never log (that would feed this handler)
            self.dropped += len(batch)

    async def aclose(self) -> None:
        """Detach, stop the flusher and write what is left."""
        self.uninstall()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.write_pending(timeout_s=2.0)


async def read_recent(valkey: Redis, count: int = 500) -> list[dict[str, Any]]:
    """The newest ``count`` captured records, newest first (malformed entries are skipped)."""
    entries: Any = await valkey.xrevrange(KEY, count=count)
    out: list[dict[str, Any]] = []
    for entry_id, fields in entries:
        raw = fields.get(FIELD) if isinstance(fields, dict) else None
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', 'replace')
        try:
            item = json.loads(raw) if isinstance(raw, str) else None
        except ValueError:
            item = None
        if isinstance(item, dict):
            item['id'] = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
            out.append(item)
    return out
