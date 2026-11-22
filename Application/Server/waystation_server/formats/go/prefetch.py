"""Go module prefetch (T-O12; FORMATS.md "Go modules", "Prefetch").

Warm items: ``<module>@<version>`` (a canonical version, e.g. ``golang.org/x/text@v0.21.0``), ``<module>@latest``
or just ``<module>`` (= ``@latest``). Warming fetches what ``go mod download`` does: ``@latest`` when asked for,
then the version's ``.info``, ``.mod`` and ``.zip`` through the repository's own GOPROXY routes (so ``.mod`` and
``.zip`` are checked against the checksum database as for a client).

Refresh: asset keys are request paths (``@v/list``, ``@latest``), asked for again as they are.
"""

from __future__ import annotations

import asyncio
import json

from waystation_common.errors import UpstreamError
from waystation_server.formats.base import WarmContext
from waystation_server.formats.go import modpath

INFO_MAX_BYTES = 64 * 1024


def parse_item(item: str) -> tuple[str, str]:
    """``(module, version or 'latest')``; raises ``ValueError`` with a readable message."""
    module, _, version = item.strip().partition('@')
    version = version or 'latest'
    try:
        modpath.check_path(module)
    except modpath.InvalidModule as exc:
        raise ValueError(f'not a module path: {exc}') from None
    if version != 'latest' and not modpath.is_canonical(version):
        raise ValueError('after @: a canonical version (v1.2.3) or latest')
    return module, version


def check_item(item: str) -> str | None:
    try:
        parse_item(item)
    except ValueError as exc:
        return str(exc)
    return None


async def warm(ctx: WarmContext, item: str) -> None:
    module, version = parse_item(item)
    escaped = modpath.escape_path(module)
    if version == 'latest':
        fetched = await ctx.get(f'{escaped}/@latest')
        try:
            info = await asyncio.to_thread(json.loads, await ctx.read(fetched, max_bytes=INFO_MAX_BYTES))
        except ValueError:
            raise UpstreamError(f'{module}@latest is not valid JSON') from None
        latest = info.get('Version') if isinstance(info, dict) else None
        if not isinstance(latest, str) or not modpath.is_canonical(latest):
            raise UpstreamError(f'{module}@latest names no canonical version')
        version = latest
    stem = f'{escaped}/@v/{modpath.escape_version(version)}'
    for ext in ('info', 'mod', 'zip'):
        await ctx.get(f'{stem}.{ext}')
