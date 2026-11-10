"""Ansible Galaxy browse views (OpenAPI ``listGalaxyNamespaces`` / ``listGalaxyCollections`` /
``listGalaxyVersions``): namespace → collection → versions.

All read ``galaxy_collections``: a hosted repository's imported versions, a proxy's cached version lists (recorded
in the background when a list is cached; checksums once a version's details were cached), and for a group its
members' records (a version several members have is shown once, from the first member). ``cached`` says whether the
artifact is stored; versions are newest first by SemVer precedence.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from waystation_common import galaxy
from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    BrowseContext,
    GalaxyCollection,
    GalaxyCollectionPage,
    GalaxyNamespace,
    GalaxyNamespacePage,
    GalaxyVersion,
    GalaxyVersionPage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import galaxy as galaxy_db
from waystation_server.db import repositories as repos_db
from waystation_server.repos.model import RepoType


async def repo_ids(ctx: BrowseContext) -> list[str]:
    """The repositories whose records a view shows: the repository, or a group's Galaxy members in order."""
    repo = ctx.repo
    if repo.type is not RepoType.GROUP:
        return [repo.id]
    out: list[str] = []
    for name in repo.group.members if repo.group is not None else ():
        member = await repos_db.get_by_name_ci(ctx.db, name)
        if member is not None and member.format == 'galaxy' and member.type is not RepoType.GROUP:
            out.append(member.id)
    return out


def _collection(doc: dict[str, Any]) -> GalaxyCollection:
    latest = doc['latest']
    return GalaxyCollection(
        namespace=str(doc['namespace']),
        name=str(doc['name']),
        latest_version=str(latest.get('version') or ''),
        description=latest.get('description'),
        requires_ansible=latest.get('requires_ansible'),
        version_count=int(doc['version_count']),
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


def _dependencies(doc: dict[str, Any]) -> dict[str, str]:
    deps = (doc.get('metadata') or {}).get('dependencies')
    if not isinstance(deps, dict):
        return {}
    return {str(k): str(v) for k, v in deps.items()}


class GalaxyBrowse:
    async def namespaces(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> GalaxyNamespacePage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        ids = await repo_ids(ctx)
        docs = await galaxy_db.namespaces(ctx.db, ids, after=after, q=q, limit=limit + 1) if ids else []
        items = [GalaxyNamespace(**d) for d in docs[:limit]]
        more = len(docs) > limit
        return GalaxyNamespacePage(
            items=items, next_cursor=encode_cursor(items[-1].namespace) if more and items else None
        )

    async def collections(
        self, ctx: BrowseContext, *, namespace: str | None, q: str | None, cursor: str | None, limit: int
    ) -> GalaxyCollectionPage:
        after: tuple[str, str] | None = None
        if cursor:
            after_ns, after_name = decode_cursor(cursor, str, str)
            after = (after_ns, after_name)
        ids = await repo_ids(ctx)
        docs = (
            await galaxy_db.collections(ctx.db, ids, namespace=namespace, after=after, q=q, limit=limit + 1)
            if ids
            else []
        )
        items = [_collection(d) for d in docs[:limit]]
        more = len(docs) > limit
        last = items[-1] if more and items else None
        return GalaxyCollectionPage(
            items=items, next_cursor=encode_cursor(last.namespace, last.name) if last is not None else None
        )

    async def versions(
        self, ctx: BrowseContext, *, namespace: str, name: str, cursor: str | None, limit: int
    ) -> GalaxyVersionPage:
        if not galaxy.valid_name(namespace, strict=False) or not galaxy.valid_name(name, strict=False):
            raise NotFound('collection not found')
        ids = await repo_ids(ctx)
        found = (
            await galaxy_db.collections(
                ctx.db, ids, namespace=namespace, after=None, q=None, limit=1, name_is=name
            )
            if ids
            else []
        )
        if not found:
            raise NotFound('collection not found')
        before = decode_cursor(cursor, str)[0] if cursor else None
        docs = await galaxy_db.versions(ctx.db, ids, namespace, name, before=before, limit=limit + 1)
        more = len(docs) > limit
        docs = docs[:limit]
        sizes = await _stored_sizes(ctx, docs)
        repo_url = f'{ctx.base_url}/repository/{ctx.repo.name}/'
        items = []
        for doc in docs:
            stored = sizes.get((str(doc['repo_id']), str(doc['path'])))
            items.append(
                GalaxyVersion(
                    version=str(doc['version']),
                    prerelease=galaxy.is_prerelease(str(doc['version'])),
                    created_at=doc.get('created'),
                    requires_ansible=doc.get('requires_ansible'),
                    sha256=doc.get('sha256'),
                    size_bytes=stored if stored is not None else doc.get('size'),
                    cached=stored is not None,
                    dependencies=_dependencies(doc),
                    uploaded_by=doc.get('uploaded_by'),
                    path=str(doc['path']),
                    download_url=repo_url + quote(str(doc['path']), safe='/+'),
                )
            )
        return GalaxyVersionPage(
            collection=_collection(found[0]),
            items=items,
            next_cursor=encode_cursor(str(docs[-1]['version_sort'])) if more and docs else None,
        )

