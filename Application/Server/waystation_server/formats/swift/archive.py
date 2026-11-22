"""Source archives of Swift package releases (SE-0292 §4.4, §4.6.1). Synchronous and CPU/IO heavy: callers run every
function here in a thread (``asyncio.to_thread``), never on the event loop.

- ``build_from_tarball``: the SCM-backed proxy's repackaging of a Git tag's ``.tar.gz`` (GitHub, GitLab, Gitea
  archive endpoints) into the zip clients download. **Byte-deterministic**: the tarball's single top-level folder is
  replaced by ``<name>/`` (what ``swift package archive-source`` writes; clients strip the first level), entries are
  sorted by path, every entry has the same timestamp (1980-01-01 00:00), Unix permissions ``0644``/``0755`` (from the
  executable bit) or a symlink, DEFLATE at level 6; directories, device files and anything else are left out. The
  same tarball therefore always gives the same zip and checksum.
- ``inspect_zip``: the checks a published archive must pass before it is stored: a readable zip, no encrypted
  entries, every entry name a safe relative path (no ``..``, absolute paths, backslashes or NUL) under one top-level
  folder, no duplicate names, no symlink leaving the archive, bounded entry count and unpacked size, and a
  ``Package.swift`` at the package root.

Both return the package manifests at the root (``Package.swift`` and ``Package@swift-*.swift``, each bounded) with
their Swift tools versions. Hostile input raises ``ArchiveRefused`` with a message safe to show the client.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from waystation_common.swift import MANIFEST, VERSIONED_MANIFEST

MAX_ENTRIES = 50_000
MAX_UNPACKED = 512 * 1024 * 1024
MAX_MANIFEST = 1024 * 1024
MAX_LINK = 4096
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
COMPRESS_LEVEL = 6
_COPY = 1024 * 1024
_TOOLS = re.compile(rb'^//\s*swift-tools-version\s*:\s*(\d+(?:\.\d+){0,2})', re.IGNORECASE)


class ArchiveRefused(Exception):  # noqa: N818 - an answer, not a failure
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Manifest:
    filename: str  # Package.swift or Package@swift-<x>.swift
    data: bytes
    tools_version: str | None  # from the "// swift-tools-version:" line
    swift_version: str | None  # the <x> of Package@swift-<x>.swift; None for Package.swift


@dataclass
class ArchiveSummary:
    top: str
    entries: int
    unpacked: int
    manifests: list[Manifest] = field(default_factory=list)

    def manifest(self, filename: str = MANIFEST) -> Manifest | None:
        return next((m for m in self.manifests if m.filename == filename), None)


def tools_version(data: bytes) -> str | None:
    """The Swift tools version a manifest declares on its first non-empty line; None when it declares none."""
    text = data.removeprefix(b'\xef\xbb\xbf')
    for line in text.splitlines()[:16]:
        stripped = line.strip()
        if not stripped:
            continue
        match = _TOOLS.match(stripped)
        return match.group(1).decode('ascii') if match else None
    return None


def _manifest(filename: str, data: bytes) -> Manifest:
    found = VERSIONED_MANIFEST.match(filename)
    swift_version = '.'.join(g for g in found.groups() if g is not None) if found else None
    return Manifest(filename=filename, data=data, tools_version=tools_version(data), swift_version=swift_version)


def is_manifest_name(name: str) -> bool:
    return name == MANIFEST or bool(VERSIONED_MANIFEST.match(name))


def _safe_relative(name: str) -> list[str]:
    """``name`` as path segments, refusing anything that could leave the archive's folder."""
    if not name or '\x00' in name or '\\' in name or name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        raise ArchiveRefused(f'unsafe path in the archive: {name[:200]!r}')
    parts = [p for p in name.split('/') if p not in ('', '.')]
    if not parts or any(p == '..' for p in parts):
        raise ArchiveRefused(f'unsafe path in the archive: {name[:200]!r}')
    return parts


