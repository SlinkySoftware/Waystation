"""Conda browse views (OpenAPI ``listCondaPackages`` / ``listCondaFiles``).

- **Packages**: ``conda_names`` (proxy: written when a cached repodata file is indexed; hosted: by the
  ``index.conda`` job), by name, with the newest version, the subdirs and the number of files listed.
- **Files**: one package's files (``conda_packages``) newest version first (conda VersionOrder), optionally of one
  subdir, each with its checksums, whether it was verified against a listing, and the stored copy (cached or
  uploaded) if any.
"""

from __future__ import annotations

from typing import Any

from waystation_common.conda import is_prerelease, valid_name, valid_subdir
from waystation_common.db import conda as conda_db
from waystation_common.errors import NotFound
from waystation_server.browse.models import (
    BrowseContext,
    CondaFile,
    CondaFilePage,
    CondaPackage,
    CondaPackagePage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.repos.model import RepoType


def _package(doc: dict[str, Any]) -> CondaPackage:
    counts = doc.get('counts') or {}
    return CondaPackage(
        name=str(doc['name']),
        latest=doc.get('latest'),
        subdirs=sorted(str(s) for s in doc.get('subdirs') or []),
        file_count=sum(int(v) for v in counts.values() if isinstance(v, int)),
        summary=doc.get('summary'),
        license=doc.get('license'),
    )


class CondaBrowse:
    @staticmethod
    def _no_group(ctx: BrowseContext) -> None:
        if ctx.repo.type is RepoType.GROUP:
            raise NotFound('conda groups are not supported')

    async def packages(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> CondaPackagePage:
        self._no_group(ctx)
        after = decode_cursor(cursor, str)[0] if cursor else None
        query = q.strip() if q and q.strip() else None
        docs = await conda_db.names_page(ctx.db, ctx.repo.id, after=after, q=query, limit=limit + 1)
        more = len(docs) > limit
        docs = docs[:limit]
        last = str(docs[-1]['name']) if docs else None
        return CondaPackagePage(
            items=[_package(d) for d in docs], next_cursor=encode_cursor(last) if more and last else None
        )

    async def files(
        self, ctx: BrowseContext, *, package: str, subdir: str | None, cursor: str | None, limit: int
    ) -> CondaFilePage:
        self._no_group(ctx)
        name = package.strip().lower()
        if not valid_name(name) or (subdir is not None and not valid_subdir(subdir)):
            raise NotFound('package not found')
        summary = await conda_db.get_name(ctx.db, ctx.repo.id, name)
        offset = decode_cursor(cursor, int)[0] if cursor else 0
        docs = await conda_db.files_page(ctx.db, ctx.repo.id, name, subdir=subdir, offset=offset, limit=limit + 1)
        if summary is None and not docs:
            raise NotFound('package not found')
        more = len(docs) > limit
        docs = docs[:limit]
        stored = await conda_db.held(ctx.db, ctx.repo.id, [f'{d["subdir"]}/{d["filename"]}' for d in docs])
        items = []
        for doc in docs:
            found = stored.get(f'{doc["subdir"]}/{doc["filename"]}')
            entry = doc.get('entry') or {}
            items.append(
                CondaFile(
                    filename=str(doc['filename']),
                    subdir=str(doc['subdir']),
                    version=str(doc['version']),
                    build=str(doc['build']),
                    build_number=int(doc.get('build_number') or 0),
                    prerelease=is_prerelease(str(doc['version'])),
                    sha256=doc.get('sha256'),
                    md5=doc.get('md5'),
                    size=doc.get('size'),
                    timestamp=doc.get('timestamp'),
                    verified=bool(doc.get('verified')),
                    depends=[str(d) for d in entry.get('depends') or []],
                    license=entry.get('license'),
                    cached=found is not None,
                    cached_at=(found or {}).get('created_at'),
                    downloads=int((found or {}).get('download_count') or 0),
                    last_downloaded_at=(found or {}).get('last_downloaded_at'),
                    uploaded_by=doc.get('uploaded_by'),
                )
            )
        head = (
            _package(summary) if summary is not None else CondaPackage(name=name, latest=None, subdirs=[], file_count=0)
        )
        return CondaFilePage(package=head, items=items, next_cursor=encode_cursor(offset + limit) if more else None)
