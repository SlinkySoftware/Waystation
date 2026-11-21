"""Terraform browse views (OpenAPI ``listTerraformProviders`` / ``listTerraformVersions`` / ``listTerraformModules`` /
``listTerraformModuleVersions``).

- **Providers** (proxy): from ``terraform_providers`` (a summary per provider address, written in the background when
  a registry version list or an archive is cached), with the archives held per provider (``.zip`` assets under
  ``<hostname>/<namespace>/<type>/``). **Versions**: the union of the cached registry version list and the versions
  the repository holds archives of, newest first in semantic version order, each with its protocols and platforms
  (from the list) and the archives held (size, sha256, ``h1:``, downloads).
- **Providers** (the hosted repository): the uploaded releases in ``terraform_providers`` (``<namespace>/<type>``),
  each platform's package with its download figures from ``assets`` (``v1/providers/<namespace>/<type>/``).
- **Modules** (proxy and hosted, T-P5-TFM): ``terraform_modules`` (proxy: the cached version list and the archives
  cached; hosted: the published versions), the archives' download figures from ``assets``
  (``v1/modules/<namespace>/<name>/<system>/``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from waystation_common.cleanup.versions import is_prerelease
from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    BrowseContext,
    TerraformArchive,
    TerraformModule,
    TerraformModuleArchive,
    TerraformModulePage,
    TerraformModuleVersion,
    TerraformModuleVersionPage,
    TerraformProvider,
    TerraformProviderPage,
    TerraformVersion,
    TerraformVersionPage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import terraform as tf_db
from waystation_server.formats.terraform import modules, paths, registry
from waystation_server.formats.terraform.descriptor import upstream_host
from waystation_server.repos.model import RepoType


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


def _files_by_path(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(d['path']): d for d in docs}


class TerraformBrowse:
    async def providers(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> TerraformProviderPage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        query = q.strip() if q and q.strip() else None
        hosted = ctx.repo.type is RepoType.HOSTED
        docs = await tf_db.page(ctx.db, ctx.repo.id, after=after, q=query, limit=limit + 1, releases=hosted)
        more = len(docs) > limit
        docs = docs[:limit]
        items = []
        if hosted:
            for doc in docs:
                platforms = [p for r in doc.get('releases') or [] for p in r.get('platforms') or []]
                items.append(
                    TerraformProvider(
                        provider=str(doc['name']),
                        latest=doc.get('latest'),
                        version_count=len(doc.get('releases') or []),
                        cached_archives=len(platforms),
                        size_bytes=sum(int(p.get('size') or 0) for p in platforms),
                    )
                )
        else:
            stats = await tf_db.archive_stats(ctx.db, ctx.repo.id, [str(d['name']) for d in docs])
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
        if ctx.repo.type is RepoType.HOSTED:
            return await self._hosted_versions(ctx, provider=provider, cursor=cursor, limit=limit)
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

    async def _hosted_versions(
        self, ctx: BrowseContext, *, provider: str, cursor: str | None, limit: int
    ) -> TerraformVersionPage:
        address = paths.parse_hosted_provider_address(provider)
        doc = await tf_db.get_provider(ctx.db, ctx.repo.id, address.key) if address is not None else None
        if address is None or doc is None or not doc.get('releases'):
            raise NotFound('provider not found')
        offset = decode_cursor(cursor, int)[0] if cursor else 0
        files = _files_by_path(await tf_db.files_under(ctx.db, ctx.repo.id, paths.provider_prefix(address)))
        releases = {str(r['version']): r for r in doc['releases']}
        ordered = registry.newest_first(list(releases))
        items = []
        total = count = 0
        for version in ordered:
            release = releases[version]
            archives = []
            for platform in release.get('platforms') or []:
                asset = files.get(paths.provider_file_key(address, version, platform['filename']), {})
                archives.append(
                    TerraformArchive(
                        os=platform['os'],
                        arch=platform['arch'],
                        filename=platform['filename'],
                        size=int(platform.get('size') or 0),
                        sha256=str(platform['sha256']),
                        h1=platform.get('h1'),
                        cached_at=release.get('created_at'),
                        last_downloaded_at=asset.get('last_downloaded_at'),
                        downloads=int(asset.get('download_count') or 0),
                    )
                )
            total += sum(a.size for a in archives)
            count += len(archives)
            items.append(
                TerraformVersion(
                    version=version,
                    prerelease=is_prerelease('terraform', version),
                    protocols=list(release.get('protocols') or []),
                    platforms=[f'{a.os}_{a.arch}' for a in archives],
                    listed=True,
                    archives=archives,
                    size_bytes=sum(a.size for a in archives),
                    key_id=release.get('key_id'),
                    uploaded_by=release.get('uploaded_by'),
                )
            )
        head = TerraformProvider(
            provider=address.key,
            latest=registry.latest_of(ordered),
            version_count=len(ordered),
            cached_archives=count,
            size_bytes=total,
        )
        more = offset + limit < len(items)
        return TerraformVersionPage(
            provider=head,
            items=items[offset : offset + limit],
            next_cursor=encode_cursor(offset + limit) if more else None,
        )

    # ------------------------------------------------------------------ modules
    async def modules(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> TerraformModulePage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        query = q.strip() if q and q.strip() else None
        docs = await tf_db.module_page(ctx.db, ctx.repo.id, after=after, q=query, limit=limit + 1)
        more = len(docs) > limit
        items = [self._module_head(ctx, doc) for doc in docs[:limit]]
        next_cursor = encode_cursor(items[-1].module) if more and items else None
        return TerraformModulePage(items=items, next_cursor=next_cursor)

    @staticmethod
    def _module_head(ctx: BrowseContext, doc: dict[str, Any]) -> TerraformModule:
        held = [v for v in doc.get('versions') or [] if v.get('sha256')]
        hosted = ctx.repo.type is RepoType.HOSTED
        return TerraformModule(
            module=str(doc['name']),
            latest=doc.get('latest'),
            version_count=len(held) if hosted else int(doc.get('version_count') or 0),
            cached_versions=len(held),
            size_bytes=sum(int(v.get('size') or 0) for v in held),
        )

    async def module_versions(
        self, ctx: BrowseContext, *, module: str, cursor: str | None, limit: int
    ) -> TerraformModuleVersionPage:
        address = paths.parse_module_address(module)
        doc = await tf_db.get_module(ctx.db, ctx.repo.id, address.key) if address is not None else None
        if address is None or doc is None:
            raise NotFound('module not found')
        offset = decode_cursor(cursor, int)[0] if cursor else 0
        hosted = ctx.repo.type is RepoType.HOSTED
        held = {str(v['version']): v for v in doc.get('versions') or [] if v.get('sha256')}
        listed: list[str] = list(held) if hosted else []
        if not hosted and ctx.repo.proxy is not None:
            listed = await self._listed_modules(ctx, address)
        files = _files_by_path(await tf_db.files_under(ctx.db, ctx.repo.id, paths.module_prefix(address)))
        ordered = registry.newest_first(list({*listed, *held}))
        items = []
        for version in ordered[offset : offset + limit]:
            entry = held.get(version)
            archive = None
            if entry is not None:
                path = paths.module_archive_key(address, version, str(entry.get('filename') or paths.ARCHIVES[0]))
                asset = files.get(path, {})
                archive = TerraformModuleArchive(
                    filename=str(entry.get('filename') or paths.ARCHIVES[0]),
                    path=path,
                    size=int(entry.get('size') or 0),
                    sha256=str(entry['sha256']),
                    source=entry.get('source'),
                    uploaded_by=entry.get('uploaded_by'),
                    created_at=entry.get('created_at'),
                    last_downloaded_at=asset.get('last_downloaded_at'),
                    downloads=int(asset.get('download_count') or 0),
                )
            items.append(
                TerraformModuleVersion(
                    version=version,
                    prerelease=is_prerelease('terraform', version),
                    listed=hosted or version in listed,
                    archive=archive,
                )
            )
        more = offset + limit < len(ordered)
        return TerraformModuleVersionPage(
            module=self._module_head(ctx, doc),
            items=items,
            next_cursor=encode_cursor(offset + limit) if more else None,
        )

    @staticmethod
    async def _listed_modules(ctx: BrowseContext, address: paths.Module) -> list[str]:
        """The versions of the proxy's cached origin list (any origin host it was fetched from)."""
        upstream = ctx.repo.proxy.upstream_url if ctx.repo.proxy else ''
        host = upstream_host(upstream)
        if host is None:
            return []
        asset = await assets_db.get(ctx.db, ctx.repo.id, paths.module_versions_key(host[0], address))
        if asset is None:
            return []
        data = await ctx.proxy.read(asset, max_bytes=modules.MAX_MODULE_VERSIONS)
        return await asyncio.to_thread(modules.parse_module_versions, data)
