"""Composer v2 plugin (T-P6-COMPOSER; FORMATS.md "Composer"): proxy, hosted and group repositories.

Routes: ``formats.composer.protocol``. ``packages.json`` is rendered per request for every repository type.

**Proxy** (the upstream URL is a Composer 2 repository, e.g. ``https://repo.packagist.org``):

- The upstream's ``packages.json`` is cached as metadata (``_upstream/packages.json``, reduced to its
  ``metadata-url``, which must be on the upstream's host, and its ``available-packages`` / patterns, passed on).
- ``p2`` documents (tagged and ``~dev``) are cached as metadata (revalidated after the metadata max age), validated
  (bounded, minified or not, entries without a valid version skipped, only the published package keys kept) and
  rewritten: archives (``zip``/``tar``) on an allowed host (``dist_hosts`` and the upstream's host) point at
  ``dists/<name>/<version>/<reference>.<type>`` of this repository; ``source`` entries are kept or stripped
  (``source_mode``). The result is stored minified and served by X-Accel.
- Archives are fetched (a GitHub API zipball straight from codeload.github.com) and cached as content: checked to be
  a readable archive that is safe to extract, verified against the entry's ``shasum`` (SHA-1) when it has one and
  against the SHA-256 pinned when the archive was first cached (``composer_packages.pins``; Packagist's ``shasum``
  is usually empty), so an archive refetched after cleanup must be the same bytes.
- A package summary (``composer_packages``: versions, newest, description) is recorded in the background when a
  tagged ``p2`` document is cached (browse, global search).

**Hosted**: ``formats.composer.hosted``. **Group**: ``formats.composer.group``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit

from starlette.responses import Response

from waystation_common import composer
from waystation_common.errors import DigestMismatch, NotFound, ValidationFailed, WaystationError
from waystation_common.events import CacheState
from waystation_server.db import composer as composer_db
from waystation_server.formats.base import FormatDescriptor, FormatRequest, WarmContext
from waystation_server.formats.common.background import Background
from waystation_server.formats.composer import archive, prefetch, protocol
from waystation_server.formats.composer.browse import ComposerBrowse
from waystation_server.formats.composer.common import SAFE_HEADERS, composer_error, generated
from waystation_server.formats.composer.descriptor import DESCRIPTOR, ComposerOptions
from waystation_server.formats.composer.group import ComposerGroup
from waystation_server.formats.composer.hosted import ComposerHosted
from waystation_server.formats.composer.protocol import Op, Route, Upstream
from waystation_server.proxy.engine import (
    AssetKind,
    AssetSpec,
    CheckedExpected,
    OnStored,
    ProxyEngine,
    StoredAsset,
    UpstreamInfo,
)
from waystation_server.repos.model import RepoType

log = logging.getLogger('waystation.formats.composer')

KEPT = 64
ROOT_MAX_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE = 256 * 1024 * 1024
FOREVER_S = 10 * 365 * 24 * 3600  # archives are pinned: a cached one never needs refetching
SUMMARY_VERSIONS = 2000


class ComposerPlugin:
    def __init__(self, descriptor: FormatDescriptor = DESCRIPTOR) -> None:
        self.descriptor = descriptor
        self.browser = ComposerBrowse()
        self.hosted = ComposerHosted()
        self.group = ComposerGroup(self)
        self._indexing = Background('composer-packages')
        self._upstreams: OrderedDict[str, Upstream] = OrderedDict()
        self._entries: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        await self._indexing.drain()

    async def warm(self, ctx: WarmContext, item: str) -> None:
        """Prefetch (T-O12): ``formats.composer.prefetch``."""
        await prefetch.warm(ctx, item)

    async def handle(self, req: FormatRequest) -> Response:
        try:
            return await self._handle(req)
        except WaystationError as exc:
            headers = getattr(exc, 'headers', None)
            return composer_error(exc.status, exc.detail, headers=dict(headers) if headers else None)

    async def _handle(self, req: FormatRequest) -> Response:
        route = protocol.parse_route(req.path)
        if route is None:
            raise NotFound('Not Found')
        req.log(op=route.op.value)
        if route.op is Op.UPLOAD:
            if req.repo.type is RepoType.HOSTED:
                return await self.hosted.handle_write(req, route)
            return composer_error(405, 'upload packages to a hosted repository', headers={'Allow': 'GET, HEAD'})
        if req.method not in ('GET', 'HEAD'):
            return composer_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD'})
        if req.repo.type is RepoType.GROUP:
            return await self.group.serve(req, route)
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.serve(req, route)
        return await self.proxy_serve(req, route)

    # ------------------------------------------------------------------ proxy
    @staticmethod
    def options(req: FormatRequest) -> ComposerOptions:
        return ComposerOptions.model_validate(req.repo.format_options or {})

    def hosts(self, req: FormatRequest) -> frozenset[str]:
        """Hosts archives may be fetched from: the ``dist_hosts`` option and the upstream's host."""
        upstream = urlsplit(req.repo.proxy.upstream_url).hostname if req.repo.proxy else None
        return frozenset({*self.options(req).dist_hosts, *([upstream.lower()] if upstream else [])})

    async def proxy_serve(self, req: FormatRequest, route: Route) -> Response:
        if req.repo.proxy is None:
            raise NotFound('Not Found')
        upstream = await self.upstream(req)
        match route.op:
            case Op.ROOT:
                started = time.perf_counter()

                def account(status: int, sent: int) -> None:
                    req.hosted.account(req, CacheState.HIT, status, sent, started)

                body = protocol.render_root(req.repo_url, available=upstream.available, patterns=upstream.patterns)
                return generated(req, body, account=account)
            case Op.P2:
                if not upstream.lists(route.name):
                    raise NotFound('Not Found')
                spec = self.p2_spec(req, upstream, route.name, dev=route.branches)
            case Op.DIST:
                spec = await self.dist_spec(req, upstream, route)
            case _:
                raise NotFound('Not Found')
        response = await req.proxy.serve(req, spec)
        response.headers.update(SAFE_HEADERS)
        return response

    def upstream_spec(self, req: FormatRequest) -> AssetSpec:
        if req.repo.proxy is None:
            raise NotFound('Not Found')

        def transform(data: bytes, info: UpstreamInfo) -> bytes:
            return protocol.dump(protocol.parse_upstream_root(data, info.url).json())

        return AssetSpec(
            path=protocol.UPSTREAM_KEY,
            kind=AssetKind.METADATA,
            upstream_url=req.repo.proxy.upstream_url.rstrip('/') + '/' + protocol.ROOT,
            upstream_headers={'Accept': protocol.JSON_TYPE},
            content_type=protocol.JSON_TYPE,
            transform=transform,
            max_size=ROOT_MAX_BYTES,
            artifact='packages.json',
        )

    async def upstream(self, req: FormatRequest) -> Upstream:
        """The proxy's upstream (cached ``packages.json``; parsed copies kept per process by blob digest)."""
        asset = await req.proxy.ensure(req, self.upstream_spec(req))
        found = self._upstreams.get(asset.blob_sha256)
        if found is None:
            data = await req.proxy.read(asset, max_bytes=ROOT_MAX_BYTES)
            found = protocol.upstream_from_json(protocol.load(data, 'the cached packages.json'))
            self._upstreams[asset.blob_sha256] = found
            while len(self._upstreams) > KEPT:
                self._upstreams.popitem(last=False)
        else:
            self._upstreams.move_to_end(asset.blob_sha256)
        return found

    def p2_spec(
        self, req: FormatRequest, upstream: Upstream, name: str, *, dev: bool, max_age_s: int | None = None
    ) -> AssetSpec:
        repo_url = req.repo_url
        hosts = self.hosts(req)
        strip = self.options(req).source_mode == 'strip'
        found: dict[str, list[dict[str, Any]]] = {}

        def transform(data: bytes, info: UpstreamInfo) -> bytes:
            entries = protocol.parse_p2(data, name)
            rewritten = protocol.rewrite_entries(entries, repo_url, hosts, strip_source=strip)
            found['entries'] = rewritten
            return protocol.render_p2(name, rewritten)

        return AssetSpec(
            path=protocol.p2_path(name, dev=dev),
            kind=AssetKind.METADATA,
            upstream_url=upstream.p2_url(name, dev=dev),
            upstream_headers={'Accept': protocol.JSON_TYPE},
            content_type=protocol.JSON_TYPE,
            transform=transform,
            max_size=protocol.MAX_DOCUMENT,
            max_age_s=max_age_s,
            on_stored=None if dev else self._on_p2_stored(req.proxy, req.repo.id, name, found),
            artifact=f'{name}{"~dev" if dev else ""} metadata',
            component=name,
        )

    def _on_p2_stored(
        self, engine: ProxyEngine, repo_id: str, name: str, found: dict[str, list[dict[str, Any]]]
    ) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            entries = found.get('entries')
            if entries is None:
                return
            fields = summary_fields(entries)

            async def index() -> None:
                await composer_db.upsert_package(engine.db, repo_id, name, fields)

            self._indexing.spawn((repo_id, name, asset.blob_sha256), index)

        return stored

    async def stored_entries(self, req: FormatRequest, name: str, *, dev: bool) -> list[dict[str, Any]]:
        """A proxy's cached (rewritten) entries of ``name`` (parsed copies kept per process by blob digest)."""
        upstream = await self.upstream(req)
        if not upstream.lists(name):
            raise NotFound('Not Found')
        asset = await req.proxy.ensure(req, self.p2_spec(req, upstream, name, dev=dev))
        found = self._entries.get(asset.blob_sha256)
        if found is None:
            data = await req.proxy.read(asset, max_bytes=protocol.MAX_DOCUMENT)
            found = await asyncio.to_thread(protocol.parse_p2, data, name)
            self._entries[asset.blob_sha256] = found
            while len(self._entries) > KEPT:
                self._entries.popitem(last=False)
        else:
            self._entries.move_to_end(asset.blob_sha256)
        return found

    async def dist_spec(self, req: FormatRequest, upstream: Upstream, route: Route) -> AssetSpec:
        entries = await self.stored_entries(req, route.name, dev=composer.is_dev(route.version))
        target = protocol.find_dist(entries, route.version, route.file)
        hosts = self.hosts(req)
        if target is None or (urlsplit(target.upstream_url).hostname or '').lower() not in hosts:
            raise NotFound(f'not found: {route.name} has no archive {route.version}/{route.file}')
        db, repo_id, path = req.proxy.db, req.repo.id, route.key

        def check(file: Any) -> None:
            try:
                archive.check_archive(file, target.kind)
            except ValidationFailed as exc:
                raise DigestMismatch(f'upstream archive refused: {exc.detail}') from None

        async def expect() -> CheckedExpected:
            # The digest is pinned at the first fetch: a later fetch (after cleanup) must bring the same bytes.
            pinned = await composer_db.pinned_sha256(db, repo_id, route.name, path)
            return CheckedExpected(sha256=pinned, sha1=target.shasum, check=check)

        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            await composer_db.pin(db, repo_id, route.name, path, asset.blob_sha256)

        return AssetSpec(
            path=path,
            kind=AssetKind.CONTENT,
            upstream_url=protocol.fetch_url(target.upstream_url, hosts),
            expect=expect,
            content_type=protocol.ARCHIVE_TYPES[target.kind],
            max_size=MAX_ARCHIVE,
            max_age_s=FOREVER_S,
            on_stored=stored,
            head_passthrough=False,
            artifact=f'{route.name} {route.version}',
            component=route.name,
        )


