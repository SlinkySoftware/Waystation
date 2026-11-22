"""npm prefetch (T-O12; FORMATS.md "npm", "Prefetch").

Warm items: ``<package>`` (its ``latest``), ``<package>@<version>`` or ``<package>@<dist-tag>``; scoped names as
``@scope/name@1.2.3``. Warming fetches the abbreviated packument (what ``npm install`` asks for), resolves the
version and fetches its tarball: both through the repository's own routes, so the tarball is verified against the
packument's ``dist.integrity`` as for a client. Version ranges are not resolved.

Refresh: the cached packuments (``<package>/-/packument.json`` and ``.install-v1.json``) are asked for again with
the matching ``Accept``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from waystation_common.errors import NotFound, UpstreamError
from waystation_server.formats.base import RefreshRequest, WarmContext
from waystation_server.formats.npm import metadata

PACKUMENT_MAX_BYTES = 128 * 1024 * 1024
FULL_SUFFIX = '/-/packument.json'
ABBREVIATED_SUFFIX = '/-/packument.install-v1.json'


def parse_item(item: str) -> tuple[str, str | None]:
    """``(package, version or dist-tag or None)``; raises ``ValueError`` with a readable message."""
    text = item.strip()
    scoped = text.startswith('@')
    name, sep, ref = (text[1:] if scoped else text).partition('@')
    package = f'@{name}' if scoped else name
    if not metadata.valid_name(package):
        raise ValueError('not an npm package name (`name`, `@scope/name`)')
    if not sep:
        return package, None
    if not (metadata.valid_version(ref) or metadata.valid_tag(ref)):
        raise ValueError('after @: an exact version (1.2.3) or a dist-tag (latest); ranges are not resolved')
    return package, ref


def check_item(item: str) -> str | None:
    try:
        parse_item(item)
    except ValueError as exc:
        return str(exc)
    return None


def refresh_request(key: str) -> RefreshRequest | None:
    if key.endswith(ABBREVIATED_SUFFIX):
        package, accept = key[: -len(ABBREVIATED_SUFFIX)], metadata.ABBREVIATED_ACCEPT
    elif key.endswith(FULL_SUFFIX):
        package, accept = key[: -len(FULL_SUFFIX)], metadata.JSON_TYPE
    else:
        return None
    return RefreshRequest(package, {'Accept': accept}) if metadata.valid_name(package) else None


def _tarball(document: Any, package: str, ref: str | None) -> str:
    if not isinstance(document, dict):
        raise UpstreamError(f'{package}: the packument is not a JSON object')
    versions = document.get('versions')
    tags = document.get('dist-tags')
    versions = versions if isinstance(versions, dict) else {}
    tags = tags if isinstance(tags, dict) else {}
    version = ref if ref in versions else tags.get(ref or 'latest')
    entry = versions.get(version) if isinstance(version, str) else None
    if not isinstance(entry, dict):
        raise NotFound(f'{package}: no version or dist-tag {ref or "latest"!r}')
    dist = entry.get('dist')
    url = dist.get('tarball') if isinstance(dist, dict) else None
    if not isinstance(url, str) or not url:
        raise UpstreamError(f'{package}@{version}: no dist.tarball')
    return metadata.tarball_filename(url)


async def warm(ctx: WarmContext, item: str) -> None:
    package, ref = parse_item(item)
    fetched = await ctx.get(package, {'Accept': metadata.ABBREVIATED_ACCEPT})
    data = await ctx.read(fetched, max_bytes=PACKUMENT_MAX_BYTES)
    try:
        document = await asyncio.to_thread(json.loads, data)
    except ValueError:
        raise UpstreamError(f'{package}: the packument is not valid JSON') from None
    filename = _tarball(document, package, ref)
    await ctx.get(metadata.tarball_path(package, filename))
