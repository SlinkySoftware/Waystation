"""Reading an uploaded conda package safely (hosted repositories; FORMATS.md "Conda"). Runs in a thread.

- ``.conda`` (format v2): a zip of at most ``MAX_ZIP_MEMBERS`` members with ``metadata.json``
  (``conda_pkg_format_version``
  2) and ``info-<stem>.tar.zst``; the info tarball is decompressed as a stream (``compression.zstd``), at most
  ``MAX_INFO_BYTES``.
- ``.tar.bz2`` (v1): the bzip2 tarball itself, streamed, at most ``MAX_TAR_BYTES`` decompressed.

Nothing is extracted: tar members are read in order (at most ``MAX_MEMBERS``) until ``info/index.json`` (a regular
file of at most ``MAX_INDEX_JSON`` bytes), parsed as a JSON object and checked field by field. The repodata entry is
made from it (the fields conda-index publishes, ``waystation_common.conda.repodata.ENTRY_FIELDS``); anything else is
dropped.
"""

from __future__ import annotations

import bz2
import json
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from waystation_common.archives import READ_ERRORS, BoundedTarInfo
from waystation_common.conda import valid_build, valid_name, valid_subdir, valid_version
from waystation_common.errors import ValidationFailed

MAX_ZIP_MEMBERS = 16
MAX_METADATA_JSON = 64 * 1024
MAX_INDEX_JSON = 1024 * 1024
MAX_INFO_BYTES = 512 * 1024 * 1024
MAX_TAR_BYTES = 8 * 1024 * 1024 * 1024
MAX_MEMBERS = 200_000
MAX_DEPENDS = 2000
MAX_SPEC = 1024
INDEX_JSON = 'info/index.json'


@dataclass(frozen=True)
class PackageMeta:
    name: str
    version: str
    build: str
    build_number: int
    subdir: str
    entry: dict[str, Any]  # the repodata entry without md5/sha256/size


class _Reader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class _Bounded:
    """A read-only stream that refuses to give more than ``limit`` bytes (decompression bombs)."""

    def __init__(self, raw: _Reader, limit: int) -> None:
        self._raw = raw
        self._left = limit

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size if size >= 0 else self._left + 1)
        self._left -= len(data)
        if self._left < 0:
            raise ValidationFailed('the package is too large once decompressed')
        return data


