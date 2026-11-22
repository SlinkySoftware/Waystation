"""Hostile archive and document builders for the security corpora (T-R01). Everything is built in memory, small on the
wire and cheap to make: bombs are streams of zeros compressed chunk by chunk, so a test never holds the expanded
data itself.
"""

from __future__ import annotations

import bz2
import io
import itertools
import lzma
import random
import struct
import tarfile
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence

_MiB = 1024 * 1024
Member = tarfile.TarInfo | tuple[str, bytes]


# ---------------------------------------------------------------------------- compression
def _compressor(kind: str) -> Callable[[Iterable[bytes]], bytes]:
    def run(chunks: Iterable[bytes]) -> bytes:
        if kind == '':
            return b''.join(chunks)
        if kind == 'gz':
            engine: zlib._Compress | bz2.BZ2Compressor | lzma.LZMACompressor = zlib.compressobj(9, wbits=31)
        elif kind == 'bz2':
            engine = bz2.BZ2Compressor(9)
        elif kind == 'xz':
            engine = lzma.LZMACompressor(format=lzma.FORMAT_XZ, preset=1)
        elif kind == 'zst':
            from compression import zstd

            zengine = zstd.ZstdCompressor(level=3)
            out = b''.join(zengine.compress(c) for c in chunks)
            return out + zengine.flush()
        elif kind == 'zlib':
            engine = zlib.compressobj(9)
        else:
            raise ValueError(kind)
        out = b''.join(engine.compress(c) for c in chunks)
        return out + engine.flush()

    return run


def compress(data: bytes, kind: str) -> bytes:
    """``data`` compressed as ``gz``, ``bz2``, ``xz``, ``zst``, ``zlib`` or not at all (``''``)."""
    return _compressor(kind)([data])


def zeros(size: int, chunk: int = _MiB) -> Iterator[bytes]:
    block = bytes(chunk)
    left = size
    while left > 0:
        yield block[: min(left, chunk)]
        left -= chunk


def bomb(size: int, kind: str, prefix: bytes = b'') -> bytes:
    """A tiny stream that decompresses to ``prefix`` and then ``size`` zero bytes."""
    return _compressor(kind)(itertools.chain([prefix], zeros(size)))


# ---------------------------------------------------------------------------- tar
def _header(info: tarfile.TarInfo, fmt: int = tarfile.GNU_FORMAT) -> bytes:
    """One raw 512-byte header (no extension headers are added for long names)."""
    return info._create_header(info.get_info(), fmt, 'utf-8', 'surrogateescape')  # type: ignore[attr-defined]


def info(name: str, data: bytes = b'', *, kind: bytes = tarfile.REGTYPE, link: str = '') -> tarfile.TarInfo:
    item = tarfile.TarInfo(name)
    item.type = kind
    item.size = len(data) if kind == tarfile.REGTYPE else 0
    item.linkname = link
    item.mtime = 0
    return item


