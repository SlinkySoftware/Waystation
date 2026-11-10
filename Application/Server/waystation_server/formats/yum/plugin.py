"""yum / dnf proxy (FORMATS.md "yum"; ARCHITECTURE §6.4). Hosted repositories: ``formats.yum.hosted``.

Everything lives under ``/repository/<repo>/`` with the upstream's layout kept (``10/BaseOS/x86_64/os/...``), so one
repository can proxy every yum repository below its upstream URL. A **repository directory** is the directory that
holds ``repodata/``; four kinds of file:

1. ``<dir>/repodata/repomd.xml``: metadata (revalidated after ``metadata_max_age_s``), bytes passed through untouched
   (dnf verifies ``repomd.xml.asc`` itself when ``repo_gpgcheck`` is on). Refused (502, nothing cached) unless it
   parses: bounded, defused XML (``repodata``). ``repomd.xml.asc`` / ``.key`` are small metadata; a newly stored
   ``repomd.xml`` drops the cached ``.asc`` so the pair is fetched together again. With the ``repomd_keys`` option
   (the upstream's public keys) ``repomd.xml.asc`` is fetched fresh right before every ``repomd.xml`` fetch or
   revalidation and the new ``repomd.xml`` is refused (502; stale-on-error applies) unless the signature verifies
   (``waystation_common.openpgp``); that ``.asc`` is then served with it.
2. **Files ``repomd.xml`` lists** (``primary``, ``filelists``, ``other``, ``updateinfo``, ``modules``, ``comps``,
   sqlite variants; ``.gz``/``.xz``/``.zst``/``.bz2``): served only by the checksum and size the currently cached
   ``repomd.xml`` of the directory lists (SHA-1/256/512), so the set always matches the ``repomd.xml`` the client
   got. By-checksum names (``<hex>-primary.xml.gz``, the createrepo default) are immutable and cached forever under
   their own path; a listed file with a plain name is stored under ``repodata/<hex>-<name>``, never as a mutable
   asset. Fetched buffered and verified before the first byte; on a mismatch ``repomd.xml`` is revalidated once and
   the request retried with the new entry, then it fails (502). An unlisted by-checksum name (the client holds an
   older ``repomd.xml``) is self-validating against the checksum in its name; any other unlisted file is 404.
3. **Packages** (``*.rpm``, ``*.drpm`` anywhere): content, validated against their ``primary.xml`` entry
   (``yum_packages``). When a ``primary`` is cached it is decompressed and parsed in a thread (bounded, chunked) in
   the background and its packages recorded. A package with no entry yet looks for the closest enclosing directory
   with a cached ``repomd.xml``, makes sure its ``primary`` is cached and indexed (waiting for it), and validates
   against that; if none lists it, it is served and recorded ``unverified`` (computed SHA-256) and checked when a
   listing arrives (a disagreeing cached copy is dropped).
4. Anything else (``.treeinfo``, ``RPM-GPG-KEY-*``, ...): metadata by path, revalidated.

Mirrorlist and metalink URLs are not proxied: repositories are configured with a concrete base URL.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections import OrderedDict
from dataclasses import dataclass

from starlette.responses import Response

from waystation_common import openpgp
from waystation_common.errors import DigestMismatch, NotFound, UpstreamError, ValidationFailed, WaystationError
from waystation_server.db import assets as assets_db
from waystation_server.db import yum as yum_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.yum import evr, repodata
from waystation_server.formats.yum.browse import YumBrowse
from waystation_server.formats.yum.descriptor import DESCRIPTOR
from waystation_server.formats.yum.hosted import YumHosted
from waystation_server.problems import native_error
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

log = logging.getLogger('waystation.formats.yum')

REPOMD = 'repomd.xml'
SIGNATURES = ('repomd.xml.asc', 'repomd.xml.key')
SIGNATURE_MAX = 1024 * 1024
REPODATA_MAX = 256 * 1024 * 1024
FOREVER = 100 * 365 * 86_400  # checksum-keyed assets never need revalidation
PARSED_CACHE = 32
MAX_DEPTH = 16  # enclosing directories searched for a repomd.xml
IDENTITY = {'Accept-Encoding': 'identity'}  # the bytes are the file (.gz/.xz/.zst), never decoded


@dataclass(frozen=True)
class Target:
    """What a request path is."""

    kind: str  # 'repomd', 'signature', 'repodata', 'package', 'other'
    repo_dir: str = ''  # the directory holding repodata/ ('' = repository root)
    name: str = ''  # file name under repodata/


def _join(*parts: str) -> str:
    return '/'.join(p for p in parts if p)


def classify(path: str) -> Target:
    parts = path.split('/')
    name = parts[-1]
    if len(parts) >= 2 and parts[-2] == 'repodata':
        repo_dir = '/'.join(parts[:-2])
        if name == REPOMD:
            return Target('repomd', repo_dir, name)
        if name in SIGNATURES:
            return Target('signature', repo_dir, name)
        return Target('repodata', repo_dir, name)
    if name.endswith(('.rpm', '.drpm')):
        return Target('package')
    return Target('other')


def expected_of(algorithm: str, digest: str, size: int | None = None) -> Expected:
    if algorithm == 'sha1':
        return Expected(sha1=digest, size=size)
    if algorithm == 'sha512':
        return Expected(sha512=digest, size=size)
    return Expected(sha256=digest, size=size)


def repomd_keys(req: FormatRequest) -> str | None:
    """The upstream's armored public keys when ``repomd.xml`` must be verified (option ``repomd_keys``)."""
    value = req.repo.format_options.get('repomd_keys')
    return value if isinstance(value, str) and value else None


