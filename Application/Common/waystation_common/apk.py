"""Alpine apk (v2) package and index formats, shared by the server (proxy, hosted uploads, browse) and the worker
(hosted ``APKINDEX.tar.gz`` generation). FORMATS.md "Alpine apk".

- **Versions** (apk-tools ``version.c``): ``<digits>{.<digits>}[<letter>]{_<suffix>[<digits>]}[-r<digits>]``;
  ``_alpha`` < ``_beta`` < ``_pre`` < ``_rc`` < (release) < ``_cvs`` < ``_svn`` < ``_git`` < ``_hg`` < ``_p``.
  ``compare`` ports apk's token comparison (leading zeros after a dot included); ``sort_string`` encodes the same
  order as a string (the ``version_sort`` contract).
- **Packages** (``.apk``, v2): concatenated gzip streams: an optional signature segment (a tar of ``.SIGN.*`` files,
  no end-of-archive), the control segment (a tar with ``.PKGINFO``, no end-of-archive) and the data segment (a
  tar.gz). The index checksum ``C:`` is ``Q1`` + base64 SHA-1 of the *compressed* control segment; ``.PKGINFO``
  ``datahash`` is the SHA-256 of the compressed data segment, so a package whose control segment matches its index
  entry is verified as a whole. Read with ``read_package`` (bounded; nothing extracted).
- **Indexes** (``APKINDEX.tar.gz``): a signature segment followed by a tar.gz of ``DESCRIPTION`` and ``APKINDEX``
  (stanzas of ``X:value`` lines separated by blank lines). ``parse_index`` reads one (bounded); ``build_index``
  writes one, signed with ``sign`` callbacks (RSA PKCS#1 v1.5 over the SHA-256 of the index tar.gz:
  ``.SIGN.RSA256.<key name>``).

Blocking CPU work: call it in a thread.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import tarfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import BinaryIO

INDEX_NAME = 'APKINDEX.tar.gz'
INDEX_MAX = 64 * 1024 * 1024  # compressed APKINDEX.tar.gz
INDEX_TEXT_MAX = 512 * 1024 * 1024  # decompressed
INDEX_ENTRIES_MAX = 1_000_000
SIGNATURE_MAX = 1024 * 1024  # decompressed signature segment
CONTROL_MAX = 16 * 1024 * 1024  # decompressed control segment
PKGINFO_MAX = 1024 * 1024
MEMBERS_MAX = 1000
FIELD_MAX = 64 * 1024
CHUNK = 64 * 1024

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$')
ARCH = re.compile(r'^[a-z0-9_]{1,32}$')
SEGMENT = re.compile(r'^[A-Za-z0-9._-]{1,64}$')  # branch and repository names
VERSION = re.compile(r'^[0-9]+(\.[0-9]+)*[a-z]?(_(alpha|beta|pre|rc|cvs|svn|git|hg|p)[0-9]*)*(-r[0-9]+)?$')
CHECKSUM = re.compile(r'^Q1[A-Za-z0-9+/]{27}=$')


class ApkError(ValueError):
    """Not a valid apk package or index (the message says why)."""


# ---------------------------------------------------------------------------- versions
_DIGIT_OR_ZERO, _DIGIT, _LETTER, _SUFFIX, _SUFFIX_NO, _REVISION_NO, _END, _INVALID = 0, 1, 2, 3, 4, 5, 6, -1
_PRE = ('alpha', 'beta', 'pre', 'rc')
_POST = ('cvs', 'svn', 'git', 'hg', 'p')
_DIGITS = '0123456789'


def _next_type(kind: int, s: str, pos: int) -> tuple[int, int]:
    if pos >= len(s):
        return _END, pos
    c = s[pos]
    if kind in (_DIGIT, _DIGIT_OR_ZERO) and 'a' <= c <= 'z':
        n = _LETTER
    elif kind == _LETTER and c in _DIGITS:
        n = _DIGIT
    elif kind == _SUFFIX and c in _DIGITS:
        n = _SUFFIX_NO
    else:
        n = _INVALID
        if c == '.':
            n = _DIGIT_OR_ZERO
        elif c == '_':
            n = _SUFFIX
        elif c == '-' and pos + 1 < len(s) and s[pos + 1] == 'r':
            n = _REVISION_NO
            pos += 1
        pos += 1
    if n < kind and not (
        (n == _DIGIT_OR_ZERO and kind == _DIGIT)
        or (n == _SUFFIX and kind == _SUFFIX_NO)
        or (n == _DIGIT and kind == _LETTER)
    ):
        n = _INVALID
    return n, pos


def _get_token(kind: int, s: str, pos: int) -> tuple[int, int, int]:
    """(value, next kind, next position) of the token of ``kind`` at ``pos`` (apk ``get_token``)."""
    if pos >= len(s):
        return 0, _END, pos
    i, value, forced = pos, 0, _INVALID
    if kind == _DIGIT_OR_ZERO and s[i] == '0':
        while i + 1 < len(s) and s[i + 1] == '0':  # every zero but the last; the last starts the next number
            i += 1
        forced, value = _DIGIT, -(i - pos)
    elif kind in (_DIGIT_OR_ZERO, _DIGIT, _SUFFIX_NO, _REVISION_NO):
        while i < len(s) and s[i] in _DIGITS and i - pos < 30:
            value = value * 10 + ord(s[i]) - 48
            i += 1
    elif kind == _LETTER:
        value = ord(s[i])
        i += 1
    elif kind == _SUFFIX:
        for k, name in enumerate(_PRE):
            if s.startswith(name, i):
                value, i = k - len(_PRE), i + len(name)
                break
        else:
            for k, name in enumerate(_POST):
                if s.startswith(name, i):
                    value, i = k, i + len(name)
                    break
            else:
                return -1, _INVALID, pos
    else:
        return -1, _INVALID, pos
    if i >= len(s):
        return value, _END, i
    if forced != _INVALID:
        return value, forced, i
    nxt, i = _next_type(kind, s, i)
    return value, nxt, i


def _tokens(version: str) -> tuple[list[tuple[int, int]], int]:
    """The (kind, value) tokens of a version and the kind that ended it (``_END`` when valid)."""
    out: list[tuple[int, int]] = []
    kind, pos = _DIGIT, 0
    while kind not in (_END, _INVALID) and len(out) < 200:
        value, nxt, pos = _get_token(kind, version, pos)
        out.append((kind, value))
        kind = nxt
    return out, kind


def valid_version(version: str) -> bool:
    """An apk version as ``apk_version_validate`` accepts it, starting with a digit and of sane length."""
    return 0 < len(version) <= 128 and VERSION.match(version) is not None and _tokens(version)[1] == _END


def compare(a: str, b: str) -> int:
    """apk version comparison (-1, 0, 1), ported from apk-tools ``apk_version_compare_blob``."""
    at = bt = _DIGIT
    av = bv = 0
    pa = pb = 0
    while at == bt and at not in (_END, _INVALID) and av == bv:
        av, nat, pa = _get_token(at, a, pa)
        bv, nbt, pb = _get_token(bt, b, pb)
        at, bt = nat, nbt
    if av < bv:
        return -1
    if av > bv:
        return 1
    if at == bt:
        return 0
    if at == _SUFFIX and _get_token(at, a, pa)[0] < 0:
        return -1
    if bt == _SUFFIX and _get_token(bt, b, pb)[0] < 0:
        return 1
    if at > bt:
        return -1
    if bt > at:
        return 1
    return 0


def _rank(kind: int, value: int) -> str:
    """Where a token kind sorts when two versions differ in kind at the same position: a pre-release suffix first,
    then the end of the version, then -r, suffix numbers, post-release suffixes, letters, digits, dotted parts."""
    if kind == _SUFFIX and value < 0:
        return '0'
    return {_END: '1', _REVISION_NO: '2', _SUFFIX_NO: '3', _SUFFIX: '4', _LETTER: '5', _DIGIT: '6'}.get(kind, '7')


def _value(value: int) -> str:
    if value < 0:
        return f'a{1000 + max(value, -999):03d}'
    digits = str(value)[:99]
    return f'b{len(digits):02d}{digits}'


def sort_string(version: str) -> str:
    """``version`` as a string whose byte order is apk's version order (``compare``); versions apk refuses sort
    first, among themselves by their text."""
    tokens, last = _tokens(version)
    if last != _END or not version:
        return '!' + version[:300]
    parts = [_rank(kind, value) + _value(value) for kind, value in tokens]
    return (''.join(parts) + '1')[:400]


def is_prerelease(version: str) -> bool:
    return any(f'_{name}' in version for name in _PRE)


def newest(versions: Iterator[str] | list[str]) -> str | None:
    best: str | None = None
    for version in versions:
        if best is None or compare(version, best) > 0:
            best = version
    return best


# ---------------------------------------------------------------------------- names and paths
def split_file_name(file_name: str) -> tuple[str, str] | None:
    """``<name>-<version>.apk`` → (name, version): the first ``-`` followed by a valid version (a version holds no
    ``-`` but its ``-r<n>``)."""
    if not file_name.endswith('.apk'):
        return None
    stem = file_name[:-4]
    for i, c in enumerate(stem):
        if c == '-' and i > 0 and i + 1 < len(stem) and stem[i + 1] in _DIGITS:
            name, version = stem[:i], stem[i + 1 :]
            if valid_version(version) and NAME.match(name):
                return name, version
    return None


KEYS_DIR = 'keys'


def key_name(repo_name: str, repo_id: str) -> str:
    """The file name of a hosted repository's public key in ``/etc/apk/keys`` (and in its index signatures):
    ``<repository name>-<last 8 characters of its id>.rsa.pub``, unique across Waystation instances in practice."""
    return f'{repo_name}-{repo_id[-8:].lower()}.rsa.pub'


def file_name(name: str, version: str) -> str:
    return f'{name}-{version}.apk'


@dataclass(frozen=True)
class Directory:
    """An apk repository directory ``<branch>/<repository>/<arch>`` (the branch may be deeper or empty)."""

    path: str

    @property
    def parts(self) -> list[str]:
        return self.path.split('/') if self.path else []

    @property
    def arch(self) -> str:
        return self.parts[-1] if self.parts else ''

    @property
    def repository(self) -> str:
        return self.parts[-2] if len(self.parts) >= 2 else ''

    @property
    def branch(self) -> str:
        return '/'.join(self.parts[:-2])


def checksum_of(control_sha1: bytes) -> str:
    """The index ``C:`` value of a control segment's SHA-1."""
    return 'Q1' + base64.b64encode(control_sha1).decode('ascii')