def _link_inside(rel: str, target: str) -> bool:
    """A symlink at ``rel`` (relative to the package root) pointing at ``target`` stays inside the package."""
    if not target or target.startswith('/') or '\x00' in target or '\\' in target:
        return False
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    return not (joined == '..' or joined.startswith(('../', '/')))


# ---------------------------------------------------------------------------- SCM tarball -> deterministic zip
@dataclass
class _Entry:
    kind: str  # file | link
    mode: int = 0o644
    temp: str = ''  # file: the extracted copy
    size: int = 0
    target: str = ''  # link


def _top_and_rel(name: str, top: str | None) -> tuple[str, str]:
    parts = _safe_relative(name)
    if top is not None and parts[0] != top:
        raise ArchiveRefused('the source tarball has more than one top-level entry')
    return parts[0], '/'.join(parts[1:])


def _extract(src: BinaryIO, work: Path) -> dict[str, _Entry]:
    entries: dict[str, _Entry] = {}
    by_tar_name: dict[str, str] = {}
    top: str | None = None
    unpacked = 0
    count = 0
    try:
        with tarfile.open(fileobj=src, mode='r|*') as tar:
            for member in tar:
                count += 1
                if count > MAX_ENTRIES:
                    raise ArchiveRefused(f'the source tarball has more than {MAX_ENTRIES} entries')
                top, rel = _top_and_rel(member.name, top)
                if not rel:
                    continue  # the top-level folder itself
                if member.isreg():
                    unpacked += member.size
                    if unpacked > MAX_UNPACKED:
                        raise ArchiveRefused('the source tarball unpacks to more than the size limit')
                    handle = tar.extractfile(member)
                    temp = str(count)
                    with (work / temp).open('wb') as out:
                        if handle is not None:
                            shutil.copyfileobj(handle, out, _COPY)
                    mode = 0o755 if member.mode & 0o111 else 0o644
                    entries[rel] = _Entry('file', mode=mode, temp=temp, size=member.size)
                    by_tar_name[member.name.removeprefix('./')] = rel
                elif member.issym():
                    if len(member.linkname) <= MAX_LINK and _link_inside(rel, member.linkname):
                        entries[rel] = _Entry('link', mode=0o777, target=member.linkname)
                    else:
                        entries.pop(rel, None)
                elif member.islnk():
                    source = by_tar_name.get(member.linkname.removeprefix('./'))
                    if source is not None and entries.get(source) is not None:
                        original = entries[source]
                        entries[rel] = _Entry('file', mode=original.mode, temp=original.temp, size=original.size)
                    else:
                        entries.pop(rel, None)
                elif member.isdir():
                    continue
                else:
                    entries.pop(rel, None)  # devices, FIFOs...: never part of a package
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        raise ArchiveRefused(f'the source tarball is not a readable .tar.gz ({type(exc).__name__})') from None
    if top is None:
        raise ArchiveRefused('the source tarball is empty')
    return entries


def _resolve_file(entries: dict[str, _Entry], rel: str) -> _Entry | None:
    """A root manifest that is a symlink to another file of the package reads as that file."""
    entry = entries.get(rel)
    for _ in range(8):
        if entry is None or entry.kind == 'file':
            return entry
        rel = posixpath.normpath(posixpath.join(posixpath.dirname(rel), entry.target))
        entry = entries.get(rel)
    return None


