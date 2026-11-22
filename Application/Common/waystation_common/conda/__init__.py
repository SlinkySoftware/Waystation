"""Conda package names, file names, subdirs and versions (FORMATS.md "Conda"), shared by the server, the worker,
cleanup and the component index.

**Files** are ``<name>-<version>-<build>.conda`` (format v2: a zip of zstd tarballs) or ``.tar.bz2`` (v1), in a
channel subdir (``noarch``, ``linux-64``, ``osx-arm64``...): ``<subdir>/<file>``. Names are lower case (letters,
digits, ``-``, ``_``, ``.``), versions and builds have no ``-``, so a file name splits at its last two ``-``.

**Versions** follow conda's ``VersionOrder`` (``conda.models.version``): an optional ``<epoch>!`` and ``+<local>``
part; components split at ``.`` and ``_`` (a trailing ``_`` stays, openssl style); each component splits into runs of
digits (numbers) and other characters (lower-cased strings, a ``0`` inserted before a leading string); ``dev`` sorts
before every other string, ``post`` after every number; strings sort before numbers; missing parts are ``0``, so
``1.1`` == ``1.1.0`` and ``1.1a1`` < ``1.1`` < ``1.1.post1``. ``compare`` is exact; ``sort_string`` gives the same
order as a plain string (``version_sort``) for versions of at most ``_COMPONENTS`` components of at most ``_TOKENS``
runs (longer ones are cut; such versions only tie).
"""

from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass
from itertools import zip_longest

PACKAGE_SUFFIXES = ('.conda', '.tar.bz2')
MAX_NAME = 128
MAX_VERSION = 64
MAX_BUILD = 128
MAX_KEY = 400
_NAME = re.compile(r'^[a-z0-9_][a-z0-9_.+-]*$')
_VERSION = re.compile(r'^[a-z0-9_.+!*]+$')
_VERSION_INPUT = re.compile(r'^[A-Za-z0-9_.+!*-]+$')
_BUILD = re.compile(r'^[A-Za-z0-9_.+]+$')
_SUBDIR = re.compile(r'^[a-z0-9]{1,20}(?:-[a-z0-9]{1,20})?$')
_RUNS = re.compile(r'([0-9]+|[*]+|[^0-9*]+)')
_COMPONENTS = 8
_TOKENS = 5
_LOCAL_COMPONENTS = 4
_PRERELEASE = frozenset({'dev', 'a', 'alpha', 'b', 'beta', 'c', 'rc', 'pre', 'preview'})

Token = int | float | str
Parts = list[list[Token]]


# ---------------------------------------------------------------------------- names
def valid_subdir(subdir: str) -> bool:
    """A channel subdir name: ``noarch`` or ``<platform>-<arch>`` (``linux-64``, ``osx-arm64``, ``wasi-wasm32``)."""
    return bool(_SUBDIR.match(subdir))


def valid_name(name: str) -> bool:
    return 0 < len(name) <= MAX_NAME and bool(_NAME.match(name))


def valid_version(version: str) -> bool:
    return 0 < len(version) <= MAX_VERSION and '-' not in version and parse_version(version) is not None


def valid_build(build: str) -> bool:
    return 0 < len(build) <= MAX_BUILD and bool(_BUILD.match(build))


@dataclass(frozen=True)
class PackageFile:
    name: str
    version: str
    build: str
    suffix: str  # '.conda' or '.tar.bz2'

    @property
    def filename(self) -> str:
        return f'{self.name}-{self.version}-{self.build}{self.suffix}'


def parse_filename(filename: str) -> PackageFile | None:
    """``<name>-<version>-<build>.conda|.tar.bz2`` (None when it is not a valid package file name)."""
    for suffix in PACKAGE_SUFFIXES:
        if filename.endswith(suffix):
            stem = filename[: -len(suffix)]
            break
    else:
        return None
    parts = stem.rsplit('-', 2)
    if len(parts) != 3:
        return None
    name, version, build = parts
    if not (valid_name(name) and valid_version(version) and valid_build(build)):
        return None
    return PackageFile(name, version, build, suffix)


def parse_path(path: str) -> tuple[str, PackageFile] | None:
    """``<subdir>/<package file>`` -> (subdir, file); None otherwise."""
    subdir, sep, filename = path.partition('/')
    if not sep or not valid_subdir(subdir):
        return None
    found = parse_filename(filename)
    return (subdir, found) if found is not None else None


