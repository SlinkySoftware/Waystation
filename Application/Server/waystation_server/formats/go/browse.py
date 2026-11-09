"""Go browse views (OpenAPI ``listGoModules`` / ``listGoVersions``).

- **Modules**: from ``go_modules`` (a summary per module path: written in the background when a version list or a
  module file is cached), with the module zips held per module (``assets`` under ``<module>/@v/``).
- **Versions**: the union of the cached version list (tagged versions) and the versions the repository holds files
  of (pseudo-versions appear only that way), newest first in semantic version order, each with the files held
  (``.info``, ``.mod``, ``.zip``) and its commit time from the cached ``.info`` (read for the page's items only,
  bounded). Groups are browsed through their members (404 here).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Literal

from waystation_common.cleanup.versions import is_prerelease
from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    BrowseContext,
    GoFile,
    GoModule,
    GoModulePage,
    GoVersion,
    GoVersionPage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import go as go_db
from waystation_server.formats.go import modpath
from waystation_server.formats.go.common import latest_of, newest_first, parse_list
from waystation_server.repos.model import RepoType

MAX_INFO = 64 * 1024
FileKind = Literal['info', 'mod', 'zip']
KINDS: tuple[FileKind, ...] = ('info', 'mod', 'zip')


def _time(data: bytes) -> datetime | None:
    try:
        doc = json.loads(data)
        value = doc.get('Time') if isinstance(doc, dict) else None
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _file(doc: dict[str, Any], kind: FileKind) -> GoFile:
    return GoFile(
        kind=kind,
        size=int(doc['size']),
        sha256=str(doc['blob_sha256']),
        cached_at=doc.get('created_at'),
        last_downloaded_at=doc.get('last_downloaded_at'),
        downloads=int(doc.get('download_count') or 0),
    )


class GoBrowse:
    @staticmethod
    def _no_group(ctx: BrowseContext) -> None:
        if ctx.repo.type is RepoType.GROUP:
            raise NotFound('a group is browsed through its members')

    async def modules(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> GoModulePage:
        self._no_group(ctx)
        after = decode_cursor(cursor, str)[0] if cursor else None
        query = q.strip() if q and q.strip() else None
        docs = await go_db.page(ctx.db, ctx.repo.id, after=after, q=query, limit=limit + 1)
        more = len(docs) > limit
        docs = docs[:limit]
        escaped = {str(d['name']): modpath.escape_path(str(d['name'])) for d in docs}
        stats = await go_db.zip_stats(ctx.db, ctx.repo.id, escaped.values())
        items = []
        for doc in docs:
            name = str(doc['name'])
            found = stats.get(escaped[name], go_db.ZipStats(0, 0))
            items.append(
                GoModule(
                    module=name,
                    latest=doc.get('latest'),
                    version_count=int(doc.get('version_count') or 0),
                    cached_versions=found.count,
                    size_bytes=found.size,
                )
            )
        return GoModulePage(items=items, next_cursor=encode_cursor(items[-1].module) if more and items else None)

    async def versions(self, ctx: BrowseContext, *, module: str, cursor: str | None, limit: int) -> GoVersionPage:
        self._no_group(ctx)
        try:
            escaped = modpath.escape_path(module.strip())
        except modpath.InvalidModule:
            raise NotFound('module not found') from None
        module = module.strip()
        offset = decode_cursor(cursor, int)[0] if cursor else 0
        held = await go_db.module_files(ctx.db, ctx.repo.id, escaped)
        listed: list[str] = []
        files: dict[str, dict[FileKind, dict[str, Any]]] = {}
        for doc in held:
            route = modpath.parse_route(str(doc['path']))
            if route is None or route.module != module:
                continue
            if route.op is modpath.Op.LIST:
                asset = await assets_db.get(ctx.db, ctx.repo.id, str(doc['path']))
                if asset is not None:
                    data = await ctx.proxy.read(asset)
                    listed = await asyncio.to_thread(parse_list, data)
            elif route.op.value in KINDS and modpath.is_canonical(route.version):
                files.setdefault(route.version, {})[route.op.value] = doc
        summary = await go_db.get_module(ctx.db, ctx.repo.id, module)
        if summary is None and not listed and not files:
            raise NotFound('module not found')
        ordered = newest_first([*listed, *files])
        listed_set = set(listed)
        items = []
        for version in ordered[offset : offset + limit]:
            held_files = files.get(version, {})
            when: datetime | None = None
            info = held_files.get('info')
            if info is not None and int(info['size']) <= MAX_INFO:
                asset = await assets_db.get(ctx.db, ctx.repo.id, str(info['path']))
                if asset is not None:
                    when = _time(await ctx.proxy.read(asset, max_bytes=MAX_INFO))
            entries = [_file(held_files[k], k) for k in KINDS if k in held_files]
            items.append(
                GoVersion(
                    version=version,
                    time=when,
                    prerelease=is_prerelease('go', version),
                    pseudo=modpath.is_pseudo(version),
                    incompatible=version.endswith('+incompatible'),
                    listed=version in listed_set,
                    cached='zip' in held_files,
                    files=entries,
                    size_bytes=sum(f.size for f in entries),
                )
            )
        zips = [f['zip'] for f in files.values() if 'zip' in f]
        head = GoModule(
            module=module,
            latest=latest_of(listed) if listed else (summary or {}).get('latest') or latest_of(list(files)),
            version_count=len(listed),
            cached_versions=len(zips),
            size_bytes=sum(int(z['size']) for z in zips),
        )
        more = offset + limit < len(ordered)
        return GoVersionPage(module=head, items=items, next_cursor=encode_cursor(offset + limit) if more else None)
