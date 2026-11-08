"""Bounded parsers for the apt metadata Waystation reads (FORMATS.md "apt"): ``Release``/``InRelease`` and
``Packages`` files, plus bounded decompression of compressed indexes.

Everything here is pure CPU work on bytes: callers run it in a thread (ARCHITECTURE §9). Upstream content is never
trusted: sizes, entry counts and line lengths are bounded, hashes must be well-formed hex, paths go through
``waystation_common.paths.normalise`` (entries with unsafe paths are dropped, never followed).

The ``InRelease`` signature is **not** verified here: Waystation passes the signed bytes through untouched and the
client verifies them with its own keyring (``Signed-By``). Waystation only uses the hash list to make sure every
index it serves is the one the served ``Release`` names.
"""

from __future__ import annotations

import bz2
import lzma
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from waystation_common.errors import PathRejected, ValidationFailed
from waystation_common.paths import normalise

RELEASE_MAX_BYTES = 32 * 1024 * 1024
RELEASE_MAX_ENTRIES = 500_000
PACKAGES_MAX_BYTES = 512 * 1024 * 1024  # decompressed
PACKAGES_MAX_ENTRIES = 1_000_000

_HEX64 = re.compile(rb'^[0-9a-f]{64}$')
_HEX128 = re.compile(rb'^[0-9a-f]{128}$')
_HASH_FIELDS = {b'sha256': 'sha256', b'sha512': 'sha512'}
_SIGNED = b'-----BEGIN PGP SIGNED MESSAGE-----'
_SIGNATURE = b'-----BEGIN PGP SIGNATURE-----'
COMPRESSIONS = ('', '.xz', '.gz', '.bz2', '.lzma', '.zst')


@dataclass(frozen=True)
class IndexEntry:
    """One file a ``Release`` lists (path relative to the suite directory)."""

    sha256: str
    size: int


@dataclass(frozen=True)
class Release:
    files: dict[str, IndexEntry]  # relative path -> SHA256 entry
    sha512: dict[str, str]  # relative path -> SHA512 hex (when listed)
    by_hash: bool  # Acquire-By-Hash: yes

    def path_for(self, algorithm: str, digest: str) -> str | None:
        """The listed path whose ``algorithm`` digest is ``digest`` (first match), for ``by-hash`` requests."""
        if algorithm == 'sha256':
            for path, entry in self.files.items():
                if entry.sha256 == digest:
                    return path
        elif algorithm == 'sha512':
            for path, value in self.sha512.items():
                if value == digest:
                    return path
        return None


@dataclass(frozen=True)
class PoolEntry:
    filename: str
    sha256: str
    size: int


def strip_clearsign(data: bytes) -> bytes:
    """The signed text of a clearsigned (``InRelease``) document; other documents are returned unchanged."""
    if not data.lstrip().startswith(_SIGNED):
        return data
    lines = data.lstrip().split(b'\n')
    i = 1
    while i < len(lines) and lines[i].strip():  # armor headers (Hash: SHA512) up to the first blank line
        i += 1
    out: list[bytes] = []
    for line in lines[i + 1 :]:
        if line.rstrip(b'\r') == _SIGNATURE:
            return b'\n'.join(out) + b'\n'
        out.append(line[2:] if line.startswith(b'- ') else line)
    raise ValidationFailed('clearsigned Release has no signature block')


def parse_release(data: bytes) -> Release:
    """Parse a ``Release`` or ``InRelease`` document (raises ``ValidationFailed`` on malformed content)."""
    if len(data) > RELEASE_MAX_BYTES:
        raise ValidationFailed('Release file is too large')
    text = strip_clearsign(data)
    files: dict[str, IndexEntry] = {}
    sha512: dict[str, str] = {}
    by_hash = False
    current: str | None = None  # the multi-line hash field being read
    count = 0
    for raw in text.split(b'\n'):
        line = raw.rstrip(b'\r')
        if not line.strip():
            if files or current is not None:
                break  # Release is a single paragraph; anything after a blank line is ignored
            continue
        if line[:1] in (b' ', b'\t'):
            if current is None:
                continue  # continuation of a field we do not read (Description...)
            parts = line.split()
            if len(parts) != 3:
                raise ValidationFailed('malformed hash line in Release')
            digest, size_raw, path_raw = (p.lower() if i == 0 else p for i, p in enumerate(parts))
            pattern = _HEX64 if current == 'sha256' else _HEX128
            if not pattern.match(digest) or not size_raw.isdigit():
                raise ValidationFailed('malformed hash line in Release')
            count += 1
            if count > RELEASE_MAX_ENTRIES:
                raise ValidationFailed('Release lists too many files')
            try:
                path = normalise(path_raw.decode('utf-8'))
            except PathRejected, UnicodeDecodeError:
                continue  # never serve or follow an unsafe path
            if current == 'sha256':
                files[path] = IndexEntry(sha256=digest.decode('ascii'), size=int(size_raw))
            else:
                sha512[path] = digest.decode('ascii')
            continue
        name, sep, value = line.partition(b':')
        if not sep:
            raise ValidationFailed('malformed Release field')
        key = name.strip().lower()
        current = _HASH_FIELDS.get(key)
        if key == b'acquire-by-hash':
            by_hash = value.strip().lower() == b'yes'
    return Release(files=files, sha512=sha512, by_hash=by_hash)


