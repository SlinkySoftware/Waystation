"""Ansible Galaxy naming rules shared by the server (routes, uploads, browse) and the worker (the collection import
job): namespace and collection names, semantic versions, artifact file names and paths (FORMATS.md "Ansible Galaxy").

- **Names**: what Galaxy NG accepts for a new collection (``NAME``: lower-case letters, digits and ``_``, starting with
  a letter, no ``__``, at most 64 characters). Names a proxy is asked for are checked with the looser ``LOOSE_NAME``
  (older upstream content), never anything that could form a path.
- **Versions**: strict SemVer 2.0.0 (what ``ansible-galaxy`` requires), at most 128 characters; ``version_sort`` is
  a string whose byte order is SemVer precedence (prereleases before their release; build metadata ignored).
- **Artifacts**: ``<namespace>-<name>-<version>.tar.gz``, stored at ``collections/<namespace>/<name>/<file>``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NAME = re.compile(r'^(?!.*__)[a-z][a-z0-9_]{0,63}$')
LOOSE_NAME = re.compile(r'^[A-Za-z0-9_]{1,64}$')
SEMVER = re.compile(
    r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)'
    r'(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?'
    r'(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$'
)
MAX_VERSION = 128
ARTIFACT_SUFFIX = '.tar.gz'
COLLECTIONS_ROOT = 'collections'
_NUMBER_WIDTH = 10


def valid_name(name: str, *, strict: bool = True) -> bool:
    return bool((NAME if strict else LOOSE_NAME).match(name))


def valid_version(version: str) -> bool:
    return len(version) <= MAX_VERSION and SEMVER.match(version) is not None


def version_sort(version: str) -> str:
    """Byte order = SemVer precedence. Anything that is not SemVer sorts before every valid version, by string."""
    match = SEMVER.match(version)
    if match is None:
        return '0' + version
    major, minor, patch, pre, _build = match.groups()
    key = '1' + ''.join(str(int(n))[-_NUMBER_WIDTH:].zfill(_NUMBER_WIDTH) for n in (major, minor, patch))
    if not pre:
        return key + '~'  # a release sorts after its prereleases
    parts = ['0' + i[-_NUMBER_WIDTH:].zfill(_NUMBER_WIDTH) if i.isdigit() else '1' + i for i in pre.split('.')]
    return key + '!' + '!'.join(parts)


def is_prerelease(version: str) -> bool:
    match = SEMVER.match(version)
    return match is not None and match.group(4) is not None


@dataclass(frozen=True)
class Artifact:
    namespace: str
    name: str
    version: str

    @property
    def filename(self) -> str:
        return f'{self.namespace}-{self.name}-{self.version}{ARTIFACT_SUFFIX}'

    @property
    def path(self) -> str:
        return artifact_path(self.namespace, self.name, self.version)

    @property
    def fqcn(self) -> str:
        return f'{self.namespace}.{self.name}'


def artifact_path(namespace: str, name: str, version: str) -> str:
    """Where a collection version's artifact is stored (and served) in a repository."""
    return f'{COLLECTIONS_ROOT}/{namespace}/{name}/{namespace}-{name}-{version}{ARTIFACT_SUFFIX}'


def parse_filename(filename: str, *, strict: bool = True) -> Artifact | None:
    """``<namespace>-<name>-<version>.tar.gz`` (names cannot contain ``-``; a version can), else None."""
    if not filename.endswith(ARTIFACT_SUFFIX):
        return None
    parts = filename[: -len(ARTIFACT_SUFFIX)].split('-', 2)
    if len(parts) != 3:
        return None
    namespace, name, version = parts
    if not valid_name(namespace, strict=strict) or not valid_name(name, strict=strict) or not valid_version(version):
        return None
    return Artifact(namespace, name, version)


def parse_artifact_path(path: str) -> Artifact | None:
    """The collection version an artifact path (``collections/<ns>/<name>/<file>``) holds, else None."""
    parts = path.split('/')
    if len(parts) != 4 or parts[0] != COLLECTIONS_ROOT:
        return None
    found = parse_filename(parts[3], strict=False)
    if found is None or (found.namespace, found.name) != (parts[1], parts[2]):
        return None
    return found
