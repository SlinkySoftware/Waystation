"""Reading an uploaded ``.deb`` safely (hosted apt, T-P5-APTH; FORMATS.md "apt").

A binary package is an ``ar`` archive: ``debian-binary`` (``2.x``), ``control.tar[.gz|.xz|.zst]``, then
``data.tar[...]`` (members whose names start with ``_`` may sit between them). Only the ``control`` file of the
control archive is read; the data archive is located and size-checked, never unpacked. Untrusted input, so:

- ``ar``: the global header, at most ``MAX_MEMBERS`` members, every header's fields (decimal size, terminator) and
  every member's extent checked against the file;
- compression: the control archive is at most ``CONTROL_TAR_MAX`` bytes compressed and decompresses to at most
  ``CONTROL_TAR_MAX`` (gzip, xz, zstd or none; a truncated or trailing-garbage stream is refused);
- tar: at most ``MAX_TAR_MEMBERS`` members, names relative without ``..`` (nothing is extracted), the ``control``
  member a regular file of at most ``CONTROL_MAX`` bytes;
- control file: one UTF-8 paragraph, no control characters, unique field names, ``Package``/``Version``/
  ``Architecture`` present and well-formed (Debian policy §5.6).

``read_deb`` is blocking (file reads, decompression, hashing the whole file for ``MD5sum``/``SHA1``/``SHA512``):
call it in a thread. Every refusal raises ``ValidationFailed`` (400).
"""

from __future__ import annotations

import bz2
import hashlib
import io
import lzma
import re
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from waystation_common.errors import ValidationFailed

AR_MAGIC = b'!<arch>\n'
AR_HEADER = 60
MAX_MEMBERS = 16
CONTROL_TAR_MAX = 16 * 1024 * 1024
CONTROL_MAX = 1024 * 1024
MAX_TAR_MEMBERS = 10_000
CONTROL_SUFFIXES = ('', '.gz', '.xz', '.zst')
DATA_SUFFIXES = ('', '.gz', '.xz', '.zst', '.bz2', '.lzma')
CHUNK = 1024 * 1024

PACKAGE = re.compile(r'^[a-z0-9][a-z0-9+.-]{1,127}$')
VERSION = re.compile(r'^(?:[0-9]{1,9}:)?[A-Za-z0-9][A-Za-z0-9.+~-]{0,127}$')
ARCHITECTURE = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
FIELD = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0a-\x1f\x7f]')
# Fields Waystation writes into the Packages paragraph itself (an uploaded control file cannot set them).
GENERATED = frozenset({'filename', 'size', 'md5sum', 'sha1', 'sha256', 'sha512', 'description-md5'})


@dataclass(frozen=True)
class Deb:
    """What an uploaded package says about itself, and the digests of the whole file."""

    fields: tuple[tuple[str, str], ...]  # the control file's fields in order (value: continuation lines kept)
    package: str
    version: str
    architecture: str
    source: str  # the source package name (``Source:``, without a version), else ``package``
    section: str | None
    size: int
    md5: str
    sha1: str
    sha256: str
    sha512: str

    @property
    def filename(self) -> str:
        """``<name>_<version without epoch>_<arch>.deb``, as Debian names pool files."""
        return f'{self.package}_{self.version.split(":", 1)[-1]}_{self.architecture}.deb'

    def pool_path(self, component: str) -> str:
        """``pool/<component>/<prefix>/<source>/<file>`` (prefix ``libx`` for ``lib*`` sources, else the first
        letter), Debian's pool layout."""
        name = self.source
        prefix = name[:4] if name.startswith('lib') and len(name) > 3 else name[:1]
        return f'pool/{component}/{prefix}/{name}/{self.filename}'

    def stanza(self, path: str) -> str:
        """The package's ``Packages`` paragraph: ``Package`` first, the control fields in their order, then the
        file fields (before ``Description``, which stays last as in Debian's indexes). Ends with a newline."""
        file_fields = [
            ('Filename', path),
            ('Size', str(self.size)),
            ('MD5sum', self.md5),
            ('SHA1', self.sha1),
            ('SHA256', self.sha256),
            ('SHA512', self.sha512),
        ]
        out = [('Package', self.package)]
        description: tuple[str, str] | None = None
        for name, value in self.fields:
            lower = name.lower()
            if lower == 'package':
                continue
            if lower == 'description':
                description = (name, value)
                continue
            out.append((name, value))
        out += file_fields
        if description is not None:
            out.append(description)
        return ''.join(f'{name}:{"" if not value or value[0] == chr(10) else " "}{value}\n' for name, value in out)


