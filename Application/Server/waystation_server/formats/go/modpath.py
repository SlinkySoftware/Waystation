"""Go module paths and versions as the GOPROXY protocol carries them (``go help goproxy``; FORMATS.md "Go modules").

Module paths and versions are **case-encoded** in URLs: every upper-case letter is ``!`` + its lower-case form
(``github.com/Azure/x`` → ``github.com/!azure/x``), so they are safe on case-insensitive file systems. The functions
here follow ``golang.org/x/mod/module``:

- ``unescape_path`` / ``unescape_version`` decode (``!`` must be followed by a lower-case letter; an upper-case letter
  in the escaped form is accepted and normalised, as the go command never sends one) and validate the decoded value;
  ``escape_path`` / ``escape_version`` give the one canonical escaped form, used for asset keys and upstream URLs.
- A module path (``CheckPath``): ASCII letters, digits and ``-._~``; elements separated by single ``/``, none empty,
  starting with ``.`` or ending with ``.``, or a Windows short name (``~`` + digits); the first element is a host
  name (lower-case letters, digits, ``-`` and ``.``, at least one dot, not starting with ``-``).
- A version as a URL element: ASCII letters, digits and ``-._~+``, at most 128 characters, not starting with ``.``.
  ``.mod`` and ``.zip`` need a **canonical** semantic version (``v1.2.3``, ``v1.2.3-pre.1``, pseudo-versions
  ``v0.0.0-20191109021931-daa7c04131f5``, ``+incompatible``); ``.info`` also answers queries (``master``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

MAX_PATH = 500
MAX_VERSION = 128

_ELEM = re.compile(r'^[A-Za-z0-9._~-]+$')
_FIRST = re.compile(r'^[a-z0-9.-]+$')
_SHORT_NAME = re.compile(r'~[0-9]+$')
_VERSION_CHARS = re.compile(r'^[A-Za-z0-9._~+-]+$')
_NUM = r'(?:0|[1-9][0-9]*)'
_PRE_IDENT = r'(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)'
_CANONICAL = re.compile(rf'^v{_NUM}\.{_NUM}\.{_NUM}(?:-{_PRE_IDENT}(?:\.{_PRE_IDENT})*)?(?:\+incompatible)?$')
# Pseudo-versions (golang.org/x/mod/module/pseudo.go ``pseudoVersionRE``): vX.0.0-yyyymmddhhmmss-abcdefabcdef,
# vX.Y.(Z+1)-0.yyyymmddhhmmss-abcdefabcdef, vX.Y.Z-pre.0.yyyymmddhhmmss-abcdefabcdef (+incompatible).
_PSEUDO = re.compile(
    r'^v[0-9]+\.(?:0\.0-|[0-9]+\.[0-9]+-(?:[^+]*\.)?0\.)[0-9]{14}-[A-Za-z0-9]+(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$'
)


class InvalidModule(ValueError):  # noqa: N818 - reads as a state
    pass


def _unescape(text: str) -> str:
    out: list[str] = []
    bang = False
    for ch in text:
        if ord(ch) >= 0x80:
            raise InvalidModule('non-ASCII character')
        if bang:
            bang = False
            if not 'a' <= ch <= 'z':
                raise InvalidModule('"!" must be followed by a lower-case letter')
            out.append(ch.upper())
            continue
        if ch == '!':
            bang = True
            continue
        out.append(ch)
    if bang:
        raise InvalidModule('trailing "!"')
    return ''.join(out)


def _escape(text: str) -> str:
    return ''.join(f'!{ch.lower()}' if 'A' <= ch <= 'Z' else ch for ch in text)


def _check_elem(elem: str) -> None:
    if not elem:
        raise InvalidModule('empty path element')
    if not _ELEM.match(elem):
        raise InvalidModule('invalid character in path element')
    if elem.strip('.') == '' or elem.startswith('.') or elem.endswith('.'):
        raise InvalidModule('path element starts or ends with a dot')
    if _SHORT_NAME.search(elem):
        raise InvalidModule('path element looks like a Windows short name')


def check_path(path: str) -> None:
    """``module.CheckPath``: raise ``InvalidModule`` unless ``path`` (decoded) is a valid module path."""
    if not path or len(path) > MAX_PATH:
        raise InvalidModule('invalid module path length')
    elems = path.split('/')
    for elem in elems:
        _check_elem(elem)
    first = elems[0]
    if not _FIRST.match(first) or '.' not in first or first.startswith('-'):
        raise InvalidModule('the first path element must be a host name')


def check_version(version: str) -> None:
    """A version (or a version query) as one URL path element (decoded)."""
    if not version or len(version) > MAX_VERSION or not _VERSION_CHARS.match(version) or version.startswith('.'):
        raise InvalidModule('invalid version')


def unescape_path(escaped: str) -> str:
    path = _unescape(escaped)
    check_path(path)
    return path


def escape_path(path: str) -> str:
    check_path(path)
    return _escape(path)


def unescape_version(escaped: str) -> str:
    version = _unescape(escaped)
    check_version(version)
    return version


def escape_version(version: str) -> str:
    check_version(version)
    return _escape(version)


def is_canonical(version: str) -> bool:
    """A canonical semantic version as module versions use it (build metadata only ``+incompatible``)."""
    return bool(_CANONICAL.match(version))


def is_pseudo(version: str) -> bool:
    """A pseudo-version (a commit without a tag): ``v0.0.0-20191109021931-daa7c04131f5`` and its variants."""
    return is_canonical(version) and bool(_PSEUDO.match(version))


class Op(StrEnum):
    LIST = 'list'
    LATEST = 'latest'
    INFO = 'info'
    MOD = 'mod'
    ZIP = 'zip'
    SUMDB = 'sumdb'


FILE_OPS = {'info': Op.INFO, 'mod': Op.MOD, 'zip': Op.ZIP}


@dataclass(frozen=True)
class Route:
    op: Op
    module: str = ''  # decoded module path
    version: str = ''  # decoded version (INFO/MOD/ZIP)
    sumdb: str = ''  # SUMDB: the database name (``sum.golang.org``)
    rest: str = ''  # SUMDB: the path below ``sumdb/<name>/``

    @property
    def escaped_module(self) -> str:
        return _escape(self.module)

    @property
    def escaped_version(self) -> str:
        return _escape(self.version)

    @property
    def key(self) -> str:
        """The canonical repository path (asset key and upstream path) of this route."""
        match self.op:
            case Op.LIST:
                return f'{self.escaped_module}/@v/list'
            case Op.LATEST:
                return f'{self.escaped_module}/@latest'
            case Op.SUMDB:
                return f'sumdb/{self.sumdb}/{self.rest}'
        return f'{self.escaped_module}/@v/{self.escaped_version}.{self.op.value}'


def parse_route(path: str) -> Route | None:
    """Map a normalised repository path to a GOPROXY operation (None: not one)."""
    segments = path.split('/')
    if len(segments) >= 3 and segments[0] == 'sumdb':
        return Route(Op.SUMDB, sumdb=segments[1], rest='/'.join(segments[2:]))
    try:
        if len(segments) >= 2 and segments[-1] == '@latest':
            return Route(Op.LATEST, unescape_path('/'.join(segments[:-1])))
        if len(segments) < 3 or segments[-2] != '@v':
            return None
        module = unescape_path('/'.join(segments[:-2]))
        name = segments[-1]
        if name == 'list':
            return Route(Op.LIST, module)
        stem, dot, ext = name.rpartition('.')
        op = FILE_OPS.get(ext) if dot else None
        if op is None:
            return None
        version = unescape_version(stem)
        if op is not Op.INFO and not is_canonical(version):
            return None
        return Route(op, module, version)
    except InvalidModule:
        return None


def module_of_asset(path: str) -> tuple[str, str | None] | None:
    """``(module, version)`` of a cached GOPROXY asset path (version None: ``list`` / ``@latest``); None otherwise."""
    route = parse_route(path)
    if route is None or route.op is Op.SUMDB:
        return None
    return route.module, route.version or None
