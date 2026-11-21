"""Search keys (SPECIFICATION §4.6, DATA_MODEL "Search keys"): the normalised names global search matches on.

Every searchable document (``npm_packages``, ``docker_tags``, ``apt_packages``, verified ``yum_packages`` entries and
``content`` assets) carries

- ``name_lc``: its name, lower-cased (files: the last path segment), at most ``MAX_KEY`` characters;
- ``base_lc`` (names with a ``/`` only): the last ``/`` segment, lower-cased, so ``nginx`` finds ``library/nginx``
  and ``node`` finds ``@types/node``;
- ``name_sfx``: the distinct suffixes of ``name_lc`` that start after its first character and are at least
  ``MIN_TERM`` long, each cut to ``SFX`` characters, sorted; empty for names shorter than ``MIN_TERM + 1``.

A search for ``q`` is first a prefix match of ``fold(q)`` on ``name_lc`` or ``base_lc``: an index range
``[lo, hi)`` (``prefix_range``). For queries of at least ``MIN_TERM`` characters it is then a substring match: a name
contains ``q`` after its start iff one of its suffixes starts with ``q``, so the same kind of prefix range over the
multikey ``name_sfx`` index finds exactly those names (``q`` cut to ``SFX`` characters; longer terms are checked
exactly on ``name_lc``), adr/0009.
The format index writers set the keys when they write a document; migrations ``0002_search_keys`` and
``0004_search_suffixes`` backfill documents written before the keys existed. Pure functions only: the writers, the
migrations and the query side share them.
"""

from __future__ import annotations

MAX_KEY = 200
SFX = 16  # characters of a suffix kept in ``name_sfx``
MIN_TERM = 3  # shortest term matched as a substring

SearchKeys = dict[str, str | list[str]]


def fold(text: str) -> str:
    """The comparison form of a name or a query: lower case, at most ``MAX_KEY`` characters."""
    return text.lower()[:MAX_KEY]


def suffixes(name_lc: str) -> list[str]:
    """``name_sfx`` of a folded name: its suffixes from the second character on, at least ``MIN_TERM`` long, each cut
    to ``SFX`` characters, distinct and sorted."""
    return sorted({name_lc[i : i + SFX] for i in range(1, len(name_lc) - MIN_TERM + 1)})


def name_keys(name: str) -> SearchKeys:
    """``name_lc`` and ``name_sfx``, plus ``base_lc`` when the name has a distinct last ``/`` segment."""
    name_lc = fold(name)
    keys: SearchKeys = {'name_lc': name_lc, 'name_sfx': suffixes(name_lc)}
    base = fold(name.rsplit('/', 1)[-1])
    if '/' in name and base and base != name_lc:
        keys['base_lc'] = base
    return keys


def file_keys(path: str) -> SearchKeys:
    """Search keys of a cached file: its last path segment."""
    name_lc = fold(path.rsplit('/', 1)[-1])
    return {'name_lc': name_lc, 'name_sfx': suffixes(name_lc)}


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
