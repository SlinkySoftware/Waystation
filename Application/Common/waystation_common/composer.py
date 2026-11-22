"""Composer package names and versions (FORMATS.md "Composer"), shared by the server, cleanup and the component index.

**Names** are ``vendor/name``, lower case (Composer's ``ValidatingArrayLoader`` rule): alphanumeric runs separated by
one ``.``, ``_`` or ``-`` (the name part also allows ``--``), at most ``MAX_NAME`` characters. Names are compared and
stored in lower case.

**Versions** are normalised as Composer's ``VersionParser::normalize`` does: four numbers (missing ones are 0, a
leading ``v`` dropped) and an expanded stability suffix (``1.0-b2`` is ``-beta2``, ``2.1.3-p1`` is ``-patch1``), date
versions (``20240115``), branches ``1.x-dev`` -> ``1.9999999.9999999.9999999-dev`` and ``dev-<branch>`` (kept as
written); build metadata (``+...``) is dropped.
**Stability**: ``dev`` (``dev-*`` and ``*-dev``), ``alpha``, ``beta``, ``RC`` or ``stable`` (``patch`` counts as
stable). **Order** (``sort_string``): ``dev-<branch>`` versions first (by name), then numerically by the numbers, then
``-dev`` < ``alpha`` < ``beta`` < ``RC`` < stable < ``patch``, then the stability's number, a ``-dev`` suffix before
the same version without it.

**Minified metadata** (``composer/metadata-minifier``, what ``p2`` files hold): each version entry after the first
lists only the keys that changed from the previous one, ``"__unset"`` marking a key that is gone.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from typing import Any

MAX_NAME = 200
MAX_VERSION = 256
MINIFIED = 'composer/2.0'
UNSET = '__unset'
_NUMBER_WIDTH = 10
_NUMBERS = 8  # date versions may have more than four parts
_NAME = re.compile(r'^[a-z0-9](?:[_.-]?[a-z0-9]+)*/[a-z0-9](?:(?:[_.]|-{1,2})?[a-z0-9]+)*$')
_MODIFIER = r'[._-]?(?:(stable|beta|b|RC|alpha|a|patch|pl|p)((?:[.-]?\d+)*+)?)?([.-]?dev)?'
_CLASSIC = re.compile(r'^v?(\d{1,5}+)(\.\d++)?(\.\d++)?(\.\d++)?' + _MODIFIER + '$', re.IGNORECASE)
_DATE = re.compile(r'^v?(\d{4}(?:[.:-]?\d{2}){1,6}(?:[.:-]?\d{1,3}){0,2})' + _MODIFIER + '$', re.IGNORECASE)
_BRANCH = re.compile(r'^v?(\d++)(\.(?:\d++|[xX*]))?(\.(?:\d++|[xX*]))?(\.(?:\d++|[xX*]))?$')
_DEV_SUFFIX = re.compile(r'^(.*?)[.-]?dev$', re.IGNORECASE)
_STABILITY_SUFFIX = re.compile(_MODIFIER + r'(?:\+.*)?$', re.IGNORECASE)
_NORMALISED = re.compile(r'^(?P<nums>\d+(?:\.\d+)*)(?:-(?P<label>alpha|beta|RC|patch)(?P<n>[\d.-]*))?(?P<dev>-dev)?$')
_SEGMENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$')
_BAD = re.compile(r'[\x00-\x1f\x7f\s]')
_RANK = {'dev': 0, 'alpha': 1, 'beta': 2, 'RC': 3, 'stable': 4, 'patch': 5}
STABILITIES = ('stable', 'RC', 'beta', 'alpha', 'dev')


def normalise_name(name: str) -> str | None:
    """The lower-case package name, None when it is not a valid ``vendor/name``."""
    lower = name.strip().lower()
    return lower if len(lower) <= MAX_NAME and _NAME.match(lower) else None


def _expand_stability(stability: str) -> str:
    lower = stability.lower()
    return {'a': 'alpha', 'b': 'beta', 'p': 'patch', 'pl': 'patch', 'rc': 'RC'}.get(lower, lower)


def _normalise_branch(name: str) -> str:
    found = _BRANCH.match(name.strip())
    if found is None:
        return 'dev-' + name.strip()
    parts = [(g if g is not None else '.x').replace('*', 'x').replace('X', 'x') for g in found.groups()]
    return ''.join(parts).replace('x', '9999999') + '-dev'


def normalise(version: str) -> str | None:
    """Composer's normalised form of ``version``; None when Composer would refuse it."""
    text = version.strip()
    if not text or len(text) > MAX_VERSION or _BAD.search(text):
        return None
    if text in ('master', 'trunk', 'default'):
        text = 'dev-' + text
    if text[:4].lower() == 'dev-':
        return 'dev-' + text[4:] if len(text) > 4 else None
    text = text.split('+', 1)[0] if '+' in text else text
    found = _CLASSIC.match(text)
    index = 5
    out = ''
    if found is not None:
        out = found.group(1) + ''.join(found.group(i) or '.0' for i in (2, 3, 4))
    else:
        found = _DATE.match(text)
        index = 2
        if found is not None:
            out = re.sub(r'\D', '.', found.group(1))
    if found is not None:
        label, number, dev = found.group(index), found.group(index + 1), found.group(index + 2)
        if label:
            if label.lower() == 'stable':
                return out
            out += '-' + _expand_stability(label) + (number.lstrip('.-') if number else '')
        if dev:
            out += '-dev'
        return out
    dev_branch = _DEV_SUFFIX.match(text)
    if dev_branch is not None:
        branch = _normalise_branch(dev_branch.group(1))
        if not branch.startswith('dev-'):
            return branch
    return None


