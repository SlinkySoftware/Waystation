"""The ``version_sort`` contract (DATA_MODEL ``components``): per format, a plain string whose byte order is the
format's version order, so "latest N" and "versions newest first" are index sorts.

- npm, go, galaxy, terraform, helm: SemVer 2.0 precedence (``waystation_common.galaxy.version_sort``; go and other
  ``v``-prefixed versions drop the ``v``); anything that is not SemVer sorts before every SemVer version;
- pypi: PEP 440 (``waystation_common.cleanup.versions.pep440_sort_string``);
- maven: Maven ``ComparableVersion`` (``versionsort.maven.sort_key``);
- yum: RPM ``rpmvercmp`` on ``version-release`` (``versionsort.rpm``; an ``epoch:`` prefix is honoured);
- apt: dpkg order (``dpkg_sort_string``);
- docker (tags), raw and everything else: a natural order (digit runs numerically, before text runs).

Every key is bounded (``MAX_KEY`` characters). Tests check each key against the format's comparison function.
"""

from __future__ import annotations

import re

from waystation_common.cleanup.versions import pep440_sort_string
from waystation_common.galaxy import version_sort as semver_sort_string
from waystation_common.versionsort import maven, rpm

MAX_KEY = 400
_RUNS = re.compile(r'\d+|\D+')
_DPKG_RUNS = re.compile(r'(\D*)(\d*)')
SEMVER_FORMATS = frozenset({'npm', 'go', 'galaxy', 'terraform', 'helm'})


def _num(digits: str) -> str:
    """A digit run: leading zeros dropped, two-digit length prefix (longer numbers sort later)."""
    clean = (digits.lstrip('0') or '0')[:99]
    return f'{len(clean):02d}{clean}'


def natural_sort_string(version: str) -> str:
    """Digit runs compare numerically and sort before text runs at the same position; text compares by bytes."""
    out = []
    for run in _RUNS.findall(version):
        out.append(('a' + _num(run)) if run.isdigit() else ('b' + run + '!'))
    return ''.join(out)[:MAX_KEY]


def _dpkg_part(text: str) -> str:
    """dpkg ``verrevcmp`` order as bytes: alternating non-digit and digit runs; in a non-digit run ``~`` sorts
    before the end of the run, which sorts before letters, which sort before every other character; digit runs
    compare numerically (an empty run is 0). The string ends with an empty non-digit run's end marker, so
    ``1.0~rc1`` < ``1.0`` < ``1.0a`` < ``1.0.1``."""
    out: list[str] = []
    pairs = [(a, b) for a, b in _DPKG_RUNS.findall(text) if a or b] or [('', '')]
    for letters, digits in pairs:
        for c in letters:
            if c == '~':
                out.append('0')
            elif c.isalpha() and c.isascii():
                out.append('2' + c)
            else:
                out.append('3' + c)
        out.append('1')
        out.append(_num(digits))
    out.append('1')
    return ''.join(out)


def dpkg_sort_string(version: str) -> str:
    """``[epoch:]upstream[-revision]`` in dpkg order (``dpkg --compare-versions``)."""
    epoch_text, sep, rest = version.partition(':')
    if not sep:
        rest = version
    epoch = int(epoch_text) if sep and epoch_text.isdigit() else 0
    upstream, _, revision = rest.rpartition('-')
    if not upstream:
        upstream, revision = revision, ''
    return f'{min(epoch, 10**10 - 1):010d}{_dpkg_part(upstream)} {_dpkg_part(revision)}'[:MAX_KEY]


def rpm_sort_string(version: str) -> str:
    """``[epoch:]version[-release]`` in RPM order (``versionsort.rpm.evr_key``)."""
    epoch: str | None = None
    head, sep, rest = version.partition(':')
    if sep:
        epoch, version = (head if head.isdigit() else None), rest
    ver, _, release = version.rpartition('-')
    if not ver:
        ver, release = release, ''
    return rpm.evr_key(epoch, ver, release)[:MAX_KEY]


def version_sort(fmt: str, version: str | None) -> str:
    """``components.version_sort`` of ``version`` in format ``fmt`` ('' for components without a version)."""
    if not version:
        return ''
    if fmt in SEMVER_FORMATS:
        return semver_sort_string(version[1:] if version.startswith('v') else version)[:MAX_KEY]
    if fmt == 'pypi':
        return pep440_sort_string(version)[:MAX_KEY]
    if fmt == 'maven':
        return maven.sort_key(version)
    if fmt == 'yum':
        return rpm_sort_string(version)
    if fmt == 'apt':
        return dpkg_sort_string(version)
    return natural_sort_string(version)


__all__ = [
    'MAX_KEY',
    'dpkg_sort_string',
    'natural_sort_string',
    'rpm_sort_string',
    'semver_sort_string',
    'version_sort',
]
