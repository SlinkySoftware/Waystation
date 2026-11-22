"""Cargo prefetch (T-O12; FORMATS.md "Cargo", "Prefetch").

Warm items: ``<crate>@<version>`` (an exact SemVer version, e.g. ``serde@1.0.228``) or just ``<crate>`` (its latest
non-yanked release). Warming fetches what ``cargo fetch`` does: the crate's index file, then the version's ``.crate``
(verified against the index ``cksum`` as for a client).

Refresh: index files (``index/<prefix>/<crate>``) are asked for again by their path; the cached upstream
``config.json`` is not refreshable that way.
"""

from __future__ import annotations

import asyncio

from waystation_common.errors import NotFound
from waystation_server.formats.base import RefreshRequest, WarmContext
from waystation_server.formats.cargo.common import (
    MAX_INDEX,
    crate_path,
    find,
    index_path,
    latest_of,
    parse_index,
    valid_name,
    valid_version,
)


def parse_item(item: str) -> tuple[str, str | None]:
    """``(crate, version or None = latest)``; raises ``ValueError`` with a readable message."""
    crate, sep, version = item.strip().partition('@')
    if not valid_name(crate):
        raise ValueError('not a crate name (ASCII letters, digits, - and _, a letter first, at most 64)')
    if sep and not valid_version(version):
        raise ValueError('after @: an exact SemVer version (1.2.3)')
    return crate, version if sep else None


def check_item(item: str) -> str | None:
    try:
        parse_item(item)
    except ValueError as exc:
        return str(exc)
    return None


def refresh_request(asset_key: str) -> RefreshRequest | None:
    return RefreshRequest(asset_key) if asset_key.startswith('index/') and asset_key != 'index/config.json' else None


async def warm(ctx: WarmContext, item: str) -> None:
    crate, version = parse_item(item)
    lower = crate.lower()
    fetched = await ctx.get(index_path(lower))
    entries = await asyncio.to_thread(parse_index, await ctx.read(fetched, max_bytes=MAX_INDEX), lower)
    if version is None:
        version = latest_of([e for e in entries if not e.get('yanked')])
        if version is None:
            raise NotFound(f'{crate} has no version that is not yanked')
    entry = find(entries, version)
    if entry is None:
        raise NotFound(f'{crate}@{version} is not in the index')
    await ctx.get(crate_path(lower, version))
