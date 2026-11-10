"""RPM package headers, read safely, and the repodata entries Waystation generates for hosted yum repositories
(T-P5-YUMH; FORMATS.md "yum / dnf").

An RPM file is a 96-byte lead, a signature header (padded to 8 bytes), the main header, then the payload. Only the
headers are read (bounded: ``HEADER_MAX`` bytes of index + store, ``INDEX_MAX`` entries), never the payload, and every
offset and count is checked against the store before use. Values are decoded as UTF-8 (replacing bad bytes) and
characters XML 1.0 cannot carry are dropped, so a hostile header cannot break the generated XML. When the signature
header carries the SHA-256 of the main header (RPMSIGTAG_SHA256) it must match.

``entries`` renders the package's ``primary.xml``, ``filelists.xml`` and ``other.xml`` fragments in the createrepo
layout (header range, dependencies with flags and EVR, files: ``/etc``, ``*bin/*`` and ``/usr/lib/sendmail`` in
``primary``, every file in ``filelists``; the newest ``CHANGELOG_MAX`` changelog entries in ``other``). The worker
concatenates the fragments of every package into the repository's repodata.

Pure CPU and file work: callers on the event loop run it in a thread (ARCHITECTURE §9).
"""

from __future__ import annotations

import hashlib
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO
from xml.sax.saxutils import escape, quoteattr

from waystation_common.errors import ValidationFailed

LEAD_SIZE = 96
LEAD_MAGIC = b'\xed\xab\xee\xdb'
HEADER_MAGIC = b'\x8e\xad\xe8\x01'
HEADER_MAX = 64 * 1024 * 1024
INDEX_MAX = 200_000
FILES_MAX = 1_000_000
CHANGELOG_MAX = 10
TEXT_MAX = 64 * 1024  # one text value (descriptions, changelog entries)

# Header data types.
CHAR, INT8, INT16, INT32, INT64, STRING, BIN, STRING_ARRAY, I18NSTRING = 1, 2, 3, 4, 5, 6, 7, 8, 9

# Signature header tags.
SIG_SHA256 = 273
SIG_PAYLOADSIZE = 1007

# Main header tags.
NAME, VERSION, RELEASE, EPOCH, SUMMARY, DESCRIPTION, BUILDTIME, BUILDHOST = (
    1000,
    1001,
    1002,
    1003,
    1004,
    1005,
    1006,
    1007,
)
SIZE, VENDOR, LICENSE, PACKAGER, GROUP, URL, ARCH = 1009, 1011, 1014, 1015, 1016, 1020, 1022
OLDFILENAMES, FILEMODES, FILEFLAGS, SOURCERPM, ARCHIVESIZE = 1027, 1030, 1037, 1044, 1046
PROVIDENAME, REQUIREFLAGS, REQUIRENAME, REQUIREVERSION = 1047, 1048, 1049, 1050
CONFLICTFLAGS, CONFLICTNAME, CONFLICTVERSION = 1053, 1054, 1055
CHANGELOGTIME, CHANGELOGNAME, CHANGELOGTEXT = 1080, 1081, 1082
OBSOLETENAME, PROVIDEFLAGS, PROVIDEVERSION, OBSOLETEFLAGS, OBSOLETEVERSION = 1090, 1112, 1113, 1114, 1115
DIRINDEXES, BASENAMES, DIRNAMES = 1116, 1117, 1118
LONGSIZE, LONGARCHIVESIZE = 5009, 271
WEAK = {  # (name, flags, version) tags
    'recommends': (5046, 5048, 5047),
    'suggests': (5049, 5051, 5050),
    'supplements': (5052, 5054, 5053),
    'enhances': (5055, 5057, 5056),
}

SENSE_LESS, SENSE_GREATER, SENSE_EQUAL = 2, 4, 8
SENSE_PREREQ, SENSE_SCRIPT_PRE, SENSE_SCRIPT_POST = 1 << 6, 1 << 9, 1 << 10
SENSE_RPMLIB = 1 << 24
FILE_GHOST = 1 << 6
FLAG_NAMES = {2: 'LT', 4: 'GT', 8: 'EQ', 10: 'LE', 12: 'GE'}

NAME_RE = re.compile(r'^[A-Za-z0-9_+][A-Za-z0-9._+~^-]{0,199}$')
VERSION_RE = re.compile(r'^[A-Za-z0-9._+~^]{1,128}$')
ARCH_RE = re.compile(r'^[A-Za-z0-9_]{1,32}$')
_XML_BAD = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]')


