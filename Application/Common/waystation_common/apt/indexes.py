"""Which index a path below a suite is, and where its content is stored (FORMATS.md "apt"). Pure functions.

An index at its plain path (``main/binary-amd64/Packages.xz``) is stored under the ``by-hash/SHA256/<h>`` key of the
hash the suite's ``Release`` lists for it (``by_hash_key``), never as a mutable asset at the plain path.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

from waystation_common.apt import deb822

FLAT_ROOT = '.'
SIGNED_RELEASES = ('InRelease', 'Release')
RELEASE_GPG = 'Release.gpg'
RELEASE_GPG_MAX = 1024 * 1024
INDEX_MAX = 256 * 1024 * 1024  # a stored index file (compressed); decompression has its own bound (deb822)


@dataclass(frozen=True)
class PackagesTarget:
    """What a ``Packages`` index lists: suite (``dists/<suite>`` without ``dists/``; flat: the directory),
    component, architecture, and its compression suffix."""

    suite: str
    component: str
    arch: str
    suffix: str


def join(*parts: str) -> str:
    return '/'.join(p for p in parts if p)


def packages_target(suite_dir: str, rel: str) -> PackagesTarget | None:
    """``<component...>/binary-<arch>/Packages[.ext]`` (or flat ``Packages[.ext]``) → what it lists."""
    directory, name = posixpath.split(rel)
    base, dot, ext = name.partition('.')
    if base != 'Packages':
        return None
    suffix = deb822.compression_of(name)
    if suffix is None or (dot and f'.{ext}' != suffix):
        return None
    suite = suite_dir.split('/', 1)[1] if suite_dir.startswith('dists/') else suite_dir
    comp_dir, _, arch_dir = directory.rpartition('/')
    if directory and not arch_dir.startswith('binary-'):
        return None
    return PackagesTarget(suite=suite, component=comp_dir, arch=arch_dir.removeprefix('binary-'), suffix=suffix)


def by_hash_key(suite_dir: str, rel: str, sha256: str) -> str:
    """The asset path an index file listed at ``rel`` with ``sha256`` is stored under."""
    return join(suite_dir, posixpath.dirname(rel), 'by-hash', 'SHA256', sha256)


def browse_suite(suite: str) -> str:
    """The suite id browse uses: ``trixie`` for ``dists/trixie``, ``bookworm/mongodb-org/8.0`` for
    ``dists/bookworm/mongodb-org/8.0``; a flat repository's directory, ``.`` for its root."""
    return suite or FLAT_ROOT
