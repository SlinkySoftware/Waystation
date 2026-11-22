"""Ruby Marshal 4.8 **writer** for the documents a hosted RubyGems repository serves (FORMATS.md "RubyGems").

Waystation never reads Ruby Marshal data (a proxy passes ``*.gemspec.rz`` and ``specs.4.8.gz`` through as opaque
bytes). It only writes the two shapes RubyGems loads with ``Gem::SafeMarshal`` (which allows exactly these classes,
instance variables and symbols):

- ``quick/Marshal.4.8/<full name>.gemspec.rz``: zlib of ``Marshal.dump(spec)``, i.e. a ``Gem::Specification`` user
  type (``_dump``) whose payload is ``Marshal.dump`` of the 19-element array ``Gem::Specification._load`` reads;
- ``specs.4.8.gz`` (and ``latest_``/``prerelease_``): gzip of ``Marshal.dump`` of ``[[name, Gem::Version, platform],
  ...]``.

Types written: nil, true, false, Integer (fixnum range), String (UTF-8, with the ``E`` encoding ivar), Symbol (with
symbol links), Array, Hash, ``Gem::Version`` / ``Gem::Requirement`` (``marshal_dump``: type ``U``), ``Gem::Dependency``
(an object with ivars: type ``o``) and ``Gem::Specification`` (``_dump``: type ``u``).
"""

from __future__ import annotations

import gzip
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

HEADER = b'\x04\x08'


@dataclass(frozen=True)
class Sym:
    name: str


@dataclass(frozen=True)
class Version:
    version: str


@dataclass(frozen=True)
class Requirement:
    requirements: tuple[tuple[str, str], ...]  # (op, version)


@dataclass(frozen=True)
class Dependency:
    name: str
    requirement: Requirement
    type: str  # runtime / development


def _long(n: int) -> bytes:
    """Marshal's ``w_long``."""
    if n == 0:
        return b'\x00'
    if 0 < n < 123:
        return bytes([n + 5])
    if -124 < n < 0:
        return bytes([(n - 5) & 0xFF])
    out = bytearray()
    x = n
    for size in range(1, 5):
        out.append(x & 0xFF)
        x >>= 8  # arithmetic shift, as Ruby's RSHIFT
        if x == 0:
            return bytes([size]) + bytes(out)
        if x == -1:
            return bytes([256 - size]) + bytes(out)
    raise ValueError('integer out of the fixnum range')


class _Writer:
    def __init__(self) -> None:
        self.out = bytearray(HEADER)
        self.symbols: dict[str, int] = {}

    def symbol(self, name: str) -> None:
        index = self.symbols.get(name)
        if index is not None:
            self.out += b';' + _long(index)
            return
        self.symbols[name] = len(self.symbols)
        raw = name.encode()
        self.out += b':' + _long(len(raw)) + raw

    def string(self, value: str) -> None:
        raw = value.encode('utf-8')
        self.out += b'I"' + _long(len(raw)) + raw + _long(1)
        self.symbol('E')
        self.out += b'T'

    def value(self, value: Any) -> None:
        match value:
            case None:
                self.out += b'0'
            case True:
                self.out += b'T'
            case False:
                self.out += b'F'
            case int():
                if not -(1 << 30) <= value < (1 << 30):
                    raise ValueError('integer out of the fixnum range')
                self.out += b'i' + _long(value)
            case str():
                self.string(value)
            case Sym():
                self.symbol(value.name)
            case Version():
                self.out += b'U'
                self.symbol('Gem::Version')
                self.value([value.version])
            case Requirement():
                self.out += b'U'
                self.symbol('Gem::Requirement')
                self.value([[[op, Version(version)] for op, version in value.requirements]])
            case Dependency():
                self.out += b'o'
                self.symbol('Gem::Dependency')
                ivars: list[tuple[str, Any]] = [
                    ('@name', value.name),
                    ('@requirement', value.requirement),
                    ('@type', Sym(value.type)),
                    ('@prerelease', False),
                ]
                self.out += _long(len(ivars))
                for name, item in ivars:
                    self.symbol(name)
                    self.value(item)
            case list() | tuple():
                self.out += b'[' + _long(len(value))
                for item in value:
                    self.value(item)
            case dict():
                self.out += b'{' + _long(len(value))
                for key, item in value.items():
                    self.value(key)
                    self.value(item)
            case _:
                raise TypeError(f'cannot marshal {type(value).__name__}')


def dump(value: Any) -> bytes:
    writer = _Writer()
    writer.value(value)
    return bytes(writer.out)


def _user_defined(class_name: str, payload: bytes) -> bytes:
    writer = _Writer()
    writer.out += b'u'
    writer.symbol(class_name)
    writer.out += _long(len(payload)) + payload
    return bytes(writer.out)


@dataclass(frozen=True)
class SpecFields:
    """What a hosted gemspec carries (from the pushed gem's metadata)."""

    name: str
    version: str
    platform: str
    date: str  # YYYY-MM-DD
    summary: str
    description: str | None
    authors: Sequence[str]
    email: str | Sequence[str] | None
    homepage: str | None
    licenses: Sequence[str]
    metadata: Mapping[str, str]
    dependencies: Sequence[Dependency]
    required_ruby_version: Requirement
    required_rubygems_version: Requirement
    rubygems_version: str
    specification_version: int


def gemspec(spec: SpecFields) -> bytes:
    """``Marshal.dump(spec)`` of a ``Gem::Specification`` (the ``_dump`` array ``_load`` reads)."""
    fields: list[Any] = [
        spec.rubygems_version,
        spec.specification_version,
        spec.name,
        Version(spec.version),
        spec.date,  # Gem::Specification#date= accepts "YYYY-MM-DD"
        spec.summary,
        spec.required_ruby_version,
        spec.required_rubygems_version,
        spec.platform,
        list(spec.dependencies),
        '',  # rubyforge_project (removed)
        spec.email if isinstance(spec.email, str) or spec.email is None else list(spec.email),
        list(spec.authors),
        spec.description,
        spec.homepage,
        True,  # has_rdoc (retired)
        spec.platform,
        list(spec.licenses),
        dict(spec.metadata),
    ]
    return _user_defined('Gem::Specification', dump(fields))


def gemspec_rz(spec: SpecFields) -> bytes:
    """``quick/Marshal.4.8/<full name>.gemspec.rz``: zlib (``Gem::Util.deflate``) of the marshalled spec."""
    return zlib.compress(gemspec(spec), 9)


def specs_gz(tuples: Sequence[tuple[str, str, str]]) -> bytes:
    """``specs.4.8.gz``: gzip of ``[[name, Gem::Version, platform], ...]`` (mtime 0: the same list, the same bytes)."""
    body = dump([[name, Version(version), platform] for name, version, platform in tuples])
    return gzip.compress(body, compresslevel=6, mtime=0)
