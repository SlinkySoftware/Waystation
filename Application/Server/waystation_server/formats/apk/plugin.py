"""Alpine apk plugin (T-P6-APK; FORMATS.md "Alpine apk"): proxy and hosted repositories.

**Proxy** (the upstream URL is an Alpine mirror root, e.g. ``https://dl-cdn.alpinelinux.org/alpine/``; one repository
serves every ``<branch>/<repository>/<arch>`` below it):

- ``<dir>/APKINDEX.tar.gz``: metadata (revalidated after ``metadata_max_age_s``), bytes passed through untouched: apk
  verifies the index signature with its own keys (``/etc/apk/keys``). Refused (502, nothing cached) unless it is an
  index (gzip members, a tar holding ``APKINDEX``; bounded). Once cached it is parsed in the background (in a thread,
  bounded) into ``apk_packages``: path, ``C:`` checksum, ``S:`` size and the package's details.
- ``<dir>/<name>-<version>.apk``: content (kept by the content max age). What can be verified: the index ``C:`` value
  is the SHA-1 of the package's *control segment* (its second gzip stream when signed), not of the file, and ``S:``
  is the file size. A fetched package is checked before anything is committed: size, the control segment's SHA-1
  (gzip stream boundaries found by inflating, bounded) and, through ``.PKGINFO`` ``datahash``, the SHA-256 of the data
  segment (``waystation_common.apk.package_check``), so the whole file is verified. A package with no listing yet
  makes sure its directory's index is cached and indexed (waiting for it); if none lists it, it is served and recorded
  ``unverified`` (its computed ``C:`` and size), and dropped if a later listing disagrees. A mismatch with the listing
  revalidates the index once, then fails (502).
- Anything else (``last-updated``, ``MIRRORS.txt``...): metadata by path.

**Hosted**: ``formats.apk.hosted``. Groups are not supported (signed indexes cannot be merged).
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from starlette.responses import Response

from waystation_common import apk
from waystation_common.errors import DigestMismatch, NotFound, UpstreamError, WaystationError
from waystation_server.db import apk as apk_db
from waystation_server.formats.apk import prefetch
from waystation_server.formats.apk.browse import ApkBrowse
from waystation_server.formats.apk.common import details
from waystation_server.formats.apk.descriptor import DESCRIPTOR, options_of
from waystation_server.formats.apk.hosted import ApkHosted
from waystation_server.formats.base import FormatRequest, WarmContext
from waystation_server.problems import native_error
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

log = logging.getLogger('waystation.formats.apk')

IDENTITY = {'Accept-Encoding': 'identity'}  # the bytes are the file, never decoded
MAX_DEPTH = 8


def directory_of(path: str) -> str | None:
    """The directory of an index or package path, when it is a plausible ``<branch>/<repo>/<arch>`` (1-8 levels)."""
    directory, _, _ = path.rpartition('/')
    parts = directory.split('/') if directory else []
    if not parts or len(parts) > MAX_DEPTH:
        return None
    return directory


def _validate_index(data: bytes, info: UpstreamInfo) -> None:
    try:
        apk.check_index(data)
    except apk.ApkError as exc:  # the upstream's fault: 502, nothing cached
        raise UpstreamError(f'upstream APKINDEX is invalid: {exc}') from None


def _check(checksum: str) -> Callable[[BinaryIO], None]:
    inner = apk.package_check(checksum)

    def check(f: BinaryIO) -> None:
        try:
            inner(f)
        except apk.ApkError as exc:
            raise DigestMismatch(str(exc)) from None

    return check


def _computed(path: Path) -> tuple[str, int] | None:
    """The ``C:`` checksum and size of a stored package, or None when it is not a readable apk v2 package."""
    try:
        with path.open('rb') as fh:
            size = fh.seek(0, io.SEEK_END)
            return apk.read_package(fh, size).checksum, size
    except apk.ApkError, OSError:
        return None


class ApkPlugin:
    descriptor = DESCRIPTOR

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._indexing: dict[tuple[str, str, str], asyncio.Task[None]] = {}
        self.browser = ApkBrowse()
        self.hosted = ApkHosted()

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def warm(self, ctx: WarmContext, item: str) -> None:
        """Prefetch (T-O12): ``formats.apk.prefetch``."""
        opts = options_of(ctx.repo.format_options)
        await prefetch.warm(
            ctx, item, branch=opts.branch, repositories=opts.repositories, architectures=opts.architectures
        )

    async def handle(self, req: FormatRequest) -> Response:
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.handle(req)
        if req.method not in ('GET', 'HEAD'):
            return native_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD'})
        if not req.path:
            raise NotFound('Not Found')
        directory = directory_of(req.path)
        name = req.path.rsplit('/', 1)[-1]
        if directory is not None and name == apk.INDEX_NAME:
            return await req.proxy.serve(req, self._index_spec(req, directory))
        if directory is not None and name.endswith('.apk'):
            return await self._serve_package(req, directory)
        return await req.proxy.serve(req, AssetSpec(path=req.path, kind=AssetKind.METADATA))

    # ------------------------------------------------------------------ APKINDEX
    def _index_spec(self, req: FormatRequest, directory: str, *, max_age_s: int | None = None) -> AssetSpec:
        return AssetSpec(
            path=f'{directory}/{apk.INDEX_NAME}',
            kind=AssetKind.METADATA,
            upstream_headers=IDENTITY,
            content_type='application/gzip',
            max_size=apk.INDEX_MAX,
            max_age_s=max_age_s,
            validate=_validate_index,
            on_stored=self._on_index_stored(req, directory),
        )

    def _on_index_stored(self, req: FormatRequest, directory: str) -> OnStored:
        engine, repo_id = req.proxy, req.repo.id

        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            self._schedule_index(engine, repo_id, directory, asset, data)

        return stored

    def _schedule_index(
        self, engine: ProxyEngine, repo_id: str, directory: str, asset: StoredAsset, data: bytes | None
    ) -> asyncio.Task[None]:
        key = (repo_id, directory, asset.blob_sha256)
        running = self._indexing.get(key)
        if running is not None:
            return running
        task = asyncio.create_task(self._index(engine, repo_id, directory, asset, data))
        self._indexing[key] = task
        self._tasks.add(task)

        def done(finished: asyncio.Task[None]) -> None:
            self._tasks.discard(finished)
            self._indexing.pop(key, None)

        task.add_done_callback(done)
        return task

    async def _index(
        self, engine: ProxyEngine, repo_id: str, directory: str, asset: StoredAsset, data: bytes | None
    ) -> None:
        try:
            raw = data if data is not None else await engine.read(asset, max_bytes=apk.INDEX_MAX)
            index = await asyncio.to_thread(apk.parse_index, raw)
            listings = [
                apk_db.Listing(
                    path=f'{directory}/{e.file_name}',
                    checksum=e.checksum,
                    size=e.size,
                    name=e.name,
                    version=e.version,
                    arch=e.arch or apk.Directory(directory).arch,
                    version_sort=apk.sort_string(e.version),
                    details=details(e.fields),
                )
                for e in index.entries
            ]
            mismatched = await apk_db.record_index(engine.db, repo_id, directory, listings, gen=asset.blob_sha256)
            for path in mismatched:
                log.warning('cached package does not match its listing; dropped', extra={'path': path})
            log.info('indexed APKINDEX', extra={'path': asset.path, 'entries': len(listings)})
        except Exception as exc:
            log.warning('APKINDEX indexing failed', extra={'path': asset.path, 'error': type(exc).__name__})

    async def _indexed_listing(
        self, req: FormatRequest, directory: str, *, refresh: bool = False
    ) -> apk_db.PackageRecord | None:
        """Make sure the directory's index is cached (``refresh``: revalidated now) and indexed, then look the package
        up again."""
        engine, repo_id = req.proxy, req.repo.id
        try:
            spec = self._index_spec(req, directory, max_age_s=0 if refresh else None)
            stored = await engine.ensure(req, spec)
            if not await apk_db.indexed(engine.db, repo_id, directory, stored.blob_sha256):
                await asyncio.shield(self._schedule_index(engine, repo_id, directory, stored, None))
        except (WaystationError, OSError) as exc:
            log.info('no APKINDEX to verify a package against', extra={'path': req.path, 'error': type(exc).__name__})
            return None
        return await apk_db.lookup(engine.db, repo_id, req.path)

    # ------------------------------------------------------------------ packages
    @staticmethod
    def _package_spec(req: FormatRequest, record: apk_db.PackageRecord) -> AssetSpec:
        return AssetSpec(
            path=req.path,
            kind=AssetKind.CONTENT,
            upstream_headers=IDENTITY,
            content_type='application/octet-stream',
            expected=CheckedExpected(size=record.size, check=_check(record.checksum)),
        )

    async def _serve_package(self, req: FormatRequest, directory: str) -> Response:
        engine = req.proxy
        record = await apk_db.lookup(engine.db, req.repo.id, req.path)
        if record is None or not record.verified:
            record = await self._indexed_listing(req, directory) or record
        if record is not None and record.verified:
            try:
                return await engine.serve(req, self._package_spec(req, record))
            except DigestMismatch:
                log.info('package does not match its listing; revalidating the index', extra={'path': req.path})
            retry = await self._indexed_listing(req, directory, refresh=True)
            if retry is None or not retry.verified:
                raise NotFound('Not Found')
            if retry == record:
                raise DigestMismatch('the package does not match the APKINDEX checksum')
            return await engine.serve(req, self._package_spec(req, retry))
        return await self._serve_unverified(req)

    async def _serve_unverified(self, req: FormatRequest) -> Response:
        engine, repo_id = req.proxy, req.repo.id

        async def unverified(asset: StoredAsset, data: bytes | None) -> None:
            computed = await asyncio.to_thread(_computed, engine.blobs.blob_path(asset.blob_sha256))
            if computed is None:
                log.warning('fetched file is not an apk package; dropped', extra={'path': asset.path})
                await apk_db.drop_cached(engine.db, repo_id, asset.path)
                return
            checksum, size = computed
            listed = await apk_db.record_unverified(engine.db, repo_id, asset.path, checksum, size)
            if listed is not None and (listed.checksum != checksum or (listed.size not in (None, size))):
                log.warning('package does not match its listing; dropped', extra={'path': asset.path})
                await apk_db.drop_cached(engine.db, repo_id, asset.path)

        log.info('package not listed in any cached APKINDEX; serving unverified', extra={'path': req.path})
        spec = AssetSpec(
            path=req.path,
            kind=AssetKind.CONTENT,
            upstream_headers=IDENTITY,
            content_type='application/octet-stream',
            on_stored=unverified,
        )
        return await engine.serve(req, spec)
