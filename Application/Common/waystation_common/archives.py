"""Shared guards for reading untrusted archives (ARCHITECTURE §7 "Input hardening").

``tarfile`` reads the data of a pax extended header or a GNU long name/link header into memory whole, recurses once per
chained extension header and keeps every GNU sparse map entry; a tiny compressed tar can therefore exhaust memory or
raise ``RecursionError`` before a reader sees its first member. Every reader of an untrusted tar opens it with
``tarinfo=BoundedTarInfo``, which refuses (``tarfile.ReadError``) extension headers larger than
``MAX_EXTENSION_HEADER``, more than ``MAX_EXTENSION_CHAIN`` chained extension headers and sparse files (no package
format uses them). Compressed tars are opened in a file mode (``r:gz``, ``r:*``), or as ``r|`` over a stream that
bounds its reads, never in a compressed stream mode (``r|gz``, ``r|*``): those inflate a whole compressed buffer per
read, megabytes for gzip and hundreds of megabytes for bzip2 or zstd.

``READ_ERRORS`` are the exceptions the standard library raises for a corrupt or hostile archive or compressed stream
(``zipfile`` raises ``ValueError``, ``NotImplementedError``, ``RuntimeError`` and ``zlib.error`` besides
``BadZipFile``): a reader maps them to its own clean refusal instead of letting them become a 500.
"""

from __future__ import annotations

import lzma
import tarfile
import zipfile
import zlib
from compression import zstd
from typing import Any

MAX_EXTENSION_HEADER = 1024 * 1024
MAX_EXTENSION_CHAIN = 16
_EXTENSIONS = frozenset({tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK, tarfile.XHDTYPE, tarfile.XGLTYPE, b'X'})
_DEPTH = '_waystation_extension_depth'
# tarfile turns this into ``ReadError`` wherever it is raised (not in typeshed).
_Refused: type[tarfile.TarError] = tarfile.SubsequentHeaderError  # type: ignore[attr-defined]

READ_ERRORS: tuple[type[BaseException], ...] = (
    tarfile.TarError,
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    OSError,
    EOFError,
    ValueError,
    NotImplementedError,
    RuntimeError,  # zipfile: encrypted members; RecursionError
    TypeError,  # tarfile stream mode on a truncated gzip header
    zlib.error,
    lzma.LZMAError,
    zstd.ZstdError,
)


class BoundedTarInfo(tarfile.TarInfo):
    """A ``TarInfo`` that bounds extension headers (see the module docstring)."""

    def _proc_member(self, archive: tarfile.TarFile) -> Any:
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise _Refused('sparse files are not supported')
        if self.type not in _EXTENSIONS:
            return super()._proc_member(archive)  # type: ignore[misc]
        if self.size > MAX_EXTENSION_HEADER:
            raise _Refused('an extended header is too large')
        depth = getattr(archive, _DEPTH, 0)
        if depth >= MAX_EXTENSION_CHAIN:
            raise _Refused('too many chained extended headers')
        setattr(archive, _DEPTH, depth + 1)
        try:
            return super()._proc_member(archive)  # type: ignore[misc]
        finally:
            setattr(archive, _DEPTH, depth)

    def _proc_gnusparse_10(self, *_: Any) -> None:
        raise _Refused('sparse files are not supported')