def build_from_tarball(src: Path, out: Path, top: str, work_dir: Path) -> ArchiveSummary:
    """Repackage the tarball at ``src`` as the deterministic zip ``out`` with the top-level folder ``top``."""
    with tempfile.TemporaryDirectory(dir=work_dir, prefix='swift-build-') as tmp:
        work = Path(tmp)
        with src.open('rb') as fh:
            entries = _extract(fh, work)
        manifests: list[Manifest] = []
        for rel in sorted(r for r in entries if '/' not in r and is_manifest_name(r)):
            entry = _resolve_file(entries, rel)
            if entry is None or entry.size > MAX_MANIFEST:
                continue
            manifests.append(_manifest(rel, (work / entry.temp).read_bytes()))
        unpacked = 0
        with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=COMPRESS_LEVEL) as zf:
            for rel in sorted(entries):
                entry = entries[rel]
                info = zipfile.ZipInfo(f'{top}/{rel}', date_time=ZIP_EPOCH)
                info.create_system = 3  # Unix: the permissions below are read by unzip
                if entry.kind == 'link':
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    info.compress_type = zipfile.ZIP_STORED
                    zf.writestr(info, entry.target.encode('utf-8'))
                    continue
                info.external_attr = (stat.S_IFREG | entry.mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                info.compress_level = COMPRESS_LEVEL
                unpacked += entry.size
                with (work / entry.temp).open('rb') as data, zf.open(info, 'w') as dest:
                    shutil.copyfileobj(data, dest, _COPY)
        summary = ArchiveSummary(top=top, entries=len(entries), unpacked=unpacked, manifests=manifests)
    if summary.manifest() is None:
        out.unlink(missing_ok=True)
        raise ArchiveRefused('the tagged source has no Package.swift at its root: not a Swift package')
    return summary


# ---------------------------------------------------------------------------- published zip
def inspect_zip(path: Path) -> ArchiveSummary:
    """Validate an uploaded source archive (see the module docstring) and read its manifests."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                raise ArchiveRefused(f'the source archive has more than {MAX_ENTRIES} entries')
            top: str | None = None
            seen: set[str] = set()
            unpacked = 0
            roots: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                if info.flag_bits & 0x1:
                    raise ArchiveRefused('the source archive has encrypted entries')
                parts = _safe_relative(info.filename)
                if top is None:
                    top = parts[0]
                elif parts[0] != top:
                    raise ArchiveRefused(
                        'the source archive must have exactly one top-level folder (swift package archive-source)'
                    )
                rel = '/'.join(parts[1:])
                if not rel:
                    continue
                if rel in seen:
                    raise ArchiveRefused(f'duplicate entry in the source archive: {rel[:200]!r}')
                seen.add(rel)
                unpacked += info.file_size
                if unpacked > MAX_UNPACKED:
                    raise ArchiveRefused('the source archive unpacks to more than the size limit')
                if stat.S_ISLNK(info.external_attr >> 16):
                    target = _read(zf, info, MAX_LINK).decode('utf-8', errors='replace')
                    if not _link_inside(rel, target):
                        raise ArchiveRefused(f'a symlink in the source archive leaves the package: {rel[:200]!r}')
                    continue
                if '/' not in rel and is_manifest_name(rel) and not info.is_dir():
                    roots[rel] = info
            if top is None:
                raise ArchiveRefused('the source archive is empty')
            manifests = []
            for name in sorted(roots):
                info = roots[name]
                if info.file_size > MAX_MANIFEST:
                    raise ArchiveRefused(f'{name} is larger than {MAX_MANIFEST} bytes')
                manifests.append(_manifest(name, _read(zf, info, MAX_MANIFEST)))
    except ArchiveRefused:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, ValueError, NotImplementedError) as exc:
        raise ArchiveRefused(f'the source archive is not a readable zip file ({type(exc).__name__})') from None
    summary = ArchiveSummary(top=top, entries=len(seen), unpacked=unpacked, manifests=manifests)
    main = summary.manifest()
    if main is None:
        raise ArchiveRefused("the package doesn't contain a valid manifest (Package.swift) at its root")
    if main.tools_version is None:
        raise ArchiveRefused('Package.swift does not start with a "// swift-tools-version:" line')
    return summary


def _read(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """An entry's bytes, never more than ``limit`` (a lying header cannot inflate past it)."""
    with zf.open(info) as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise ArchiveRefused(f'{info.filename[:200]!r} is larger than declared')
    return data


def remove(path: Path) -> None:
    path.unlink(missing_ok=True)
