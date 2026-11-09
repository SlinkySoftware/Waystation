"""Maven browse views (OpenAPI ``listMavenArtifacts`` / ``listMavenVersions``): groupId → artifactId → versions,
from the repository's components (proxies: what is cached; hosted: what was deployed). A version's files are the
generic folder listing of its ``path``. Group repositories hold nothing themselves: they list nothing."""

from __future__ import annotations

from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    BrowseContext,
    MavenArtifact,
    MavenArtifactPage,
    MavenVersion,
    MavenVersionPage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import maven as maven_db
from waystation_server.formats.maven.version import is_snapshot


class MavenBrowse:
    async def artifacts(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> MavenArtifactPage:
        after: tuple[str, str] | None = None
        if cursor:
            group_id, artifact_id = decode_cursor(cursor, str, str)
            after = (group_id, artifact_id)
        rows = await maven_db.artifact_page(ctx.db, ctx.repo.id, after=after, q=q or None, limit=limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        latest = await maven_db.latest_versions(ctx.db, ctx.repo.id, rows)
        items = [
            MavenArtifact(
                group_id=r.group_id,
                artifact_id=r.artifact_id,
                latest=latest.get((r.group_id, r.artifact_id)),
                version_count=r.version_count,
                updated_at=r.updated_at,
            )
            for r in rows
        ]
        next_cursor = encode_cursor(rows[-1].group_id, rows[-1].artifact_id) if more and rows else None
        return MavenArtifactPage(items=items, next_cursor=next_cursor)

    async def versions(
        self, ctx: BrowseContext, *, group_id: str, artifact_id: str, cursor: str | None, limit: int
    ) -> MavenVersionPage:
        before = str(decode_cursor(cursor, str)[0]) if cursor else None
        docs = await maven_db.version_page(ctx.db, ctx.repo.id, group_id, artifact_id, before=before, limit=limit + 1)
        if not docs and before is None:
            raise NotFound(f'{group_id}:{artifact_id} is not in this repository')
        more = len(docs) > limit
        docs = docs[:limit]
        ga = f'{group_id.replace(".", "/")}/{artifact_id}'
        folders = {str(d['version']): f'{ga}/{d["version"]}/' for d in docs}
        stats = await maven_db.file_stats(ctx.db, ctx.repo.id, ga, folders)
        items = []
        for d in docs:
            version = str(d['version'])
            folder = folders[version]
            found = stats.get(version)
            items.append(
                MavenVersion(
                    version=version,
                    snapshot=is_snapshot(version),
                    path=folder.rstrip('/'),
                    file_count=found.count if found else 0,
                    size_bytes=found.size if found else 0,
                    created_at=d.get('created_at'),
                    updated_at=d.get('updated_at'),
                    uploaded_by=d.get('uploaded_by'),
                )
            )
        next_cursor = encode_cursor(str(docs[-1]['version_sort'])) if more and docs else None
        return MavenVersionPage(group_id=group_id, artifact_id=artifact_id, items=items, next_cursor=next_cursor)
