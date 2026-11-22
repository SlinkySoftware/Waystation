"""Composer browse views (OpenAPI ``listComposerPackages`` / ``listComposerVersions``).

Packages come from ``composer_packages`` (hosted: uploaded packages; proxy: packages whose tagged ``p2`` document is
cached; group: its members', first member wins per name). Versions come from the summary (a proxy's cached tagged
versions, ``cached`` = the archive is stored; a hosted repository's uploads, with who uploaded them), for a group
its members' (first member wins per version); newest first by Composer version order.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast

from waystation_common import composer
from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    BrowseContext,
    ComposerPackage,
    ComposerPackagePage,
    ComposerVersion,
    ComposerVersionPage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import composer as composer_db
from waystation_server.db import repositories as repos_db
from waystation_server.repos.model import RepoConfig, RepoType

Stability = Literal['stable', 'RC', 'beta', 'alpha', 'dev']


async def members(ctx: BrowseContext) -> list[RepoConfig]:
    """The repositories a view shows: the repository, or a group's Composer members in order."""
    repo = ctx.repo
    if repo.type is not RepoType.GROUP:
        return [repo]
    out: list[RepoConfig] = []
    for name in repo.group.members if repo.group is not None else ():
        member = await repos_db.get_by_name_ci(ctx.db, name)
        if member is not None and member.format == 'composer' and member.type is not RepoType.GROUP:
            out.append(member)
    return out


def _package(doc: dict[str, Any]) -> ComposerPackage:
    return ComposerPackage(
        name=str(doc['name']),
        latest_version=doc.get('latest'),
        version_count=int(doc.get('version_count') or 0),
        description=doc.get('description'),
        type=doc.get('type'),
        updated_at=doc.get('updated_at'),
    )


def _published(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


class ComposerBrowse:
    async def packages(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> ComposerPackagePage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        chosen: dict[str, dict[str, Any]] = {}
        for repo in await members(ctx):
            for doc in await composer_db.page(ctx.db, repo.id, after=after, q=q, limit=limit + 1):
                if doc.get('versions') is not None or doc.get('version_count'):
                    chosen.setdefault(str(doc['name']), doc)
        names = sorted(chosen)[: limit + 1]
        more = len(names) > limit
        items = [_package(chosen[n]) for n in names[:limit]]
        return ComposerPackagePage(items=items, next_cursor=encode_cursor(items[-1].name) if more and items else None)

    async def versions(
        self, ctx: BrowseContext, *, package: str, cursor: str | None, limit: int
    ) -> ComposerVersionPage:
        name = composer.normalise_name(package)
        if name is None:
            raise NotFound('package not found')
        summary: dict[str, Any] | None = None
        found: dict[str, ComposerVersion] = {}
        repo_url = f'{ctx.base_url}/repository/{ctx.repo.name}/'
        for repo in await members(ctx):
            doc = await composer_db.get_package(ctx.db, repo.id, name)
            if doc is None or not doc.get('version_count'):
                continue
            summary = summary or doc
            for item in await self._versions(ctx, repo, doc, repo_url):
                found.setdefault(item.version_normalized, item)
        if summary is None:
            raise NotFound('package not found')
        before = decode_cursor(cursor, str)[0] if cursor else None
        ordered = sorted(found.values(), key=lambda v: composer.sort_string(v.version_normalized), reverse=True)
        if before is not None:
            ordered = [v for v in ordered if composer.sort_string(v.version_normalized) < before]
        page = ordered[:limit]
        more = len(ordered) > limit
        return ComposerVersionPage(
            package=_package(summary),
            items=page,
            next_cursor=encode_cursor(composer.sort_string(page[-1].version_normalized)) if more and page else None,
        )

    @staticmethod
    async def _versions(
        ctx: BrowseContext, repo: RepoConfig, summary: dict[str, Any], repo_url: str
    ) -> list[ComposerVersion]:
        rows = [v for v in summary.get('versions') or () if isinstance(v, dict) and v.get('normalized')]
        paths = [str(v['path']) for v in rows if v.get('path')]
        sizes = await assets_db.sizes(ctx.db, repo.id, paths)
        out = []
        for v in rows:
            path = str(v['path']) if v.get('path') else None
            normalized = str(v['normalized'])
            out.append(
                ComposerVersion(
                    version=str(v.get('version') or normalized),
                    version_normalized=normalized,
                    stability=cast(Stability, composer.stability(normalized)),
                    cached=path is not None and path in sizes,
                    size_bytes=sizes.get(path) if path else None,
                    published_at=_published(v.get('time')),
                    uploaded_by=v.get('uploaded_by'),
                    reference=v.get('reference'),
                    path=path,
                    download_url=repo_url + path if path else None,
                )
            )
        return out
