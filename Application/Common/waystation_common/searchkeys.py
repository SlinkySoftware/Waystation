"""Search keys (SPECIFICATION §4.6, DATA_MODEL "Search keys"): the normalised names global search matches on.

Every searchable document (``npm_packages``, ``docker_tags``, ``apt_packages``, verified ``yum_packages`` entries and
``content`` assets) carries

- ``name_lc``: its name, lower-cased (files: the last path segment), at most ``MAX_KEY`` characters;
- ``base_lc`` (names with a ``/`` only): the last ``/`` segment, lower-cased, so ``nginx`` finds ``library/nginx``
  and ``node`` finds ``@types/node``.

A search for ``q`` is a prefix match of ``fold(q)`` on either key: an index range ``[lo, hi)`` (``prefix_range``).
The format index writers set the keys when they write a document; migration ``0002_search_keys`` backfills documents
written before the keys existed. Pure functions only: the writers, the migration and the query side share them.
"""

from __future__ import annotations

MAX_KEY = 200


def fold(text: str) -> str:
    """The comparison form of a name or a query: lower case, at most ``MAX_KEY`` characters."""
    return text.lower()[:MAX_KEY]


def name_keys(name: str) -> dict[str, str]:
    """``{'name_lc': ...}`` plus ``base_lc`` when the name has a distinct last ``/`` segment."""
    keys = {'name_lc': fold(name)}
    base = fold(name.rsplit('/', 1)[-1])
    if '/' in name and base and base != keys['name_lc']:
        keys['base_lc'] = base
    return keys


def file_keys(path: str) -> dict[str, str]:
    """Search keys of a cached file: its last path segment."""
    return {'name_lc': fold(path.rsplit('/', 1)[-1])}


def prefix_range(prefix: str) -> tuple[str, str | None]:
    """``(lo, hi)`` such that a string starts with ``prefix`` iff ``lo <= s < hi`` (``hi`` None = unbounded).

    MongoDB compares strings by UTF-8 bytes, which orders like code points; incrementing the last code point gives
    the exclusive upper bound (a last code point that cannot be incremented is dropped and the next one tried)."""
    chars = list(prefix)
    while chars:
        last = ord(chars[-1])
        if last < 0x10FFFF and not 0xD7FF <= last < 0xE000:
            return prefix, ''.join(chars[:-1]) + chr(last + 1)
        if 0xD7FF <= last < 0xE000:
            return prefix, ''.join(chars[:-1]) + chr(0xE000)
        chars.pop()
    return prefix, None
