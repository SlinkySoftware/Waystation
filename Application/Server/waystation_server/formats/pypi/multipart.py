"""A streaming ``multipart/form-data`` reader for the PyPI upload API (twine, ``uv publish``).

The request body is read once, incrementally: text fields are collected (bounded), and a file part is handed to the
caller as an async iterator of chunks, so a distribution streams straight into the blob store without a temporary
copy or a whole-body buffer. Bounds: part headers 16 KiB, each text field ``MAX_FIELD``, all text fields together
``MAX_FIELDS_TOTAL``, at most ``MAX_PARTS`` parts. Anything malformed raises ``MultipartError`` (400).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from waystation_common.errors import PayloadTooLarge, ValidationFailed

MAX_HEADERS = 16 * 1024
MAX_FIELD = 4 * 1024 * 1024  # long_description (README) is the largest text field
MAX_FIELDS_TOTAL = 8 * 1024 * 1024
MAX_PARTS = 200
_BOUNDARY = re.compile(r'^[0-9A-Za-z\'()+_,\-./:=? ]{1,70}$')


class MultipartError(ValidationFailed):
    code = 'invalid_multipart'


def boundary_of(content_type: str | None) -> str:
    """The boundary of a ``multipart/form-data`` Content-Type (raises ``MultipartError``)."""
    media, params = parse_header(content_type or '')
    if media != 'multipart/form-data':
        raise MultipartError('expected a multipart/form-data upload')
    boundary = params.get('boundary', '')
    if not _BOUNDARY.match(boundary) or boundary.endswith(' '):
        raise MultipartError('invalid multipart boundary')
    return boundary


def parse_header(value: str) -> tuple[str, dict[str, str]]:
    """``value; key=val; key="quoted \\"val\\""`` → (lower-case value, {lower-case key: val})."""
    parts: list[str] = []
    current: list[str] = []
    quoted = escaped = False
    for ch in value:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == '\\' and quoted:
            escaped = True
        elif ch == '"':
            quoted = not quoted
        elif ch == ';' and not quoted:
            parts.append(''.join(current))
            current = []
        else:
            current.append(ch)
    parts.append(''.join(current))
    params: dict[str, str] = {}
    for item in parts[1:]:
        key, sep, val = item.partition('=')
        if sep:
            params[key.strip().lower()] = val.strip()
    return parts[0].strip().lower(), params


@dataclass
class Part:
    name: str
    filename: str | None
    content_type: str | None


class MultipartReader:
    """``async for part in reader.parts()``: text parts are read with ``await reader.text()``, file parts with
    ``async for chunk in reader.chunks()`` (or skipped with ``await reader.drain()``)."""

    def __init__(self, body: AsyncIterator[bytes], boundary: str) -> None:
        self._body = body
        self._delimiter = b'--' + boundary.encode('latin-1')
        self._marker = b'\r\n' + self._delimiter
        self._buffer = bytearray()
        self._eof = False
        self._in_part = False
        self._done = False
        self._fields_total = 0

    async def _more(self) -> bool:
        if self._eof:
            return False
        try:
            chunk = await anext(self._body)
        except StopAsyncIteration:
            self._eof = True
            return False
        self._buffer += chunk
        return True

    async def _need(self, size: int) -> None:
        while len(self._buffer) < size:
            if not await self._more():
                raise MultipartError('the upload ended early')

    async def _start(self) -> None:
        """Skip the preamble up to the first delimiter."""
        while True:
            index = self._buffer.find(self._delimiter)
            if index >= 0:
                del self._buffer[:index]
                return
            if len(self._buffer) > MAX_HEADERS:
                raise MultipartError('no multipart delimiter found')
            if not await self._more():
                raise MultipartError('no multipart delimiter found')

    async def _after_delimiter(self) -> bool:
        """At a delimiter: consume it; True when a part follows, False at the closing delimiter."""
        await self._need(len(self._delimiter) + 2)
        del self._buffer[: len(self._delimiter)]
        if self._buffer[:2] == b'--':
            self._done = True
            return False
        # transport padding (spaces/tabs) is allowed before the CRLF
        while True:
            index = self._buffer.find(b'\r\n')
            if index >= 0:
                if self._buffer[:index].strip(b' \t'):
                    raise MultipartError('malformed multipart delimiter')
                del self._buffer[: index + 2]
                return True
            if len(self._buffer) > 256 or not await self._more():
                raise MultipartError('malformed multipart delimiter')

    async def _headers(self) -> Part:
        while True:
            index = self._buffer.find(b'\r\n\r\n')
            if index >= 0:
                break
            if len(self._buffer) > MAX_HEADERS:
                raise MultipartError('multipart part headers too large')
            if not await self._more():
                raise MultipartError('the upload ended early')
        raw = bytes(self._buffer[:index]).decode('utf-8', errors='replace')
        del self._buffer[: index + 4]
        disposition, content_type = None, None
        for line in raw.split('\r\n'):
            key, sep, value = line.partition(':')
            if not sep:
                continue
            key = key.strip().lower()
            if key == 'content-disposition':
                disposition = value
            elif key == 'content-type':
                content_type = value.strip()
        kind, params = parse_header(disposition or '')
        if kind != 'form-data' or 'name' not in params:
            raise MultipartError('multipart part without a form-data name')
        return Part(name=params['name'], filename=params.get('filename'), content_type=content_type)

    async def parts(self) -> AsyncIterator[Part]:
        await self._start()
        count = 0
        while await self._after_delimiter():
            count += 1
            if count > MAX_PARTS:
                raise MultipartError('too many multipart parts')
            part = await self._headers()
            self._in_part = True
            yield part
            if self._in_part:  # the caller did not read it
                await self.drain()

    async def chunks(self) -> AsyncIterator[bytes]:
        """The current part's bytes, as they arrive (the part's end is held back until it is certain)."""
        keep = len(self._marker) - 1
        while self._in_part:
            index = self._buffer.find(self._marker)
            if index >= 0:
                data = bytes(self._buffer[:index])
                del self._buffer[: index + 2]  # leave the delimiter (without its CRLF) for _after_delimiter
                self._in_part = False
                if data:
                    yield data
                return
            if len(self._buffer) > keep:
                data = bytes(self._buffer[: len(self._buffer) - keep])
                del self._buffer[: len(self._buffer) - keep]
                yield data
            if not await self._more():
                raise MultipartError('the upload ended early')

    async def text(self) -> str:
        out = bytearray()
        async for chunk in self.chunks():
            out += chunk
            self._fields_total += len(chunk)
            if len(out) > MAX_FIELD or self._fields_total > MAX_FIELDS_TOTAL:
                raise PayloadTooLarge('upload form fields are too large')
        return out.decode('utf-8', errors='replace')

    async def drain(self) -> None:
        async for _ in self.chunks():
            pass