def stability(version: str) -> str:
    """``dev``, ``alpha``, ``beta``, ``RC`` or ``stable`` (Composer's ``VersionParser::parseStability``)."""
    text = version.split('#', 1)[0]
    if text[:4].lower() == 'dev-' or text[-4:].lower() == '-dev':
        return 'dev'
    found = _STABILITY_SUFFIX.search(text.lower())
    if found is None:
        return 'stable'
    if found.group(3):
        return 'dev'
    label = (found.group(1) or '').lower()
    if label in ('beta', 'b'):
        return 'beta'
    if label in ('alpha', 'a'):
        return 'alpha'
    if label == 'rc':
        return 'RC'
    return 'stable'


def is_dev(version: str) -> bool:
    """Versions Composer lists in ``~dev`` metadata: branches (``dev-*``, ``*-dev``)."""
    return version[:4].lower() == 'dev-' or version[-4:].lower() == '-dev'


def is_prerelease(version: str) -> bool:
    return stability(version) != 'stable'


def _num(value: str) -> str:
    return f'{min(int(value), 10**_NUMBER_WIDTH - 1):0{_NUMBER_WIDTH}d}'


def sort_string(version: str) -> str:
    """A string that orders versions (normalised or not) as Composer does (see the module docstring)."""
    normalised = normalise(version) or version
    if normalised.startswith('dev-'):
        return '0' + normalised[4:].lower()
    found = _NORMALISED.match(normalised)
    if found is None:
        return '0' + normalised.lower()
    numbers = found.group('nums').split('.')[:_NUMBERS]
    numbers += ['0'] * (_NUMBERS - len(numbers))
    label = found.group('label')
    dev = bool(found.group('dev'))
    rank = _RANK[label] if label else (_RANK['dev'] if dev else _RANK['stable'])
    extra = [p for p in re.split(r'[.-]', found.group('n') or '') if p][:4]
    extra += ['0'] * (4 - len(extra))
    suffix = '0' if dev and label else '1'
    return '1' + ''.join(_num(n) for n in numbers) + str(rank) + ''.join(_num(n) for n in extra) + suffix


def newest(versions: Iterable[str]) -> str | None:
    """The newest stable version, else the newest (``dev-*`` branches only when nothing else exists)."""
    pool = list(versions)
    stable = [v for v in pool if not is_prerelease(v)]
    chosen = stable or [v for v in pool if not v.lower().startswith('dev-')] or pool
    return max(chosen, key=sort_string) if chosen else None


def path_segment(version_normalized: str) -> str:
    """The version as one path segment of an archive URL: the normalised version when it is safe, else (a branch
    name with ``/`` or other characters) a readable, hashed stand-in."""
    if _SEGMENT.match(version_normalized) and '..' not in version_normalized:
        return version_normalized
    readable = re.sub(r'[^A-Za-z0-9._-]+', '_', version_normalized.removeprefix('dev-'))[:60].strip('._-') or 'branch'
    digest = hashlib.sha256(version_normalized.encode()).hexdigest()[:12]
    return f'dev-{readable}-{digest}'


# ---------------------------------------------------------------------------- minified metadata
def expand(versions: Sequence[Any]) -> list[dict[str, Any]]:
    """Expand a minified version list (entries that are not objects are skipped)."""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for entry in versions:
        if not isinstance(entry, dict):
            continue
        if current is None:
            current = {k: v for k, v in entry.items() if v != UNSET}
        else:
            current = dict(current)
            for key, value in entry.items():
                if value == UNSET:
                    current.pop(key, None)
                else:
                    current[key] = value
        out.append(current)
    return out


def minify(versions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The minified form of an expanded version list (``expand(minify(x)) == x``)."""
    out: list[dict[str, Any]] = []
    last: dict[str, Any] | None = None
    for entry in versions:
        if last is None:
            last = dict(entry)
            out.append(dict(entry))
            continue
        changed: dict[str, Any] = {}
        for key, value in entry.items():
            if key not in last or last[key] != value:
                changed[key] = value
                last[key] = value
        for key in [k for k in last if k not in entry]:
            changed[key] = UNSET
            del last[key]
        out.append(changed)
    return out
