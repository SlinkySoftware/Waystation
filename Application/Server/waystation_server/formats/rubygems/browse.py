"""RubyGems browse views (OpenAPI ``listRubygemsGems`` / ``listRubygemsVersions``).

Gems come from ``rubygems_gems`` (hosted: pushed gems; proxy: gems whose info file is cached; group: its members',
first member wins per name). Versions come from the summary (a proxy's listed versions with their checksums,
``cached`` = the ``.gem`` is stored; a hosted repository's pushes, with who pushed them), for a group its members'
(first member wins per version and platform); newest first in ``Gem::Version`` order, one row per platform.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from waystation_common import rubygems
from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    BrowseContext,
    RubygemsDependency,
    RubygemsGem,
    RubygemsGemPage,
    RubygemsVersion,
    RubygemsVersionPage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import repositories as repos_db
from waystation_server.db import rubygems as gems_db
from waystation_server.repos.model import RepoConfig, RepoType


async def members(ctx: BrowseContext) -> list[RepoConfig]:
    """The repositories a view shows: the repository, or a group's RubyGems members in order."""
    repo = ctx.repo
    if repo.type is not RepoType.GROUP:
        return [repo]
    out: list[RepoConfig] = []
    for name in repo.group.members if repo.group is not None else ():
        member = await repos_db.get_by_name_ci(ctx.db, name)
        if member is not None and member.format == 'rubygems' and member.type is not RepoType.GROUP:
            out.append(member)
    return out


def _gem(doc: dict[str, Any]) -> RubygemsGem:
    return RubygemsGem(
        name=str(doc['name']),
        latest_version=doc.get('latest'),
        version_count=int(doc.get('version_count') or 0),
        summary=doc.get('summary'),
        updated_at=doc.get('updated_at'),
    )


def _key(version: str, platform: str) -> str:
    """Sorted descending: newest first, then ``ruby`` before the other platforms."""
    return rubygems.sort_string(version) + ('\x02' if platform == rubygems.RUBY else '\x01' + platform)


class RubygemsBrowse:
    async def gems(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> RubygemsGemPage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        chosen: dict[str, dict[str, Any]] = {}
        for repo in await members(ctx):
            for doc in await gems_db.page(ctx.db, repo.id, after=after, q=q, limit=limit + 1):
                if doc.get('version_count'):
                    chosen.setdefault(str(doc['name']), doc)
        names = sorted(chosen)[: limit + 1]
        more = len(names) > limit
        items = [_gem(chosen[n]) for n in names[:limit]]
        return RubygemsGemPage(items=items, next_cursor=encode_cursor(items[-1].name) if more and items else None)

    async def versions(self, ctx: BrowseContext, *, gem: str, cursor: str | None, limit: int) -> RubygemsVersionPage:
        if not rubygems.valid_name(gem):
            raise NotFound('gem not found')
        summary: dict[str, Any] | None = None
        found: dict[str, RubygemsVersion] = {}
        repo_url = f'{ctx.base_url}/repository/{ctx.repo.name}/'
        for repo in await members(ctx):
            doc = await gems_db.get_gem(ctx.db, repo.id, gem)
            if doc is None or not doc.get('version_count'):
                continue
            summary = summary or doc
            for item in await self._versions(ctx, repo, doc, repo_url):
                found.setdefault(f'{item.version}-{item.platform}', item)
        if summary is None:
            raise NotFound('gem not found')
        before = decode_cursor(cursor, str)[0] if cursor else None
        ordered = sorted(found.values(), key=lambda v: _key(v.version, v.platform), reverse=True)
        if before is not None:
            ordered = [v for v in ordered if _key(v.version, v.platform) < before]
        page = ordered[:limit]
        more = len(ordered) > limit
        last = page[-1] if page else None
        return RubygemsVersionPage(
            gem=_gem(summary),
            items=page,
            next_cursor=encode_cursor(_key(last.version, last.platform)) if more and last else None,
        )

    @staticmethod
    async def _versions(
        ctx: BrowseContext, repo: RepoConfig, summary: dict[str, Any], repo_url: str
    ) -> list[RubygemsVersion]:
        rows = [v for v in summary.get('versions') or () if isinstance(v, dict) and v.get('version') and v.get('path')]
        sizes = await assets_db.sizes(ctx.db, repo.id, [str(v['path']) for v in rows])
        out = []
        for v in rows:
            path = str(v['path'])
            version = str(v['version'])
            published = v.get('published')
            deps = [
                RubygemsDependency(name=str(d[0]), requirement=str(d[1]))
                for d in v.get('dependencies') or ()
                if isinstance(d, list | tuple) and len(d) == 2
            ]
            out.append(
                RubygemsVersion(
                    version=version,
                    platform=str(v.get('platform') or rubygems.RUBY),
                    prerelease=rubygems.is_prerelease(version),
                    cached=path in sizes,
                    size_bytes=sizes.get(path),
                    sha256=v.get('sha256'),
                    published_at=published if isinstance(published, datetime) else None,
                    uploaded_by=v.get('uploaded_by'),
                    ruby_requirement=v.get('ruby'),
                    dependencies=deps,
                    path=path,
                    download_url=repo_url + path,
                )
            )
        return out