def clean(text: str, limit: int = TEXT_MAX) -> str:
    return _XML_BAD.sub('', text)[:limit]


@dataclass(frozen=True)
class Dependency:
    name: str
    flags: int = 0
    version: str = ''

    def xml(self) -> str:
        attrs = f'name={quoteattr(clean(self.name, 1024))}'
        kind = FLAG_NAMES.get(self.flags & (SENSE_LESS | SENSE_GREATER | SENSE_EQUAL))
        if kind and self.version:
            epoch, ver, rel = split_evr(self.version)
            attrs += f' flags="{kind}" epoch={quoteattr(epoch)} ver={quoteattr(clean(ver, 256))}'
            if rel:
                attrs += f' rel={quoteattr(clean(rel, 256))}'
        if self.flags & (SENSE_PREREQ | SENSE_SCRIPT_PRE | SENSE_SCRIPT_POST):
            attrs += ' pre="1"'
        return f'<rpm:entry {attrs}/>'


@dataclass(frozen=True)
class Header:
    """What Waystation reads from an RPM: identity, descriptive fields, dependencies, files, changelog and the
    byte range of the main header in the file."""

    name: str
    epoch: str
    version: str
    release: str
    arch: str
    summary: str = ''
    description: str = ''
    packager: str = ''
    url: str = ''
    license: str = ''
    vendor: str = ''
    group: str = ''
    buildhost: str = ''
    sourcerpm: str = ''
    build_time: int = 0
    installed_size: int = 0
    archive_size: int = 0
    header_start: int = 0
    header_end: int = 0
    deps: dict[str, tuple[Dependency, ...]] = field(default_factory=dict)
    files: tuple[tuple[str, str], ...] = ()  # (path, '' | 'dir' | 'ghost')
    changelog: tuple[tuple[str, int, str], ...] = ()  # (author, time, text), newest first

    @property
    def filename(self) -> str:
        return f'{self.name}-{self.version}-{self.release}.{self.arch}.rpm'


def split_evr(value: str) -> tuple[str, str, str]:
    """``[epoch:]version[-release]`` → (epoch, version, release); epoch defaults to ``0``."""
    epoch, sep, rest = value.partition(':')
    if not sep or not epoch.isdigit():
        epoch, rest = '0', value
    version, _, release = rest.partition('-')
    return epoch, version, release


# ---------------------------------------------------------------------------- header structure
@dataclass
class _Section:
    entries: dict[int, tuple[int, int, int]]  # tag -> (type, offset, count)
    store: bytes
    raw: bytes  # the whole header structure (magic .. end of store)

    def strings(self, tag: int) -> list[str]:
        entry = self.entries.get(tag)
        if entry is None:
            return []
        kind, offset, count = entry
        if kind not in (STRING, STRING_ARRAY, I18NSTRING):
            raise ValidationFailed('RPM header value has an unexpected type')
        if count > len(self.store):  # every string takes at least its terminating NUL
            raise ValidationFailed('RPM header array outside its store')
        out: list[str] = []
        pos = offset
        # A STRING is one value; of an I18NSTRING the first (C locale) translation is the one listed.
        for _ in range(count if kind == STRING_ARRAY else 1):
            end = self.store.find(b'\x00', pos) if 0 <= pos < len(self.store) else -1
            if end < 0:
                raise ValidationFailed('RPM header string outside its store')
            out.append(self.store[pos:end].decode('utf-8', 'replace'))
            pos = end + 1
        return out

    def string(self, tag: int) -> str:
        values = self.strings(tag)
        return values[0] if values else ''

    def ints(self, tag: int) -> list[int]:
        entry = self.entries.get(tag)
        if entry is None:
            return []
        kind, offset, count = entry
        width = {CHAR: 1, INT8: 1, INT16: 2, INT32: 4, INT64: 8}.get(kind)
        if width is None:
            raise ValidationFailed('RPM header value has an unexpected type')
        end = offset + width * count
        if offset < 0 or count < 0 or end > len(self.store):
            raise ValidationFailed('RPM header array outside its store')
        data = self.store[offset:end]
        return [int.from_bytes(data[i : i + width], 'big') for i in range(0, len(data), width)]

    def int(self, tag: int, default: int = 0) -> int:
        values = self.ints(tag)
        return values[0] if values else default


def _read_exact(fh: BinaryIO, size: int) -> bytes:
    data = fh.read(size)
    if len(data) != size:
        raise ValidationFailed('the RPM file is truncated')
    return data


