"""apk browse views (OpenAPI ``listApkPackages`` / ``listApkVersions``): package names → their files.

Both read ``apk_packages``: a proxy's listings (every stanza of the cached ``APKINDEX.tar.gz`` files, across every
branch, repository and architecture below the upstream) and a hosted repository's uploads. Unverified proxy
fetches (no listing named them yet) have no name and show only in the folder tree. ``cached`` says whether the
package file is stored; files are newest version first (apk version order).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from waystation_common import apk
from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    ApkPackage,
    ApkPackagePage,
    ApkVersion,
    ApkVersionPage,
    BrowseContext,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import apk as apk_db
from waystation_server.db import assets as assets_db

MAX_NAME = 200
URL_SAFE = "/:@!$&'()*+,;=-._~"


def _package(doc: dict[str, Any]) -> ApkPackage:
    latest = doc['latest']
    return ApkPackage(
        name=str(doc['name']),
        latest_version=str(latest.get('version') or '') or None,
        arches=list(doc['arches']),
        version_count=int(doc['version_count']),
        description=latest.get('description') or None,
        origin=latest.get('origin') or None,
    )


class ApkBrowse:
    async def packages(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> ApkPackagePage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        docs = await apk_db.package_names(ctx.db, ctx.repo.id, after=after, q=q, limit=limit + 1)
        more = len(docs) > limit
        items = [_package(d) for d in docs[:limit]]
        return ApkPackagePage(items=items, next_cursor=encode_cursor(items[-1].name) if more and items else None)

    async def versions(self, ctx: BrowseContext, *, package: str, cursor: str | None, limit: int) -> ApkVersionPage:
        if not package or len(package) > MAX_NAME:
            raise NotFound('package not found')
        found = await apk_db.package_names(ctx.db, ctx.repo.id, after=None, q=None, limit=1, name_is=package)
        if not found:
            raise NotFound('package not found')
        after: tuple[str, str] | None = None
        if cursor:
            sort, path = decode_cursor(cursor, str, str)
            after = (sort, path)
        docs = await apk_db.package_versions(ctx.db, ctx.repo.id, package, after=after, limit=limit + 1)
        more = len(docs) > limit
        docs = docs[:limit]
        sizes = await assets_db.sizes(ctx.db, ctx.repo.id, [str(d['path']) for d in docs])
        repo_url = f'{ctx.base_url}/repository/{ctx.repo.name}/'
        items = []
        for doc in docs:
            path = str(doc['path'])
            stored = sizes.get(path)
            directory = apk.Directory(str(doc.get('dir') or path.rpartition('/')[0]))
            items.append(
                ApkVersion(
                    version=str(doc.get('version') or ''),
                    arch=str(doc.get('arch') or directory.arch),
                    branch=directory.branch,
                    repository=directory.repository,
                    directory=directory.path,
                    path=path,
                    size_bytes=stored if stored is not None else doc.get('size'),
                    installed_size=doc.get('installed_size'),
                    checksum=doc.get('checksum') or None,
                    verified=bool(doc.get('verified')),
                    cached=stored is not None,
                    license=doc.get('license'),
                    origin=doc.get('origin'),
                    url=doc.get('url'),
                    build_time=doc.get('build_time'),
                    depends=[str(d) for d in doc.get('depends') or []],
                    uploaded_at=doc.get('uploaded_at'),
                    uploaded_by=doc.get('uploaded_by'),
                    download_url=repo_url + quote(path, safe=URL_SAFE),
                )
            )
        last = docs[-1] if docs else None
        next_cursor = encode_cursor(str(last.get('version_sort') or ''), str(last['path'])) if more and last else None
        return ApkVersionPage(package=_package(found[0]), items=items, next_cursor=next_cursor)
