"""RubyGems plugin (T-P6-GEMS; FORMATS.md "RubyGems"): proxy, hosted and group repositories.

Routes: ``formats.rubygems.protocol``. The retired dependency API (``api/v1/dependencies``) answers 404 everywhere,
so Bundler and ``gem`` use the compact index.

**Proxy** (the upstream URL is a RubyGems server with the compact index, e.g. ``https://rubygems.org``):

- ``versions``, ``names`` and ``info/<gem>`` are cached as metadata (revalidated after the metadata max age), checked
  to be compact index files and served as they are by X-Accel, with ``Repr-Digest`` of the whole file: nginx answers
  Bundler's ``Range`` requests on ``versions`` (it appends the tail and checks the digest; a full ``200`` when the
  copy was just fetched). Bundler's ``If-None-Match`` is answered by the server (``304``).
- ``.gem`` files are cached as content (kept forever) after their SHA-256 matched the ``checksum`` of the version's
  line in the cached info file (refreshed once when the version is not listed yet); a version without a checksum is
  refused. A file name ``<name>-<version>[-<platform>]`` is read the ways it can be (a name may hold ``-<digit>``)
  and the info file decides.
- ``quick/Marshal.4.8/*.gemspec.rz`` (kept forever) and the legacy ``specs.4.8.gz`` / ``latest_specs.4.8.gz`` /
  ``prerelease_specs.4.8.gz`` (metadata) are opaque bytes: only checked to be zlib / gzip (bounded), never unmarshalled.
- A gem summary (``rubygems_gems``: versions with platform, checksum and dependencies) is written in the background
  when an info file is cached (browse, global search).

**Hosted**: ``formats.rubygems.hosted``. **Group**: ``formats.rubygems.group``.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import zlib
from collections import OrderedDict
from typing import Any

from starlette.responses import Response

from waystation_common import rubygems
from waystation_common.errors import NotFound, UpstreamError, ValidationFailed, WaystationError
from waystation_server.db import rubygems as gems_db
from waystation_server.formats.base import FormatDescriptor, FormatRequest, WarmContext
from waystation_server.formats.common.background import Background
from waystation_server.formats.rubygems import prefetch, protocol
from waystation_server.formats.rubygems.browse import RubygemsBrowse
from waystation_server.formats.rubygems.common import add_digest, gem_error
from waystation_server.formats.rubygems.descriptor import DESCRIPTOR
from waystation_server.formats.rubygems.group import RubygemsGroup
from waystation_server.formats.rubygems.hosted import RubygemsHosted
from waystation_server.formats.rubygems.protocol import InfoLine, Op, Route
from waystation_server.proxy.engine import (
    AssetKind,
    AssetSpec,
    Expected,
    OnStored,
    ProxyEngine,
    StoredAsset,
    UpstreamInfo,
)
from waystation_server.repos.model import RepoType

KEPT = 256
MAX_GEM = 512 * 1024 * 1024
FOREVER_S = 10 * 365 * 24 * 3600  # verified gems and their gemspecs never change
SUMMARY_VERSIONS = 2000
MAX_SPEC_INFLATED = 64 * 1024 * 1024
READINGS = 3


def check_gemspec(data: bytes, info: UpstreamInfo) -> None:
    """A ``.gemspec.rz`` is zlib data that inflates (bounded) to something starting with the Marshal 4.8 header."""
    inflater = zlib.decompressobj()
    try:
        head = inflater.decompress(data, MAX_SPEC_INFLATED)
    except zlib.error:
        raise UpstreamError('the upstream gemspec is not zlib data') from None
    if inflater.unconsumed_tail or not head.startswith(b'\x04\x08'):
        raise UpstreamError('the upstream gemspec is not a marshalled specification')


def check_specs(data: bytes, info: UpstreamInfo) -> None:
    """A legacy specs index is gzip (only the stream is checked: the content stays opaque)."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            while gz.read(1024 * 1024):
                pass
    except OSError, EOFError, zlib.error:
        raise UpstreamError('the upstream specs index is not gzip') from None


