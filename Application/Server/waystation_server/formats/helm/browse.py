"""Helm browse views (OpenAPI ``listHelmCharts`` / ``listHelmVersions``): charts → versions.

Both read ``helm_charts``: a hosted repository's uploaded chart versions, a proxy's cached ``index.yaml`` listing
(recorded when the index is stored), and for a group its members' records (a version listed by several members is
shown once, from the first member). ``cached`` says whether the chart file is stored (always for hosted
repositories); versions are newest first by SemVer precedence.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    BrowseContext,
    HelmChart,
    HelmChartPage,
    HelmVersion,
    HelmVersionPage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import helm as helm_db
from waystation_server.db import repositories as repos_db
from waystation_server.formats.helm.index import URL_SAFE
from waystation_server.repos.model import RepoType

MAX_NAME = 250


async def repo_ids(ctx: BrowseContext) -> list[str]:
    """The repositories whose records a view shows: the repository, or a group's Helm members in order."""
    repo = ctx.repo
    if repo.type is not RepoType.GROUP:
        return [repo.id]
    out: list[str] = []
    for name in repo.group.members if repo.group is not None else ():
        member = await repos_db.get_by_name_ci(ctx.db, name)
        if member is not None and member.format == 'helm' and member.type is not RepoType.GROUP:
            out.append(member.id)
    return out


def _chart(name: str, latest: dict[str, Any], count: int) -> HelmChart:
    return HelmChart(
        name=name,
        latest_version=str(latest.get('version') or ''),
        app_version=latest.get('app_version'),
        description=latest.get('description'),
        icon=latest.get('icon'),
        deprecated=bool(latest.get('deprecated')),
        version_count=count,
    )


async def _stored_sizes(ctx: BrowseContext, docs: Sequence[dict[str, Any]]) -> dict[tuple[str, str], int]:
    by_repo: dict[str, list[str]] = {}
    for doc in docs:
        by_repo.setdefault(str(doc['repo_id']), []).append(str(doc['path']))
    out: dict[tuple[str, str], int] = {}
    for repo_id, paths in by_repo.items():
        for path, size in (await assets_db.sizes(ctx.db, repo_id, paths)).items():
            out[(repo_id, path)] = size
    return out


class HelmBrowse:
    async def charts(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> HelmChartPage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        ids = await repo_ids(ctx)
        docs = await helm_db.charts(ctx.db, ids, after=after, q=q, limit=limit + 1) if ids else []
        more = len(docs) > limit
        items = [_chart(str(d['name']), d['latest'], int(d['version_count'])) for d in docs[:limit]]
        return HelmChartPage(items=items, next_cursor=encode_cursor(items[-1].name) if more and items else None)

    async def versions(self, ctx: BrowseContext, *, chart: str, cursor: str | None, limit: int) -> HelmVersionPage:
        if not chart or len(chart) > MAX_NAME:
            raise NotFound('chart not found')
        ids = await repo_ids(ctx)
        found = await helm_db.charts(ctx.db, ids, after=None, q=None, limit=1, name_is=chart) if ids else []
        if not found:
            raise NotFound('chart not found')
        before = decode_cursor(cursor, str)[0] if cursor else None
        docs = await helm_db.versions(ctx.db, ids, chart, before=before, limit=limit + 1)
        more = len(docs) > limit
        docs = docs[:limit]
        sizes = await _stored_sizes(ctx, docs)
        repo_url = f'{ctx.base_url}/repository/{ctx.repo.name}/'
        items = []
        for doc in docs:
            stored = sizes.get((str(doc['repo_id']), str(doc['path'])))
            items.append(
                HelmVersion(
                    version=str(doc['version']),
                    app_version=doc.get('app_version'),
                    description=doc.get('description'),
                    created_at=doc.get('created'),
                    digest=doc.get('digest'),
                    size_bytes=stored if stored is not None else doc.get('size'),
                    cached=stored is not None,
                    deprecated=bool(doc.get('deprecated')),
                    path=str(doc['path']),
                    download_url=repo_url + quote(str(doc['path']), safe=URL_SAFE),
                )
            )
        latest = found[0]
        return HelmVersionPage(
            chart=_chart(chart, latest['latest'], int(latest['version_count'])),
            items=items,
            next_cursor=encode_cursor(str(docs[-1]['version_sort'])) if more and docs else None,
        )