# ---------------------------------------------------------------------------- ar
@dataclass(frozen=True)
class _Member:
    name: str
    offset: int
    size: int


def _members(fh: BinaryIO, total: int) -> list[_Member]:
    if fh.read(len(AR_MAGIC)) != AR_MAGIC:
        raise ValidationFailed('not a Debian package (no ar archive header)')
    members: list[_Member] = []
    offset = len(AR_MAGIC)
    while offset < total:
        if len(members) >= MAX_MEMBERS:
            raise ValidationFailed('the package archive has too many members')
        fh.seek(offset)
        header = fh.read(AR_HEADER)
        if len(header) != AR_HEADER or header[58:60] != b'`\n':
            raise ValidationFailed('the package archive has a damaged member header')
        name = header[:16].decode('ascii', errors='replace').rstrip(' ').removesuffix('/')
        size_text = header[48:58].decode('ascii', errors='replace').strip()
        if not size_text.isdigit() or not name or '/' in name:
            raise ValidationFailed('the package archive has a damaged member header')
        size = int(size_text)
        start = offset + AR_HEADER
        if start + size > total:
            raise ValidationFailed('the package archive is truncated')
        members.append(_Member(name, start, size))
        offset = start + size + (size & 1)  # members are padded to an even offset
    return members


def _layout(members: list[_Member], version: bytes) -> tuple[_Member, str]:
    """The control member and its compression suffix; checks the member order dpkg requires and the format
    version (``version``: the start of the first member)."""
    if not members or members[0].name != 'debian-binary' or not version.startswith(b'2.'):
        raise ValidationFailed('not a Debian binary package (debian-binary 2.x must come first)')
    rest = [m for m in members[1:] if not m.name.startswith('_')]
    if len(rest) != 2 or not rest[0].name.startswith('control.tar') or not rest[1].name.startswith('data.tar'):
        raise ValidationFailed('a Debian package holds control.tar and data.tar, in this order')
    control, data = rest
    control_suffix = control.name.removeprefix('control.tar')
    if control_suffix not in CONTROL_SUFFIXES:
        raise ValidationFailed(f'unsupported control archive compression {control.name}')
    if data.name.removeprefix('data.tar') not in DATA_SUFFIXES:
        raise ValidationFailed(f'unsupported data archive compression {data.name}')
    if control.size > CONTROL_TAR_MAX:
        raise ValidationFailed('the control archive is too large')
    return control, control_suffix


# ---------------------------------------------------------------------------- compression and tar
def decompress(data: bytes, suffix: str, *, limit: int = CONTROL_TAR_MAX) -> bytes:
    """``data`` decompressed (by member suffix) to at most ``limit`` bytes; larger, truncated or trailing data is
    refused."""
    if suffix == '':
        return data
    try:
        if suffix == '.gz':
            gz = zlib.decompressobj(wbits=31)
            out = gz.decompress(data, limit + 1)
            done, rest = gz.eof, gz.unused_data or gz.unconsumed_tail
        elif suffix in ('.xz', '.lzma', '.bz2', '.zst'):
            if suffix == '.zst':
                from compression import zstd

                stream: zstd.ZstdDecompressor | lzma.LZMADecompressor | bz2.BZ2Decompressor = zstd.ZstdDecompressor()
            elif suffix == '.bz2':
                stream = bz2.BZ2Decompressor()
            else:
                stream = lzma.LZMADecompressor(format=lzma.FORMAT_XZ if suffix == '.xz' else lzma.FORMAT_ALONE)
            out = stream.decompress(data, max_length=limit + 1)
            done, rest = stream.eof, stream.unused_data
        else:
            raise ValidationFailed('unsupported compression')
    except ValidationFailed:
        raise
    except Exception as exc:  # zlib.error, LZMAError, OSError, EOFError, ZstdError
        raise ValidationFailed('the control archive does not decompress') from exc
    if len(out) > limit:
        raise ValidationFailed('the control archive decompresses beyond the size limit')
    if not done or rest:
        raise ValidationFailed('the control archive is truncated or has trailing data')
    return out


def _safe_member(name: str) -> bool:
    parts = [p for p in name.split('/') if p not in ('', '.')]
    return not name.startswith('/') and '..' not in parts and '\x00' not in name


