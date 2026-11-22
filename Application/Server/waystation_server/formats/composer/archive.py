"""Reading Composer package archives safely (FORMATS.md "Composer"; ARCHITECTURE §7). Blocking: run in a thread.

Nothing is extracted. A zip's central directory is checked the way an extracting client needs it to be (zip-slip
guards: every entry name relative, without ``..`` segments, drive letters, NUL or control characters; no encrypted
entries; at most ``MAX_ENTRIES`` entries whose declared sizes total at most ``MAX_UNPACKED``). A tar archive (proxies
only) is checked the same way by its member headers (regular files and directories only).

``read_composer_json`` (hosted uploads) also finds ``composer.json`` at the root or in the archive's single top-level
folder (GitHub/GitLab archives), reads it bounded to ``MAX_COMPOSER_JSON`` bytes and validates it: a JSON object
whose ``name`` (when given) is the package being uploaded and whose ``version`` (when given) is the version uploaded.
Only the package fields Composer repositories publish are kept (``ENTRY_KEYS``), bounded in size.
"""

from __future__ import annotations

import json
import re
import tarfile
import zipfile
import zlib
from typing import IO, Any

from waystation_common import composer
from waystation_common.errors import ValidationFailed
from waystation_server.formats.composer.protocol import ENTRY_KEYS

MAX_ENTRIES = 100_000
MAX_UNPACKED = 4 * 1024**3  # declared, before anything is decompressed
MAX_COMPOSER_JSON = 1024 * 1024
MAX_DETAILS = 256 * 1024  # the kept composer.json fields, serialised
_BAD = re.compile(r'[\x00-\x1f\x7f]')
_DRIVE = re.compile(r'^[A-Za-z]:')
# Fields a hosted repository computes itself (never taken from the uploaded composer.json).
COMPUTED = frozenset({'name', 'version', 'version_normalized', 'dist', 'source', 'time', 'notification-url'})


def safe_name(name: str) -> str:
    """The entry name as a relative path (``\\`` read as ``/``); raises ``ValidationFailed`` when unsafe."""
    clean = name.replace('\\', '/')
    if not clean or clean.startswith('/') or _DRIVE.match(clean) or _BAD.search(clean):
        raise ValidationFailed('the archive has an unsafe file name')
    parts = [p for p in clean.split('/') if p not in ('', '.')]
    if any(p == '..' for p in parts):
        raise ValidationFailed('the archive has an unsafe file name')
    return '/'.join(parts) + ('/' if clean.endswith('/') and parts else '')


def _zip_entries(archive: zipfile.ZipFile) -> list[tuple[str, zipfile.ZipInfo]]:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ValidationFailed('the archive has too many files')
    total = 0
    out: list[tuple[str, zipfile.ZipInfo]] = []
    for info in infos:
        name = safe_name(info.filename)
        if info.flag_bits & 0x1:
            raise ValidationFailed('the archive has encrypted files')
        total += max(0, info.file_size)
        if total > MAX_UNPACKED:
            raise ValidationFailed('the archive is too large when unpacked')
        if name:
            out.append((name, info))
    return out


def check_zip(file: IO[bytes]) -> None:
    """Refuse (``ValidationFailed``) a zip archive that is unreadable or unsafe to extract."""
    try:
        with zipfile.ZipFile(file) as archive:
            _zip_entries(archive)
    except zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, NotImplementedError:
        raise ValidationFailed('not a zip archive') from None


def check_tar(file: IO[bytes]) -> None:
    """Refuse (``ValidationFailed``) a tar archive that is unreadable or unsafe to extract."""
    total = count = 0
    try:
        with tarfile.open(fileobj=file, mode='r:*') as archive:
            for member in archive:
                count += 1
                if count > MAX_ENTRIES:
                    raise ValidationFailed('the archive has too many files')
                safe_name(member.name)
                if not (member.isfile() or member.isdir()) and member.type not in (tarfile.XHDTYPE, tarfile.XGLTYPE):
                    raise ValidationFailed('the archive has links or special files')
                total += max(0, member.size)
                if total > MAX_UNPACKED:
                    raise ValidationFailed('the archive is too large when unpacked')
    except tarfile.TarError, EOFError, OSError, zlib.error:
        raise ValidationFailed('not a tar archive') from None


