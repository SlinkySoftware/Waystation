"""A tiny scriptable HTTP/1.1 upstream on 127.0.0.1 for proxy-engine and fault-injection tests.

Routes map a path to a ``Respond`` (or a function of the ``Seen`` request returning one). ``Respond`` can split
the body into chunks with delays, cut the connection after N body bytes (``reset_after``), never answer (``hang``),
stop after N body bytes until an event is set (``pause_at`` + ``gate``) or for good (``hang_after``). Every request
is recorded in ``upstream.seen``.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]


@dataclass
class Respond:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b''
    chunk: int | None = None
    chunk_delay: float = 0.0
    delay: float = 0.0  # before the status line
    reset_after: int | None = None  # close the connection after this many body bytes
    hang: bool = False
    content_length: bool = True
    gate: asyncio.Event | None = None  # wait for this before sending body bytes past ``pause_at``
    pause_at: int = 0
    hang_after: int | None = None  # stop sending (keep the connection open) after this many body bytes


Route = Respond | tuple[int, dict[str, str], bytes] | Callable[[Seen], Respond]

REASONS = {200: 'OK', 304: 'Not Modified', 302: 'Found', 401: 'Unauthorized', 404: 'Not Found', 500: 'Error'}


class FakeUpstream:
    def __init__(
        self, routes: dict[str, Route] | None = None, *, tls: ssl.SSLContext | None = None, host: str = '127.0.0.1'
    ) -> None:
        self.host = host
        self.routes: dict[str, Route] = dict(routes or {})
        self.tls = tls  # serve HTTPS with this server context
        self.seen: list[Seen] = []
        self._server: asyncio.Server | None = None
        self.port = 0
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def url(self) -> str:
        return f'{"https" if self.tls else "http"}://{self.host}:{self.port}/'

    def hits(self, path: str) -> int:
        return sum(1 for s in self.seen if s.path == path)

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle, self.host, 0, ssl=self.tls)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        assert self._server is not None
        self._server.close()
        for task in list(self._tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), 2)

    def _resolve(self, seen: Seen) -> Respond:
        route = self.routes.get(seen.path)
        if route is None:
            return Respond(404, {}, b'not found')
        if isinstance(route, Respond):
            return route
        if isinstance(route, tuple):
            return Respond(route[0], route[1], route[2])
        return route(seen)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)  # type: ignore[arg-type]
        try:
            while True:
                try:
                    head = await reader.readuntil(b'\r\n\r\n')
                except asyncio.IncompleteReadError, ConnectionError, ssl.SSLError:
                    return
                lines = head.decode('latin-1').split('\r\n')
                method, target, _ = lines[0].split(' ', 2)
                headers = {}
                for line in lines[1:]:
                    if ':' in line:
                        k, v = line.split(':', 1)
                        headers[k.strip().lower()] = v.strip()
                if int(headers.get('content-length', '0') or 0):
                    await reader.readexactly(int(headers['content-length']))
                seen = Seen(method, target, headers)
                self.seen.append(seen)
                plan = self._resolve(seen)
                if plan.hang:
                    await asyncio.sleep(3600)
                    return
                if plan.delay:
                    await asyncio.sleep(plan.delay)
                body = b'' if method == 'HEAD' else plan.body
                out = [f'HTTP/1.1 {plan.status} {REASONS.get(plan.status, "X")}']
                hdrs = {k.lower(): v for k, v in plan.headers.items()}
                if plan.content_length and 'content-length' not in hdrs and plan.status not in (204, 304):
                    hdrs['content-length'] = str(len(plan.body))
                if not plan.content_length:
                    hdrs['connection'] = 'close'
                out += [f'{k}: {v}' for k, v in hdrs.items()]
                writer.write(('\r\n'.join(out) + '\r\n\r\n').encode('latin-1'))
                await writer.drain()
                sent = 0
                step = plan.chunk or max(len(body), 1)
                for i in range(0, len(body), step):
                    piece = body[i : i + step]
                    if plan.gate is not None and sent >= plan.pause_at:
                        await plan.gate.wait()
                    if plan.hang_after is not None and sent >= plan.hang_after:
                        await asyncio.sleep(3600)
                        return
                    if plan.reset_after is not None and sent + len(piece) > plan.reset_after:
                        writer.write(piece[: plan.reset_after - sent])
                        await writer.drain()
                        writer.transport.abort()
                        return
                    writer.write(piece)
                    await writer.drain()
                    sent += len(piece)
                    if plan.chunk_delay:
                        await asyncio.sleep(plan.chunk_delay)
                if not plan.content_length or headers.get('connection', '').lower() == 'close':
                    writer.close()
                    return
        except ConnectionError, asyncio.CancelledError:
            return
        finally:
            self._tasks.discard(task)  # type: ignore[arg-type]
            with contextlib.suppress(Exception):
                writer.close()
