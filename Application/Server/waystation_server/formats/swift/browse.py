"""Swift Package Registry browse views (OpenAPI ``listSwiftScopes`` / ``listSwiftPackages`` /
``listSwiftReleases``): scope → package → releases.

They read ``swift_packages`` and ``swift_releases``: a hosted repository's published (and removed) releases, a
proxy's pinned releases (a release is pinned the first time a client asks for it, so the Git host's other tags are
not listed here). Releases are newest first by SemVer precedence.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from waystation_common.errors import NotFound
from waystation_common.galaxy import is_prerelease
from waystation_common.swift import PackageId, valid_name, valid_scope
from waystation_server.browse.models import (
    BrowseContext,
    SwiftManifest,
    SwiftPackage,
    SwiftPackagePage,
    SwiftRelease,
    SwiftReleasePage,
    SwiftScope,
    SwiftScopePage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import swift as swift_db


def package_of(doc: dict[str, Any]) -> SwiftPackage:
    return SwiftPackage(
        scope=str(doc['scope']),
        name=str(doc['name']),
        id=f'{doc["scope"]}.{doc["name"]}',
        latest_version=doc.get('latest'),
        release_count=int(doc.get('release_count') or 0),
        removed_count=int(doc.get('removed_count') or 0),
        description=doc.get('description'),
        repository_url=doc.get('repository_url'),
    )


def manifest_url(repo_url: str, pkg: PackageId, version: str, swift_version: str | None) -> str:
    """The registry URL a client fetches a manifest from (``?swift-version=`` for a version-specific one)."""
    url = f'{repo_url}{pkg.prefix}/{quote(version, safe="+")}/Package.swift'
    return f'{url}?swift-version={quote(swift_version)}' if swift_version else url


class SwiftBrowse:
    async def scopes(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> SwiftScopePage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        docs = await swift_db.scopes(ctx.db, ctx.repo.id, after=after, q=q, limit=limit + 1)
        items = [SwiftScope(scope=d['scope'], package_count=d['package_count']) for d in docs[:limit]]
        more = len(docs) > limit
        return SwiftScopePage(items=items, next_cursor=encode_cursor(docs[limit - 1]['key']) if more else None)

    async def packages(
        self, ctx: BrowseContext, *, scope: str | None, q: str | None, cursor: str | None, limit: int
    ) -> SwiftPackagePage:
        after: tuple[str, str] | None = None
        if cursor:
            scope_key, name_key = decode_cursor(cursor, str, str)
            after = (scope_key, name_key)
        docs = await swift_db.packages(
            ctx.db, ctx.repo.id, scope_key=scope.lower() if scope else None, after=after, q=q, limit=limit + 1
        )
        items = [package_of(d) for d in docs[:limit]]
        more = len(docs) > limit
        last = docs[limit - 1] if more else None
        return SwiftPackagePage(
            items=items,
            next_cursor=encode_cursor(last['scope_key'], last['name_key']) if last is not None else None,
        )

    async def releases(
        self, ctx: BrowseContext, *, scope: str, name: str, cursor: str | None, limit: int
    ) -> SwiftReleasePage:
        if not valid_scope(scope) or not valid_name(name):
            raise NotFound('package not found')
        pkg = PackageId(scope, name)
        found = await swift_db.packages(
            ctx.db, ctx.repo.id, scope_key=pkg.scope_key, name_key=pkg.name_key, after=None, q=None, limit=1
        )
        if not found:
            raise NotFound('package not found')
        before = decode_cursor(cursor, str)[0] if cursor else None
        docs = await swift_db.release_page(
            ctx.db, ctx.repo.id, pkg.scope_key, pkg.name_key, before=before, limit=limit + 1
        )
        more = len(docs) > limit
        docs = docs[:limit]
        repo_url = f'{ctx.base_url}/repository/{ctx.repo.name}/'
        items = []
        for doc in docs:
            signature = doc.get('signature') or {}
            items.append(
                SwiftRelease(
                    version=str(doc['version']),
                    prerelease=is_prerelease(str(doc['version'])),
                    state=doc.get('state') or swift_db.PUBLISHED,
                    source=doc.get('source') or 'hosted',
                    published_at=doc.get('published_at'),
                    published_by=doc.get('published_by'),
                    removed_at=doc.get('removed_at'),
                    removed_by=doc.get('removed_by'),
                    checksum=str(doc['checksum']),
                    size_bytes=doc.get('size'),
                    tools_version=doc.get('tools_version'),
                    manifests=[
                        SwiftManifest(
                            filename=str(m['filename']),
                            swift_version=m.get('swift_version'),
                            tools_version=m.get('tools_version'),
                            size_bytes=m.get('size'),
                            url=manifest_url(repo_url, pkg, str(doc['version']), m.get('swift_version')),
                        )
                        for m in doc.get('manifests') or []
                    ],
                    signed=bool(signature.get('base64')),
                    signer=signature.get('signer'),
                    tag=doc.get('tag'),
                    commit=doc.get('commit'),
                    path=str(doc['archive_path']),
                    download_url=repo_url + quote(str(doc['archive_path']), safe='/+'),
                )
            )
        return SwiftReleasePage(
            package=package_of(found[0]),
            items=items,
            next_cursor=encode_cursor(str(docs[-1]['version_sort'])) if more and docs else None,
        )