def _strings(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_DEPENDS:
        raise ValidationFailed(f'info/index.json: {field} must be a list of at most {MAX_DEPENDS} strings')
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > MAX_SPEC or '\x00' in item:
            raise ValidationFailed(f'info/index.json: {field} has an invalid entry')
    return list(value)


def _text(raw: dict[str, Any], field: str, *, limit: int = 1024) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValidationFailed(f'info/index.json: {field} must be a short string')
    return value


def parse_index(data: bytes) -> PackageMeta:
    """``info/index.json`` -> the package's identity and repodata entry (raises ``ValidationFailed``)."""
    try:
        raw = json.loads(data)
    except UnicodeDecodeError, ValueError, RecursionError:
        raise ValidationFailed('info/index.json is not valid JSON') from None
    if not isinstance(raw, dict):
        raise ValidationFailed('info/index.json is not a JSON object')
    name, version, build, subdir = raw.get('name'), raw.get('version'), raw.get('build'), raw.get('subdir')
    if not isinstance(name, str) or not valid_name(name):
        raise ValidationFailed('info/index.json: name is not a valid conda package name')
    if not isinstance(version, str) or not valid_version(version):
        raise ValidationFailed('info/index.json: version is not a valid conda version')
    if not isinstance(build, str) or not valid_build(build):
        raise ValidationFailed('info/index.json: build is not a valid build string')
    if not isinstance(subdir, str) or not valid_subdir(subdir):
        raise ValidationFailed('info/index.json: subdir is not a valid subdir')
    number = raw.get('build_number', 0)
    if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number < 2**31:
        raise ValidationFailed('info/index.json: build_number must be a non-negative integer')
    entry: dict[str, Any] = {
        'name': name,
        'version': version,
        'build': build,
        'build_number': number,
        'subdir': subdir,
        'depends': _strings(raw.get('depends'), 'depends'),
    }
    if 'constrains' in raw:
        entry['constrains'] = _strings(raw.get('constrains'), 'constrains')
    for field in ('arch', 'platform', 'license', 'license_family', 'features', 'track_features'):
        value = _text(raw, field)
        if value is not None:
            entry[field] = value
    site = _text(raw, 'python_site_packages_path', limit=256)
    if site is not None:
        entry['python_site_packages_path'] = site
    noarch = raw.get('noarch')
    if noarch is True:  # very old packages: "noarch": true
        entry['noarch'] = 'generic'
    elif noarch not in (None, False):
        if noarch not in ('python', 'generic'):
            raise ValidationFailed('info/index.json: noarch must be "python" or "generic"')
        entry['noarch'] = noarch
    stamp = raw.get('timestamp')
    if stamp is not None:
        if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp < 0:
            raise ValidationFailed('info/index.json: timestamp must be an integer')
        entry['timestamp'] = stamp
    return PackageMeta(name, version, build, number, subdir, entry)


def _find_index(stream: Any) -> PackageMeta:
    try:
        with tarfile.open(fileobj=stream, mode='r|', tarinfo=BoundedTarInfo) as tar:
            for count, member in enumerate(tar):
                if count >= MAX_MEMBERS:
                    raise ValidationFailed('the package has too many files')
                if member.name.removeprefix('./') != INDEX_JSON:
                    continue
                if not member.isfile() or member.size > MAX_INDEX_JSON:
                    raise ValidationFailed('info/index.json is not a small regular file')
                fh = tar.extractfile(member)
                if fh is None:
                    raise ValidationFailed('info/index.json cannot be read')
                return parse_index(fh.read(MAX_INDEX_JSON + 1))
    except READ_ERRORS as exc:
        raise ValidationFailed(f'the package is not a readable archive ({type(exc).__name__})') from None
    raise ValidationFailed('the package has no info/index.json')


def _read_conda(path: Path) -> PackageMeta:
    from compression import zstd

    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS:
                raise ValidationFailed('not a .conda package (too many zip members)')
            names = {m.filename: m for m in members}
            meta = names.get('metadata.json')
            if meta is None or meta.file_size > MAX_METADATA_JSON:
                raise ValidationFailed('not a .conda package (no metadata.json)')
            # Read bounded: ``archive.read`` inflates the whole member at once whatever its header declares.
            with archive.open(meta) as fh:
                raw_meta = fh.read(MAX_METADATA_JSON + 1)
            if len(raw_meta) > MAX_METADATA_JSON:
                raise ValidationFailed('not a .conda package (metadata.json is too large)')
            try:
                version = json.loads(raw_meta).get('conda_pkg_format_version')
            except AttributeError, UnicodeDecodeError, ValueError, RecursionError:
                raise ValidationFailed('not a .conda package (metadata.json is not a JSON object)') from None
            if version != 2:
                raise ValidationFailed('unsupported .conda format version')
            infos = [m for m in members if m.filename.startswith('info-') and m.filename.endswith('.tar.zst')]
            if len(infos) != 1 or '/' in infos[0].filename:
                raise ValidationFailed('not a .conda package (no info-*.tar.zst member)')
            with archive.open(infos[0]) as raw, zstd.ZstdFile(raw, 'rb') as tarball:
                return _find_index(_Bounded(tarball, MAX_INFO_BYTES))
    except zstd.ZstdError:
        raise ValidationFailed('the .conda info archive cannot be decompressed') from None
    except READ_ERRORS:
        raise ValidationFailed('not a .conda package (not a readable zip file)') from None


def _read_tar_bz2(path: Path) -> PackageMeta:
    try:
        with bz2.open(path, 'rb') as tarball:
            return _find_index(_Bounded(tarball, MAX_TAR_BYTES))
    except (OSError, EOFError, ValueError) as exc:
        if isinstance(exc, ValidationFailed):
            raise
        raise ValidationFailed('not a .tar.bz2 package (bzip2 data cannot be read)') from None


def read_package(path: Path, suffix: str) -> PackageMeta:
    """The ``info/index.json`` of the stored package at ``path`` (``suffix``: ``.conda`` or ``.tar.bz2``)."""
    return _read_conda(path) if suffix == '.conda' else _read_tar_bz2(path)