def summary_fields(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Browse fields of a package from its (tagged) entries: versions newest first, the newest stable version and
    the description and type of the newest version."""
    ordered = protocol.newest_first(entries)
    latest = composer.newest(str(e['version_normalized']) for e in ordered)
    newest = next((e for e in ordered if e['version_normalized'] == latest), ordered[0] if ordered else {})
    versions = []
    for entry in ordered[:SUMMARY_VERSIONS]:
        dist = entry.get('dist')
        dist = dist if isinstance(dist, dict) else {}
        reference = dist.get('reference')
        normalized = str(entry['version_normalized'])
        served = protocol.UPSTREAM_URL_KEY in dist and protocol.archive_type(dist) is not None
        versions.append(
            {
                'version': str(entry.get('version'))[:256],
                'normalized': normalized,
                'reference': reference[:128] if isinstance(reference, str) else None,
                'time': str(entry['time'])[:64] if entry.get('time') else None,
                'path': protocol.dist_path(
                    str(entry['name']), composer.path_segment(normalized), protocol.dist_file(dist)
                )
                if served
                else None,
            }
        )
    description = newest.get('description')
    kind = newest.get('type')
    return {
        'versions': versions,
        'version_count': len(ordered),
        'latest': str(newest.get('version')) if newest else None,
        'description': description[:1024] if isinstance(description, str) else None,
        'type': kind[:100] if isinstance(kind, str) else None,
    }
