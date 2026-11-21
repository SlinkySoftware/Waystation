"""Where a suite ends in a ``dists/`` path (FORMATS.md "apt", multi-segment suites).

A suite is any directory below ``dists/`` holding an ``InRelease``/``Release``: usually one segment (``trixie``),
but real repositories nest it (``dists/bookworm/mongodb-org/8.0/``, old Debian security ``dists/buster/updates/``).
For an index or ``by-hash`` path the suite boundary is found by, in order:

1. **Known suites**: the longest suite directory of the repository whose ``InRelease``/``Release`` is cached
   (the client always fetches it first, so this answers almost every request);
2. **Structure**: the suite ends before the component directory, the segment preceding the deepest
   ``binary-<arch>``/``source``/``i18n``/``dep11``/``cnf`` directory (``<component>/debian-installer`` counts as
   one component);
3. otherwise the first segment (the single-segment layout).

Suites are at most ``MAX_SUITE_DEPTH`` segments deep; deeper paths are refused. Pure functions, no I/O.
"""

from __future__ import annotations

from collections.abc import Collection

from waystation_common.errors import PathRejected
from waystation_common.paths import normalise

DISTS = 'dists'
MAX_SUITE_DEPTH = 8
COMPONENT_MARKERS = frozenset({'source', 'i18n', 'dep11', 'cnf'})
NESTED_COMPONENTS = frozenset({'debian-installer'})


def is_marker(segment: str) -> bool:
    """A directory that sits directly below a component (``binary-amd64``, ``source``, ``i18n``...)."""
    return segment.startswith('binary-') or segment in COMPONENT_MARKERS


def safe(path: str) -> bool:
    """The path is already normal (no ``..``, empty or ``.`` segments, not absolute, no control characters)."""
    try:
        return normalise(path) == path
    except PathRejected:
        return False


def release_suite_dir(path: str) -> str | None:
    """``dists/<suite...>`` for a ``dists/<suite...>/{InRelease,Release,Release.gpg}`` path, else None. A release
    file below a component directory (``main/binary-amd64/Release``) is an index, not a suite's release."""
    parts = path.split('/')
    if len(parts) < 3 or parts[0] != DISTS or parts[-1] not in ('InRelease', 'Release', 'Release.gpg'):
        return None
    suite = parts[1:-1]
    if len(suite) > MAX_SUITE_DEPTH or any(is_marker(s) for s in suite[1:]):
        return None
    return '/'.join(parts[:-1])


def structural_depth(segments: list[str]) -> int:
    """How many of ``segments`` (directories below ``dists/``) belong to the suite, from the layout alone."""
    for i in range(len(segments) - 1, 1, -1):
        if is_marker(segments[i]):
            component = i - 1
            if segments[component] in NESTED_COMPONENTS and component >= 2:
                component -= 1
            return component
    return 1


def suite_depth(segments: list[str], known: Collection[str]) -> int:
    """How many of ``segments`` (directories below ``dists/``) belong to the suite: the longest known suite
    directory among them, else ``structural_depth``."""
    for depth in range(min(len(segments), MAX_SUITE_DEPTH), 0, -1):
        if '/'.join([DISTS, *segments[:depth]]) in known:
            return depth
    return structural_depth(segments)


def candidates(segments: list[str]) -> list[str]:
    """The suite directories ``segments`` could belong to (the lookups for ``suite_depth``), shortest first."""
    return ['/'.join([DISTS, *segments[:depth]]) for depth in range(1, min(len(segments), MAX_SUITE_DEPTH) + 1)]