def _section(fh: BinaryIO) -> _Section:
    intro = _read_exact(fh, 16)
    if intro[:4] != HEADER_MAGIC:
        raise ValidationFailed('not an RPM header')
    count = int.from_bytes(intro[8:12], 'big')
    size = int.from_bytes(intro[12:16], 'big')
    if count > INDEX_MAX or size > HEADER_MAX or count * 16 + size > HEADER_MAX:
        raise ValidationFailed('the RPM header is too large')
    index = _read_exact(fh, count * 16)
    store = _read_exact(fh, size)
    entries: dict[int, tuple[int, int, int]] = {}
    for i in range(count):
        tag, kind, offset, n = (int.from_bytes(index[i * 16 + j : i * 16 + j + 4], 'big') for j in (0, 4, 8, 12))
        if offset > size:
            raise ValidationFailed('RPM header entry outside its store')
        entries.setdefault(tag, (kind, offset, n))
    return _Section(entries, store, intro + index + store)


def _deps(main: _Section, names: int, flags: int, versions: int) -> tuple[Dependency, ...]:
    n, f, v = main.strings(names), main.ints(flags), main.strings(versions)
    out: list[Dependency] = []
    seen: set[Dependency] = set()
    for i, name in enumerate(n):
        dep = Dependency(name, f[i] if i < len(f) else 0, v[i] if i < len(v) else '')
        if dep not in seen:
            seen.add(dep)
            out.append(dep)
    return tuple(out)


def read_header(path: Path) -> Header:
    """The headers of the RPM file at ``path`` (raises ``ValidationFailed`` for anything that is not a well-formed
    binary or source RPM within the limits)."""
    with path.open('rb') as fh:
        lead = _read_exact(fh, LEAD_SIZE)
        if lead[:4] != LEAD_MAGIC:
            raise ValidationFailed('not an RPM file')
        signature = _section(fh)
        pad = (-len(signature.raw)) % 8
        _read_exact(fh, pad)
        start = LEAD_SIZE + len(signature.raw) + pad
        main = _section(fh)
    end = start + len(main.raw)
    expected = signature.string(SIG_SHA256) if SIG_SHA256 in signature.entries else ''
    if expected and hashlib.sha256(main.raw).hexdigest() != expected.lower():
        raise ValidationFailed('the RPM header does not match its SHA-256 digest')

    name, version, release = main.string(NAME), main.string(VERSION), main.string(RELEASE)
    is_source = SOURCERPM not in main.entries
    arch = 'src' if is_source else main.string(ARCH)
    if not NAME_RE.match(name) or not VERSION_RE.match(version) or not VERSION_RE.match(release):
        raise ValidationFailed('the RPM has an invalid name, version or release')
    if not ARCH_RE.match(arch):
        raise ValidationFailed('the RPM has an invalid architecture')
    epoch = str(main.int(EPOCH)) if EPOCH in main.entries else '0'

    deps: dict[str, tuple[Dependency, ...]] = {
        'provides': _deps(main, PROVIDENAME, PROVIDEFLAGS, PROVIDEVERSION),
        'requires': _deps(main, REQUIRENAME, REQUIREFLAGS, REQUIREVERSION),
        'conflicts': _deps(main, CONFLICTNAME, CONFLICTFLAGS, CONFLICTVERSION),
        'obsoletes': _deps(main, OBSOLETENAME, OBSOLETEFLAGS, OBSOLETEVERSION),
    }
    for kind, (names, flags, versions) in WEAK.items():
        deps[kind] = _deps(main, names, flags, versions)

    paths = main.strings(OLDFILENAMES)
    if not paths:
        dirs, bases, index = main.strings(DIRNAMES), main.strings(BASENAMES), main.ints(DIRINDEXES)
        if len(bases) != len(index) or any(i >= len(dirs) for i in index):
            raise ValidationFailed('the RPM file list is inconsistent')
        paths = [dirs[i] + base for i, base in zip(index, bases, strict=True)]
    if len(paths) > FILES_MAX:
        raise ValidationFailed('the RPM lists too many files')
    modes, fflags = main.ints(FILEMODES), main.ints(FILEFLAGS)
    files: list[tuple[str, str]] = []
    for i, file_path in enumerate(paths):
        kind = ''
        if i < len(fflags) and fflags[i] & FILE_GHOST:
            kind = 'ghost'
        elif i < len(modes) and stat.S_ISDIR(modes[i]):
            kind = 'dir'
        files.append((clean(file_path, 4096), kind))

    # Like createrepo: no rpmlib() requirements, nor what the package provides or contains itself.
    own = {(d.name, d.version) for d in deps['provides']}
    own_files = {p for p, _ in files}
    deps['requires'] = tuple(
        d
        for d in deps['requires']
        if not d.flags & SENSE_RPMLIB
        and not d.name.startswith('rpmlib(')
        and (d.name, d.version) not in own
        and d.name not in own_files
    )

    times, authors, texts = main.ints(CHANGELOGTIME), main.strings(CHANGELOGNAME), main.strings(CHANGELOGTEXT)
    changelog = tuple(
        (clean(authors[i], 1024), times[i], clean(texts[i])) for i in range(min(len(times), len(authors), len(texts)))
    )[:CHANGELOG_MAX]

    return Header(
        name=name,
        epoch=epoch,
        version=version,
        release=release,
        arch=arch,
        summary=clean(main.string(SUMMARY), 1024),
        description=clean(main.string(DESCRIPTION)),
        packager=clean(main.string(PACKAGER), 1024),
        url=clean(main.string(URL), 2048),
        license=clean(main.string(LICENSE), 1024),
        vendor=clean(main.string(VENDOR), 1024),
        group=clean(main.string(GROUP), 1024),
        buildhost=clean(main.string(BUILDHOST), 1024),
        sourcerpm=clean(main.string(SOURCERPM), 1024),
        build_time=main.int(BUILDTIME),
        installed_size=main.int(LONGSIZE) or main.int(SIZE),
        archive_size=signature.int(LONGARCHIVESIZE) or signature.int(SIG_PAYLOADSIZE) or main.int(ARCHIVESIZE),
        header_start=start,
        header_end=end,
        deps=deps,
        files=tuple(files),
        changelog=changelog,
    )