def control_file(tar_bytes: bytes) -> bytes:
    """The ``control`` file of an uncompressed control archive (read, never extracted)."""
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode='r:') as tar:
            found: bytes | None = None
            count = 0
            while (member := tar.next()) is not None:
                count += 1
                if count > MAX_TAR_MEMBERS:
                    raise ValidationFailed('the control archive has too many members')
                if not _safe_member(member.name):
                    raise ValidationFailed('the control archive has an unsafe member name')
                if member.name.removeprefix('./') != 'control' or found is not None:
                    continue
                if not member.isreg() or member.size > CONTROL_MAX:
                    raise ValidationFailed('the control file is not a regular file of at most 1 MiB')
                handle = tar.extractfile(member)
                if handle is None:
                    raise ValidationFailed('the control file cannot be read')
                found = handle.read(CONTROL_MAX + 1)
    except tarfile.TarError as exc:
        raise ValidationFailed('the control archive is not a valid tar archive') from exc
    if found is None:
        raise ValidationFailed('the package has no control file')
    return found


# ---------------------------------------------------------------------------- control file
def parse_control(data: bytes) -> tuple[tuple[str, str], ...]:
    """The fields of a binary control file in order (one paragraph; continuation lines kept, newline-joined)."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise ValidationFailed('the control file is not UTF-8') from None
    fields: list[tuple[str, str]] = []
    seen: set[str] = set()
    ended = False
    for raw in text.split('\n'):
        line = raw.removesuffix('\r')
        if not line.strip():
            if fields:
                ended = True
            continue
        if ended:
            raise ValidationFailed('the control file has more than one paragraph')
        if _CONTROL_CHARS.search(line):
            raise ValidationFailed('the control file contains control characters')
        if line[0] in ' \t':
            if not fields:
                raise ValidationFailed('the control file starts with a continuation line')
            name, value = fields[-1]
            fields[-1] = (name, f'{value}\n{line}')
            continue
        if line.startswith('#'):
            continue
        name, sep, value = line.partition(':')
        if not sep or not FIELD.match(name):
            raise ValidationFailed(f'the control file has an invalid line: {line[:60]!r}')
        if name.lower() in seen:
            raise ValidationFailed(f'the control file repeats the field {name}')
        seen.add(name.lower())
        if name.lower() not in GENERATED:
            fields.append((name, value.strip()))
    return tuple(fields)


def _field(fields: tuple[tuple[str, str], ...], name: str) -> str | None:
    for key, value in fields:
        if key.lower() == name:
            return value
    return None


def describe(fields: tuple[tuple[str, str], ...]) -> tuple[str, str, str, str, str | None]:
    """(package, version, architecture, source name, section) of a control file, checked."""
    package = _field(fields, 'package') or ''
    version = _field(fields, 'version') or ''
    architecture = _field(fields, 'architecture') or ''
    if not PACKAGE.match(package):
        raise ValidationFailed('the control file has no valid Package field')
    if not VERSION.match(version) or version.endswith('-') or version.count(':') > 1:
        raise ValidationFailed('the control file has no valid Version field')
    if not ARCHITECTURE.match(architecture):
        raise ValidationFailed('the control file has no valid Architecture field (one architecture, or all)')
    source = (_field(fields, 'source') or package).split(' ', 1)[0]
    if not PACKAGE.match(source):
        raise ValidationFailed('the control file has an invalid Source field')
    section = _field(fields, 'section') or None
    return package, version, architecture, source, section


# ---------------------------------------------------------------------------- the whole package
def read_deb(path: Path, *, sha256: str) -> Deb:
    """Check and describe the package file at ``path`` (its blob; ``sha256`` as committed). Blocking."""
    md5 = hashlib.md5(usedforsecurity=False)
    sha1 = hashlib.sha1(usedforsecurity=False)
    sha512 = hashlib.sha512()
    with path.open('rb') as fh:
        total = fh.seek(0, io.SEEK_END)
        fh.seek(0)
        members = _members(fh, total)
        fh.seek(members[0].offset if members else 0)
        control, suffix = _layout(members, fh.read(min(members[0].size, 16)) if members else b'')
        fh.seek(0)
        while chunk := fh.read(CHUNK):
            md5.update(chunk)
            sha1.update(chunk)
            sha512.update(chunk)
        fh.seek(control.offset)
        compressed = fh.read(control.size)
    fields = parse_control(control_file(decompress(compressed, suffix)))
    package, version, architecture, source, section = describe(fields)
    return Deb(
        fields=fields,
        package=package,
        version=version,
        architecture=architecture,
        source=source,
        section=section,
        size=total,
        md5=md5.hexdigest(),
        sha1=sha1.hexdigest(),
        sha256=sha256,
        sha512=sha512.hexdigest(),
    )
