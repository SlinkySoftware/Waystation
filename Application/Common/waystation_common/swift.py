"""Swift Package Registry naming rules shared by the server (routes, publishing, the SCM-backed proxy, browse), the
component index and cleanup (FORMATS.md "Swift Package Registry"; SE-0292 registry specification §3.6).

- **Scopes**: letters, digits and single inner hyphens, at most 39 characters; **names**: letters, digits and single
  inner ``-``/``_``, at most 100 characters. Both compare case-insensitively: records and asset paths use the
  lower-cased keys, responses the case a release was first published (or pinned) with.
- **Versions**: SemVer 2.0.0 (``waystation_common.galaxy.SEMVER``, at most 128 characters). Git tags of an
  SCM-backed proxy are read leniently (``v`` prefix, two components) and normalised to a three-part version.
- **Asset paths** (per repository, lower-case keys): the source archive ``<scope>/<name>/<version>.zip``, the
  manifests ``<scope>/<name>/<version>/Package.swift`` and ``.../Package@swift-<x>.swift``; the SCM proxy's cached
  upstream files live under ``_scm/`` (``_scm/<scope>/<name>/refs``, ``_scm/<scope>/<name>/<tag>.tar.gz``). A
  leading ``_`` can never be a scope, so the two never collide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from waystation_common.galaxy import MAX_VERSION, SEMVER

SCOPE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$')
NAME = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9]|[-_](?=[A-Za-z0-9])){0,99}$')
# A Git tag read as a version: optional "v", two or three numeric components, SemVer pre-release and build parts.
_TAG = re.compile(
    r'^v?(0|[1-9]\d{0,9})\.(0|[1-9]\d{0,9})(?:\.(0|[1-9]\d{0,9}))?'
    r'(-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$'
)
MANIFEST = 'Package.swift'
# SE-0292 §4.3: version-specific manifests at the package root.
VERSIONED_MANIFEST = re.compile(r'^Package@swift-(\d+)(?:\.(\d+))?(?:\.(\d+))?\.swift$')
SCM_ROOT = '_scm'
ARCHIVE_SUFFIX = '.zip'


def valid_scope(scope: str) -> bool:
    return bool(SCOPE.match(scope))


def valid_name(name: str) -> bool:
    return bool(NAME.match(name))


def valid_version(version: str) -> bool:
    return len(version) <= MAX_VERSION and bool(SEMVER.match(version))


def version_of_tag(tag: str) -> str | None:
    """The SemVer version a Git tag names (``v1.2.3`` → ``1.2.3``, ``1.2`` → ``1.2.0``); None when it names none."""
    match = _TAG.match(tag)
    if match is None:
        return None
    major, minor, patch, pre, build = match.groups()
    version = f'{major}.{minor}.{patch or 0}{pre or ""}{build or ""}'
    return version if valid_version(version) else None


@dataclass(frozen=True)
class PackageId:
    scope: str
    name: str

    @property
    def id(self) -> str:
        return f'{self.scope}.{self.name}'

    @property
    def scope_key(self) -> str:
        return self.scope.lower()

    @property
    def name_key(self) -> str:
        return self.name.lower()

    @property
    def prefix(self) -> str:
        """The package's folder in the repository: ``<scope>/<name>`` (lower case)."""
        return f'{self.scope_key}/{self.name_key}'

    def archive_path(self, version: str) -> str:
        return f'{self.prefix}/{version}{ARCHIVE_SUFFIX}'

    def manifest_path(self, version: str, filename: str) -> str:
        return f'{self.prefix}/{version}/{filename}'


def parse_id(text: str) -> PackageId | None:
    """``scope.name`` → ``PackageId``; None when either part is invalid."""
    scope, sep, name = text.partition('.')
    if not sep or not valid_scope(scope) or not valid_name(name):
        return None
    return PackageId(scope, name)


def parse_archive_path(path: str) -> tuple[str, str, str] | None:
    """``<scope>/<name>/<version>.zip`` → (scope key, name key, version); None for every other path."""
    parts = path.split('/')
    if len(parts) != 3 or not parts[2].endswith(ARCHIVE_SUFFIX):
        return None
    scope, name, version = parts[0], parts[1], parts[2][: -len(ARCHIVE_SUFFIX)]
    if not valid_scope(scope) or not valid_name(name) or not valid_version(version):
        return None
    return scope, name, version
