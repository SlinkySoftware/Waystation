"""Conda ``repodata.json`` (FORMATS.md "Conda"): a bounded streaming reader for cached upstream files and the writer
of hosted repositories' files.

``repodata.json`` is one JSON object: ``info`` (``subdir``), ``packages`` (``.tar.bz2`` files) and ``packages.conda``
(``.conda`` files), each a map of file name -> entry (``name``, ``version``, ``build``, ``build_number``,
``depends``, ``sha256``, ``md5``, ``size``, ``timestamp``...), plus ``removed`` and ``repodata_version``. conda-forge's
``linux-64`` file is hundreds of megabytes, so it is never loaded whole: ``read_entries`` decompresses (``.zst``,
``.bz2`` or plain; bounded by ``MAX_DECOMPRESSED``) and scans it chunk by chunk, decoding one entry at a time
(``json.JSONDecoder.raw_decode``), so memory stays bounded by the largest single value (``MAX_VALUE``). Entries
whose file name, name, version, build, digests or size are not valid are skipped and counted; the scan stops with
``RepodataError`` for malformed JSON or past ``MAX_ENTRIES``.
"""

from __future__ import annotations

import bz2
import codecs
import io
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from waystation_common.conda import parse_filename, valid_subdir

MAX_DECOMPRESSED = 2 * 1024 * 1024 * 1024
MAX_VALUE = 64 * 1024 * 1024
MAX_ENTRIES = 3_000_000
CHUNK = 1024 * 1024
SECTIONS = {'packages': '.tar.bz2', 'packages.conda': '.conda'}
# Entry fields kept in hosted repodata (what conda-index writes); anything else from index.json is dropped.
ENTRY_FIELDS = (
    'arch',
    'build',
    'build_number',
    'constrains',
    'depends',
    'features',
    'license',
    'license_family',
    'md5',
    'name',
    'noarch',
    'platform',
    'python_site_packages_path',
    'sha256',
    'size',
    'subdir',
    'timestamp',
    'track_features',
    'version',
)
_WS = re.compile(r'[ \t\n\r]*')
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_HEX32 = re.compile(r'^[0-9a-f]{32}$')
_DECODER = json.JSONDecoder()


class RepodataError(ValueError):
    """The file is not a repodata document Waystation can read (malformed, too large)."""


@dataclass(frozen=True)
class Entry:
    filename: str
    name: str
    version: str
    build: str
    build_number: int
    sha256: str | None
    md5: str | None
    size: int | None
    timestamp: int | None  # milliseconds since the epoch


@dataclass
class Scan:
    """What a scan found besides the entries: ``info.subdir`` and the count of skipped (invalid) entries."""

    subdir: str | None = None
    skipped: int = 0
    entries: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def compression_of(path: str) -> str:
    """``zst``, ``bz2`` or ``json`` from a repodata file name."""
    if path.endswith('.zst'):
        return 'zst'
    if path.endswith('.bz2'):
        return 'bz2'
    return 'json'


def _open(path: Path, compression: str) -> io.BufferedIOBase:
    if compression == 'zst':
        from compression import zstd

        return zstd.open(path, 'rb')
    if compression == 'bz2':
        return bz2.open(path, 'rb')
    return path.open('rb')


def _text_chunks(path: Path, compression: str, limit: int) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder('utf-8')()
    total = 0
    try:
        with _open(path, compression) as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise RepodataError('repodata is too large once decompressed')
                yield decoder.decode(chunk)
            tail = decoder.decode(b'', final=True)
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise RepodataError(f'repodata cannot be read ({type(exc).__name__})') from None
    except Exception as exc:  # zstd errors (compression.zstd.ZstdError)
        if isinstance(exc, RepodataError):
            raise
        raise RepodataError(f'repodata cannot be decompressed ({type(exc).__name__})') from None
    if tail:
        yield tail


class _Scanner:
    def __init__(self, chunks: Iterator[str], max_value: int) -> None:
        self._chunks = chunks
        self._max = max_value
        self.buf = ''
        self.pos = 0
        self.eof = False

    def _more(self) -> bool:
        """Append input (at least as much as is pending, so a large value is retried a logarithmic number of
        times); False at the end."""
        if self.eof:
            return False
        pending = self.buf[self.pos :]
        if len(pending) > self._max:
            raise RepodataError('a repodata value is too large')
        added: list[str] = []
        size = 0
        while size < max(CHUNK, len(pending)):
            nxt = next(self._chunks, None)
            if nxt is None:
                self.eof = True
                break
            added.append(nxt)
            size += len(nxt)
        self.buf = pending + ''.join(added)
        self.pos = 0
        return bool(added)

    def peek(self) -> str:
        while True:
            match = _WS.match(self.buf, self.pos)
            self.pos = match.end() if match else self.pos
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if not self._more():
                return ''

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise RepodataError(f'malformed repodata (expected {char!r})')
        self.pos += 1

    def value(self) -> Any:
        if not self.peek():
            raise RepodataError('repodata ends early')
        while True:
            try:
                value, end = _DECODER.raw_decode(self.buf, self.pos)
            except json.JSONDecodeError:
                if self._more():
                    continue
                raise RepodataError('malformed repodata') from None
            if end >= len(self.buf) and not self.eof and self._more():
                continue  # a number at the end of the buffer may go on
            self.pos = end
            return value

    def separator(self, close: str) -> bool:
        """After a member: True when another follows (``,``), False at ``close``."""
        char = self.peek()
        self.pos += 1
        if char == ',':
            return True
        if char == close:
            return False
        raise RepodataError('malformed repodata')


