"""The ``h1:`` hash of a provider package (Terraform ``getproviders.PackageHashV1``: ``dirhash.HashZip`` with
``dirhash.Hash1``): sha256 over the sorted lines ``"<sha256 hex>  <member name>\\n"`` of every zip member, base64.

Computed from a verified archive (its sha256 matched the signed ``SHA256SUMS``) while it is committed, so the
mirror's version document can list it next to ``zh:``. Bounded (``MAX_FILES``, ``MAX_UNCOMPRESSED``); never extracts
to disk. Blocking: run in a thread.
"""

from __future__ import annotations

import base64
import hashlib
import zipfile
from typing import BinaryIO

MAX_FILES = 10_000
MAX_UNCOMPRESSED = 2 * 1024 * 1024 * 1024
_CHUNK = 1024 * 1024


class NotHashable(ValueError):  # noqa: N818 - reads as a state
    """The archive is not a zip, is corrupt, or exceeds the bounds."""


def _raw_name(info: zipfile.ZipInfo) -> bytes:
    """The member name as stored (Python decodes names without the UTF-8 flag as cp437)."""
    if info.flag_bits & 0x800:
        return info.orig_filename.encode('utf-8')
    return info.orig_filename.encode('cp437')


def zip_h1(file: BinaryIO) -> str:
    try:
        archive = zipfile.ZipFile(file)
    except zipfile.BadZipFile, OSError, ValueError:
        raise NotHashable('not a zip archive') from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise NotHashable('too many files')
        members: dict[bytes, zipfile.ZipInfo] = {}
        for info in infos:
            members[_raw_name(info)] = info  # a duplicated name: the last entry wins, as in Go's map
        lines: list[tuple[bytes, str]] = []
        total = 0
        for info in infos:
            name = _raw_name(info)
            if b'\n' in name:
                raise NotHashable('a member name has a newline')
            digest = hashlib.sha256()
            try:
                with archive.open(members[name]) as member:
                    while chunk := member.read(_CHUNK):
                        total += len(chunk)
                        if total > MAX_UNCOMPRESSED:
                            raise NotHashable('too large uncompressed')
                        digest.update(chunk)
            except NotHashable:
                raise
            except zipfile.BadZipFile, OSError, ValueError, NotImplementedError, EOFError:
                raise NotHashable('corrupt zip archive') from None
            lines.append((name, digest.hexdigest()))
    summary = hashlib.sha256()
    for name, hexdigest in sorted(lines):
        summary.update(f'{hexdigest}  '.encode() + name + b'\n')
    return 'h1:' + base64.b64encode(summary.digest()).decode('ascii')
