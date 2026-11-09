"""PyPI browse views (OpenAPI ``listPypiProjects`` / ``listPypiVersions``) and yanking (``yankPypiVersion``).

- **Projects**: from ``pypi_packages`` (a summary per project: written in the background when a proxy caches a
  project page, and on every hosted upload), with the files held per project (``assets`` under
  ``packages/<project>/``, sidecars excluded).
- **Versions**: a proxy's cached project page (JSON preferred, else HTML), a hosted repository's stored files, newest
  first in PEP 440 order, each with its files; ``cached`` = the file is held by the repository. Groups are browsed
  through their members (404 here).
- **Yank** (hosted only): every file of the version yanked (with a reason) or un-yanked; audited
  (``content.update``).

Parsing runs in a thread; the engine's metadata bound applies to cached pages.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from waystation_common.cleanup.versions import pep440_is_prerelease, pep440_key
from waystation_common.errors import NotFound
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.browse.models import (
    BrowseContext,
    PypiFile,
    PypiProject,
    PypiProjectPage,
    PypiVersion,
    PypiVersionPage,
    PypiYank,
    PypiYankResult,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import pypi as pypi_db
from waystation_server.formats.pypi import simple
from waystation_server.formats.pypi.common import page_from_assets, page_key, summary_fields
from waystation_server.repos.model import RepoType

_SEPARATORS = re.compile(r'[-_.]+')


def _kind(filename: str) -> simple.DistKind:
    dist = simple.parse_filename(filename)
    return dist.kind if dist is not None else 'other'


class PypiBrowse:
    @staticmethod
    def _no_group(ctx: BrowseContext) -> None:
        if ctx.repo.type is RepoType.GROUP:
            raise NotFound('a group is browsed through its members')

    async def projects(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> PypiProjectPage:
        self._no_group(ctx)
        after = decode_cursor(cursor, str)[0] if cursor else None
        query = _SEPARATORS.sub('-', q.strip().lower()) if q and q.strip() else None
        docs = await pypi_db.page(ctx.db, ctx.repo.id, after=after, q=query, limit=limit + 1)
        more = len(docs) > limit
        docs = docs[:limit]
        stats = await pypi_db.file_stats(ctx.db, ctx.repo.id, [str(d['name']) for d in docs])
        items = []
        for doc in docs:
            name = str(doc['name'])
            found = stats.get(name, pypi_db.FileStats(0, 0))
            items.append(
                PypiProject(
                    name=name,
                    display_name=str(doc.get('display_name') or name),
                    summary=doc.get('summary'),
                    latest=doc.get('latest'),
                    version_count=int(doc.get('version_count') or 0),
                    files=found.count,
                    size_bytes=found.size,
                )
            )
        return PypiProjectPage(items=items, next_cursor=encode_cursor(items[-1].name) if more and items else None)

    async def _page(self, ctx: BrowseContext, project: str, held: list[dict[str, Any]]) -> simple.Page:
        if ctx.repo.type is RepoType.HOSTED:
            page = await asyncio.to_thread(page_from_assets, held, project)
            if not page.files:
                raise NotFound('project not found')
            return page
        for as_json in (True, False):
            asset = await assets_db.get(ctx.db, ctx.repo.id, page_key(project, as_json=as_json))
            if asset is None:
                continue
            data = await ctx.proxy.read(asset)
            if as_json:
                return await asyncio.to_thread(simple.parse_json, data, '', internal=True)
            return await asyncio.to_thread(simple.parse_html, data, 'https://localhost/', project)
        raise NotFound('project not found')

    async def versions(self, ctx: BrowseContext, *, project: str, cursor: str | None, limit: int) -> PypiVersionPage:
        self._no_group(ctx)
        project = simple.normalise(project)
        if not simple.valid_project(project):
            raise NotFound('project not found')
        offset = decode_cursor(cursor, int)[0] if cursor else 0
        held = await pypi_db.project_files(ctx.db, ctx.repo.id, project)
        page = await self._page(ctx, project, held)
        docs = {str(d['path']).rsplit('/', 1)[-1]: d for d in held}
        by_version: dict[str, list[PypiFile]] = {}
        for entry in page.files:
            dist = simple.parse_filename(entry.filename)
            version = dist.version if dist is not None else ''
            doc = docs.get(entry.filename)
            details: dict[str, Any] = (doc or {}).get('pypi') or {}
            by_version.setdefault(version, []).append(
                PypiFile(
                    filename=entry.filename,
                    kind=_kind(entry.filename),
                    size=int(doc['size']) if doc else entry.size,
                    sha256=entry.hashes.get('sha256'),
                    requires_python=entry.requires_python,
                    yanked=bool(entry.yanked),
                    yanked_reason=entry.yanked if isinstance(entry.yanked, str) else None,
                    core_metadata=bool(entry.core_metadata),
                    cached=doc is not None,
                    uploaded_at=doc.get('created_at') if doc else None,
                    uploaded_by=details.get('uploaded_by'),
                    last_downloaded_at=doc.get('last_downloaded_at') if doc else None,
                    downloads=int(doc.get('download_count') or 0) if doc else 0,
                )
            )
        for version in page.versions or ():
            by_version.setdefault(version, [])
        ordered = sorted(by_version, key=pep440_key, reverse=True)
        items = []
        for version in ordered[offset : offset + limit]:
            files = sorted(by_version[version], key=lambda f: (f.kind != 'sdist', f.filename))
            held_files = [f for f in files if f.cached]
            items.append(
                PypiVersion(
                    version=version,
                    prerelease=pep440_is_prerelease(version),
                    yanked=bool(files) and all(f.yanked for f in files),
                    files=files,
                    cached_files=len(held_files),
                    size_bytes=sum(f.size or 0 for f in held_files),
                )
            )
        stored = await pypi_db.get_package(ctx.db, ctx.repo.id, project)
        versions = [v for v in by_version if v]
        fields = summary_fields(page.name or project, versions)
        held_dists = [d for d in held if not str(d['path']).endswith('.metadata')]
        summary = PypiProject(
            name=project,
            display_name=str((stored or {}).get('display_name') or fields['display_name']),
            summary=(stored or {}).get('summary'),
            latest=fields['latest'],
            version_count=len(versions),
            files=len(held_dists),
            size_bytes=sum(int(d['size']) for d in held_dists),
        )
        more = offset + limit < len(ordered)
        return PypiVersionPage(
            project=summary, items=items, next_cursor=encode_cursor(offset + limit) if more else None
        )

    async def yank(self, ctx: BrowseContext, body: PypiYank) -> PypiYankResult:
        if ctx.repo.type is not RepoType.HOSTED:
            raise NotFound('only files of hosted repositories can be yanked')
        project = simple.normalise(body.project)
        reason = (body.reason or '').strip()[:500] if body.yanked else None
        changed = await pypi_db.set_yanked(ctx.db, ctx.repo.id, project, body.version, reason)
        if not changed:
            raise NotFound('no files of this version')
        audit.record(
            AuditAction.CONTENT_UPDATE,
            target=audit.target('pypi_release', None, f'{ctx.repo.name}/{project}=={body.version}'),
            details={
                'repository': ctx.repo.name,
                'project': project,
                'version': body.version,
                'yanked': body.yanked,
                'reason': reason,
                'files': changed,
            },
        )
        return PypiYankResult(project=project, version=body.version, yanked=body.yanked, files=changed)