def summary_fields(lines: list[InfoLine], name: str) -> dict[str, Any]:
    """Browse fields of a gem from its info lines: versions newest first, the newest release."""
    ordered = sorted(lines, key=lambda line: rubygems.sort_string(line.version), reverse=True)
    versions = [
        {
            'version': line.version,
            'platform': line.platform,
            'sha256': line.checksum,
            'dependencies': [[dep, req.replace('&', ', ')] for dep, req in line.dependencies[:100]],
            'ruby': line.ruby.replace('&', ', ') if line.ruby else None,
            'path': protocol.gem_path(rubygems.full_name(name, line.version, line.platform)),
        }
        for line in ordered[:SUMMARY_VERSIONS]
    ]
    return {
        'versions': versions,
        'version_count': len({line.version for line in lines}),
        'latest': rubygems.newest(line.version for line in lines),
    }


class RubygemsPlugin:
    def __init__(self, descriptor: FormatDescriptor = DESCRIPTOR) -> None:
        self.descriptor = descriptor
        self.browser = RubygemsBrowse()
        self.hosted = RubygemsHosted()
        self.group = RubygemsGroup(self)
        self._indexing = Background('rubygems-gems')
        self._infos: OrderedDict[str, list[InfoLine]] = OrderedDict()

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        await self._indexing.drain()

    async def warm(self, ctx: WarmContext, item: str) -> None:
        """Prefetch (T-O12): ``formats.rubygems.prefetch``."""
        await prefetch.warm(ctx, item)

    async def handle(self, req: FormatRequest) -> Response:
        try:
            return await self._handle(req)
        except WaystationError as exc:
            headers = getattr(exc, 'headers', None)
            return gem_error(exc.status, exc.detail, headers=dict(headers) if headers else None)

    async def _handle(self, req: FormatRequest) -> Response:
        route = protocol.parse_route(req.path)
        if route is None:
            raise NotFound('Not Found')
        req.log(op=route.op.value)
        if route.op is Op.DEPENDENCIES:
            return gem_error(404, 'the dependency API is not available: use the compact index')
        if route.op in (Op.PUSH, Op.YANK):
            if req.repo.type is RepoType.HOSTED:
                return await self.hosted.handle_write(req, route)
            return gem_error(405, 'push gems to a hosted repository', headers={'Allow': 'GET, HEAD'})
        if req.method not in ('GET', 'HEAD'):
            return gem_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD'})
        if req.repo.type is RepoType.GROUP:
            return await self.group.serve(req, route)
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.serve(req, route)
        return await self.proxy_serve(req, route)

    # ------------------------------------------------------------------ proxy
    async def proxy_serve(self, req: FormatRequest, route: Route) -> Response:
        if req.repo.proxy is None:
            raise NotFound('Not Found')
        match route.op:
            case Op.VERSIONS:
                spec = self.versions_spec()
            case Op.NAMES:
                spec = AssetSpec(
                    path=protocol.NAMES,
                    kind=AssetKind.METADATA,
                    content_type=protocol.TEXT_TYPE,
                    validate=lambda data, info: protocol.check_index_text(data, 'names file'),
                    max_size=protocol.MAX_NAMES,
                    artifact='names',
                )
            case Op.INFO:
                spec = self.info_spec(req, route.name)
            case Op.SPECS:
                spec = AssetSpec(
                    path=route.key,
                    kind=AssetKind.METADATA,
                    content_type=protocol.GZIP_TYPE,
                    validate=check_specs,
                    max_size=protocol.MAX_SPECS,
                    artifact=route.key,
                )
            case Op.GEMSPEC:
                readings = rubygems.split_full_name(route.name)
                spec = AssetSpec(
                    path=route.key,
                    kind=AssetKind.METADATA,
                    content_type=protocol.SPEC_TYPE,
                    validate=check_gemspec,
                    max_size=protocol.MAX_GEMSPEC,
                    max_age_s=FOREVER_S,
                    artifact=f'{route.name}.gemspec.rz',
                    component=readings[0][0],
                )
            case Op.GEM:
                spec = self.gem_spec(req, route)
            case _:
                raise NotFound('Not Found')
        return add_digest(await req.proxy.serve(req, spec))

    @staticmethod
    def versions_spec() -> AssetSpec:
        return AssetSpec(
            path=protocol.VERSIONS,
            kind=AssetKind.METADATA,
            content_type=protocol.TEXT_TYPE,
            validate=lambda data, info: protocol.check_versions(data),
            max_size=protocol.MAX_VERSIONS_FILE,
            artifact='versions',
        )

    def info_spec(self, req: FormatRequest, name: str, *, max_age_s: int | None = None) -> AssetSpec:
        return AssetSpec(
            path=f'{protocol.INFO}/{name}',
            kind=AssetKind.METADATA,
            content_type=protocol.TEXT_TYPE,
            validate=lambda data, info: protocol.check_index_text(data, 'info file'),
            max_size=protocol.MAX_INFO,
            max_age_s=max_age_s,
            on_stored=self._on_info_stored(req.proxy, req.repo.id, name),
            artifact=f'{name} info',
            component=name,
        )

    def _on_info_stored(self, engine: ProxyEngine, repo_id: str, name: str) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            if data is None:
                return

            async def index() -> None:
                lines = await asyncio.to_thread(protocol.parse_info, data)
                if lines:
                    await gems_db.upsert_gem(engine.db, repo_id, name, summary_fields(lines, name))

            self._indexing.spawn((repo_id, name, asset.blob_sha256), index)

        return stored

    async def info_lines(self, req: FormatRequest, name: str, *, refresh: bool = False) -> list[InfoLine]:
        """A proxy's cached info lines of ``name`` (parsed copies kept per process by blob digest)."""
        asset = await req.proxy.ensure(req, self.info_spec(req, name, max_age_s=0 if refresh else None))
        found = self._infos.get(asset.blob_sha256)
        if found is None:
            data = await req.proxy.read(asset, max_bytes=protocol.MAX_INFO)
            found = await asyncio.to_thread(protocol.parse_info, data)
            self._infos[asset.blob_sha256] = found
            while len(self._infos) > KEPT:
                self._infos.popitem(last=False)
        else:
            self._infos.move_to_end(asset.blob_sha256)
        return found

    async def find_line(self, req: FormatRequest, stem: str) -> tuple[str, InfoLine] | None:
        """The gem and info line a file stem ``<name>-<version>[-<platform>]`` stands for (None: not listed)."""
        readings = rubygems.split_full_name(stem)[:READINGS]
        for refresh in (False, True):
            for name, version, platform in readings:
                try:
                    lines = await self.info_lines(req, name, refresh=refresh)
                except NotFound:
                    continue
                line = next((li for li in lines if (li.version, li.platform) == (version, platform)), None)
                if line is not None:
                    return name, line
        return None

    def gem_spec(self, req: FormatRequest, route: Route) -> AssetSpec:
        name = rubygems.split_full_name(route.name)[0][0]

        async def expect() -> Expected:
            found = await self.find_line(req, route.name)
            if found is None:
                raise NotFound(f'{route.name}.gem is not listed by the upstream')
            if found[1].checksum is None:
                raise ValidationFailed(f'the upstream lists no checksum for {route.name}.gem: not cached')
            return Expected(sha256=found[1].checksum)

        return AssetSpec(
            path=route.key,
            kind=AssetKind.CONTENT,
            expect=expect,
            content_type=protocol.GEM_TYPE,
            max_size=MAX_GEM,
            max_age_s=FOREVER_S,
            head_passthrough=False,
            artifact=f'{route.name}.gem',
            component=name,
        )