def check_archive(file: IO[bytes], kind: str) -> None:
    if kind == 'zip':
        check_zip(file)
    else:
        check_tar(file)


def _composer_entry(entries: list[tuple[str, zipfile.ZipInfo]]) -> zipfile.ZipInfo:
    files = [(n, i) for n, i in entries if not n.endswith('/')]
    root = [i for n, i in files if n == 'composer.json']
    if root:
        return root[0]
    tops = {n.split('/', 1)[0] for n, _ in files}
    if len(tops) == 1:
        top = tops.pop()
        nested = [i for n, i in files if n == f'{top}/composer.json']
        if nested:
            return nested[0]
    raise ValidationFailed('the archive has no composer.json at its root (or in its single top-level folder)')


def read_composer_json(file: IO[bytes]) -> dict[str, Any]:
    """The ``composer.json`` of the zip in ``file`` (seekable), parsed, after checking the archive."""
    try:
        with zipfile.ZipFile(file) as archive:
            info = _composer_entry(_zip_entries(archive))
            if info.file_size > MAX_COMPOSER_JSON:
                raise ValidationFailed('composer.json is too large')
            with archive.open(info) as handle:
                data = handle.read(MAX_COMPOSER_JSON + 1)
    except zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, zlib.error, NotImplementedError:
        raise ValidationFailed('not a Composer package (a zip archive)') from None
    except RuntimeError:  # encrypted members (zipfile asks for a password)
        raise ValidationFailed('the archive has encrypted files') from None
    if len(data) > MAX_COMPOSER_JSON:
        raise ValidationFailed('composer.json is too large')
    try:
        doc = json.loads(data.decode('utf-8-sig'))
    except ValueError:
        raise ValidationFailed('composer.json is not valid JSON') from None
    if not isinstance(doc, dict):
        raise ValidationFailed('composer.json is not a JSON object')
    return doc


def package_details(doc: dict[str, Any], name: str, version: str) -> dict[str, Any]:
    """The published fields of an uploaded package's ``composer.json``, checked against the upload's name and
    version (normalised); raises ``ValidationFailed``."""
    given = doc.get('name')
    if given is not None and (not isinstance(given, str) or composer.normalise_name(given) != name):
        raise ValidationFailed(f'composer.json names the package {str(given)[:200]!r}, not {name}')
    declared = doc.get('version')
    if declared is not None and (
        not isinstance(declared, str) or composer.normalise(declared) != composer.normalise(version)
    ):
        raise ValidationFailed(f'composer.json declares the version {str(declared)[:100]!r}, not {version}')
    kind = doc.get('type')
    if kind is not None and (not isinstance(kind, str) or len(kind) > 100):
        raise ValidationFailed('composer.json has an invalid type')
    for key in ('require', 'require-dev', 'conflict', 'replace', 'provide', 'suggest', 'autoload', 'autoload-dev'):
        if key in doc and not isinstance(doc[key], dict | list):
            raise ValidationFailed(f'composer.json has an invalid {key}')
    for key in ('require', 'require-dev', 'conflict', 'replace', 'provide'):
        for dependency, constraint in (doc.get(key) or {}).items() if isinstance(doc.get(key), dict) else ():
            if not isinstance(constraint, str) or len(constraint) > 1000 or len(dependency) > composer.MAX_NAME:
                raise ValidationFailed(f'composer.json has an invalid {key} entry')
    details = {k: v for k, v in doc.items() if k in ENTRY_KEYS and k not in COMPUTED}
    if len(json.dumps(details)) > MAX_DETAILS:
        raise ValidationFailed('composer.json is too large')
    return details
