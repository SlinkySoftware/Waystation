"""RubyGems repository routes and compact index documents (FORMATS.md "RubyGems"). Pure functions, no I/O.

Repository layout (``/repository/<repo>/``):

- compact index: ``versions`` (every gem with its versions and the MD5 of its info file), ``info/<gem>`` (one line
  per version: runtime dependencies, SHA-256 checksum, Ruby and RubyGems requirements), ``names``;
- ``gems/<name>-<version>[-<platform>].gem``: the packages;
- ``quick/Marshal.4.8/<name>-<version>[-<platform>].gemspec.rz``: a version's specification (zlib-deflated Ruby
  Marshal; opaque bytes for a proxy, written by ``formats.rubygems.marshal`` for a hosted repository);
- legacy indexes ``specs.4.8.gz``, ``latest_specs.4.8.gz``, ``prerelease_specs.4.8.gz`` (gzip of Ruby Marshal);
- ``api/v1/gems`` (``POST``: ``gem push``) and ``api/v1/gems/yank`` (``DELETE``: ``gem yank``);
- ``api/v1/dependencies``: the retired dependency API, always 404 (Bundler then uses the compact index).

Compact index lines (https://guides.rubygems.org/rubygems-org-compact-index-api/):
``versions``: ``created_at: <time>`` then ``---`` then ``<name> <v>[,<v>...] <md5 of info/<name>>``; the file only
grows: a later line for the same gem adds versions, ``-<v>`` removes one (a yank), its MD5 replaces the earlier.
``info/<name>``: ``---`` then ``<version>[-<platform>] <dep>:<req>[&<req>][,<dep>...]|checksum:<sha256>[,ruby:<req>]
[,rubygems:<req>]``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from waystation_common import rubygems
from waystation_common.errors import UpstreamError

VERSIONS = 'versions'
NAMES = 'names'
INFO = 'info'
GEMS = 'gems'
QUICK = 'quick/Marshal.4.8'
SPECS = ('specs.4.8.gz', 'latest_specs.4.8.gz', 'prerelease_specs.4.8.gz')
PUSH = 'api/v1/gems'
YANK = 'api/v1/gems/yank'
DEPENDENCIES = 'api/v1/dependencies'
TEXT_TYPE = 'text/plain; charset=utf-8'
GEM_TYPE = 'application/octet-stream'
SPEC_TYPE = 'application/octet-stream'
GZIP_TYPE = 'application/x-gzip'
MAX_VERSIONS_FILE = 512 * 1024 * 1024  # rubygems.org: ~30 MiB in 2026
MAX_INFO = 32 * 1024 * 1024
MAX_NAMES = 64 * 1024 * 1024
MAX_SPECS = 256 * 1024 * 1024
MAX_GEMSPEC = 16 * 1024 * 1024
MAX_LINE = 64 * 1024
REQUIREMENT_OPS = ('=', '!=', '>', '<', '>=', '<=', '~>')


class Op(StrEnum):
    VERSIONS = 'versions'
    NAMES = 'names'
    INFO = 'info'
    GEM = 'gem'
    GEMSPEC = 'gemspec'
    SPECS = 'specs'
    PUSH = 'push'
    YANK = 'yank'
    DEPENDENCIES = 'dependencies'


@dataclass(frozen=True)
class Route:
    op: Op
    name: str = ''  # info: the gem; gem/gemspec: the file stem <name>-<version>[-<platform>]; specs: the file
    key: str = ''


def parse_route(path: str) -> Route | None:
    """Map a normalised repository path to a RubyGems operation (None: not a RubyGems path)."""
    if path == VERSIONS:
        return Route(Op.VERSIONS, key=path)
    if path == NAMES:
        return Route(Op.NAMES, key=path)
    if path in SPECS:
        return Route(Op.SPECS, path, key=path)
    if path == PUSH:
        return Route(Op.PUSH, key=path)
    if path == YANK:
        return Route(Op.YANK, key=path)
    if path == DEPENDENCIES or path.startswith((DEPENDENCIES + '.', DEPENDENCIES + '/', DEPENDENCIES + '?')):
        return Route(Op.DEPENDENCIES, key=path)
    head, _, rest = path.partition('/')
    if head == INFO and rest and '/' not in rest and rubygems.valid_name(rest):
        return Route(Op.INFO, rest, key=path)
    if head == GEMS and rest.endswith('.gem') and '/' not in rest:
        stem = rest[: -len('.gem')]
        return Route(Op.GEM, stem, key=path) if rubygems.split_full_name(stem) else None
    if path.startswith(QUICK + '/') and path.endswith('.gemspec.rz'):
        stem = path[len(QUICK) + 1 : -len('.gemspec.rz')]
        if '/' not in stem and rubygems.split_full_name(stem):
            return Route(Op.GEMSPEC, stem, key=path)
    return None


def gem_path(full_name: str) -> str:
    return f'{GEMS}/{full_name}.gem'


def gemspec_path(full_name: str) -> str:
    return f'{QUICK}/{full_name}.gemspec.rz'


def md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------- info files
@dataclass(frozen=True)
class InfoLine:
    """One version of an info file (``raw``: the line as it was read, without the newline)."""

    version: str
    platform: str
    dependencies: tuple[tuple[str, str], ...]  # (gem, requirement as "op v&op v")
    checksum: str | None
    ruby: str | None
    rubygems: str | None
    raw: str

    @property
    def key(self) -> str:
        return self.version if self.platform == rubygems.RUBY else f'{self.version}-{self.platform}'


def _hex64(value: str) -> str | None:
    value = value.strip().lower()
    return value if len(value) == 64 and all(c in '0123456789abcdef' for c in value) else None


def parse_info_line(line: str) -> InfoLine | None:
    """A version line of an info file (None: not a valid line; skipped)."""
    if len(line) > MAX_LINE:
        return None
    head, _, tail = line.partition('|')
    key, _, deps_text = head.partition(' ')
    parsed = rubygems.split_index_version(key)
    if parsed is None:
        return None
    deps: list[tuple[str, str]] = []
    for item in deps_text.strip().split(',') if deps_text.strip() else ():
        dep, sep, requirement = item.partition(':')
        if not sep or not rubygems.valid_name(dep):
            continue
        deps.append((dep, requirement.strip()))
    checksum = ruby = gems = None
    for item in tail.split(','):
        name, sep, value = item.partition(':')
        if not sep:
            continue
        match name.strip():
            case 'checksum':
                checksum = _hex64(value)
            case 'ruby':
                ruby = value.strip()
            case 'rubygems':
                gems = value.strip()
    return InfoLine(parsed[0], parsed[1], tuple(deps), checksum, ruby, gems, line)


def parse_info(data: bytes) -> list[InfoLine]:
    """The version lines of an info file (bounded; invalid lines skipped)."""
    if len(data) > MAX_INFO:
        raise UpstreamError('the info file is too large')
    text = data.decode('utf-8', 'replace')
    out = []
    for line in text.split('\n'):
        if not line or line == '---' or line.startswith('created_at:'):
            continue
        found = parse_info_line(line)
        if found is not None:
            out.append(found)
    return out


def requirement_text(requirements: Sequence[tuple[str, str]]) -> str:
    """``[('>=', '1.0'), ('<', '2')]`` → ``>= 1.0&< 2`` (compact index form)."""
    return '&'.join(f'{op} {version}' for op, version in requirements)


def is_default_requirement(requirements: Sequence[tuple[str, str]]) -> bool:
    return not requirements or list(requirements) == [('>=', '0')]


def render_info_line(
    version: str,
    platform: str,
    dependencies: Sequence[tuple[str, Sequence[tuple[str, str]]]],
    checksum: str,
    ruby: Sequence[tuple[str, str]] = (),
    gems: Sequence[tuple[str, str]] = (),
) -> str:
    key = version if platform == rubygems.RUBY else f'{version}-{platform}'
    deps = ','.join(f'{name}:{requirement_text(reqs) or ">= 0"}' for name, reqs in dependencies)
    tail = [f'checksum:{checksum}']
    if not is_default_requirement(ruby):
        tail.append(f'ruby:{requirement_text(ruby)}')
    if not is_default_requirement(gems):
        tail.append(f'rubygems:{requirement_text(gems)}')
    return f'{key} {deps}|{",".join(tail)}'


def render_info(lines: Iterable[str]) -> bytes:
    return ('---\n' + ''.join(line + '\n' for line in lines)).encode()


def merge_info(members: Sequence[Sequence[InfoLine]]) -> list[InfoLine]:
    """Members' info lines merged: the first member that has a version (and platform) wins it; order kept."""
    chosen: dict[str, InfoLine] = {}
    for lines in members:
        for line in lines:
            chosen.setdefault(line.key, line)
    return list(chosen.values())