def compression_of(path: str) -> str | None:
    """The compression suffix of an index path ('' = uncompressed), or None when unsupported."""
    for suffix in COMPRESSIONS[1:]:
        if path.endswith(suffix):
            return suffix
    return '' if '.' not in path.rsplit('/', 1)[-1] else None


class _Stream(Protocol):
    @property
    def eof(self) -> bool: ...

    def decompress(self, data: bytes, max_length: int = -1) -> bytes: ...


def _decompressor(suffix: str) -> _Stream:
    if suffix == '.xz':
        return lzma.LZMADecompressor(format=lzma.FORMAT_AUTO)
    if suffix == '.lzma':
        return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
    if suffix == '.bz2':
        return bz2.BZ2Decompressor()
    if suffix == '.zst':
        from compression import zstd

        return zstd.ZstdDecompressor()
    raise ValidationFailed('unsupported index compression')


def decompress(data: bytes, suffix: str, *, limit: int = PACKAGES_MAX_BYTES) -> bytes:
    """Decompress ``data`` (by file suffix) to at most ``limit`` bytes; larger or truncated streams raise."""
    if suffix == '':
        if len(data) > limit:
            raise ValidationFailed('index is too large')
        return data
    try:
        if suffix == '.gz':
            gz = zlib.decompressobj(wbits=47)  # gzip or zlib header
            out = gz.decompress(data, limit + 1)
            if len(out) <= limit and not gz.unconsumed_tail:
                out += gz.flush()
            eof = gz.eof
        else:
            stream = _decompressor(suffix)
            out = stream.decompress(data, max_length=limit + 1)
            eof = stream.eof
    except (lzma.LZMAError, OSError, EOFError, zlib.error) as exc:
        raise ValidationFailed('index does not decompress') from exc
    if len(out) > limit:
        raise ValidationFailed('index decompresses beyond the size limit')
    if not eof:
        raise ValidationFailed('compressed index is truncated')
    return out


def _paragraph_fields(data: bytes) -> Iterator[dict[bytes, bytes]]:
    fields: dict[bytes, bytes] = {}
    for raw in data.split(b'\n'):
        line = raw.rstrip(b'\r')
        if not line:
            if fields:
                yield fields
                fields = {}
            continue
        if line[:1] in (b' ', b'\t', b'#'):
            continue
        name, sep, value = line.partition(b':')
        if not sep:
            continue
        key = name.lower()
        if key in (b'filename', b'sha256', b'size'):
            fields[key] = value.strip()
    if fields:
        yield fields


def parse_packages(data: bytes) -> list[PoolEntry]:
    """``Filename → (SHA256, Size)`` for every well-formed stanza of a (decompressed) ``Packages`` file.

    Stanzas without a SHA256, with a malformed hash or size, or with an unsafe ``Filename`` are skipped: such files
    are then served unverified rather than trusted.
    """
    entries: list[PoolEntry] = []
    for fields in _paragraph_fields(data):
        filename, digest, size = fields.get(b'filename'), fields.get(b'sha256'), fields.get(b'size')
        if not filename or not digest or not size or not size.isdigit():
            continue
        digest = digest.lower()
        if not _HEX64.match(digest):
            continue
        try:
            path = normalise(filename.decode('utf-8'))
        except PathRejected, UnicodeDecodeError:
            continue
        entries.append(PoolEntry(filename=path, sha256=digest.decode('ascii'), size=int(size)))
        if len(entries) > PACKAGES_MAX_ENTRIES:
            raise ValidationFailed('Packages lists too many files')
    return entries


def parse_packages_file(data: bytes, suffix: str) -> list[PoolEntry]:
    return parse_packages(decompress(data, suffix))
