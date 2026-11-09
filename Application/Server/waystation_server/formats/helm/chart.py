"""Reading an uploaded chart package (``.tgz``) safely (FORMATS.md "Helm"; ARCHITECTURE §7 input hardening).

Nothing is extracted to disk: the archive is read as a stream (in a thread) and every member is checked the way an
extractor would need it to be (tar-slip guards): no absolute paths, no ``..``, no backslashes or control
characters, one top-level directory, regular files and directories only (no links or devices), bounded member count
and total size. ``<top>/Chart.yaml`` (at most ``MAX_CHART_YAML`` bytes) is parsed with the safe YAML loader of
``formats.helm.index`` and validated: ``apiVersion`` ``v1``/``v2``, a ``name`` Helm accepts and a SemVer ``version``.
"""

from __future__ import annotations

import re
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from waystation_common.errors import ValidationFailed
from waystation_server.formats.helm.index import is_semver, load_yaml, normalise_entry

MAX_CHART_YAML = 1024 * 1024
MAX_MEMBERS = 20_000
MAX_UNPACKED = 1024 * 1024 * 1024  # 1 GiB of member data
MAX_ENTRY_JSON = 256 * 1024
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,249}$')
_BAD = re.compile(r'[\x00-\x1f\x7f\\]')


@dataclass(frozen=True)
class ChartMeta:
    name: str
    version: str
    entry: dict[str, Any]  # Chart.yaml fields as the index lists them (Helm's types)

    @property
    def filename(self) -> str:
        return f'{self.name}-{self.version}.tgz'


def _member_name(name: str) -> list[str]:
    if not name or name.startswith('/') or _BAD.search(name):
        raise ValidationFailed('the chart archive has an unsafe file name')
    parts = [p for p in name.split('/') if p not in ('', '.')]
    if not parts or any(p == '..' for p in parts):
        raise ValidationFailed('the chart archive has an unsafe file name')
    return parts


def validate_metadata(raw: Any) -> ChartMeta:
    if not isinstance(raw, dict):
        raise ValidationFailed('Chart.yaml is not a mapping')
    entry = normalise_entry(raw)
    api = entry.get('apiVersion') or 'v1'
    if api not in ('v1', 'v2'):
        raise ValidationFailed('Chart.yaml apiVersion must be v1 or v2')
    entry['apiVersion'] = api
    name, version = entry.get('name'), entry.get('version')
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in ('.', '..'):
        raise ValidationFailed('Chart.yaml needs a name of letters, digits, ".", "_" and "-"')
    if not isinstance(version, str) or len(version) > 128 or not is_semver(version):
        raise ValidationFailed('Chart.yaml needs a SemVer version')
    for key in ('urls', 'digest', 'created', 'removed'):  # index-only fields come from the repository
        entry.pop(key, None)
    if len(repr(entry)) > MAX_ENTRY_JSON:
        raise ValidationFailed('Chart.yaml is too large')
    return ChartMeta(name=name, version=version, entry=entry)


def read_chart(path: Path) -> ChartMeta:
    """Validate the chart archive at ``path`` and return its metadata (blocking: run in a thread)."""
    top: str | None = None
    chart_yaml: bytes | None = None
    members = 0
    unpacked = 0
    try:
        with tarfile.open(path, mode='r|gz') as tar:
            for member in tar:
                members += 1
                if members > MAX_MEMBERS:
                    raise ValidationFailed('the chart archive has too many files')
                parts = _member_name(member.name)
                if not (member.isfile() or member.isdir()):
                    raise ValidationFailed('the chart archive may hold only files and directories')
                if top is None:
                    top = parts[0]
                elif parts[0] != top:
                    raise ValidationFailed('the chart archive must have a single top-level directory')
                unpacked += max(0, member.size)
                if unpacked > MAX_UNPACKED:
                    raise ValidationFailed('the chart archive is too large when unpacked')
                if member.isfile() and len(parts) == 2 and parts[1] == 'Chart.yaml':
                    if member.size > MAX_CHART_YAML:
                        raise ValidationFailed('Chart.yaml is too large')
                    handle = tar.extractfile(member)
                    if handle is None:  # pragma: no cover - regular files always have data
                        raise ValidationFailed('Chart.yaml is unreadable')
                    chart_yaml = handle.read(MAX_CHART_YAML + 1)
    except tarfile.TarError, EOFError, OSError, zlib.error:
        raise ValidationFailed('not a chart package (a gzip-compressed tar archive)') from None
    if chart_yaml is None:
        raise ValidationFailed('the chart archive has no <chart>/Chart.yaml')
    return validate_metadata(load_yaml(chart_yaml))
