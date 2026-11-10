"""Terraform browse views (OpenAPI ``listTerraformProviders`` / ``listTerraformVersions``).

- **Providers**: from ``terraform_providers`` (a summary per provider address, written in the background when a
  registry version list or an archive is cached), with the archives held per provider (``.zip`` assets under
  ``<hostname>/<namespace>/<type>/``).
- **Versions**: the union of the cached registry version list and the versions the repository holds archives of,
  newest first in semantic version order, each with its protocols and platforms (from the list) and the archives
  held (size, sha256, ``h1:``, downloads).
"""

from __future__ import annotations

import asyncio
from typing import Any

from waystation_common.cleanup.versions import is_prerelease
from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    BrowseContext,
    TerraformArchive,
    TerraformProvider,
    TerraformProviderPage,
    TerraformVersion,
    TerraformVersionPage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import terraform as tf_db
from waystation_server.formats.terraform import paths, registry


def _archive(doc: dict[str, Any], route: paths.Route, h1: str | None) -> TerraformArchive:
    return TerraformArchive(
        os=route.os,
        arch=route.arch,
        filename=route.filename,
        size=int(doc['size']),
        sha256=str(doc['blob_sha256']),
        h1=h1,
        cached_at=doc.get('created_at'),
        last_downloaded_at=doc.get('last_downloaded_at'),
        downloads=int(doc.get('download_count') or 0),
    )


class TerraformBrowse:
    async def providers(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> TerraformProviderPage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        query = q.strip() if q and q.strip() else None
        docs = await tf_db.page(ctx.db, ctx.repo.id, after=after, q=query, limit=limit + 1)
        more = len(docs) > limit
        docs = docs[:limit]
        stats = await tf_db.archive_stats(ctx.db, ctx.repo.id, [str(d['name']) for d in docs])
        items = []
        for doc in docs:
            name = str(doc['name'])
            held = stats.get(name, tf_db.ArchiveStats(0, 0))
            items.append(
                TerraformProvider(
                    provider=name,
                    latest=doc.get('latest'),
                    version_count=int(doc.get('version_count') or 0),
                    cached_archives=held.count,
                    size_bytes=held.size,
                )
            )
        next_cursor = encode_cursor(items[-1].provider) if more and items else None
        return TerraformProviderPage(items=items, next_cursor=next_cursor)

    async def versions(
        self, ctx: BrowseContext, *, provider: str, cursor: str | None, limit: int
    ) -> TerraformVersionPage:
        address = paths.parse_address(provider)
        if address is None:
            raise NotFound('provider not found')
        offset = decode_cursor(cursor, int)[0] if cursor else 0
        listed: dict[str, registry.VersionInfo] = {}
        asset = await assets_db.get(ctx.db, ctx.repo.id, paths.versions_key(address))
        if asset is not None:
            data = await ctx.proxy.read(asset, max_bytes=registry.MAX_VERSIONS)
            listed = await asyncio.to_thread(registry.parse_versions, data)
        summary = await tf_db.get_provider(ctx.db, ctx.repo.id, address.key)
        hashes = {
            (str(e.get('version')), str(e.get('os')), str(e.get('arch'))): e.get('h1')
            for e in (summary or {}).get('archives') or []
        }
        held: dict[str, list[TerraformArchive]] = {}
        for doc in await tf_db.provider_archives(ctx.db, ctx.repo.id, address.key):
            route = paths.parse_route(str(doc['path']))
            if route is None or route.op is not paths.Op.ARCHIVE or route.provider != address:
                continue
            h1 = hashes.get((route.version, route.os, route.arch))
            held.setdefault(route.version, []).append(_archive(doc, route, str(h1) if h1 else None))
        if summary is None and not listed and not held:
            raise NotFound('provider not found')
        ordered = registry.newest_first(list({*listed, *held}))
        items = []
        for version in ordered[offset : offset + limit]:
            info = listed.get(version)
            archives = held.get(version, [])
            items.append(
                TerraformVersion(
                    version=version,
                    prerelease=is_prerelease('terraform', version),
                    protocols=list(info.protocols) if info else [],
                    platforms=[f'{os_}_{arch}' for os_, arch in info.platforms] if info else [],
                    listed=info is not None,
                    archives=archives,
                    size_bytes=sum(a.size for a in archives),
                )
            )
        every = [a for group in held.values() for a in group]
        head = TerraformProvider(
            provider=address.key,
            latest=registry.latest_of(list(listed)) if listed else (summary or {}).get('latest'),
            version_count=len(listed),
            cached_archives=len(every),
            size_bytes=sum(a.size for a in every),
        )
        more = offset + limit < len(ordered)
        return TerraformVersionPage(
            provider=head, items=items, next_cursor=encode_cursor(offset + limit) if more else None
        )
