"""Cargo browse views (OpenAPI ``listCargoCrates`` / ``listCargoVersions``).

- **Crates**: from ``cargo_crates`` (one summary per crate: proxy, written in the background when an index file is
  cached; hosted, on publish and yank), with the ``.crate`` files held per crate (``assets`` under ``crates/<crate>/``).
- **Versions**: the crate's index entries (hosted: the stored entries; proxy: the cached index file, read bounded) and
  the versions the repository holds files of, newest first in SemVer order, each with its checksum, yanked state,
  dependencies and features and the file held. Groups are browsed through their members (404 here).
"""

from __future__ import annotations

import asyncio
from typing import Any

from waystation_common.cleanup.versions import is_prerelease
from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    BrowseContext,
    CargoCrate,
    CargoCratePage,
    CargoVersion,
    CargoVersionPage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import cargo as cargo_db
from waystation_server.formats.cargo.common import (
    MAX_INDEX,
    index_path,
    latest_of,
    newest_first,
    parse_index,
    parse_route,
    valid_name,
)
from waystation_server.formats.cargo.hosted import entries_of
from waystation_server.repos.model import RepoType


class CargoBrowse:
    @staticmethod
    def _no_group(ctx: BrowseContext) -> None:
        if ctx.repo.type is RepoType.GROUP:
            raise NotFound('a group is browsed through its members')

    async def crates(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> CargoCratePage:
        self._no_group(ctx)
        after = decode_cursor(cursor, str)[0] if cursor else None
        query = q.strip() if q and q.strip() else None
        docs = await cargo_db.page(ctx.db, ctx.repo.id, after=after, q=query, limit=limit + 1)
        more = len(docs) > limit
        docs = docs[:limit]
        stats = await cargo_db.file_stats(ctx.db, ctx.repo.id, [str(d['name']) for d in docs])
        items = []
        for doc in docs:
            found = stats.get(str(doc['name']), cargo_db.FileStats(0, 0))
            items.append(
                CargoCrate(
                    name=str(doc.get('display_name') or doc['name']),
                    description=doc.get('description'),
                    latest=doc.get('latest'),
                    version_count=int(doc.get('version_count') or 0),
                    cached_versions=found.count,
                    size_bytes=found.size,
                )
            )
        last = str(docs[-1]['name']) if docs else None
        return CargoCratePage(items=items, next_cursor=encode_cursor(last) if more and last else None)

    async def _entries(self, ctx: BrowseContext, crate: str, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if ctx.repo.type is RepoType.HOSTED:
            return entries_of(files)
        asset = await assets_db.get(ctx.db, ctx.repo.id, index_path(crate))
        if asset is None:
            return []
        data = await ctx.proxy.read(asset, max_bytes=MAX_INDEX)
        return await asyncio.to_thread(parse_index, data, crate)

    async def versions(self, ctx: BrowseContext, *, crate: str, cursor: str | None, limit: int) -> CargoVersionPage:
        self._no_group(ctx)
        name = crate.strip()
        if not valid_name(name):
            raise NotFound('crate not found')
        lower = name.lower()
        offset = decode_cursor(cursor, int)[0] if cursor else 0
        files = await cargo_db.crate_files(ctx.db, ctx.repo.id, lower)
        entries = {str(e['vers']): e for e in await self._entries(ctx, lower, files)}
        held: dict[str, dict[str, Any]] = {}
        for found in files:
            route = parse_route(str(found['path']), 'GET')
            if route is not None and route.crate == lower:
                held[route.version] = found
        summary = await cargo_db.get_crate(ctx.db, ctx.repo.id, lower)
        if summary is None and not entries and not held:
            raise NotFound('crate not found')
        ordered = newest_first([*entries, *held])
        items = []
        for version in ordered[offset : offset + limit]:
            entry: dict[str, Any] = entries.get(version) or {}
            doc = held.get(version)
            details = (doc or {}).get('cargo') or {}
            features = [*dict(entry.get('features') or {}), *dict(entry.get('features2') or {})]
            items.append(
                CargoVersion(
                    version=version,
                    prerelease=is_prerelease('cargo', version),
                    yanked=bool(entry.get('yanked')),
                    cksum=str(entry.get('cksum') or (doc or {}).get('blob_sha256') or ''),
                    rust_version=entry.get('rust_version'),
                    dependencies=len(entry.get('deps') or []),
                    features=sorted(set(features)),
                    cached=doc is not None,
                    size=int(doc['size']) if doc is not None else None,
                    published_by=details.get('published_by'),
                    published_at=details.get('published_at'),
                    cached_at=(doc or {}).get('created_at'),
                    last_downloaded_at=(doc or {}).get('last_downloaded_at'),
                    downloads=int((doc or {}).get('download_count') or 0),
                )
            )
        head = CargoCrate(
            name=str((summary or {}).get('display_name') or next(iter(entries.values()), {}).get('name') or name),
            description=(summary or {}).get('description'),
            latest=latest_of(entries.values()) if entries else (summary or {}).get('latest'),
            version_count=len(entries),
            cached_versions=len(held),
            size_bytes=sum(int(d['size']) for d in held.values()),
        )
        more = offset + limit < len(ordered)
        return CargoVersionPage(crate=head, items=items, next_cursor=encode_cursor(offset + limit) if more else None)
