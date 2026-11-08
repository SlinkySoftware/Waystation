"""Bounded, defused parsers for the yum metadata Waystation reads (FORMATS.md "yum"): ``repodata/repomd.xml`` and
the ``primary`` package list, plus bounded streaming decompression of compressed repodata files.

Everything here is pure CPU work on bytes: callers run it in a thread (ARCHITECTURE §9). Upstream content is never
trusted:

- XML goes through expat with **no document type declarations** (a ``<!DOCTYPE``, an entity or a notation
  declaration rejects the document), so there are no custom entities to expand and no external entities to fetch;
  parameter entities are never parsed.
- Sizes are bounded (``repomd.xml`` before parsing; ``primary`` *while* decompressing, in chunks, so a
  decompression bomb never materialises), as are the number of entries and every text value read.
- Checksums must be well-formed hex of their algorithm's length; algorithms other than SHA-1/256/512 are reported
  as unsupported (``checksum is None``) rather than guessed.
- ``location href``s go through ``waystation_common.paths.normalise``: entries with unsafe paths are dropped, never
  followed; entries with an ``xml:base`` (the file lives on another server) are dropped too.

``repomd.xml.asc`` signatures are **not** verified here: Waystation passes ``repomd.xml`` through untouched and dnf
verifies it (``repo_gpgcheck``) and every package (``gpgcheck``) with its own keys.
"""

from __future__ import annotations

import bz2
import lzma
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol
from xml.parsers import expat

from waystation_common.errors import PathRejected, ValidationFailed
from waystation_common.paths import normalise

REPOMD_MAX_BYTES = 8 * 1024 * 1024
REPOMD_MAX_ENTRIES = 1000
PRIMARY_MAX_BYTES = 2 * 1024 * 1024 * 1024  # decompressed
PRIMARY_MAX_PACKAGES = 1_000_000
TEXT_MAX = 1024
CHUNK = 1024 * 1024

# Checksum type names used in repodata (``sha`` is createrepo's old name for SHA-1) -> hashlib name.
ALGORITHMS = {'sha': 'sha1', 'sha1': 'sha1', 'sha256': 'sha256', 'sha512': 'sha512'}
HEX_LENGTHS = {'sha1': 40, 'sha256': 64, 'sha512': 128}
_HEX = re.compile(r'^[0-9a-f]+$')
COMPRESSIONS = ('.gz', '.xz', '.zst', '.bz2')


@dataclass(frozen=True)
class Checksum:
    algorithm: str  # hashlib name: sha1, sha256, sha512
    digest: str  # lower-case hex


@dataclass(frozen=True)
class RepomdEntry:
    """One ``<data>`` of ``repomd.xml``: ``href`` relative to the repository directory (``repodata/<name>``)."""

    type: str
    href: str
    checksum: Checksum | None  # None: an algorithm Waystation does not verify (the file cannot be served)
    size: int | None


@dataclass(frozen=True)
class Repomd:
    revision: str
    entries: dict[str, RepomdEntry]  # href -> entry

    def of_type(self, kind: str) -> RepomdEntry | None:
        for entry in self.entries.values():
            if entry.type == kind:
                return entry
        return None


@dataclass(frozen=True)
class Package:
    """One ``<package>`` of ``primary.xml``: ``href`` relative to the repository directory."""

    href: str
    checksum: Checksum
    size: int
    name: str
    epoch: str
    version: str
    release: str
    arch: str


def checksum(kind: str | None, value: str | None) -> Checksum | None:
    """A well-formed checksum of a supported algorithm, else None."""
    algorithm = ALGORITHMS.get((kind or '').strip().lower())
    digest = (value or '').strip().lower()
    if algorithm is None or len(digest) != HEX_LENGTHS[algorithm] or not _HEX.match(digest):
        return None
    return Checksum(algorithm, digest)


def checksum_prefix(name: str) -> Checksum | None:
    """The checksum a by-checksum file name starts with (``<hex>-primary.xml.gz``), by its length, else None."""
    head, sep, _ = name.partition('-')
    if not sep:
        return None
    for algorithm, length in HEX_LENGTHS.items():
        if len(head) == length:
            return checksum(algorithm, head)
    return None


