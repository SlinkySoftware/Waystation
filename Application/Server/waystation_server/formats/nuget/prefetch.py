"""NuGet prefetch (T-O12; FORMATS.md "NuGet v3", "Prefetch").

Warm items: ``<id>@<version>`` (``Newtonsoft.Json@13.0.3``) or ``<id>`` alone (= the newest stable version the
upstream lists, else the newest). Warming fetches what ``dotnet restore`` does, through the repository's own routes:
the flat container version list, then the version's ``.nupkg`` (verified as for a client) and ``.nuspec``.

Refresh (``descriptor.refresh_request``): version lists and registration indexes, asked for again by their paths.
"""

from __future__ import annotations

import asyncio

from waystation_common import nuget
from waystation_common.errors import NotFound
from waystation_server.formats.base import WarmContext
from waystation_server.formats.nuget import protocol

VERSIONS_MAX_BYTES = 16 * 1024 * 1024


def parse_item(item: str) -> tuple[str, str | None]:
    """``(id, flat version or None for the newest)``; raises ``ValueError`` with a readable message."""
    package_id, sep, version = item.strip().partition('@')
    if not nuget.valid_id(package_id):
        raise ValueError('a package id: letters, digits, ".", "_" and "-" (at most 100 characters)')
    if not sep:
        return package_id, None
    flat = nuget.flat_version(version)
    if flat is None:
        raise ValueError('after @: a NuGet version (1.2.3, 1.2.3-beta.1)')
    return package_id, flat


def check_item(item: str) -> str | None:
    try:
        parse_item(item)
    except ValueError as exc:
        return str(exc)
    return None


def newest(versions: list[str]) -> str | None:
    stable = [v for v in versions if not nuget.is_prerelease(v)]
    pool = stable or versions
    return max(pool, key=nuget.sort_string) if pool else None


async def warm(ctx: WarmContext, item: str) -> None:
    package_id, version = parse_item(item)
    lower = package_id.lower()
    fetched = await ctx.get(f'{protocol.FLAT}/{lower}/index.json')
    data = await ctx.read(fetched, max_bytes=VERSIONS_MAX_BYTES)
    versions = await asyncio.to_thread(protocol.parse_versions, data)
    if version is None:
        version = newest(versions)
        if version is None:
            raise NotFound(f'{package_id} has no versions')
    elif version not in versions:
        raise NotFound(f'{package_id} {version} is not listed by the upstream')
    await ctx.get(protocol.nupkg_path(lower, version))
    await ctx.get(protocol.nuspec_path(lower, version))