# ---------------------------------------------------------------------------- versions
@functools.lru_cache(maxsize=4096)
def _parsed(version: str) -> tuple[Parts, Parts] | None:
    text = version.strip().lower()
    if not text or len(text) > MAX_VERSION * 4 or not _VERSION_INPUT.match(text):
        return None
    if not _VERSION.match(text):
        if '-' in text and '_' not in text:
            text = text.replace('-', '_')
        if not _VERSION.match(text):
            return None
    pieces = text.split('!')
    if len(pieces) > 2 or (len(pieces) == 2 and not pieces[0].isdigit()):
        return None
    epoch = pieces[0] if len(pieces) == 2 else '0'
    main_local = pieces[-1].split('+')
    if len(main_local) > 2 or not main_local[0]:
        return None
    main = main_local[0]
    local = main_local[1].replace('_', '.').split('.') if len(main_local) == 2 else []
    if main.endswith('_'):
        components = main[:-1].replace('_', '.').split('.')
        components[-1] += '_'
    else:
        components = main.replace('_', '.').split('.')
    out: list[Parts] = []
    for raw in ([epoch, *components], local):
        parts: Parts = []
        for component in raw:
            runs = _RUNS.findall(component)
            if not runs:
                return None
            tokens: list[Token] = []
            for run in runs:
                if run.isdigit():
                    tokens.append(int(run))
                elif run == 'post':
                    tokens.append(math.inf)
                elif run == 'dev':
                    tokens.append('DEV')
                else:
                    tokens.append(run)
            if not component[0].isdigit():  # components start with a number (conda keeps them in phase)
                tokens.insert(0, 0)
            parts.append(tokens)
        out.append(parts)
    return out[0], out[1]


def parse_version(version: str) -> tuple[Parts, Parts] | None:
    """(main components with the epoch first, local components), or None for a version conda refuses."""
    return _parsed(version)


def _less(a: Parts, b: Parts) -> int:
    empty: list[Token] = []
    for t1, t2 in zip_longest(a, b, fillvalue=empty):
        for c1, c2 in zip_longest(t1, t2, fillvalue=0):
            if c1 == c2:
                continue
            if isinstance(c1, str):
                if not isinstance(c2, str):
                    return -1
                return -1 if c1 < c2 else 1
            if isinstance(c2, str):
                return 1
            return -1 if c1 < c2 else 1
    return 0


def compare(a: str, b: str) -> int:
    """conda ``VersionOrder`` comparison (-1, 0, 1); versions conda refuses sort before valid ones, by text."""
    pa, pb = _parsed(a), _parsed(b)
    if pa is None or pb is None:
        if pa is None and pb is None:
            return (a > b) - (a < b)
        return -1 if pa is None else 1
    return _less(pa[0], pb[0]) or _less(pa[1], pb[1])


def _token(value: Token) -> str:
    if isinstance(value, str):
        return '1' + value + ' '
    if value == math.inf:
        return '4'
    number = int(value)
    if number == 0:
        return '2'
    digits = str(number)[:99]
    return f'3{len(digits):02d}{digits}'


def _encode(parts: Parts, components: int) -> str:
    out: list[str] = []
    for index in range(components):
        tokens = parts[index] if index < len(parts) else []
        for position in range(_TOKENS):
            out.append(_token(tokens[position]) if position < len(tokens) else '2')
    return ''.join(out)


def sort_string(version: str) -> str:
    """A string whose byte order is conda's version order (``version_sort``); see the module docstring."""
    parsed = _parsed(version)
    if parsed is None:
        return ('0' + version.lower())[:MAX_KEY]
    main, local = parsed
    return ('1' + _encode(main, 1 + _COMPONENTS) + _encode(local, _LOCAL_COMPONENTS))[:MAX_KEY]


def is_prerelease(version: str) -> bool:
    """A development or pre-release version (``1.0rc1``, ``2.0.dev3``, ``1.0a1``, ``1.0b2``)."""
    parsed = _parsed(version)
    if parsed is None:
        return False
    for component in parsed[0][1:]:
        for i, token in enumerate(component):
            if token == 'DEV':
                return True
            # a pre-release label is followed by its number (1.0b2, 2.0rc1); tzdata's 2025b is a release
            following = component[i + 1] if i + 1 < len(component) else None
            if isinstance(token, str) and token in _PRERELEASE and isinstance(following, int):
                return True
    return False


def newest(versions: list[str]) -> str | None:
    """The highest version of ``versions`` in conda order."""
    best: str | None = None
    for version in versions:
        if best is None or compare(version, best) > 0:
            best = version
    return best


__all__ = [
    'PACKAGE_SUFFIXES',
    'PackageFile',
    'compare',
    'is_prerelease',
    'newest',
    'parse_filename',
    'parse_path',
    'parse_version',
    'sort_string',
    'valid_build',
    'valid_name',
    'valid_subdir',
    'valid_version',
]
