"""RubyGems names, versions and file names (FORMATS.md "RubyGems"), shared by the server, cleanup and the component
index.

**Names** follow rubygems.org's rule: letters, digits, ``.``, ``-`` and ``_``, at least one letter or digit, at most
``MAX_NAME`` characters. Names are case-sensitive (``RedCloth``).

**Versions** are ``Gem::Version`` strings: numbers and letters in ``.``-separated segments (``1.2.3``,
``2.0.0.rc1``; ``-`` reads as ``.pre.``). A version with a letter is a prerelease. **Order** (``sort_string``):
``Gem::Version#<=>`` on the canonical segments (numbers and letter runs; trailing zeros of the release part and of
the prerelease part dropped): a letter segment sorts before any number, so ``1.0.a < 1.0 < 1.0.1``; a missing
segment counts as 0.

**Files**: ``<name>-<version>.gem`` (platform ``ruby``) or ``<name>-<version>-<platform>.gem``; the compact index
names a version ``<version>`` or ``<version>-<platform>``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_NAME = 200
MAX_VERSION = 128
MAX_PLATFORM = 64
RUBY = 'ruby'
_NAME = re.compile(r'^(?=.*[A-Za-z0-9])[A-Za-z0-9._-]{1,200}$')
_VERSION = re.compile(r'^[0-9]+(?:\.[0-9A-Za-z]+)*(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$')
_PLAIN_VERSION = re.compile(r'^[0-9]+(?:\.[0-9A-Za-z]+)*$')  # no '-': as in file names followed by a platform
_PLATFORM = re.compile(r'^[A-Za-z][A-Za-z0-9_.-]{0,63}$')
_SEGMENT = re.compile(r'[0-9]+|[A-Za-z]+')
_WIDTH = 12
_MAX_SEGMENTS = 16


def valid_name(name: str) -> bool:
    return bool(_NAME.match(name)) and name not in ('.', '..')


def valid_version(version: str) -> bool:
    return len(version) <= MAX_VERSION and bool(_VERSION.match(version))


def valid_platform(platform: str) -> bool:
    return platform == RUBY or bool(_PLATFORM.match(platform))


def is_prerelease(version: str) -> bool:
    return any(c.isalpha() for c in version)


def _canonical(version: str) -> list[int | str]:
    parts: list[int | str] = [int(s) if s.isdigit() else s for s in _SEGMENT.findall(version.replace('-', '.pre.'))][
        : _MAX_SEGMENTS * 2
    ]
    first_string = next((i for i, s in enumerate(parts) if isinstance(s, str)), len(parts))
    out: list[int | str] = []
    for chunk in (parts[:first_string], parts[first_string:]):
        while chunk and chunk[-1] == 0:
            chunk = chunk[:-1]
        out.extend(chunk)
    return out


def sort_string(version: str) -> str:
    """A string that orders versions as ``Gem::Version#<=>`` does (see the module docstring)."""
    out = []
    for segment in _canonical(version)[:_MAX_SEGMENTS]:
        if isinstance(segment, int):
            out.append('2' + f'{min(segment, 10**_WIDTH - 1):0{_WIDTH}d}')
        else:
            out.append('1' + segment[:20] + '.')  # '.' sorts before letters: 'a' < 'ab'
    out.append('2' + '0' * _WIDTH)  # the end reads as a 0 segment
    return ''.join(out)


def newest(versions: Iterable[str]) -> str | None:
    """The newest release, else the newest prerelease."""
    pool = list(versions)
    releases = [v for v in pool if not is_prerelease(v)]
    chosen = releases or pool
    return max(chosen, key=sort_string) if chosen else None


def full_name(name: str, version: str, platform: str = RUBY) -> str:
    """``<name>-<version>`` or ``<name>-<version>-<platform>`` (``Gem::Specification#full_name``)."""
    return f'{name}-{version}' if platform in ('', RUBY) else f'{name}-{version}-{platform}'


def split_index_version(text: str) -> tuple[str, str] | None:
    """A compact index version (``1.2.3`` or ``1.2.3-x86_64-linux``) → (version, platform)."""
    version, sep, platform = text.partition('-')
    if not _PLAIN_VERSION.match(version) or len(version) > MAX_VERSION:
        return None
    if not sep:
        return version, RUBY
    return (version, platform) if valid_platform(platform) else None


def split_full_name(stem: str) -> list[tuple[str, str, str]]:
    """The readings of a file stem ``<name>-<version>[-<platform>]`` as (name, version, platform), shortest name
    first (a name may itself contain ``-<digit>``; the compact index decides which reading exists)."""
    out = []
    for found in re.finditer(r'-(?=[0-9])', stem):
        name, rest = stem[: found.start()], stem[found.end() :]
        parsed = split_index_version(rest)
        if parsed is not None and valid_name(name):
            out.append((name, parsed[0], parsed[1]))
    return out