def _href(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return normalise(raw) or None
    except PathRejected:
        return None


def _local(name: str) -> str:
    return name.rpartition(' ')[2]


def _is_base(attrs: dict[str, str]) -> bool:
    return any(_local(key) == 'base' for key in attrs)


def _forbidden(*_: Any) -> None:
    raise ValidationFailed('XML document type declarations are not allowed')


def _no_external(*_: Any) -> int:
    raise ValidationFailed('XML external entities are not allowed')


def _parser() -> expat.XMLParserType:
    """An expat parser that refuses DTDs, entity and notation declarations (namespace-aware: names are
    ``<uri> <local>``)."""
    parser = expat.ParserCreate(namespace_separator=' ')
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _forbidden
    parser.EntityDeclHandler = _forbidden
    parser.NotationDeclHandler = _forbidden
    parser.ExternalEntityRefHandler = _no_external
    return parser


class _Text:
    """Bounded character data of the element being read."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.active = False

    def start(self) -> None:
        self.parts, self.length, self.active = [], 0, True

    def add(self, data: str) -> None:
        if not self.active:
            return
        self.length += len(data)
        if self.length > TEXT_MAX:
            raise ValidationFailed('XML text value is too long')
        self.parts.append(data)

    def end(self) -> str:
        self.active = False
        return ''.join(self.parts).strip()


# ---------------------------------------------------------------------------- repomd.xml
class _RepomdHandler:
    def __init__(self) -> None:
        self.depth = 0
        self.root = ''
        self.revision = ''
        self.entries: dict[str, RepomdEntry] = {}
        self.count = 0
        self.text = _Text()
        self._data: dict[str, Any] | None = None

    def start(self, name: str, attrs: dict[str, str]) -> None:
        self.depth += 1
        local = _local(name)
        if self.depth == 1:
            self.root = local
        elif self.depth == 2 and local == 'data':
            self._data = {'type': (attrs.get('type') or '')[:TEXT_MAX]}
        elif self.depth == 2 and local == 'revision':
            self.text.start()
        elif self.depth == 3 and self._data is not None:
            if local == 'checksum':
                self._data['checksum_type'] = attrs.get('type')
                self.text.start()
            elif local == 'location':
                self._data['href'] = None if _is_base(attrs) else _href(attrs.get('href'))
            elif local == 'size':
                self.text.start()

    def end(self, name: str) -> None:
        local = _local(name)
        if self.depth == 2 and local == 'revision':
            self.revision = self.text.end()
        elif self.depth == 3 and self._data is not None:
            if local == 'checksum':
                self._data['checksum'] = self.text.end()
            elif local == 'size':
                self._data['size'] = self.text.end()
        elif self.depth == 2 and local == 'data' and self._data is not None:
            self._finish(self._data)
            self._data = None
        self.depth -= 1

    def _finish(self, data: dict[str, Any]) -> None:
        self.count += 1
        if self.count > REPOMD_MAX_ENTRIES:
            raise ValidationFailed('repomd.xml lists too many files')
        href = data.get('href')
        if not href or not data['type']:
            return
        size_raw = str(data.get('size') or '')
        self.entries[href] = RepomdEntry(
            type=data['type'],
            href=href,
            checksum=checksum(data.get('checksum_type'), data.get('checksum')),
            size=int(size_raw) if size_raw.isdigit() else None,
        )


def parse_repomd(data: bytes) -> Repomd:
    """Parse ``repomd.xml`` (raises ``ValidationFailed`` on malformed or oversized content)."""
    if len(data) > REPOMD_MAX_BYTES:
        raise ValidationFailed('repomd.xml is too large')
    handler = _RepomdHandler()
    parser = _parser()
    parser.StartElementHandler = handler.start
    parser.EndElementHandler = handler.end
    parser.CharacterDataHandler = handler.text.add
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise ValidationFailed(f'repomd.xml is not well-formed XML: {exc}') from None
    if handler.root != 'repomd':
        raise ValidationFailed('not a repomd.xml document')
    return Repomd(revision=handler.revision, entries=handler.entries)


# ---------------------------------------------------------------------------- primary.xml
_PACKAGE_TEXT = frozenset({'name', 'arch', 'checksum'})


class _PrimaryHandler:
    def __init__(self) -> None:
        self.depth = 0
        self.root = ''
        self.packages: list[Package] = []
        self.text = _Text()
        self._pkg: dict[str, Any] | None = None

    def start(self, name: str, attrs: dict[str, str]) -> None:
        self.depth += 1
        local = _local(name)
        if self.depth == 1:
            self.root = local
            if local != 'metadata':
                raise ValidationFailed('not a primary.xml document')
        elif self.depth == 2 and local == 'package':
            self._pkg = {'type': attrs.get('type', 'rpm')}
        elif self.depth == 3 and self._pkg is not None:
            if local in _PACKAGE_TEXT:
                self.text.start()
                if local == 'checksum':
                    self._pkg['checksum_type'] = attrs.get('type')
            elif local == 'version':
                self._pkg['epoch'] = (attrs.get('epoch') or '0')[:TEXT_MAX]
                self._pkg['version'] = (attrs.get('ver') or '')[:TEXT_MAX]
                self._pkg['release'] = (attrs.get('rel') or '')[:TEXT_MAX]
            elif local == 'size':
                self._pkg['size'] = attrs.get('package')
            elif local == 'location':
                self._pkg['href'] = None if _is_base(attrs) else _href(attrs.get('href'))

    def end(self, name: str) -> None:
        local = _local(name)
        if self.depth == 3 and self._pkg is not None and local in _PACKAGE_TEXT:
            self._pkg[local] = self.text.end()
        elif self.depth == 2 and local == 'package' and self._pkg is not None:
            self._finish(self._pkg)
            self._pkg = None
        self.depth -= 1

    def _finish(self, pkg: dict[str, Any]) -> None:
        href = pkg.get('href')
        digest = checksum(pkg.get('checksum_type'), pkg.get('checksum'))
        size = str(pkg.get('size') or '')
        if not href or digest is None or not size.isdigit():
            return  # served unverified rather than trusted
        self.packages.append(
            Package(
                href=href,
                checksum=digest,
                size=int(size),
                name=str(pkg.get('name') or ''),
                epoch=str(pkg.get('epoch') or '0'),
                version=str(pkg.get('version') or ''),
                release=str(pkg.get('release') or ''),
                arch=str(pkg.get('arch') or ''),
            )
        )
        if len(self.packages) > PRIMARY_MAX_PACKAGES:
            raise ValidationFailed('primary.xml lists too many packages')


def parse_primary(data: bytes, suffix: str, *, limit: int = PRIMARY_MAX_BYTES) -> list[Package]:
    """The packages of a (compressed) ``primary.xml``, decompressed and parsed in chunks (bounded by ``limit``)."""
    handler = _PrimaryHandler()
    parser = _parser()
    parser.StartElementHandler = handler.start
    parser.EndElementHandler = handler.end
    parser.CharacterDataHandler = handler.text.add
    try:
        for chunk in decompress_chunks(data, suffix, limit=limit):
            parser.Parse(chunk, False)
        parser.Parse(b'', True)
    except expat.ExpatError as exc:
        raise ValidationFailed(f'primary.xml is not well-formed XML: {exc}') from None
    if handler.root != 'metadata':
        raise ValidationFailed('not a primary.xml document')
    return handler.packages


# ---------------------------------------------------------------------------- decompression
def compression_of(href: str) -> str | None:
    """The compression suffix of a repodata file ('' = uncompressed ``.xml``), or None when unsupported."""
    for suffix in COMPRESSIONS:
        if href.endswith(suffix):
            return suffix
    return '' if href.endswith('.xml') else None


class _Stream(Protocol):
    @property
    def eof(self) -> bool: ...

    @property
    def needs_input(self) -> bool: ...

    def decompress(self, data: bytes, max_length: int = -1) -> bytes: ...


def _decompressor(suffix: str) -> _Stream:
    if suffix == '.xz':
        return lzma.LZMADecompressor(format=lzma.FORMAT_AUTO)
    if suffix == '.bz2':
        return bz2.BZ2Decompressor()
    if suffix == '.zst':
        from compression import zstd

        return zstd.ZstdDecompressor()
    raise ValidationFailed('unsupported repodata compression')


def _gzip_chunks(data: bytes) -> Iterator[bytes]:
    stream = zlib.decompressobj(wbits=47)  # gzip or zlib header
    pending = data
    while True:
        out = stream.decompress(pending, CHUNK)
        pending = stream.unconsumed_tail
        if out:
            yield out
        if stream.eof:
            return
        if not out and not pending:
            raise ValidationFailed('compressed repodata is truncated')


def _stream_chunks(suffix: str, data: bytes) -> Iterator[bytes]:
    stream = _decompressor(suffix)
    out = stream.decompress(data, max_length=CHUNK)
    while True:
        if out:
            yield out
        if stream.eof:
            return
        if stream.needs_input:
            raise ValidationFailed('compressed repodata is truncated')
        out = stream.decompress(b'', max_length=CHUNK)


def decompress_chunks(data: bytes, suffix: str, *, limit: int = PRIMARY_MAX_BYTES) -> Iterator[bytes]:
    """Decompress ``data`` (by file suffix) in chunks of at most ``CHUNK`` bytes; more than ``limit`` bytes in
    total, a truncated or a corrupt stream raise ``ValidationFailed``."""
    if suffix == '':
        if len(data) > limit:
            raise ValidationFailed('repodata file is too large')
        for start in range(0, len(data), CHUNK):
            yield data[start : start + CHUNK]
        return
    chunks = _gzip_chunks(data) if suffix == '.gz' else _stream_chunks(suffix, data)
    total = 0
    try:
        for chunk in chunks:
            total += len(chunk)
            if total > limit:
                raise ValidationFailed('repodata decompresses beyond the size limit')
            yield chunk
    except ValidationFailed:
        raise
    except Exception as exc:  # lzma, bz2 (OSError), zlib, zstd errors: the stream is corrupt
        raise ValidationFailed('repodata does not decompress') from exc