def tar(members: Sequence[Member], kind: str = 'gz', fmt: int = tarfile.GNU_FORMAT) -> bytes:
    """A tar of ``(name, data)`` pairs and ``TarInfo`` entries (no data), compressed as ``kind``."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w', format=fmt) as out:
        for member in members:
            if isinstance(member, tarfile.TarInfo):
                out.addfile(member)
            else:
                name, data = member
                out.addfile(info(name, data), io.BytesIO(data))
    return compress(raw.getvalue(), kind)


def raw_tar(entries: Sequence[tuple[str, bytes]], kind: str = 'gz') -> bytes:
    """A tar whose names are written verbatim (no normalisation by ``tarfile``): NUL, backslashes, ``..``."""
    out = bytearray()
    for name, data in entries:
        item = info('x', data)
        block = bytearray(_header(item, tarfile.USTAR_FORMAT))
        encoded = name.encode('utf-8', 'surrogateescape')[:100]
        block[0:100] = encoded.ljust(100, b'\0')
        block[148:156] = b' ' * 8
        block[148:156] = f'{sum(block):06o}\0 '.encode()
        out += block + data + bytes(-len(data) % 512)
    out += bytes(1024)
    return compress(bytes(out), kind)


def pax_bomb(size: int, kind: str = 'gz', *, gnu_longname: bool = False) -> bytes:
    """A tar whose first member is a pax extended header (or a GNU long name) of ``size`` bytes of digits: a reader
    that loads extension headers whole holds ``size`` bytes for a tiny download."""
    item = info('././@PaxHeader')
    item.type = tarfile.GNUTYPE_LONGNAME if gnu_longname else tarfile.XHDTYPE
    item.size = size
    return _compressor(kind)(itertools.chain([_header(item)], (b'1' * len(c) for c in zeros(size))))


def longname_chain(count: int, kind: str = 'gz', *, pax: bool = False) -> bytes:
    """``count`` chained extension headers before one file: ``tarfile`` recurses once per header."""
    out = bytearray()
    for _ in range(count):
        item = info('././@LongLink')
        if pax:
            item.type, payload = tarfile.XHDTYPE, b'10 a=bbbb\n'
        else:
            item.type, payload = tarfile.GNUTYPE_LONGNAME, b'name\0'
        item.size = len(payload)
        out += _header(item) + payload.ljust(512, b'\0')
    out += _header(info('file')) + bytes(1024)
    return compress(bytes(out), kind)


def sparse_bomb(blocks: int, kind: str = 'gz') -> bytes:
    """An old GNU sparse member followed by ``blocks`` extension blocks of sparse map entries (each block marks the
    next as extended): ``tarfile`` keeps every entry in memory while reading the header."""
    item = info('sparse')
    item.type = tarfile.GNUTYPE_SPARSE
    head = bytearray(_header(item))
    head[482] = 1  # isextended
    head[148:156] = b' ' * 8
    head[148:156] = f'{sum(head):06o}\0 '.encode()
    entry = b'%011o\0%011o\0' % (1, 1)
    block = (entry * 21).ljust(504, b'\0') + b'\x01' + bytes(7)

    def chunks() -> Iterator[bytes]:
        yield bytes(head)
        group = block * 2048
        for _ in range(blocks // 2048):
            yield group

    return _compressor(kind)(chunks())


def many_tar_entries(count: int, kind: str = 'gz', prefix: str = 'pkg/') -> bytes:
    """``count`` empty regular files (one 512-byte header each), streamed to the compressor."""

    def chunks() -> Iterator[bytes]:
        for i in range(count):
            yield _header(info(f'{prefix}f{i}'))
        yield bytes(1024)

    return _compressor(kind)(chunks())


# ---------------------------------------------------------------------------- zip
def zip_bytes(entries: Sequence[tuple[str, bytes]], *, method: int = zipfile.ZIP_DEFLATED) -> bytes:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w', compression=method) as out:
        for name, data in entries:
            out.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), data, compress_type=method)
    return raw.getvalue()


def zip_bomb(name: str, size: int, others: Sequence[tuple[str, bytes]] = (), *, declared: int | None = None) -> bytes:
    """A zip whose member ``name`` inflates to ``size`` zero bytes; ``declared`` (when given) is the uncompressed
    size written in its headers instead (a lying header)."""
    raw = bytes(zip_bytes([*others, (name, b'')]))
    deflated = bomb(size, 'zlib')[2:-4]  # raw deflate (no zlib header or adler32)
    crc = 0
    for chunk in zeros(size):
        crc = zlib.crc32(chunk, crc)
    return rewrite_member(raw, name, deflated, crc=crc, size=size if declared is None else declared)


def rewrite_member(archive: bytes, name: str, payload: bytes, *, crc: int, size: int) -> bytes:
    """Replace the (deflated) data of ``name`` in a zip made by ``zip_bytes``, fixing every offset."""
    source = zipfile.ZipFile(io.BytesIO(archive))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as target:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == name:
                item.compress_type = zipfile.ZIP_DEFLATED
                target.writestr(item, b'')
                continue
            target.writestr(item, data)
    raw = bytearray(out.getvalue())
    # Splice the payload into the (empty) member, then patch its local and central headers.
    target_info = zipfile.ZipFile(io.BytesIO(bytes(raw))).getinfo(name)
    local = target_info.header_offset
    name_len, extra_len = struct.unpack('<HH', raw[local + 26 : local + 30])
    data_at = local + 30 + name_len + extra_len
    old_len = target_info.compress_size
    raw[data_at : data_at + old_len] = payload
    delta = len(payload) - old_len
    struct.pack_into('<III', raw, local + 14, crc, len(payload), size)
    # The central directory follows every member: patch the entry for ``name`` and the offsets after it.
    end = raw.rfind(b'PK\x05\x06')
    cd_offset = struct.unpack('<I', raw[end + 16 : end + 20])[0]
    cd_offset += delta
    struct.pack_into('<I', raw, end + 16, cd_offset)
    pos = cd_offset
    while raw[pos : pos + 4] == b'PK\x01\x02':
        n, e, c = struct.unpack('<HHH', raw[pos + 28 : pos + 34])
        entry_name = bytes(raw[pos + 46 : pos + 46 + n]).decode('utf-8', 'replace')
        offset = struct.unpack('<I', raw[pos + 42 : pos + 46])[0]
        if entry_name == name:
            struct.pack_into('<III', raw, pos + 16, crc, len(payload), size)
        elif offset > local:
            struct.pack_into('<I', raw, pos + 42, offset + delta)
        pos += 46 + n + e + c
    return bytes(raw)


def many_zip_entries(count: int, prefix: str = '') -> bytes:
    return zip_bytes([(f'{prefix}f{i}', b'') for i in range(count)], method=zipfile.ZIP_STORED)


def encrypted(archive: bytes, name: str) -> bytes:
    """``archive`` with the encryption flag set on ``name`` (local and central headers)."""
    raw = bytearray(archive)
    for sig, flag_at, name_at, len_at in ((b'PK\x03\x04', 6, 30, 26), (b'PK\x01\x02', 8, 46, 28)):
        pos = raw.find(sig)
        while pos >= 0:
            n = struct.unpack('<H', raw[pos + len_at : pos + len_at + 2])[0]
            if bytes(raw[pos + name_at : pos + name_at + n]) == name.encode():
                raw[pos + flag_at] |= 1
            pos = raw.find(sig, pos + 4)
    return bytes(raw)


# ---------------------------------------------------------------------------- damage
def mutations(data: bytes, *, seed: int = 7, flips: int = 120, cuts: int = 40) -> Iterator[bytes]:
    """Truncations and single-byte corruptions of ``data`` (deterministic)."""
    rng = random.Random(seed)
    size = len(data)
    for i in range(1, cuts + 1):
        yield data[: size * i // (cuts + 1)]
    yield b''
    yield data[:-1]
    for _ in range(flips):
        pos = rng.randrange(size)
        yield data[:pos] + bytes([data[pos] ^ (1 << rng.randrange(8))]) + data[pos + 1 :]
    for _ in range(flips // 4):  # runs of garbage
        pos = rng.randrange(size)
        yield data[:pos] + rng.randbytes(16) + data[pos + 16 :]


# ---------------------------------------------------------------------------- documents
def nested_json(depth: int) -> bytes:
    return b'{"a":' * depth + b'1' + b'}' * depth


def yaml_merge_bomb(levels: int) -> bytes:
    """Each level merges the previous one twice (``!!merge``): exponential work for a loader that flattens merges."""
    lines = ['a0: &a0 {k: v}']
    lines += [f'a{i}: &a{i} {{!!merge <<: [*a{i - 1}, *a{i - 1}]}}' for i in range(1, levels)]
    return ('\n'.join(lines) + '\n').encode()


def yaml_laughs(levels: int = 9, width: int = 9) -> bytes:
    """The billion laughs: every level is a list of ``width`` aliases of the previous one."""
    lines = ['l0: &l0 [lol]']
    for i in range(1, levels):
        lines.append(f'l{i}: &l{i} [{", ".join([f"*l{i - 1}"] * width)}]')
    return ('\n'.join(lines) + '\n').encode()


def xml_laughs(root: str = 'package', inner: str = '') -> bytes:
    """The billion laughs as an XML internal entity (and an external one)."""
    entities = '<!ENTITY a0 "lol">' + ''.join(f'<!ENTITY a{i} "{f"&a{i - 1};" * 10}">' for i in range(1, 10))
    return (
        f'<?xml version="1.0"?><!DOCTYPE {root} [{entities}'
        f'<!ENTITY ext SYSTEM "file:///etc/passwd">]><{root}>{inner}&a9;&ext;</{root}>'
    ).encode()
