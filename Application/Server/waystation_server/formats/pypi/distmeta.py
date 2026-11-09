"""Core metadata of an uploaded distribution (hosted PyPI): ``*.dist-info/METADATA`` of a wheel, ``PKG-INFO`` of an
sdist. Blocking (zip/tar reads): run in a thread.

Archives are untrusted: entry counts, the metadata size and the total scan are bounded, member names must be
relative paths without ``..``, backslashes or NULs (zip/tar-slip: nothing is ever extracted, but an archive that
would write outside its directory is refused, as an installer could be tricked by it), and the metadata file must be
a regular file. Anything else raises ``DistributionInvalid`` (400).
"""

from __future__ import annotations

import tarfile
import zipfile
from dataclasses import dataclass, field
from email.parser import BytesHeaderParser
from email.policy import compat32
from pathlib import Path

from waystation_common.errors import ValidationFailed

MAX_METADATA = 1024 * 1024
MAX_ENTRIES = 100_000
MAX_TEXT = 1024
MAX_REQUIRES = 200


class DistributionInvalid(ValidationFailed):
    code = 'invalid_distribution'


@dataclass(frozen=True)
class CoreMetadata:
    raw: bytes  # the METADATA / PKG-INFO file (served as the PEP 658 ``.metadata`` file for wheels)
    metadata_version: str | None
    name: str
    version: str
    summary: str | None = None
    requires_python: str | None = None
    license: str | None = None
    home_page: str | None = None
    author: str | None = None
    requires_dist: tuple[str, ...] = field(default_factory=tuple)


def _safe_name(name: str) -> bool:
    if not name or '\x00' in name or '\\' in name or name.startswith('/') or (len(name) > 1 and name[1] == ':'):
        return False
    return '..' not in name.split('/')


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    value = ' '.join(str(value).split())
    return value[:MAX_TEXT] or None


def parse_metadata(raw: bytes) -> CoreMetadata:
    """Parse core metadata (RFC 822 style headers; the body, the long description, is ignored)."""
    headers = BytesHeaderParser(policy=compat32).parsebytes(raw)
    name, version = _text(headers.get('Name')), _text(headers.get('Version'))
    if not name or not version:
        raise DistributionInvalid('the distribution metadata has no Name or Version')
    requires = tuple(r for r in (_text(v) for v in (headers.get_all('Requires-Dist') or [])[:MAX_REQUIRES]) if r)
    return CoreMetadata(
        raw=raw,
        metadata_version=_text(headers.get('Metadata-Version')),
        name=name,
        version=version,
        summary=_text(headers.get('Summary')),
        requires_python=_text(headers.get('Requires-Python')),
        license=_text(headers.get('License-Expression')) or _text(headers.get('License')),
        home_page=_text(headers.get('Home-page')),
        author=_text(headers.get('Author')) or _text(headers.get('Author-email')),
        requires_dist=requires,
    )


def _read_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.is_dir() or info.file_size > MAX_METADATA:
        raise DistributionInvalid('the distribution metadata is too large')
    with archive.open(info) as fh:
        data = fh.read(MAX_METADATA + 1)
    if len(data) > MAX_METADATA:
        raise DistributionInvalid('the distribution metadata is too large')
    return data


def _zip_entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise DistributionInvalid('the archive has too many entries')
    for info in infos:
        if not _safe_name(info.filename):
            raise DistributionInvalid('the archive contains an unsafe path')
    return infos


def read_wheel(path: Path) -> CoreMetadata:
    """``<name>-<version>.dist-info/METADATA`` of a wheel (exactly one ``.dist-info`` directory)."""
    try:
        with zipfile.ZipFile(path) as archive:
            found = [
                i
                for i in _zip_entries(archive)
                if i.filename.count('/') == 1 and i.filename.endswith('.dist-info/METADATA')
            ]
            if len(found) != 1:
                raise DistributionInvalid('a wheel must contain exactly one .dist-info/METADATA')
            return parse_metadata(_read_zip_member(archive, found[0]))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, NotImplementedError, ValueError) as exc:
        if isinstance(exc, DistributionInvalid):
            raise
        raise DistributionInvalid('the wheel is not a valid zip archive') from None


def _read_sdist_zip(path: Path) -> CoreMetadata:
    try:
        with zipfile.ZipFile(path) as archive:
            found = [
                i for i in _zip_entries(archive) if i.filename.count('/') == 1 and i.filename.endswith('/PKG-INFO')
            ]
            if not found:
                raise DistributionInvalid('the sdist has no PKG-INFO')
            return parse_metadata(_read_zip_member(archive, found[0]))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, NotImplementedError, ValueError) as exc:
        if isinstance(exc, DistributionInvalid):
            raise
        raise DistributionInvalid('the sdist is not a valid zip archive') from None


def _read_sdist_tar(path: Path) -> CoreMetadata:
    """``<dir>/PKG-INFO`` of a tar sdist; every member is checked (names, link targets), not only those before it."""
    raw: bytes | None = None
    try:
        with tarfile.open(path, mode='r:*') as archive:
            for count, member in enumerate(archive):
                if count >= MAX_ENTRIES:
                    raise DistributionInvalid('the archive has too many entries')
                name = member.name.removeprefix('./')
                linked = member.issym() or member.islnk()
                if not _safe_name(name) or (linked and not _safe_name(member.linkname)):
                    raise DistributionInvalid('the archive contains an unsafe path')
                if raw is None and name.count('/') == 1 and name.endswith('/PKG-INFO'):
                    if not member.isfile() or member.size > MAX_METADATA:
                        raise DistributionInvalid('the sdist PKG-INFO is not a regular file of a sane size')
                    fh = archive.extractfile(member)
                    if fh is None:
                        raise DistributionInvalid('the sdist PKG-INFO cannot be read')
                    with fh:
                        raw = fh.read(MAX_METADATA + 1)[:MAX_METADATA]
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        if isinstance(exc, DistributionInvalid):
            raise
        raise DistributionInvalid('the sdist is not a valid archive') from None
    if raw is None:
        raise DistributionInvalid('the sdist has no PKG-INFO')
    return parse_metadata(raw)


def read_distribution(path: Path, filename: str) -> CoreMetadata:
    """Core metadata of the distribution file at ``path`` named ``filename`` (a wheel or an sdist)."""
    if filename.endswith('.whl'):
        return read_wheel(path)
    if filename.endswith('.zip'):
        return _read_sdist_zip(path)
    if filename.endswith(('.tar.gz', '.tgz', '.tar.bz2', '.tar.xz', '.tar')):
        return _read_sdist_tar(path)
    raise DistributionInvalid('only wheels and sdists (.tar.gz, .zip) can be uploaded')