# ---------------------------------------------------------------------------- gzip segments
@dataclass(frozen=True)
class Segment:
    start: int
    end: int  # offset after the gzip member
    data: bytes  # decompressed
    sha1: bytes  # of the compressed member


def _gzip_member(f: BinaryIO, start: int, max_out: int, what: str) -> Segment:
    """Decompress the gzip member at ``start`` (at most ``max_out`` bytes out), hashing its compressed bytes."""
    f.seek(start)
    inflater = zlib.decompressobj(31)
    out = bytearray()
    read = 0
    sha1 = hashlib.sha1(usedforsecurity=False)
    while not inflater.eof:
        chunk = f.read(CHUNK)
        if not chunk:
            raise ApkError(f'the {what} is truncated')
        read += len(chunk)
        pending = chunk
        try:
            while pending and not inflater.eof:
                out += inflater.decompress(pending, max_out + 1 - len(out))
                if len(out) > max_out:
                    raise ApkError(f'the {what} is too large')
                pending = inflater.unconsumed_tail
        except zlib.error:
            raise ApkError(f'the {what} is not valid gzip data') from None
        used = len(chunk) - len(inflater.unused_data) if inflater.eof else len(chunk)
        sha1.update(chunk[:used])
    end = start + read - len(inflater.unused_data)
    return Segment(start, end, bytes(out), sha1.digest())


