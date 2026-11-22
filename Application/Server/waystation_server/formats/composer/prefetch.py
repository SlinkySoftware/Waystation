"""Composer prefetch (T-O12; FORMATS.md "Composer", "Prefetch").

Warm items: ``vendor/name:version`` (``monolog/monolog:3.8.1``, any spelling Composer normalises the same way) or
``vendor/name`` alone (= the newest stable version, else the newest). Warming fetches what ``composer install``
does, through the repository's own routes: the package's ``p2`` document (``~dev`` for a branch version), then the
version's archive when the repository serves it (verified and pinned as for a client).

Refresh (``descriptor.refresh_request``): ``p2`` documents, asked for again by their paths.
"""

from __future__ import annotations

import asyncio

from waystation_common import composer
from waystation_common.errors import NotFound
from waystation_server.formats.base import WarmContext
from waystation_server.formats.composer import protocol

P2_MAX_BYTES = protocol.MAX_DOCUMENT


def parse_item(item: str) -> tuple[str, str | None]:
    """``(name, normalised version or None for the newest)``; raises ``ValueError`` with a readable message."""
    text, sep, version = item.strip().partition(':')
    name = composer.normalise_name(text)
    if name is None:
        raise ValueError('a package name vendor/name (lower case letters, digits, ".", "_" and "-")')
    if not sep:
        return name, None
    normalised = composer.normalise(version)
    if normalised is None:
        raise ValueError('after ":": a Composer version (1.2.3, v2.0.0-beta1, dev-main)')
    return name, normalised


def check_item(item: str) -> str | None:
    try:
        parse_item(item)
    except ValueError as exc:
        return str(exc)
    return None


async def warm(ctx: WarmContext, item: str) -> None:
    name, version = parse_item(item)
    dev = version is not None and composer.is_dev(version)
    fetched = await ctx.get(protocol.p2_path(name, dev=dev))
    data = await ctx.read(fetched, max_bytes=P2_MAX_BYTES)
    entries = await asyncio.to_thread(protocol.parse_p2, data, name)
    if version is None:
        chosen = composer.newest(str(e['version_normalized']) for e in entries)
        if chosen is None:
            raise NotFound(f'{name} has no versions')
        version = chosen
    entry = next((e for e in entries if e['version_normalized'] == version), None)
    if entry is None:
        raise NotFound(f'{name} {version} is not listed by the upstream')
    dist = entry.get('dist')
    url = dist.get('url') if isinstance(dist, dict) else None
    prefix = f'/repository/{ctx.repo.name}/'
    if isinstance(url, str) and f'{prefix}{protocol.DISTS}/{name}/' in url:  # an archive this repository serves
        await ctx.get(url.split(prefix, 1)[1])
