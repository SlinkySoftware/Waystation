"""RubyGems prefetch (T-O12; FORMATS.md "RubyGems", "Prefetch").

Warm items: ``gem`` (= the newest release, else the newest version), ``gem:version`` (the ``ruby`` platform) or
``gem:version-platform`` (``nokogiri:1.18.8-x86_64-linux``). Warming fetches what ``gem install`` does, through the
repository's own routes: the gem's info file, the version's ``.gem`` (verified against the info checksum as for a
client) and its ``quick/Marshal.4.8`` gemspec.

Refresh (``descriptor.refresh_request``): ``versions``, ``names`` and ``info/<gem>``, asked for again by their paths.
"""

from __future__ import annotations

import asyncio

from waystation_common import rubygems
from waystation_common.errors import NotFound
from waystation_server.formats.base import WarmContext
from waystation_server.formats.rubygems import protocol


def parse_item(item: str) -> tuple[str, tuple[str, str] | None]:
    """``(gem, (version, platform) or None for the newest)``; raises ``ValueError`` with a readable message."""
    name, sep, version = item.strip().partition(':')
    if not rubygems.valid_name(name):
        raise ValueError('a gem name (letters, digits, ".", "_" and "-")')
    if not sep:
        return name, None
    parsed = rubygems.split_index_version(version.strip())
    if parsed is None:
        raise ValueError('after ":": a gem version, optionally with a platform (1.2.3, 1.2.3-x86_64-linux)')
    return name, parsed


def check_item(item: str) -> str | None:
    try:
        parse_item(item)
    except ValueError as exc:
        return str(exc)
    return None


async def warm(ctx: WarmContext, item: str) -> None:
    name, wanted = parse_item(item)
    fetched = await ctx.get(f'{protocol.INFO}/{name}')
    lines = await asyncio.to_thread(protocol.parse_info, await ctx.read(fetched, max_bytes=protocol.MAX_INFO))
    if wanted is None:
        newest = rubygems.newest(line.version for line in lines if line.platform == rubygems.RUBY)
        newest = newest or rubygems.newest(line.version for line in lines)
        chosen = next((line for line in lines if line.version == newest), None)
    else:
        chosen = next((line for line in lines if (line.version, line.platform) == wanted), None)
    if chosen is None:
        raise NotFound(f'{item.strip()} is not listed by the upstream')
    full = rubygems.full_name(name, chosen.version, chosen.platform)
    await ctx.get(protocol.gem_path(full))
    await ctx.get(protocol.gemspec_path(full))
