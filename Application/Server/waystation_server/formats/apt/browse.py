"""apt browse views (OpenAPI ``listAptSuites`` / ``listAptPackages``).

- **Suites**: one per cached ``dists/<suite>/InRelease`` (else ``Release``; a suite may be several segments deep,
  ``bookworm/mongodb-org/8.0``); flat repositories: one per directory
  holding a cached ``Release`` (the root is suite ``.``). Origin, codename, date, components and architectures come
  from the cached file itself; ``last_refreshed_at`` is when the upstream last confirmed it. ``package_count`` counts
  the entries of the suite's cached ``Packages`` indexes (``apt_packages``; a package listed for two architectures
  counts twice), ``cached_package_count`` those whose file is cached.
- **Packages**: the entries of the suite's cached ``Packages`` indexes, by name, filtered by index architecture
  (``binary-<arch>``; ``Architecture: all`` packages appear under every architecture that lists them), component,
  name substring and "cached only". ``verified`` compares the cached file's digest with the listing's SHA256.

Indexes cached before this view existed are listed once they are next refreshed.
"""

from __future__ import annotations

import contextlib
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Protocol

from waystation_common.errors import WaystationError
from waystation_server.browse.models import (
    AptPackage,
    AptPackagePage,
    AptSuite,
    BrowseContext,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import apt as apt_db
from waystation_server.db import assets as assets_db
from waystation_server.db import browse as browse_db
from waystation_server.formats.apt import suites
from waystation_server.formats.apt.descriptor import AptOptions

if TYPE_CHECKING:
    from waystation_server.formats.apt import deb822
    from waystation_server.proxy.engine import ProxyEngine, StoredAsset

FLAT_ROOT = '.'
_RELEASE = r'(InRelease|Release)$'


def browse_suite(suite: str) -> str:
    """The suite id browse uses: ``trixie`` for ``dists/trixie``, ``bookworm/mongodb-org/8.0`` for
    ``dists/bookworm/mongodb-org/8.0``; a flat repository's directory, ``.`` for its root."""
    return suite or FLAT_ROOT


class ReleaseParser(Protocol):
    async def parse_release(self, engine: ProxyEngine, asset: StoredAsset) -> deb822.Release: ...


def _date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except TypeError, ValueError:
        return None


class AptBrowse:
    def __init__(self, releases: ReleaseParser) -> None:
        self.releases = releases

    async def _release_paths(self, ctx: BrowseContext) -> dict[str, str]:
        """Suite id → path of its cached ``InRelease`` (preferred) or ``Release``."""
        flat = AptOptions.model_validate(ctx.repo.format_options).flat
        if flat:
            docs = await browse_db.matching(ctx.db, ctx.repo.id, '', f'(^|/){_RELEASE}', browse_db.PATH_ONLY)
        else:
            depth = f'{{1,{suites.MAX_SUITE_DEPTH}}}'
            docs = await browse_db.matching(
                ctx.db, ctx.repo.id, 'dists/', f'^dists/(?:[^/]+/){depth}{_RELEASE}', browse_db.PATH_ONLY
            )
        found: dict[str, str] = {}
        for doc in docs:
            path = str(doc['path'])
            if not flat and suites.release_suite_dir(path) is None:
                continue
            directory, _, name = path.rpartition('/')
            suite = browse_suite(directory if flat else directory.split('/', 1)[1])
            if name == 'InRelease' or suite not in found:
                found[suite] = path
        return found

    async def suites(self, ctx: BrowseContext) -> list[AptSuite]:
        paths = await self._release_paths(ctx)
        cached = await apt_db.cached_counts(ctx.db, ctx.repo.id) if paths else {}
        out: list[AptSuite] = []
        for suite, path in sorted(paths.items()):
            asset = await assets_db.get(ctx.db, ctx.repo.id, path)
            if asset is None:
                continue
            info: dict[str, str] = {}
            with contextlib.suppress(WaystationError):  # unreadable or invalid: list the suite without details
                info = (await self.releases.parse_release(ctx.proxy, asset)).info
            out.append(
                AptSuite(
                    suite=suite,
                    origin=info.get('origin'),
                    codename=info.get('codename'),
                    components=info.get('components', '').split(),
                    architectures=info.get('architectures', '').split(),
                    release_date=_date(info.get('date')),
                    last_refreshed_at=asset.upstream.fetched_at,
                    package_count=await apt_db.suite_count(ctx.db, ctx.repo.id, suite),
                    cached_package_count=cached.get(suite, 0),
                )
            )
        return out

    async def packages(
        self,
        ctx: BrowseContext,
        *,
        suite: str,
        component: str | None,
        arch: str | None,
        cached: bool | None,
        q: str | None,
        cursor: str | None,
        limit: int,
    ) -> AptPackagePage:
        after = decode_cursor(cursor, str, str, str, str) if cursor else None
        query = apt_db.cached_package_page if cached else apt_db.package_page
        docs = await query(
            ctx.db,
            ctx.repo.id,
            suite=suite,
            component=component,
            arch=arch,
            name_regex=re.escape(q) if q else None,
            after=after,
            limit=limit + 1,
        )
        more = len(docs) > limit
        docs = docs[:limit]
        files = await browse_db.by_paths(ctx.db, ctx.repo.id, (str(d['filename']) for d in docs))
        items = [self._package(doc, files.get(str(doc['filename']))) for doc in docs]
        next_cursor = None
        if more and docs:
            last = docs[-1]
            next_cursor = encode_cursor(*(str(last[k]) for k in apt_db.PACKAGE_SORT))
        return AptPackagePage(items=items, next_cursor=next_cursor)

    @staticmethod
    def _package(doc: dict[str, Any], asset: dict[str, Any] | None) -> AptPackage:
        return AptPackage(
            name=str(doc.get('name') or ''),
            version=str(doc.get('version') or ''),
            architecture=str(doc.get('architecture') or doc.get('arch') or ''),
            component=str(doc.get('component') or ''),
            section=doc.get('section'),
            size=doc.get('size'),
            filename=str(doc['filename']),
            sha256=doc.get('sha256'),
            cached=asset is not None,
            verified=None if asset is None else asset.get('blob_sha256') == doc.get('sha256'),
        )