# ---------------------------------------------------------------------------- versions files
@dataclass
class GemVersions:
    """A gem's current versions in a versions file (in the order they were added) and the MD5 of its info file."""

    versions: list[str] = field(default_factory=list)
    md5: str = ''


def parse_versions(data: bytes) -> dict[str, GemVersions]:
    """Every gem of a versions file with its current versions (yanks applied) and the newest MD5 (blocking: large)."""
    out: dict[str, GemVersions] = {}
    started = False
    for raw in data.split(b'\n'):
        if not started:
            started = raw == b'---'
            continue
        if not raw or len(raw) > MAX_LINE * 64:
            continue
        parts = raw.decode('utf-8', 'replace').split(' ')
        if len(parts) != 3 or not rubygems.valid_name(parts[0]):
            continue
        name, versions, digest = parts
        entry = out.setdefault(name, GemVersions())
        for version in versions.split(','):
            if version.startswith('-'):
                if version[1:] in entry.versions:
                    entry.versions.remove(version[1:])
            elif version and version not in entry.versions:
                entry.versions.append(version)
        entry.md5 = digest
    return out


def render_versions(gems: Mapping[str, GemVersions], created: datetime) -> bytes:
    head = f'created_at: {created.strftime("%Y-%m-%dT%H:%M:%SZ")}\n---\n'
    lines = [f'{name} {",".join(g.versions)} {g.md5}\n' for name, g in gems.items() if g.versions]
    return (head + ''.join(lines)).encode()


def render_names(names: Iterable[str]) -> bytes:
    return ('---\n' + ''.join(f'{n}\n' for n in names)).encode()


def check_versions(data: bytes) -> None:
    """A versions file the proxy accepts from upstream: a ``---`` line near the start (else refused)."""
    if b'\n---\n' not in data[:4096] and not data.startswith(b'---\n'):
        raise UpstreamError('the upstream versions file is not a compact index')


def check_index_text(data: bytes, what: str) -> None:
    if data and not data.startswith(b'---'):
        raise UpstreamError(f'the upstream {what} is not a compact index file')