def _entry(filename: Any, raw: Any, suffix: str) -> Entry | None:
    if not isinstance(filename, str) or not isinstance(raw, dict) or not filename.endswith(suffix):
        return None
    found = parse_filename(filename)
    if found is None:
        return None
    name, version, build = raw.get('name'), raw.get('version'), raw.get('build')
    if name != found.name or version != found.version or build != found.build:
        return None
    sha256 = raw.get('sha256')
    md5 = raw.get('md5')
    size = raw.get('size')
    number = raw.get('build_number')
    stamp = raw.get('timestamp')
    if sha256 is not None and not (isinstance(sha256, str) and _HEX64.match(sha256)):
        return None
    if md5 is not None and not (isinstance(md5, str) and _HEX32.match(md5)):
        return None
    if sha256 is None and md5 is None:
        return None
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
        return None
    return Entry(
        filename=filename,
        name=found.name,
        version=found.version,
        build=found.build,
        build_number=number if isinstance(number, int) and not isinstance(number, bool) and number >= 0 else 0,
        sha256=sha256,
        md5=md5,
        size=size,
        timestamp=stamp if isinstance(stamp, int) and not isinstance(stamp, bool) and stamp > 0 else None,
    )


def scan(chunks: Iterator[str], result: Scan, *, max_entries: int = MAX_ENTRIES) -> Iterator[Entry]:
    """The valid entries of a repodata document given as text chunks; ``result`` collects ``info.subdir`` and
    counts. Raises ``RepodataError`` for malformed documents."""
    s = _Scanner(chunks, MAX_VALUE)
    s.expect('{')
    if s.peek() == '}':
        s.pos += 1
        return
    more = True
    while more:
        key = s.value()
        if not isinstance(key, str):
            raise RepodataError('malformed repodata')
        s.expect(':')
        suffix = SECTIONS.get(key)
        if suffix is not None and s.peek() == '{':
            s.pos += 1
            if s.peek() == '}':
                s.pos += 1
            else:
                inner = True
                while inner:
                    filename = s.value()
                    s.expect(':')
                    entry = _entry(filename, s.value(), suffix)
                    result.entries += 1
                    if result.entries > max_entries:
                        raise RepodataError('repodata lists too many packages')
                    if entry is None:
                        result.skipped += 1
                    else:
                        yield entry
                    inner = s.separator('}')
        else:
            value = s.value()
            if key == 'info' and isinstance(value, dict):
                subdir = value.get('subdir')
                if isinstance(subdir, str) and valid_subdir(subdir):
                    result.subdir = subdir
            elif key == 'repodata_version' and isinstance(value, int):
                result.extra['repodata_version'] = value
        more = s.separator('}')
    if s.peek():
        raise RepodataError('trailing data after the repodata document')


def read_entries(path: Path, compression: str, result: Scan, *, limit: int = MAX_DECOMPRESSED) -> Iterator[Entry]:
    """The entries of a stored repodata file (``compression``: ``json``, ``zst`` or ``bz2``), streamed."""
    return scan(_text_chunks(path, compression, limit), result)


def batches(entries: Iterator[Entry], size: int) -> Iterator[list[Entry]]:
    batch: list[Entry] = []
    for entry in entries:
        batch.append(entry)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# ---------------------------------------------------------------------------- hosted repodata
def render(subdir: str, entries: Mapping[str, Mapping[str, Any]]) -> bytes:
    """``repodata.json`` of a hosted subdir from its entries by file name (sorted, compact, UTF-8)."""
    packages = {k: dict(v) for k, v in sorted(entries.items()) if k.endswith('.tar.bz2')}
    conda = {k: dict(v) for k, v in sorted(entries.items()) if k.endswith('.conda')}
    document = {
        'info': {'subdir': subdir},
        'packages': packages,
        'packages.conda': conda,
        'removed': [],
        'repodata_version': 1,
    }
    return json.dumps(document, ensure_ascii=False, separators=(',', ':'), sort_keys=False).encode() + b'\n'


def empty(subdir: str) -> bytes:
    return render(subdir, {})


def channeldata(packages: Mapping[str, Mapping[str, Any]], subdirs: list[str]) -> bytes:
    """``channeldata.json``: per package its subdirs, newest version and license; the channel's subdirs."""
    document = {
        'channeldata_version': 1,
        'packages': {name: dict(fields) for name, fields in sorted(packages.items())},
        'subdirs': sorted(subdirs),
    }
    return json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'


def compress_zst(data: bytes) -> bytes:
    from compression import zstd

    return zstd.compress(data, level=10)


__all__ = [
    'ENTRY_FIELDS',
    'Entry',
    'RepodataError',
    'Scan',
    'batches',
    'channeldata',
    'compress_zst',
    'compression_of',
    'empty',
    'read_entries',
    'render',
    'scan',
]
