"""npm browse views (OpenAPI ``listNpmPackages`` / ``listNpmVersions``).

- **Packages**: every package whose packument the repository cached, from ``npm_packages`` (a summary recorded in the
  background when a packument is stored: ``latest``, dist-tags, version count, and description/licence when the full
  packument was fetched; npm itself only fetches the abbreviated one). Cached tarball counts and bytes come from the
  ``assets`` under ``<package>/-/``.
- **Versions**: parsed from the cached packument (the full one when cached: it has publish times; else the abbreviated
  one, whose versions get the packument's ``modified`` time), newest first by semver precedence. ``cached`` = the
  version's tarball is in the cache. Parsed packuments are kept in a small in-process cache keyed by blob digest.

Parsing runs in a thread and is bounded by the engine's metadata limit (ARCHITECTURE §9).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from waystation_common.errors import NotFound, ValidationFailed
from waystation_server.browse.models import (
    BrowseContext,
    NpmPackage,
    NpmPackagePage,
    NpmVersion,
    NpmVersionPage,
    decode_cursor,
    encode_cursor,
)
from waystation_server.db import assets as assets_db
from waystation_server.db import browse as browse_db
from waystation_server.db import npm as npm_db
from waystation_server.formats.npm.metadata import packument_key, tarball_filename, valid_name

PARSED_CACHE = 16
MAX_TEXT = 1024
MAX_TAGS = 100
MAX_SHORT = 256
_SEMVER = re.compile(r'^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$')


# ---------------------------------------------------------------------------- pure helpers (run in a thread)
def version_key(version: str) -> tuple[Any, ...]:
    """Semver precedence (prereleases before their release, numeric identifiers numerically); anything that is not
    semver sorts before every valid version, by string."""
    match = _SEMVER.match(version)
    if match is None:
        return (0, 0, 0, 0, 0, (), version)
    major, minor, patch, pre = match.groups()
    identifiers = tuple((0, int(p), '') if p.isdigit() else (1, 0, p) for p in pre.split('.')) if pre else ()
    return (1, int(major), int(minor), int(patch), 0 if pre else 1, identifiers, version)


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _short(value: Any, limit: int = MAX_SHORT) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _license(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get('type')
    return _short(value)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class VersionRow:
    version: str
    filename: str | None
    published: datetime | None
    unpacked_size: int | None
    deprecated: str | None
    integrity: str | None


@dataclass(frozen=True)
class Parsed:
    name: str
    latest: str
    dist_tags: dict[str, str]
    description: str | None
    license: str | None
    modified: datetime | None
    rows: list[VersionRow] = field(default_factory=list)  # newest first

    def summary(self) -> dict[str, Any]:
        """The ``npm_packages`` fields this packument provides (description/licence only when present)."""
        fields: dict[str, Any] = {
            'latest': self.latest,
            'dist_tags': self.dist_tags,
            'version_count': len(self.rows),
            'modified': self.modified,
        }
        if self.description is not None:
            fields['description'] = self.description
        if self.license is not None:
            fields['license'] = self.license
        return fields


def parse_packument(data: bytes, package: str) -> Parsed:
    """The browse view of a (cached or upstream) packument. Raises ``ValidationFailed`` when it is not one."""
    try:
        doc = json.loads(data)
    except ValueError:
        raise ValidationFailed('packument is not valid JSON') from None
    if not isinstance(doc, dict):
        raise ValidationFailed('packument is not a JSON object')
    versions = _obj(doc.get('versions'))
    times = _obj(doc.get('time'))
    tags_raw = _obj(doc.get('dist-tags'))
    dist_tags = {str(k)[:MAX_SHORT]: v[:MAX_SHORT] for k, v in list(tags_raw.items())[:MAX_TAGS] if isinstance(v, str)}
    modified = _time(doc.get('modified')) or _time(times.get('modified'))
    rows: list[VersionRow] = []
    for version, meta in versions.items():
        if not isinstance(version, str) or not isinstance(meta, dict):
            continue
        dist = _obj(meta.get('dist'))
        tarball = dist.get('tarball')
        unpacked = dist.get('unpackedSize')
        deprecated = meta.get('deprecated')
        rows.append(
            VersionRow(
                version=version[:MAX_SHORT],
                filename=tarball_filename(tarball) if isinstance(tarball, str) else None,
                published=_time(times.get(version)),
                unpacked_size=unpacked if isinstance(unpacked, int) and not isinstance(unpacked, bool) else None,
                deprecated=_short(deprecated, MAX_TEXT),
                integrity=_short(dist.get('integrity'), MAX_TEXT),
            )
        )
    rows.sort(key=lambda r: version_key(r.version), reverse=True)
    latest = dist_tags.get('latest') or (rows[0].version if rows else '')
    licence = _license(doc.get('license'))
    if licence is None:
        latest_meta = versions.get(latest)
        licence = _license(latest_meta.get('license')) if isinstance(latest_meta, dict) else None
    return Parsed(
        name=package,
        latest=latest,
        dist_tags=dist_tags,
        description=_short(doc.get('description'), MAX_TEXT),
        license=licence,
        modified=modified,
        rows=rows,
    )


# ---------------------------------------------------------------------------- views
class NpmBrowse:
    def __init__(self) -> None:
        self._parsed: OrderedDict[str, Parsed] = OrderedDict()

    async def packages(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> NpmPackagePage:
        after = decode_cursor(cursor, str)[0] if cursor else None
        docs = await npm_db.page(ctx.db, ctx.repo.id, after=after, q=q, limit=limit + 1)
        more = len(docs) > limit
        docs = docs[:limit]
        stats = await npm_db.tarball_stats(ctx.db, ctx.repo.id, [str(d['name']) for d in docs])
        items = []
        for doc in docs:
            name = str(doc['name'])
            found = stats.get(name, npm_db.TarballStats(0, 0))
            items.append(
                NpmPackage(
                    name=name,
                    description=doc.get('description'),
                    latest=str(doc.get('latest') or ''),
                    version_count=int(doc.get('version_count') or 0),
                    cached_versions=found.count,
                    size_bytes=found.size,
                    license=doc.get('license'),
                )
            )
        return NpmPackagePage(items=items, next_cursor=encode_cursor(items[-1].name) if more and items else None)

    async def versions(self, ctx: BrowseContext, *, package: str, cursor: str | None, limit: int) -> NpmVersionPage:
        if not valid_name(package):
            raise NotFound('package not found')
        offset = decode_cursor(cursor, int)[0] if cursor else 0
        parsed, fetched_at = await self._packument(ctx, package)
        files = await browse_db.in_folder(ctx.db, ctx.repo.id, f'{package}/-/')
        tarballs = {str(a['path']).rsplit('/', 1)[-1]: int(a['size']) for a in files if a['path'].endswith('.tgz')}
        tags_of: dict[str, list[str]] = {}
        for tag, version in parsed.dist_tags.items():
            tags_of.setdefault(version, []).append(tag)
        rows = parsed.rows[offset : offset + limit]
        items = [
            NpmVersion(
                version=row.version,
                published_at=row.published or parsed.modified or fetched_at,
                size_bytes=tarballs.get(row.filename) if row.filename else None,
                unpacked_size=row.unpacked_size,
                cached=row.filename is not None and row.filename in tarballs,
                deprecated=row.deprecated,
                integrity=row.integrity,
                dist_tags=sorted(tags_of.get(row.version, [])),
            )
            for row in rows
        ]
        summary = NpmPackage(
            name=package,
            description=parsed.description,
            latest=parsed.latest,
            version_count=len(parsed.rows),
            cached_versions=len(tarballs),
            size_bytes=sum(tarballs.values()),
            license=parsed.license,
        )
        if parsed.description is None or parsed.license is None:
            stored = await npm_db.get(ctx.db, ctx.repo.id, package)
            if stored is not None:
                summary.description = summary.description or stored.get('description')
                summary.license = summary.license or stored.get('license')
        more = offset + limit < len(parsed.rows)
        return NpmVersionPage(
            package=summary,
            dist_tags=parsed.dist_tags,
            items=items,
            next_cursor=encode_cursor(offset + limit) if more else None,
        )

    async def _packument(self, ctx: BrowseContext, package: str) -> tuple[Parsed, datetime]:
        """The cached packument (full preferred) parsed; ``NotFound`` when none is cached."""
        for abbreviated in (False, True):
            asset = await assets_db.get(ctx.db, ctx.repo.id, packument_key(package, abbreviated=abbreviated))
            if asset is None:
                continue
            parsed = self._parsed.get(asset.blob_sha256)
            if parsed is None:
                data = await ctx.proxy.read(asset)
                try:
                    parsed = await asyncio.to_thread(parse_packument, data, package)
                except ValidationFailed:
                    continue
                self._parsed[asset.blob_sha256] = parsed
                while len(self._parsed) > PARSED_CACHE:
                    self._parsed.popitem(last=False)
            else:
                self._parsed.move_to_end(asset.blob_sha256)
            return parsed, asset.upstream.fetched_at
        raise NotFound('package not found')
