"""NuGet package ids and versions (FORMATS.md "NuGet v3"), shared by the server, cleanup and the component index.

**Versions** follow ``NuGetVersion`` (SemVer 2.0 with an optional fourth number): ``Major[.Minor[.Patch[.Revision]]]
[-Release][+Metadata]``. The *normalised* form (what NuGet writes in registrations) has no leading zeros, at least three
numbers, the fourth only when it is not 0, and keeps the metadata; the *flat container* form (package URLs) is the
normalised form without metadata, in lower case. Order (``compare``): the numbers, then a version without a release
label after every prerelease of it, release labels compared by dot-separated identifiers (numeric ones numerically
and before text ones, text case-insensitively, fewer identifiers first); metadata is ignored.

**Ids**: letters, digits, ``.``, ``_`` and ``-``, at most 100 characters, compared case-insensitively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_ID = 100
MAX_VERSION = 256
_NUMBER_WIDTH = 10
_ID = re.compile(r'^[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9_])?$')
_VERSION = re.compile(
    r'^(?P<nums>\d+(?:\.\d+){0,3})'
    r'(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'
    r'(?:\+(?P<meta>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$'
)


@dataclass(frozen=True)
class NugetVersion:
    numbers: tuple[int, int, int, int]
    release: tuple[str, ...]  # prerelease identifiers as written ('' tuple for a stable version)
    metadata: str

    @property
    def is_prerelease(self) -> bool:
        return bool(self.release)

    @property
    def is_semver2(self) -> bool:
        """Only clients that ask for SemVer 2.0.0 see it (``semVerLevel``): build metadata or a dotted label."""
        return bool(self.metadata) or len(self.release) > 1

    @property
    def normalised(self) -> str:
        """``1.0.0``, ``1.0.0-beta.1+abc``, a fourth number only when it is not 0 (NuGet's ``ToNormalizedString`` plus
        metadata)."""
        major, minor, patch, revision = self.numbers
        text = f'{major}.{minor}.{patch}' + (f'.{revision}' if revision else '')
        if self.release:
            text += '-' + '.'.join(self.release)
        if self.metadata:
            text += '+' + self.metadata
        return text

    @property
    def flat(self) -> str:
        """The version as package URLs spell it: normalised, no metadata, lower case."""
        return self.normalised.split('+', 1)[0].lower()


def parse_version(text: str) -> NugetVersion | None:
    """A NuGet version, or None when ``text`` is not one."""
    if not text or len(text) > MAX_VERSION:
        return None
    match = _VERSION.match(text.strip())
    if match is None:
        return None
    parts = [int(n) for n in match['nums'].split('.')]
    if any(n >= 2**31 for n in parts):
        return None
    while len(parts) < 4:
        parts.append(0)
    pre = match['pre']
    release = tuple(pre.split('.')) if pre else ()
    # Numeric identifiers of a SemVer 2 label must not have leading zeros; NuGet tolerates them in legacy labels.
    return NugetVersion((parts[0], parts[1], parts[2], parts[3]), release, match['meta'] or '')


def normalise_version(text: str) -> str | None:
    found = parse_version(text)
    return found.normalised if found is not None else None


def flat_version(text: str) -> str | None:
    found = parse_version(text)
    return found.flat if found is not None else None


def valid_id(package_id: str) -> bool:
    return bool(_ID.match(package_id)) and '..' not in package_id


def _ident_key(ident: str) -> str:
    if ident.isdigit():  # numerically: length prefix, then the digits
        clean = (ident.lstrip('0') or '0')[:99]
        return f'0{len(clean):02d}{clean}'
    return '1' + ident.lower()


def sort_string(text: str) -> str:
    """A string whose byte order is NuGet's version order (see the module docstring). Anything that is not a NuGet
    version sorts before every version, by string."""
    found = parse_version(text)
    if found is None:
        return '0' + text[:MAX_VERSION]
    key = '1' + ''.join(str(n).zfill(_NUMBER_WIDTH) for n in found.numbers)
    if not found.release:
        return key + '~'  # a stable version sorts after its prereleases
    return key + '!' + '!'.join(_ident_key(i) for i in found.release)


def compare(a: str, b: str) -> int:
    """-1, 0 or 1 by NuGet version order (metadata ignored)."""
    ka, kb = sort_string(a), sort_string(b)
    return (ka > kb) - (ka < kb)


def is_prerelease(text: str) -> bool:
    found = parse_version(text)
    return found.is_prerelease if found is not None else False


def package_file(package_id: str, version: str) -> str:
    """The flat container path of a package (``<id>/<version>/<id>.<version>.nupkg``, lower case), relative to the
    flat container root."""
    lower = package_id.lower()
    return f'{lower}/{version}/{lower}.{version}.nupkg'


__all__ = [
    'MAX_ID',
    'NugetVersion',
    'compare',
    'flat_version',
    'is_prerelease',
    'normalise_version',
    'package_file',
    'parse_version',
    'sort_string',
    'valid_id',
]