@dataclass(frozen=True)
class TarMember:
    name: str
    data: bytes


def _tar_members(data: bytes, what: str) -> list[TarMember]:
    """Regular files of an uncompressed tar stream (no end-of-archive needed); nothing is extracted."""
    members: list[TarMember] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as tar:
            for info in tar:
                if len(members) >= MEMBERS_MAX:
                    raise ApkError(f'the {what} has too many members')
                if not info.isfile():
                    continue
                extracted = tar.extractfile(info)
                members.append(TarMember(info.name, extracted.read() if extracted is not None else b''))
    except (tarfile.TarError, EOFError) as exc:
        if not members:
            raise ApkError(f'the {what} is not a tar archive') from exc
    return members


# ---------------------------------------------------------------------------- packages
@dataclass(frozen=True)
class PackageInfo:
    """What a ``.apk`` file says about itself (``.PKGINFO``) and its index checksum."""

    name: str
    version: str
    arch: str
    checksum: str  # C: Q1<base64 sha1 of the control segment>
    size: int  # S: the whole file
    signed_by: tuple[str, ...]  # key names of the package's signature segment
    fields: dict[str, list[str]] = field(default_factory=dict)  # every .PKGINFO key (repeatable)

    def first(self, key: str) -> str:
        values = self.fields.get(key)
        return values[0] if values else ''


