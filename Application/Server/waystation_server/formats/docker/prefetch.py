"""Docker / OCI prefetch (T-O12; FORMATS.md "Docker / OCI", "Prefetch").

Warm items: ``<image>[:<tag>|@<digest>] [<platforms>]``, e.g. ``nginx:1.27``, ``library/redis``,
``nginx:1.27 linux/amd64,linux/arm64/v8`` or ``nginx@sha256:<hex> all``. The tag defaults to ``latest``; platforms
(``os/arch[/variant]``, comma-separated; a platform without a variant matches every variant) default to
``linux/amd64``; ``all`` takes every platform of an index except attestations. Warming pulls like
``docker pull``: the tag's manifest with the full ``Accept`` list (the same cached variant a client gets), for an
index the manifest of each chosen platform by digest, then each image's config and layers (non-distributable layers
with ``urls`` are skipped). Every file goes through the repository's own ``/v2/`` routes, so manifests and blobs are
verified against their digests and recorded for browsing as for a client.

Refresh: cached tag manifests (``<name>/manifests/<tag>``, also the narrower ``@accept=`` variants) and tag lists
(``<name>/tags/list``) are asked for again; a stale tag is first re-checked with an upstream ``HEAD``.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from waystation_common.errors import NotFound, UpstreamError
from waystation_server.formats.base import Fetched, RefreshRequest, WarmContext
from waystation_server.formats.docker import protocol as p

DEFAULT_PLATFORMS = ('linux/amd64',)
MAX_PLATFORMS = 16
LAYER_CONCURRENCY = 3
_PLATFORM = re.compile(r'[a-z0-9._-]{1,32}/[a-z0-9._-]{1,32}(?:/[a-z0-9._-]{1,32})?')
_LONG = {short: media for media, short in p._SHORT.items()}  # the asset-key variant names, reversed
ATTESTATION = 'vnd.docker.reference.type'
ACCEPT = ', '.join(p.MANIFEST_TYPES)


def parse_item(item: str) -> tuple[str, str, tuple[str, ...] | None]:
    """``(name, tag or digest, platforms or None for all)``; raises ``ValueError`` with a readable message."""
    words = item.split()
    if not words or len(words) > 2:
        raise ValueError('use `image[:tag|@digest] [os/arch,...]`')
    ref = words[0]
    if '@' in ref:
        name, _, reference = ref.partition('@')
        if not p.DIGEST_RE.fullmatch(reference):
            raise ValueError('after @: a digest (sha256:<64 hex>)')
    else:
        slash = ref.rfind('/')
        colon = ref.rfind(':')
        name, reference = (ref[:colon], ref[colon + 1 :]) if colon > slash else (ref, 'latest')
        if not p.TAG_RE.fullmatch(reference):
            raise ValueError('not a valid tag')
    if len(name) > p.NAME_MAX or not p.NAME_RE.fullmatch(name):
        raise ValueError('not a valid image name (lower case, e.g. `library/nginx`)')
    if len(words) == 1:
        return name, reference, DEFAULT_PLATFORMS
    if words[1] == 'all':
        return name, reference, None
    platforms = tuple(dict.fromkeys(words[1].split(',')))
    if len(platforms) > MAX_PLATFORMS or not all(_PLATFORM.fullmatch(x) for x in platforms):
        raise ValueError('platforms: `os/arch[/variant]`, comma-separated, or `all`')
    return name, reference, platforms


def check_item(item: str) -> str | None:
    try:
        parse_item(item)
    except ValueError as exc:
        return str(exc)
    return None


def refresh_request(key: str) -> RefreshRequest | None:
    if key.endswith('/tags/list'):
        name = key[: -len('/tags/list')]
        return RefreshRequest(f'v2/{name}/tags/list') if p.NAME_RE.fullmatch(name) else None
    name, sep, rest = key.rpartition('/manifests/')
    if not sep or not p.NAME_RE.fullmatch(name):
        return None
    tag, _, variant = rest.partition('@accept=')
    if not p.TAG_RE.fullmatch(tag):
        return None  # by digest: content, never refreshed
    types = p.MANIFEST_TYPES
    if variant:
        try:
            types = tuple(_LONG[short] for short in variant.split('+'))
        except KeyError:
            return None
    return RefreshRequest(f'v2/{name}/manifests/{tag}', {'Accept': ', '.join(types)})


def _matches(platform: Any, wanted: tuple[str, ...] | None) -> bool:
    if not isinstance(platform, dict):
        return False
    os_, arch = str(platform.get('os') or ''), str(platform.get('architecture') or '')
    if not os_ or not arch or os_ == 'unknown' or arch == 'unknown':
        return False
    if wanted is None:
        return True
    variant = str(platform.get('variant') or '')
    exact = f'{os_}/{arch}/{variant}' if variant else f'{os_}/{arch}'
    return exact in wanted or f'{os_}/{arch}' in wanted


def _digest(value: Any) -> str | None:
    return value if isinstance(value, str) and p.DIGEST_RE.fullmatch(value) else None


async def _document(ctx: WarmContext, fetched: Fetched, what: str) -> dict[str, Any]:
    data = await ctx.read(fetched, max_bytes=p.MANIFEST_MAX_BYTES)
    try:
        doc = await asyncio.to_thread(json.loads, data)
    except ValueError:
        raise UpstreamError(f'{what}: the manifest is not valid JSON') from None
    if not isinstance(doc, dict):
        raise UpstreamError(f'{what}: the manifest is not a JSON object')
    return doc


async def _image(ctx: WarmContext, name: str, manifest: dict[str, Any]) -> None:
    digests: list[str] = []
    config = manifest.get('config')
    if isinstance(config, dict) and (digest := _digest(config.get('digest'))):
        digests.append(digest)
    layers = manifest.get('layers')
    for layer in layers if isinstance(layers, list) else []:
        if isinstance(layer, dict) and not layer.get('urls') and (digest := _digest(layer.get('digest'))):
            digests.append(digest)
    limit = asyncio.Semaphore(LAYER_CONCURRENCY)

    async def blob(digest: str) -> None:
        async with limit:
            await ctx.get(f'v2/{name}/blobs/{digest}')

    async with asyncio.TaskGroup() as group:
        for digest in dict.fromkeys(digests):
            group.create_task(blob(digest))


async def warm(ctx: WarmContext, item: str) -> None:
    name, reference, platforms = parse_item(item)
    top = await ctx.get(f'v2/{name}/manifests/{reference}', {'Accept': ACCEPT})
    doc = await _document(ctx, top, f'{name}:{reference}')
    entries = doc.get('manifests')
    if not isinstance(entries, list):  # a single-platform image
        await _image(ctx, name, doc)
        return
    chosen: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        annotations = entry.get('annotations')
        if isinstance(annotations, dict) and ATTESTATION in annotations:
            continue
        digest = _digest(entry.get('digest'))
        if digest is not None and _matches(entry.get('platform'), platforms):
            chosen.append(digest)
    if not chosen:
        wanted = ','.join(platforms) if platforms else 'any platform'
        raise NotFound(f'{name}:{reference} has no manifest for {wanted}')
    for digest in dict.fromkeys(chosen):
        fetched = await ctx.get(f'v2/{name}/manifests/{digest}', {'Accept': ACCEPT})
        await _image(ctx, name, await _document(ctx, fetched, f'{name}@{digest[:19]}'))
