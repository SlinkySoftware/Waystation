"""Conda prefetch (T-O12; FORMATS.md "Conda", "Prefetch").

Warm items: ``<subdir>/<name>`` (the newest version listed), ``<subdir>/<name>==<version>`` (every build of that
version) or ``<subdir>/<name>==<version>=<build>`` (one file), e.g. ``noarch/tzdata==2024b``,
``linux-64/numpy==2.1.3``. Warming fetches the subdir's ``repodata.json.zst`` (else ``repodata.json``), waits for its
listing to be indexed, then the matching package files (verified against the listing as for a client).

Refresh: only ``channeldata.json`` (metadata). Repodata files are cached like content and revalidated after the
repository's metadata max age when a client asks for them.
"""

from __future__ import annotations

import json

from waystation_common.conda import valid_build, valid_name, valid_subdir, valid_version
from waystation_common.errors import NotFound
from waystation_server.formats.base import RefreshRequest, WarmContext
from waystation_server.formats.conda.common import ANY, CHANNELDATA, RESOLVE

MAX_RESOLVE = 1024 * 1024


def parse_item(item: str) -> tuple[str, str, str | None, str | None]:
    """``(subdir, name, version or None, build or None)``; raises ``ValueError`` with a readable message."""
    subdir, sep, spec = item.strip().partition('/')
    if not sep or not valid_subdir(subdir):
        raise ValueError('start with the subdir: noarch/<name>, linux-64/<name>==<version>[=<build>]')
    name, sep, rest = spec.partition('==')
    version, _, build = rest.partition('=')
    if not valid_name(name):
        raise ValueError('not a conda package name (lower-case letters, digits, -, _ and .)')
    if sep and not valid_version(version):
        raise ValueError('after ==: a conda version (no spaces or -)')
    if build and not valid_build(build):
        raise ValueError('after the second =: a build string')
    return subdir, name, version if sep else None, build or None


def check_item(item: str) -> str | None:
    try:
        parse_item(item)
    except ValueError as exc:
        return str(exc)
    return None


def refresh_request(asset_key: str) -> RefreshRequest | None:
    return RefreshRequest(asset_key) if asset_key == CHANNELDATA else None


async def warm(ctx: WarmContext, item: str) -> None:
    subdir, name, version, build = parse_item(item)
    try:
        await ctx.get(f'{subdir}/repodata.json.zst')
    except NotFound:
        await ctx.get(f'{subdir}/repodata.json')
    fetched = await ctx.get(f'{RESOLVE}{subdir}/{name}/{version or ANY}/{build or ANY}')
    files = json.loads(await ctx.read(fetched, max_bytes=MAX_RESOLVE)).get('files') or []
    if not files:
        raise NotFound(f'{item} is not in the {subdir} repodata')
    for filename in files:
        await ctx.get(f'{subdir}/{filename}')