# ---------------------------------------------------------------------------- repodata fragments
def _primary_file(path: str) -> bool:
    return path.startswith('/etc/') or '/bin/' in path or path == '/usr/lib/sendmail'


def _file_xml(path: str, kind: str) -> str:
    return f'<file type="{kind}">{escape(path)}</file>' if kind else f'<file>{escape(path)}</file>'


@dataclass(frozen=True)
class Entries:
    primary: str
    filelists: str
    other: str


def entries(header: Header, *, href: str, sha256: str, size: int, upload_time: int) -> Entries:
    """The ``<package>`` elements of one package for ``primary.xml``, ``filelists.xml`` and ``other.xml``."""
    h = header
    version = f'<version epoch={quoteattr(h.epoch)} ver={quoteattr(h.version)} rel={quoteattr(h.release)}/>'
    deps = ''.join(
        f'<rpm:{kind}>{"".join(d.xml() for d in values)}</rpm:{kind}>' for kind, values in h.deps.items() if values
    )
    primary_files = ''.join(_file_xml(p, k) for p, k in h.files if _primary_file(p))
    primary = (
        '<package type="rpm">'
        f'<name>{escape(h.name)}</name><arch>{escape(h.arch)}</arch>{version}'
        f'<checksum type="sha256" pkgid="YES">{sha256}</checksum>'
        f'<summary>{escape(h.summary)}</summary><description>{escape(h.description)}</description>'
        f'<packager>{escape(h.packager)}</packager><url>{escape(h.url)}</url>'
        f'<time file="{upload_time}" build="{h.build_time}"/>'
        f'<size package="{size}" installed="{h.installed_size}" archive="{h.archive_size}"/>'
        f'<location href={quoteattr(href)}/>'
        '<format>'
        f'<rpm:license>{escape(h.license)}</rpm:license><rpm:vendor>{escape(h.vendor)}</rpm:vendor>'
        f'<rpm:group>{escape(h.group)}</rpm:group><rpm:buildhost>{escape(h.buildhost)}</rpm:buildhost>'
        f'<rpm:sourcerpm>{escape(h.sourcerpm)}</rpm:sourcerpm>'
        f'<rpm:header-range start="{h.header_start}" end="{h.header_end}"/>'
        f'{deps}{primary_files}'
        '</format></package>\n'
    )
    ident = f'pkgid="{sha256}" name={quoteattr(h.name)} arch={quoteattr(h.arch)}'
    filelists = f'<package {ident}>{version}{"".join(_file_xml(p, k) for p, k in h.files)}</package>\n'
    changes = ''.join(
        f'<changelog author={quoteattr(author)} date="{when}">{escape(text)}</changelog>'
        for author, when, text in h.changelog
    )
    other = f'<package {ident}>{version}{changes}</package>\n'
    return Entries(primary=primary, filelists=filelists, other=other)
