"""Go checksum database records and the ``h1:`` hashes they hold (``golang.org/x/mod/sumdb/dirhash``).

A ``lookup/<module>@<version>`` answer of a checksum database (``sum.golang.org``) is::

    <record id>
    <module> <version> h1:<base64 sha256>
    <module> <version>/go.mod h1:<base64 sha256>

    <signed tree note>

The ``go.sum`` lines are what Waystation verifies downloads against (``Expected.check``): ``.mod`` files against the
``/go.mod`` line, module ``.zip`` files against the other. ``h1`` (``dirhash.Hash1``) is the sha256 of the sorted
lines ``"<sha256 hex>  <file name>\\n"`` over every file (a ``.mod`` file is hashed as the single file ``go.mod``).
Waystation does not check the note's signature or the record's inclusion in the tree: the go command does that for
its own downloads, through the same proxied database.

Zip hashing reads every member (bounded: ``MAX_FILES`` entries, ``MAX_UNCOMPRESSED`` bytes in total, as the go
command's own limits), never extracts anything to disk.
"""

from __future__ import annotations

import base64
import hashlib
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import BinaryIO

from waystation_common.errors import DigestMismatch, UpstreamError

MAX_LOOKUP = 64 * 1024
MAX_FILES = 100_000
MAX_UNCOMPRESSED = 512 * 1024 * 1024
_CHUNK = 1024 * 1024
_H1 = re.compile(r'^h1:[A-Za-z0-9+/]{43}=$')


@dataclass(frozen=True)
class SumRecord:
    """The ``go.sum`` hashes of one module version (``h1:...``; None when the record has no such line)."""

    zip_h1: str | None
    mod_h1: str | None


def parse_lookup(data: bytes, module: str, version: str) -> SumRecord:
    """The hashes of ``module@version`` in a lookup answer; raises ``UpstreamError`` when it is malformed or names
    another module or version."""
    if len(data) > MAX_LOOKUP:
        raise UpstreamError('checksum database record is too large')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise UpstreamError('checksum database record is not UTF-8') from None
    lines = text.split('\n')
    if not lines or not lines[0].isdigit():
        raise UpstreamError('checksum database record has no record id')
    zip_h1: str | None = None
    mod_h1: str | None = None
    for line in lines[1:]:
        if not line:
            break  # the signed note follows
        parts = line.split(' ')
        if len(parts) != 3 or not _H1.match(parts[2]):
            raise UpstreamError('malformed checksum database record')
        path, ver, h1 = parts
        if path != module or ver not in (version, f'{version}/go.mod'):
            raise UpstreamError('checksum database record is for another module version')
        if ver == version:
            zip_h1 = h1
        else:
            mod_h1 = h1
    if zip_h1 is None and mod_h1 is None:
        raise UpstreamError('checksum database record has no hashes')
    return SumRecord(zip_h1=zip_h1, mod_h1=mod_h1)


def _hash1(lines: list[tuple[bytes, str]]) -> str:
    """``dirhash.Hash1`` from ``(file name bytes, sha256 hex)`` pairs."""
    summary = hashlib.sha256()
    for name, digest in sorted(lines):
        if b'\n' in name:
            raise DigestMismatch('module zip has a file name with a newline')
        summary.update(f'{digest}  '.encode() + name + b'\n')
    return 'h1:' + base64.b64encode(summary.digest()).decode('ascii')


def mod_h1(data: bytes) -> str:
    """``h1`` of a ``.mod`` file (hashed as the single file ``go.mod``)."""
    return _hash1([(b'go.mod', hashlib.sha256(data).hexdigest())])


def _raw_name(info: zipfile.ZipInfo) -> bytes:
    """The member name as stored (Python decodes names without the UTF-8 flag as cp437)."""
    if info.flag_bits & 0x800:
        return info.orig_filename.encode('utf-8')
    return info.orig_filename.encode('cp437')


def zip_h1(file: BinaryIO) -> str:
    """``h1`` of a module zip (``dirhash.HashZip``): every member, hashed as it is decompressed. Blocking."""
    try:
        archive = zipfile.ZipFile(file)
    except zipfile.BadZipFile, OSError, ValueError:
        raise DigestMismatch('not a valid module zip') from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise DigestMismatch('module zip has too many files')
        members: dict[bytes, zipfile.ZipInfo] = {}
        for info in infos:
            members[_raw_name(info)] = info  # the last entry of a duplicated name wins, as in Go's map
        lines: list[tuple[bytes, str]] = []
        total = 0
        for info in infos:
            name = _raw_name(info)
            digest = hashlib.sha256()
            try:
                with archive.open(members[name]) as member:
                    while chunk := member.read(_CHUNK):
                        total += len(chunk)
                        if total > MAX_UNCOMPRESSED:
                            raise DigestMismatch('module zip is too large uncompressed')
                        digest.update(chunk)
            except zipfile.BadZipFile, OSError, ValueError, NotImplementedError, EOFError:
                raise DigestMismatch('module zip is corrupt') from None
            lines.append((name, digest.hexdigest()))
    return _hash1(lines)


def mod_check(expected_h1: str) -> Callable[[BinaryIO], None]:
    """An ``Expected.check`` for a ``.mod`` file."""

    def check(file: BinaryIO) -> None:
        if mod_h1(file.read(MAX_UNCOMPRESSED)) != expected_h1:
            raise DigestMismatch('go.mod does not match the checksum database')

    return check


def zip_check(expected_h1: str) -> Callable[[BinaryIO], None]:
    """An ``Expected.check`` for a module zip."""

    def check(file: BinaryIO) -> None:
        if zip_h1(file) != expected_h1:
            raise DigestMismatch('module zip does not match the checksum database')

    return check