def parse_pkginfo(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for raw in text.split('\n'):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition(' = ')
        if not sep:
            raise ApkError('.PKGINFO has a line without "key = value"')
        if not re.fullmatch(r'[a-z_]{1,32}', key) or len(value) > FIELD_MAX:
            raise ApkError('.PKGINFO has an invalid line')
        if any(ord(c) < 32 for c in value):
            raise ApkError('.PKGINFO has control characters')
        out.setdefault(key, []).append(value)
    return out


def read_package(f: BinaryIO, size: int) -> PackageInfo:
    """Read and check a v2 ``.apk`` (bounded): its segments, ``.PKGINFO`` and, when ``.PKGINFO`` has a
    ``datahash``, the data segment's SHA-256. Raises ``ApkError``."""
    f.seek(0)
    if f.read(2) != b'\x1f\x8b':
        raise ApkError('not an apk v2 package (gzip streams); apk v3 packages are not supported')
    first = _gzip_member(f, 0, max(SIGNATURE_MAX, CONTROL_MAX), 'first segment')
    members = _tar_members(first.data, 'first segment')
    signed_by: tuple[str, ...] = ()
    control = first
    if members and all(m.name.startswith('.SIGN.') for m in members):
        if len(first.data) > SIGNATURE_MAX:
            raise ApkError('the signature segment is too large')
        signed_by = tuple(m.name.split('.', 3)[-1] for m in members)
        if first.end >= size:
            raise ApkError('the package has no control segment')
        control = _gzip_member(f, first.end, CONTROL_MAX, 'control segment')
        members = _tar_members(control.data, 'control segment')
    pkginfo = next((m for m in members if m.name == '.PKGINFO'), None)
    if pkginfo is None:
        raise ApkError('the control segment has no .PKGINFO')
    if len(pkginfo.data) > PKGINFO_MAX:
        raise ApkError('.PKGINFO is too large')
    try:
        fields = parse_pkginfo(pkginfo.data.decode('utf-8'))
    except UnicodeDecodeError:
        raise ApkError('.PKGINFO is not UTF-8') from None
    name, version, arch = (fields.get(k, [''])[0] for k in ('pkgname', 'pkgver', 'arch'))
    if not NAME.match(name):
        raise ApkError('.PKGINFO pkgname is missing or invalid')
    if not valid_version(version):
        raise ApkError('.PKGINFO pkgver is missing or not an apk version')
    if not ARCH.match(arch):
        raise ApkError('.PKGINFO arch is missing or invalid')
    if control.end >= size:
        raise ApkError('the package has no data segment')
    datahash = fields.get('datahash', [''])[0]
    if datahash:
        digest = hashlib.sha256()
        f.seek(control.end)
        while chunk := f.read(CHUNK):
            digest.update(chunk)
        if digest.hexdigest() != datahash.lower():
            raise ApkError('the data segment does not match the datahash of .PKGINFO')
    return PackageInfo(name, version, arch, checksum_of(control.sha1), size, signed_by, fields)


def package_check(checksum: str) -> Callable[[BinaryIO], None]:
    """A check for a fetched ``.apk`` (proxy ``CheckedExpected``): its control segment must have the index's ``C:``
    checksum (and its data segment the ``datahash`` that control segment names). Raises ``ApkError``."""

    def check(f: BinaryIO) -> None:
        f.seek(0, io.SEEK_END)
        size = f.tell()
        info = read_package(f, size)
        if info.checksum != checksum:
            raise ApkError('the control segment does not match the APKINDEX checksum')

    return check


# ---------------------------------------------------------------------------- indexes
@dataclass(frozen=True)
class IndexEntry:
    name: str  # P
    version: str  # V
    arch: str  # A
    checksum: str  # C
    size: int | None  # S
    fields: dict[str, str]  # every field by letter

    @property
    def file_name(self) -> str:
        return file_name(self.name, self.version)


def decompress_index(data: bytes, *, max_out: int = INDEX_TEXT_MAX) -> bytes:
    """The concatenated tar stream of an ``APKINDEX.tar.gz`` (every gzip member), bounded."""
    if data[:2] != b'\x1f\x8b':
        raise ApkError('not a gzip file')
    out = bytearray()
    rest = data
    members = 0
    while rest:
        members += 1
        if members > 8:
            raise ApkError('too many gzip members')
        inflater = zlib.decompressobj(31)
        pending = rest
        try:
            while pending and not inflater.eof:
                out += inflater.decompress(pending, max_out + 1 - len(out))
                if len(out) > max_out:
                    raise ApkError('the index is too large')
                pending = inflater.unconsumed_tail
        except zlib.error:
            raise ApkError('the index is not valid gzip data') from None
        if not inflater.eof:
            raise ApkError('the index is truncated')
        rest = inflater.unused_data
    return bytes(out)


@dataclass(frozen=True)
class Index:
    description: str
    entries: list[IndexEntry]
    signed_by: tuple[str, ...]


def index_text(data: bytes) -> tuple[str, str, tuple[str, ...]]:
    """(``APKINDEX`` text, ``DESCRIPTION``, signature key names) of an ``APKINDEX.tar.gz``."""
    members = _tar_members(decompress_index(data), 'index')
    text = next((m for m in members if m.name == 'APKINDEX'), None)
    if text is None:
        raise ApkError('the archive has no APKINDEX')
    description = next((m.data for m in members if m.name == 'DESCRIPTION'), b'')
    signed = tuple(m.name.split('.', 3)[-1] for m in members if m.name.startswith('.SIGN.'))
    return (
        text.data.decode('utf-8', 'replace'),
        description.decode('utf-8', 'replace').strip()[:500],
        signed,
    )


def check_index(data: bytes) -> None:
    """Raise ``ApkError`` unless ``data`` is an ``APKINDEX.tar.gz`` (bounded)."""
    index_text(data)


def parse_entries(text: str) -> list[IndexEntry]:
    """The stanzas of an ``APKINDEX`` (bounded); stanzas without a valid name, version or checksum are skipped."""
    entries: list[IndexEntry] = []
    current: dict[str, str] = {}

    def close() -> None:
        if not current:
            return
        name, version, checksum = current.get('P', ''), current.get('V', ''), current.get('C', '')
        size = current.get('S', '')
        if NAME.match(name) and valid_version(version) and CHECKSUM.match(checksum):
            arch = current.get('A', '')
            entries.append(
                IndexEntry(
                    name,
                    version,
                    arch if ARCH.match(arch) else '',
                    checksum,
                    int(size) if size.isdigit() else None,
                    dict(current),
                )
            )
        current.clear()

    for line in text.split('\n'):
        if not line:
            close()
            if len(entries) >= INDEX_ENTRIES_MAX:
                raise ApkError('the index has too many entries')
            continue
        key, sep, value = line.partition(':')
        if sep and len(key) == 1 and len(value) <= FIELD_MAX:
            current[key] = value
    close()
    return entries


def parse_index(data: bytes) -> Index:
    text, description, signed = index_text(data)
    return Index(description, parse_entries(text), signed)


def stanza(info: PackageInfo, *, arch: str) -> str:
    """The ``APKINDEX`` stanza of an uploaded package (``apk index`` order; ``A:`` is the directory's architecture,
    as ``apk index --rewrite-arch`` writes noarch packages)."""
    first = info.first
    lines = [
        f'C:{info.checksum}',
        f'P:{info.name}',
        f'V:{info.version}',
        f'A:{arch}',
        f'S:{info.size}',
        f'I:{first("size") or "0"}',
        f'T:{first("pkgdesc")}',
        f'U:{first("url")}',
        f'L:{first("license")}',
    ]
    for key, letter in (('origin', 'o'), ('maintainer', 'm'), ('builddate', 't'), ('commit', 'c')):
        if first(key):
            lines.append(f'{letter}:{first(key)}')
    if first('provider_priority'):
        lines.append(f'k:{first("provider_priority")}')
    for key, letter in (('depend', 'D'), ('provides', 'p'), ('install_if', 'i')):
        values = info.fields.get(key) or []
        if values:
            lines.append(f'{letter}:{" ".join(values)}')
    return '\n'.join(lines) + '\n'


def _tar_header(name: str, size: int, mtime: int) -> bytes:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = mtime
    info.mode = 0o644
    info.uname = info.gname = 'root'
    return info.tobuf(format=tarfile.USTAR_FORMAT)


def _tar_file(name: str, data: bytes, mtime: int) -> bytes:
    pad = (-len(data)) % 512
    return _tar_header(name, len(data), mtime) + data + b'\0' * pad


def _gzip(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
    return compressor.compress(data) + compressor.flush()


Signer = Callable[[bytes], bytes]  # RSA PKCS#1 v1.5 signature of the SHA-256 of the given bytes


def build_index(stanzas: list[str], description: str, *, key_name: str, sign: Signer, mtime: int) -> bytes:
    """A signed ``APKINDEX.tar.gz``: the signature segment (``.SIGN.RSA256.<key_name>``, no end-of-archive, as
    ``abuild-sign`` writes it) followed by the gzip of a tar holding ``DESCRIPTION`` and ``APKINDEX``."""
    text = ''.join(s.rstrip('\n') + '\n\n' for s in stanzas)  # each stanza ends with a blank line, as apk index writes
    body = _tar_file('DESCRIPTION', description.encode('utf-8'), mtime) + _tar_file(
        'APKINDEX', text.encode('utf-8'), mtime
    )
    index = _gzip(body + b'\0' * 1024)
    signature = sign(index)
    sig_segment = _gzip(_tar_file(f'.SIGN.RSA256.{key_name}', signature, mtime))
    return sig_segment + index


__all__ = [
    'INDEX_NAME',
    'ApkError',
    'Directory',
    'Index',
    'IndexEntry',
    'PackageInfo',
    'build_index',
    'check_index',
    'compare',
    'is_prerelease',
    'newest',
    'package_check',
    'parse_entries',
    'parse_index',
    'read_package',
    'sort_string',
    'split_file_name',
    'stanza',
    'valid_version',
]
