"""yum browse views (OpenAPI ``listYumPackages`` / ``listYumVersions``): package names → their files.

Both read ``yum_packages``: a proxy's listings (every ``<package>`` of the cached ``primary.xml`` files, across every
yum repository directory below the upstream: releases, BaseOS/AppStream, architectures) and a hosted repository's
uploads. Unverified proxy fetches (no listing named them yet) have no name and show only in the folder tree.
``cached`` says whether the package file is stored; files are newest EVR first (RPM version order, ``evr``).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    BrowseContext,
    YumPackage,
    YumPackagePage,
    YumVersion,
    YumVersionPage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import yum as yum_db
from waystation_server.formats.yum.evr import evr_string

MAX_NAME = 250
URL_SAFE = "/:@!$&'()*+,;=-._~"


def _package(doc: dict[str, Any]) -> YumPackage:
    latest = doc['latest']
    return YumPackage(
        name=str(doc['name']),
        latest_version=evr_string(
            latest.get('epoch'), str(latest.get('version') or ''), str(latest.get('release') or '')
        ),
        arches=list(doc['arches']),
        version_count=int(doc['version_count']),
        summary=latest.get('summary') or None,
    )


class YumBrowse:
    async def packages(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> YumPackagePage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        docs = await yum_db.package_names(ctx.db, ctx.repo.id, after=after, q=q, limit=limit + 1)
        more = len(docs) > limit
        items = [_package(d) for d in docs[:limit]]
        return YumPackagePage(items=items, next_cursor=encode_cursor(items[-1].name) if more and items else None)

    async def versions(self, ctx: BrowseContext, *, package: str, cursor: str | None, limit: int) -> YumVersionPage:
        if not package or len(package) > MAX_NAME:
            raise NotFound('package not found')
        found = await yum_db.package_names(ctx.db, ctx.repo.id, after=None, q=None, limit=1, name_is=package)
        if not found:
            raise NotFound('package not found')
        after: tuple[str, str] | None = None
        if cursor:
            sort, path = decode_cursor(cursor, str, str)
            after = (sort, path)
        docs = await yum_db.package_versions(ctx.db, ctx.repo.id, package, after=after, limit=limit + 1)
        more = len(docs) > limit
        docs = docs[:limit]
        sizes = await assets_db.sizes(ctx.db, ctx.repo.id, [str(d['path']) for d in docs])
        repo_url = f'{ctx.base_url}/repository/{ctx.repo.name}/'
        items = []
        for doc in docs:
            path = str(doc['path'])
            stored = sizes.get(path)
            epoch, version, release = (
                str(doc.get('epoch') or '0'),
                str(doc.get('version') or ''),
                str(doc.get('release') or ''),
            )
            items.append(
                YumVersion(
                    evr=evr_string(epoch, version, release),
                    epoch=epoch,
                    version=version,
                    release=release,
                    arch=str(doc.get('arch') or ''),
                    path=path,
                    directory=str(doc.get('repo_dir') or ''),
                    size_bytes=stored if stored is not None else doc.get('size'),
                    checksum=f'{doc["algorithm"]}:{doc["digest"]}' if doc.get('digest') else None,
                    cached=stored is not None,
                    uploaded_at=doc.get('uploaded_at'),
                    download_url=repo_url + quote(path, safe=URL_SAFE),
                )
            )
        last = docs[-1] if docs else None
        next_cursor = encode_cursor(str(last.get('version_sort') or ''), str(last['path'])) if more and last else None
        return YumVersionPage(package=_package(found[0]), items=items, next_cursor=next_cursor)
