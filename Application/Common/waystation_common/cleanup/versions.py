"""Version ordering and pre-release detection per format, for "keep the latest N versions" (SPECIFICATION §4.8).

* npm: Semantic Versioning 2.0 precedence (``1.0.0-rc.1 < 1.0.0``); anything that is not semver sorts before
  every semver version, by a generic natural order.
* apt: dpkg ordering (``epoch:upstream-revision``; ``~`` sorts before everything, even the end of the string).
* yum: rpm ``rpmvercmp`` on version, then release (``~`` pre-releases, ``^`` post-releases).
* everything else: a generic natural order (digit runs numerically, other runs as text).

``sort_key(fmt, version)`` returns a key usable with ``sorted``; ``is_prerelease(fmt, version)`` follows each
format's convention, falling back to well-known markers (``rc``, ``beta``, ``nightly``...).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from typing import Any

_SEMVER = re.compile(
    r'^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$'
)
_RUNS = re.compile(r'\d+|[^\d]+')
_MARKERS = re.compile(
    r'(?:^|[^a-z])(alpha|beta|rc|pre|preview|dev|snapshot|nightly|canary|edge|next|unstable)(?:[^a-z]|\d|$)',
    re.IGNORECASE,
)
_DIGITS = frozenset('0123456789')
_LETTERS = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ')


# ---------------------------------------------------------------- generic
def natural_key(version: str) -> tuple[tuple[int, int, str], ...]:
    """Digit runs compare numerically and sort before text runs at the same position."""
    return tuple((0, int(run), '') if run.isdigit() else (1, 0, run) for run in _RUNS.findall(version))


# ---------------------------------------------------------------- semver
def semver_key(version: str) -> tuple[Any, ...] | None:
    match = _SEMVER.match(version)
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    if pre is None:
        pre_key: tuple[Any, ...] = (1,)
    else:
        idents = tuple((0, int(p), '') if p.isdigit() else (1, 0, p) for p in pre.split('.'))
        pre_key = (0, idents)
    return (int(major), int(minor), int(patch), pre_key)


def _npm_key(version: str) -> tuple[Any, ...]:
    key = semver_key(version)
    return (1, key) if key is not None else (0, natural_key(version))


# ---------------------------------------------------------------- dpkg
def _dpkg_order(c: str) -> int:
    if not c or c in _DIGITS:
        return 0
    if c in _LETTERS:
        return ord(c)
    if c == '~':
        return -1
    return ord(c) + 256


def _verrevcmp(a: str, b: str) -> int:
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and a[i] not in _DIGITS) or (j < len(b) and b[j] not in _DIGITS):
            ac = _dpkg_order(a[i]) if i < len(a) else 0
            bc = _dpkg_order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < len(a) and a[i] == '0':
            i += 1
        while j < len(b) and b[j] == '0':
            j += 1
        while i < len(a) and a[i] in _DIGITS and j < len(b) and b[j] in _DIGITS:
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i] in _DIGITS:
            return 1
        if j < len(b) and b[j] in _DIGITS:
            return -1
        if first_diff:
            return first_diff
    return 0


def _dpkg_split(version: str) -> tuple[int, str, str]:
    epoch = 0
    if ':' in version:
        head, version = version.split(':', 1)
        epoch = int(head) if head.isdigit() else 0
    upstream, _, revision = version.rpartition('-')
    if not upstream:
        upstream, revision = revision, ''
    return epoch, upstream, revision


def dpkg_compare(a: str, b: str) -> int:
    ea, ua, ra = _dpkg_split(a)
    eb, ub, rb = _dpkg_split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return _sign(_verrevcmp(ua, ub)) or _sign(_verrevcmp(ra, rb))


# ---------------------------------------------------------------- rpm
def _rpm_sep(c: str) -> bool:
    return c not in _DIGITS and c not in _LETTERS and c not in '~^'


def rpmvercmp(a: str, b: str) -> int:
    if a == b:
        return 0
    i = j = 0
    while i < len(a) or j < len(b):
        while i < len(a) and _rpm_sep(a[i]):
            i += 1
        while j < len(b) and _rpm_sep(b[j]):
            j += 1
        a_tilde, b_tilde = i < len(a) and a[i] == '~', j < len(b) and b[j] == '~'
        if a_tilde or b_tilde:
            if not a_tilde:
                return 1
            if not b_tilde:
                return -1
            i += 1
            j += 1
            continue
        a_caret, b_caret = i < len(a) and a[i] == '^', j < len(b) and b[j] == '^'
        if a_caret or b_caret:
            if i >= len(a):
                return -1
            if j >= len(b):
                return 1
            if not a_caret:
                return 1
            if not b_caret:
                return -1
            i += 1
            j += 1
            continue
        if i >= len(a) or j >= len(b):
            break
        numeric = a[i] in _DIGITS
        kind = _DIGITS if numeric else _LETTERS
        si, sj = i, j
        while i < len(a) and a[i] in kind:
            i += 1
        while j < len(b) and b[j] in kind:
            j += 1
        sa, sb = a[si:i], b[sj:j]
        if not sb:
            return 1 if numeric else -1
        if numeric:
            sa, sb = sa.lstrip('0'), sb.lstrip('0')
            if len(sa) != len(sb):
                return 1 if len(sa) > len(sb) else -1
        if sa != sb:
            return 1 if sa > sb else -1
    if i >= len(a) and j >= len(b):
        return 0
    return 1 if i < len(a) else -1


def _rpm_split(version: str) -> tuple[int, str, str]:
    epoch = 0
    if ':' in version:
        head, version = version.split(':', 1)
        epoch = int(head) if head.isdigit() else 0
    ver, _, rel = version.rpartition('-')
    if not ver:
        ver, rel = rel, ''
    return epoch, ver, rel


def rpm_compare(a: str, b: str) -> int:
    """Compare ``[epoch:]version-release`` strings."""
    ea, va, ra = _rpm_split(a)
    eb, vb, rb = _rpm_split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return rpmvercmp(va, vb) or rpmvercmp(ra, rb)


def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


# ---------------------------------------------------------------- dispatch
_CMP: dict[str, Callable[[str, str], int]] = {'apt': dpkg_compare, 'yum': rpm_compare}


def sort_key(fmt: str, version: str) -> Any:
    """Ascending sort key for versions of one format (higher = newer)."""
    if fmt == 'npm':
        return _npm_key(version)
    compare = _CMP.get(fmt)
    if compare is not None:
        return functools.cmp_to_key(compare)(version)
    return natural_key(version)


def is_prerelease(fmt: str, version: str) -> bool:
    if fmt == 'npm':
        key = semver_key(version)
        if key is not None:
            return bool(key[3][0] == 0)
    if fmt in ('apt', 'yum') and '~' in version:
        return True
    return _MARKERS.search(version) is not None
