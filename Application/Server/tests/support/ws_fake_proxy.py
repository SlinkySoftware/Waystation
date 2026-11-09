"""A tiny forward HTTP proxy on 127.0.0.1 for outbound-proxy tests: ``CONNECT host:port`` tunnels and absolute-form
``GET http://host/path`` requests (relayed in origin form). Records the target of each connection's first request in
``seen`` (``CONNECT 127.0.0.1:443`` / ``GET http://127.0.0.1:80/x``). It connects anywhere: the SSRF guard has to
stop private targets before they reach it."""

from __future__ import annotations

import asyncio
import contextlib
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit


class FakeProxy:
    def __init__(self) -> None:
        self.seen: list[str] = []
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.port = 0

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle, '127.0.0.1', 0)
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

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)  # type: ignore[arg-type]
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            lines = head.decode('latin-1').split('\r\n')
            method, target, version = lines[0].split(' ', 2)
            self.seen.append(f'{method} {target}')
            if method == 'CONNECT':
                host, _, port = target.rpartition(':')
                up_reader, upstream_writer = await asyncio.open_connection(host.strip('[]'), int(port))
                writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
                await writer.drain()
            else:
                parts = urlsplit(target)
                up_reader, upstream_writer = await asyncio.open_connection(parts.hostname, parts.port or 80)
                path = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
                rest = '\r\n'.join(line for line in lines[1:] if not line.lower().startswith('proxy-'))
                upstream_writer.write(f'{method} {path} {version}\r\n{rest}'.encode('latin-1'))
                await upstream_writer.drain()
            await asyncio.gather(self._pipe(reader, upstream_writer), self._pipe(up_reader, writer))
        except asyncio.IncompleteReadError, ConnectionError, OSError, ValueError, asyncio.CancelledError:
            return
        finally:
            self._tasks.discard(task)  # type: ignore[arg-type]
            for w in (writer, upstream_writer):
                if w is not None:
                    with contextlib.suppress(Exception):
                        w.close()

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except ConnectionError, OSError:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
