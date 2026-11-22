"""NuGet browse views (OpenAPI ``listNugetPackages`` / ``listNugetVersions`` / ``setNugetListing``).

Packages come from ``nuget_packages`` (hosted: pushed packages; proxy: packages whose version list or registration is
cached; group: its members', first member wins per id). Versions: a hosted repository's packages (assets, with their
listing state, size and who pushed them), a proxy's cached version list (``cached`` = the ``.nupkg`` is stored), for a
group its members' (first member wins per version); newest first by NuGet version order.
"""

from __future__ import annotations

from typing import Any

from waystation_common import nuget
from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    BrowseContext,
    NugetListing,
    NugetPackage,
    NugetPackagePage,
    NugetVersion,
    NugetVersionPage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import nuget as nuget_db
from waystation_server.db import repositories as repos_db
from waystation_server.formats.nuget import protocol
from waystation_server.formats.nuget.hosted import change_listing, stored_versions
from waystation_server.repos.model import RepoConfig, RepoType


async def members(ctx: BrowseContext) -> list[RepoConfig]:
    """The repositories a view shows: the repository, or a group's NuGet members in order."""
    repo = ctx.repo
    if repo.type is not RepoType.GROUP:
        return [repo]
    out: list[RepoConfig] = []
    for name in repo.group.members if repo.group is not None else ():
        member = await repos_db.get_by_name_ci(ctx.db, name)
        if member is not None and member.format == 'nuget' and member.type is not RepoType.GROUP:
            out.append(member)
    return out


def _package(doc: dict[str, Any]) -> NugetPackage:
    return NugetPackage(
        name=str(doc['name']),
        id=str(doc.get('display_name') or doc['name']),
        latest_version=doc.get('latest'),
        version_count=int(doc.get('version_count') or 0),
        description=doc.get('description'),
        authors=doc.get('authors'),
        updated_at=doc.get('updated_at'),
    )


class NugetBrowse:
    async def packages(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> NugetPackagePage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        chosen: dict[str, dict[str, Any]] = {}
        for repo in await members(ctx):
            for doc in await nuget_db.page(ctx.db, repo.id, after=after, q=q, limit=limit + 1):
                chosen.setdefault(str(doc['name']), doc)
        names = sorted(chosen)[: limit + 1]
        more = len(names) > limit
        items = [_package(chosen[n]) for n in names[:limit]]
        return NugetPackagePage(items=items, next_cursor=encode_cursor(items[-1].name) if more and items else None)

    async def versions(self, ctx: BrowseContext, *, package: str, cursor: str | None, limit: int) -> NugetVersionPage:
        if not nuget.valid_id(package):
            raise NotFound('package not found')
        name = package.lower()
        summary: dict[str, Any] | None = None
        found: dict[str, NugetVersion] = {}
        repo_url = f'{ctx.base_url}/repository/{ctx.repo.name}/'
        for repo in await members(ctx):
            doc = await nuget_db.get_package(ctx.db, repo.id, name)
            if doc is None:
                continue
            summary = summary or doc
            for item in await self._versions(ctx, repo, doc, repo_url):
                found.setdefault(item.version.lower(), item)
        if summary is None:
            raise NotFound('package not found')
        before = decode_cursor(cursor, str)[0] if cursor else None
        ordered = sorted(found.values(), key=lambda v: nuget.sort_string(v.version), reverse=True)
        if before is not None:
            ordered = [v for v in ordered if nuget.sort_string(v.version) < before]
        page = ordered[:limit]
        more = len(ordered) > limit
        return NugetVersionPage(
            package=_package(summary),
            items=page,
            next_cursor=encode_cursor(nuget.sort_string(page[-1].version)) if more and page else None,
        )

    @staticmethod
    async def _versions(
        ctx: BrowseContext, repo: RepoConfig, summary: dict[str, Any], repo_url: str
    ) -> list[NugetVersion]:
        name = str(summary['name'])
        if repo.type is RepoType.HOSTED:
            out = []
            for v in await stored_versions(ctx.db, repo.id, name):
                path = protocol.nupkg_path(name, v.flat)
                published = v.details.get('published')
                out.append(
                    NugetVersion(
                        version=v.version,
                        prerelease=nuget.is_prerelease(v.version),
                        listed=v.listed,
                        cached=True,
                        size_bytes=v.size,
                        published_at=published,
                        uploaded_by=v.details.get('uploaded_by'),
                        path=path,
                        download_url=repo_url + path,
                    )
                )
            return out
        versions = [str(v) for v in summary.get('versions') or ()]
        paths = {v: protocol.nupkg_path(name, v) for v in versions}
        sizes = await assets_db.sizes(ctx.db, repo.id, list(paths.values()))
        return [
            NugetVersion(
                version=v,
                prerelease=nuget.is_prerelease(v),
                listed=True,
                cached=paths[v] in sizes,
                size_bytes=sizes.get(paths[v]),
                path=paths[v],
                download_url=repo_url + paths[v],
            )
            for v in versions
        ]

    async def listing(self, ctx: BrowseContext, body: NugetListing) -> NugetListing:
        if ctx.repo.type is not RepoType.HOSTED:
            raise NotFound('only versions of hosted repositories can be unlisted')
        if not nuget.valid_id(body.package):
            raise NotFound('package not found')
        flat = await change_listing(ctx.db, ctx.repo, body.package, body.version, listed=body.listed)
        return NugetListing(package=body.package.lower(), version=flat, listed=body.listed)