@functools.lru_cache(maxsize=32)
def parsed_keys(text: str) -> tuple[openpgp.PublicKey, ...]:
    return tuple(openpgp.read_public_keys(text))


def _signature_spec(repo_dir: str, *, max_age_s: int | None) -> AssetSpec:
    return AssetSpec(
        path=_join(repo_dir, 'repodata', SIGNATURES[0]),
        kind=AssetKind.METADATA,
        upstream_headers=IDENTITY,
        max_size=SIGNATURE_MAX,
        max_age_s=max_age_s,
    )


def _validate_repomd(data: bytes, info: UpstreamInfo) -> None:
    try:
        repodata.parse_repomd(data)
    except ValidationFailed as exc:  # the upstream's fault: 502, nothing cached
        raise UpstreamError(f'upstream repomd.xml is invalid: {exc.detail}') from None


class YumPlugin:
    descriptor = DESCRIPTOR

    def __init__(self) -> None:
        self._parsed: OrderedDict[str, repodata.Repomd] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._indexing: dict[tuple[str, str, str], asyncio.Task[None]] = {}
        self.browser = YumBrowse()
        self.hosted = YumHosted()

    async def handle(self, req: FormatRequest) -> Response:
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.handle(req)
        if req.method not in ('GET', 'HEAD'):
            return native_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD'})
        if not req.path:
            raise NotFound('Not Found')
        target = classify(req.path)
        match target.kind:
            case 'repomd':
                return await req.proxy.serve(req, self._repomd_spec(req, target.repo_dir))
            case 'signature':
                return await self._serve_signature(req, target)
            case 'repodata':
                return await self._serve_repodata(req, target)
            case 'package':
                return await self._serve_package(req)
        return await req.proxy.serve(req, AssetSpec(path=req.path, kind=AssetKind.METADATA))

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------------ repomd.xml
    def _repomd_spec(self, req: FormatRequest, repo_dir: str, *, max_age_s: int | None = None) -> AssetSpec:
        keys = repomd_keys(req)
        if keys is None:
            return AssetSpec(
                path=_join(repo_dir, 'repodata', REPOMD),
                kind=AssetKind.METADATA,
                upstream_headers=IDENTITY,
                max_size=repodata.REPOMD_MAX_BYTES,
                max_age_s=max_age_s,
                validate=_validate_repomd,
                on_stored=self._on_repomd_stored(req, repo_dir),
            )
        # Verified mode: just before repomd.xml is fetched (or revalidated) upstream, its repomd.xml.asc is fetched
        # fresh and cached; the new repomd.xml is refused unless that signature verifies with a configured key.
        signatures: list[bytes] = []

        async def fetch_signature() -> Expected | None:
            try:
                asset = await req.proxy.ensure(req, _signature_spec(repo_dir, max_age_s=0))
                signatures.append(await req.proxy.read(asset, max_bytes=SIGNATURE_MAX))
            except NotFound:
                raise UpstreamError('upstream has no repomd.xml.asc to verify repomd.xml with') from None
            return None

        def validate(data: bytes, info: UpstreamInfo) -> None:
            _validate_repomd(data, info)
            try:
                openpgp.verify_detached(data, signatures[-1] if signatures else b'', parsed_keys(keys))
            except ValidationFailed as exc:
                raise UpstreamError(f'upstream repomd.xml is not signed by a configured key ({exc.detail})') from None

        return AssetSpec(
            path=_join(repo_dir, 'repodata', REPOMD),
            kind=AssetKind.METADATA,
            upstream_headers=IDENTITY,
            max_size=repodata.REPOMD_MAX_BYTES,
            max_age_s=max_age_s,
            expect=fetch_signature,
            validate=validate,
        )

    @staticmethod
    def _on_repomd_stored(req: FormatRequest, repo_dir: str) -> OnStored:
        db, repo_id = req.proxy.db, req.repo.id

        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            # A new repomd.xml: its detached signature must be fetched again with it.
            await yum_db.drop_cached(db, repo_id, _join(repo_dir, 'repodata', SIGNATURES[0]))

        return stored

    async def _serve_signature(self, req: FormatRequest, target: Target) -> Response:
        """``repomd.xml.asc`` / ``.key``. Verified mode: ``.asc`` is the one ``repomd.xml`` was verified with (fetched
        with it, never revalidated on its own), so the repomd.xml is brought up to date first."""
        if target.name == SIGNATURES[0] and repomd_keys(req) is not None:
            await self._current_repomd(req, target.repo_dir, refresh_stale=True)
            return await req.proxy.serve(req, _signature_spec(target.repo_dir, max_age_s=FOREVER))
        return await req.proxy.serve(req, AssetSpec(path=req.path, kind=AssetKind.METADATA, max_size=SIGNATURE_MAX))

    async def _parse(self, engine: ProxyEngine, asset: StoredAsset) -> repodata.Repomd:
        """A cached ``repomd.xml`` parsed (in a thread, bounded; kept in a small cache by digest)."""
        cached = self._parsed.get(asset.blob_sha256)
        if cached is not None:
            self._parsed.move_to_end(asset.blob_sha256)
            return cached
        data = await engine.read(asset, max_bytes=repodata.REPOMD_MAX_BYTES)
        parsed = await asyncio.to_thread(repodata.parse_repomd, data)
        self._parsed[asset.blob_sha256] = parsed
        while len(self._parsed) > PARSED_CACHE:
            self._parsed.popitem(last=False)
        return parsed

    async def _current_repomd(
        self, req: FormatRequest, repo_dir: str, *, refresh: bool = False, refresh_stale: bool = False
    ) -> repodata.Repomd:
        """The ``repomd.xml`` the directory is served with now: the cached one, else fetched. ``refresh``
        revalidates it upstream first; ``refresh_stale`` when it is older than the repository's metadata max age."""
        engine = req.proxy
        if refresh_stale:
            return await self._parse(engine, await engine.ensure(req, self._repomd_spec(req, repo_dir)))
        if not refresh:
            asset = await assets_db.get(engine.db, req.repo.id, _join(repo_dir, 'repodata', REPOMD))
            if asset is not None and await engine.blobs.exists(asset.blob_sha256):
                return await self._parse(engine, asset)
        spec = self._repomd_spec(req, repo_dir, max_age_s=0 if refresh else FOREVER)
        return await self._parse(engine, await engine.ensure(req, spec))

    # ------------------------------------------------------------------ files repomd.xml lists
    def _repodata_spec(self, req: FormatRequest, repo_dir: str, name: str, entry: repodata.RepomdEntry) -> AssetSpec:
        if entry.checksum is None:
            raise UpstreamError(f'repomd.xml lists {name} with a checksum type Waystation cannot verify')
        digest = entry.checksum.digest
        plain = _join(repo_dir, 'repodata', name)
        key = plain if name.startswith(f'{digest}-') else _join(repo_dir, 'repodata', f'{digest}-{name}')
        return AssetSpec(
            path=key,
            kind=AssetKind.METADATA,  # buffered: verified before the first byte, so a mismatch can be retried
            upstream_path=plain,
            upstream_headers=IDENTITY,
            expected=expected_of(entry.checksum.algorithm, digest, entry.size),
            content_type='application/octet-stream' if repodata.compression_of(name) else None,
            max_size=REPODATA_MAX,
            max_age_s=FOREVER,
            head_passthrough=False,
            on_stored=self._on_primary_stored(req, repo_dir, entry) if entry.type == 'primary' else None,
            artifact=name,
        )

    async def _serve_repodata(self, req: FormatRequest, target: Target) -> Response:
        repomd = await self._current_repomd(req, target.repo_dir)
        href = f'repodata/{target.name}'
        entry = repomd.entries.get(href)
        if entry is None:
            return await self._serve_by_checksum(req, target)
        try:
            return await req.proxy.serve(req, self._repodata_spec(req, target.repo_dir, target.name, entry))
        except DigestMismatch:
            log.info('repodata file does not match repomd.xml; revalidating it', extra={'path': req.path})
        repomd = await self._current_repomd(req, target.repo_dir, refresh=True)
        retry = repomd.entries.get(href)
        if retry is None:
            raise NotFound('Not Found')
        if retry == entry:
            raise DigestMismatch('repodata file does not match repomd.xml')
        return await req.proxy.serve(req, self._repodata_spec(req, target.repo_dir, target.name, retry))

    async def _serve_by_checksum(self, req: FormatRequest, target: Target) -> Response:
        """A repodata file the cached ``repomd.xml`` does not list: only a by-checksum name can be verified."""
        named = repodata.checksum_prefix(target.name)
        if named is None:
            raise NotFound('Not Found')
        spec = AssetSpec(
            path=req.path,
            kind=AssetKind.CONTENT,
            upstream_headers=IDENTITY,
            expected=expected_of(named.algorithm, named.digest),
            content_type='application/octet-stream',
            max_size=REPODATA_MAX,
            max_age_s=FOREVER,
        )
        return await req.proxy.serve(req, spec)

    # ------------------------------------------------------------------ primary.xml indexing
    def _on_primary_stored(self, req: FormatRequest, repo_dir: str, entry: repodata.RepomdEntry) -> OnStored:
        engine, repo_id = req.proxy, req.repo.id

        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            self._schedule_primary(engine, repo_id, repo_dir, entry.href, asset, data)

        return stored

    def _schedule_primary(
        self, engine: ProxyEngine, repo_id: str, repo_dir: str, href: str, asset: StoredAsset, data: bytes | None
    ) -> asyncio.Task[None]:
        key = (repo_id, repo_dir, asset.blob_sha256)
        running = self._indexing.get(key)
        if running is not None:
            return running
        task = asyncio.create_task(self._index_primary(engine, repo_id, repo_dir, href, asset, data))
        self._indexing[key] = task
        self._tasks.add(task)

        def done(finished: asyncio.Task[None]) -> None:
            self._tasks.discard(finished)
            self._indexing.pop(key, None)

        task.add_done_callback(done)
        return task

    async def _index_primary(
        self, engine: ProxyEngine, repo_id: str, repo_dir: str, href: str, asset: StoredAsset, data: bytes | None
    ) -> None:
        try:
            suffix = repodata.compression_of(href)
            if suffix is None:
                log.info('primary has an unsupported compression; not indexed', extra={'path': asset.path})
                return
            raw = data if data is not None else await engine.read(asset, max_bytes=REPODATA_MAX)
            packages = await asyncio.to_thread(repodata.parse_primary, raw, suffix)
            listings = [
                yum_db.Listing(
                    path=_join(repo_dir, p.href),
                    algorithm=p.checksum.algorithm,
                    digest=p.checksum.digest,
                    size=p.size,
                    name=p.name,
                    epoch=p.epoch,
                    version=p.version,
                    release=p.release,
                    arch=p.arch,
                    version_sort=evr.evr_key(p.epoch, p.version, p.release),
                )
                for p in packages
            ]
            mismatched = await yum_db.record_primary(engine.db, repo_id, repo_dir, listings, gen=asset.blob_sha256)
            for path in mismatched:
                log.warning('cached package does not match its listing; dropped', extra={'path': path})
            log.info('indexed primary', extra={'path': asset.path, 'entries': len(listings)})
        except Exception as exc:
            log.warning('primary indexing failed', extra={'path': asset.path, 'error': type(exc).__name__})

    async def _listing_from_enclosing_repo(self, req: FormatRequest) -> yum_db.PackageRecord | None:
        """No entry for this package yet: index the ``primary`` of the closest enclosing directory with a cached
        ``repomd.xml`` (fetching the primary if needed) and look again."""
        engine, repo_id = req.proxy, req.repo.id
        parts = req.path.split('/')[:-1]
        for depth in range(len(parts), max(-1, len(parts) - MAX_DEPTH - 1), -1):
            repo_dir = '/'.join(parts[:depth])
            asset = await assets_db.get(engine.db, repo_id, _join(repo_dir, 'repodata', REPOMD))
            if asset is None:
                continue
            try:
                primary = (await self._parse(engine, asset)).of_type('primary')
                if primary is None or not primary.href.startswith('repodata/'):
                    return None
                name = primary.href.removeprefix('repodata/')
                stored = await engine.ensure(req, self._repodata_spec(req, repo_dir, name, primary))
                if not await yum_db.indexed(engine.db, repo_id, repo_dir, stored.blob_sha256):
                    task = self._schedule_primary(engine, repo_id, repo_dir, primary.href, stored, None)
                    await asyncio.shield(task)
            except (WaystationError, OSError) as exc:
                log.info(
                    'no primary to validate a package against', extra={'path': req.path, 'error': type(exc).__name__}
                )
                return None
            return await yum_db.lookup(engine.db, repo_id, req.path)
        return None

    # ------------------------------------------------------------------ packages
    async def _serve_package(self, req: FormatRequest) -> Response:
        engine = req.proxy
        record = await yum_db.lookup(engine.db, req.repo.id, req.path)
        if record is None:
            record = await self._listing_from_enclosing_repo(req)
        if record is not None and record.verified:
            spec = AssetSpec(
                path=req.path,
                kind=AssetKind.CONTENT,
                expected=expected_of(record.algorithm, record.digest, record.size),
            )
            return await engine.serve(req, spec)
        repo_id = req.repo.id

        async def unverified(asset: StoredAsset, data: bytes | None) -> None:
            listed = await yum_db.record_unverified(engine.db, repo_id, asset.path, asset.blob_sha256, asset.size)
            if listed is not None and (
                listed.algorithm != 'sha256' or listed.digest != asset.blob_sha256 or listed.size != asset.size
            ):
                log.warning('package does not match its listing; dropped', extra={'path': asset.path})
                await yum_db.drop_cached(engine.db, repo_id, asset.path)

        log.info('package not listed in any cached primary; serving unverified', extra={'path': req.path})
        return await engine.serve(req, AssetSpec(path=req.path, kind=AssetKind.CONTENT, on_stored=unverified))
