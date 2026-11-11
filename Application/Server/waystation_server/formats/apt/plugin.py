"""apt proxy (FORMATS.md "apt"; ARCHITECTURE §6.4).

Three kinds of file, all under ``/repository/<repo>/``:

1. ``dists/<suite>/{InRelease,Release,Release.gpg}``: metadata (revalidated after ``metadata_max_age_s``), bytes
   passed through untouched; clients verify the signature with their own keyring.
2. **Index files** (``Packages*``, ``Sources*``, ``Translation-*``, ``Contents-*``, dep11...):
   - at their plain path they are served by the hash the currently cached ``InRelease`` (else ``Release``) of the
     suite lists, so the index set always matches the ``Release`` the client got. The blob is stored under the
     file's ``by-hash/SHA256/<h>`` key, never as a mutable asset at the plain path. A miss is fetched (from the
     upstream ``by-hash`` path when the ``Release`` says ``Acquire-By-Hash: yes``, else the plain path) and verified
     against the listed SHA256 and size; on a mismatch the ``Release`` is revalidated once and the request retried
     with the new hash, then it fails. Files the ``Release`` does not list answer 404 (they cannot be verified).
   - ``by-hash/SHA256/<h>`` (and ``SHA512``) paths are immutable and self-validating.
   When a ``Packages`` file is cached its ``Filename → (SHA256, Size)`` entries are written to ``apt_pool_index``
   and its stanzas (name, version, architecture, section) to ``apt_packages`` for browsing (parsed and decompressed
   in a thread, bounded; in the background, so the response is not delayed).
3. **Pool files** (``pool/...``): content, validated against their ``apt_pool_index`` listing; fetched before any
   listing they are served and recorded ``unverified`` (computed hash), and checked when a listing arrives.

Several suites (``trixie``, ``trixie-updates``, ``trixie-security``...) through one repository work: everything is
keyed by path. A suite may be several segments deep (``dists/bookworm/mongodb-org/8.0/``): an index path belongs to
the longest suite directory with a cached ``Release``, else the layout decides (``suites``). Flat repositories
(``flat: true``) keep ``Release`` and indexes next to each other; the ``Release`` of an index is the one in the same
directory.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from collections import OrderedDict
from collections.abc import Collection
from dataclasses import dataclass

from starlette.responses import Response

from waystation_common.errors import DigestMismatch, NotFound, UpstreamError, ValidationFailed
from waystation_server.db import apt as apt_db
from waystation_server.db import assets as assets_db
from waystation_server.formats.apt import deb822, suites
from waystation_server.formats.apt.browse import AptBrowse, browse_suite
from waystation_server.formats.apt.descriptor import DESCRIPTOR, AptOptions
from waystation_server.formats.base import FormatRequest
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

log = logging.getLogger('waystation.formats.apt')

RELEASE_FILES = ('InRelease', 'Release', 'Release.gpg')
SIGNED_RELEASES = ('InRelease', 'Release')
FOREVER = 100 * 365 * 86_400  # hash-keyed assets never need revalidation
RELEASE_GPG_MAX = 1024 * 1024
INDEX_MAX = 256 * 1024 * 1024
PARSED_CACHE = 32
BY_HASH_ALGS = {'SHA256': ('sha256', 64), 'SHA512': ('sha512', 128)}
INDEX_PREFIXES = ('Packages', 'Sources', 'Translation-', 'Contents-', 'Components-', 'icons-', 'Commands-', 'Index')


@dataclass(frozen=True)
class Target:
    """What a request path is."""

    kind: str  # 'release', 'index', 'by-hash', 'pool', 'other'
    suite_dir: str = ''  # 'dists/<suite>' ('dists/bookworm/mongodb-org/8.0'; or the flat directory, '' = root)
    rel: str = ''  # index path relative to suite_dir
    algorithm: str = ''  # by-hash: 'sha256' / 'sha512'
    digest: str = ''


def _join(*parts: str) -> str:
    return '/'.join(p for p in parts if p)


def suite_segments(path: str, *, flat: bool) -> list[str] | None:
    """For an index or ``by-hash`` path below ``dists/``: the directories below ``dists/`` that hold its suite
    (``classify`` needs to know which of them are cached suites); None for every other path."""
    if flat:
        return None
    parts = path.split('/')
    if parts[0] != suites.DISTS or len(parts) < 3 or suites.release_suite_dir(path) is not None:
        return None
    if len(parts) >= 5 and parts[-3] == 'by-hash':
        return parts[1:-3]
    return parts[1:-1]


def _dists_target(parts: list[str], known: Collection[str]) -> Target:
    path = '/'.join(parts)
    if not suites.safe(path):
        return Target('invalid')
    suite_dir = suites.release_suite_dir(path)
    if suite_dir is not None:
        return Target('release', suite_dir=suite_dir, rel=parts[-1])
    if parts[-1] in RELEASE_FILES and len(parts) - 2 > suites.MAX_SUITE_DEPTH:
        return Target('invalid')  # a suite's release, too deep
    by_hash = len(parts) >= 3 and parts[-3] == 'by-hash'
    segments = parts[1:-3] if by_hash else parts[1:-1]  # never empty: classify checked the length
    depth = suites.suite_depth(segments, known)
    if depth > suites.MAX_SUITE_DEPTH:
        return Target('invalid')
    suite_dir = '/'.join(parts[: depth + 1])
    if by_hash:
        alg = BY_HASH_ALGS[parts[-2]]
        return Target('by-hash', suite_dir=suite_dir, algorithm=alg[0], digest=parts[-1].lower())
    return Target('index', suite_dir=suite_dir, rel='/'.join(parts[depth + 1 :]))


def classify(path: str, *, flat: bool, known: Collection[str] = ()) -> Target:
    """What ``path`` is. ``known`` = the repository's cached suite directories among ``suite_segments(path)``'s
    candidates (``dists/bookworm/mongodb-org/8.0``): the suite of an index is the longest of them, else the layout
    decides (``suites``)."""
    parts = path.split('/')
    name = parts[-1]
    if len(parts) >= 3 and parts[-3] == 'by-hash':
        alg = BY_HASH_ALGS.get(parts[-2])
        digest = parts[-1].lower()
        if alg is None or len(digest) != alg[1] or any(c not in '0123456789abcdef' for c in digest):
            return Target('invalid')
        base = parts[:-3]
        if flat:
            return Target('by-hash', suite_dir='/'.join(base), algorithm=alg[0], digest=digest)
        if base[:1] != [suites.DISTS] or len(base) < 2:
            return Target('invalid')
        return _dists_target(parts, known)
    if not flat:
        if parts[0] == suites.DISTS and len(parts) >= 3:
            return _dists_target(parts, known)
        if parts[0] == 'pool':
            return Target('pool')
        return Target('other')
    directory = '/'.join(parts[:-1])
    if name in RELEASE_FILES:
        return Target('release', suite_dir=directory, rel=name)
    if name.startswith(INDEX_PREFIXES):
        return Target('index', suite_dir=directory, rel=name)
    if name.endswith(('.deb', '.udeb', '.ddeb', '.dsc')) or '.orig.' in name or '.debian.' in name:
        return Target('pool')
    return Target('other')


@dataclass(frozen=True)
class PackagesTarget:
    suite: str
    component: str
    arch: str
    suffix: str


def packages_target(suite_dir: str, rel: str) -> PackagesTarget | None:
    """``<component...>/binary-<arch>/Packages[.ext]`` (or flat ``Packages[.ext]``) → what it lists."""
    directory, name = posixpath.split(rel)
    base, dot, ext = name.partition('.')
    if base != 'Packages':
        return None
    suffix = deb822.compression_of(name)
    if suffix is None or (dot and f'.{ext}' != suffix):
        return None
    suite = suite_dir.split('/', 1)[1] if suite_dir.startswith('dists/') else suite_dir
    comp_dir, _, arch_dir = directory.rpartition('/')
    if directory and not arch_dir.startswith('binary-'):
        return None
    return PackagesTarget(suite=suite, component=comp_dir, arch=arch_dir.removeprefix('binary-'), suffix=suffix)


def _validate_release(data: bytes, info: UpstreamInfo) -> None:
    try:
        deb822.parse_release(data)
    except ValidationFailed as exc:  # the upstream's fault: 502, nothing cached
        raise UpstreamError(f'upstream Release is invalid: {exc.detail}') from None


class AptPlugin:
    descriptor = DESCRIPTOR

    def __init__(self) -> None:
        self._parsed: OrderedDict[str, deb822.Release] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._indexing: set[tuple[str, str]] = set()
        self.browser = AptBrowse(self)

    async def handle(self, req: FormatRequest) -> Response:
        if req.method not in ('GET', 'HEAD'):
            return native_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD'})
        if not req.path:
            raise NotFound('Not Found')
        opts = AptOptions.model_validate(req.repo.format_options)
        target = classify(req.path, flat=opts.flat, known=await self._known_suites(req, opts.flat))
        match target.kind:
            case 'release':
                return await req.proxy.serve(req, self._release_spec(_join(target.suite_dir, target.rel)))
            case 'index':
                return await self._serve_index(req, target)
            case 'by-hash':
                return await self._serve_by_hash(req, target)
            case 'pool':
                return await self._serve_pool(req)
            case 'other':
                return await req.proxy.serve(req, AssetSpec(path=req.path, kind=AssetKind.METADATA))
        raise NotFound('Not Found')

    @staticmethod
    async def _known_suites(req: FormatRequest, flat: bool) -> set[str]:
        """The cached suites an index/by-hash path could belong to (one indexed query; none when the path leaves
        no choice)."""
        segments = suite_segments(req.path, flat=flat)
        if segments is None or len(segments) < 2:
            return set()
        return await apt_db.known_suites(req.proxy.db, req.repo.id, suites.candidates(segments))

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ------------------------------------------------------------------ Release
    @staticmethod
    def _release_spec(path: str, *, max_age_s: int | None = None) -> AssetSpec:
        signed = path.rsplit('/', 1)[-1] in SIGNED_RELEASES
        return AssetSpec(
            path=path,
            kind=AssetKind.METADATA,
            max_size=deb822.RELEASE_MAX_BYTES if signed else RELEASE_GPG_MAX,
            max_age_s=max_age_s,
            validate=_validate_release if signed else None,
        )

    async def parse_release(self, engine: ProxyEngine, asset: StoredAsset) -> deb822.Release:
        """A cached ``Release``/``InRelease`` parsed (in a thread, bounded; kept in a small cache by digest)."""
        return await self._parse(engine, asset)

    async def _parse(self, engine: ProxyEngine, asset: StoredAsset) -> deb822.Release:
        cached = self._parsed.get(asset.blob_sha256)
        if cached is not None:
            self._parsed.move_to_end(asset.blob_sha256)
            return cached
        data = await engine.read(asset, max_bytes=deb822.RELEASE_MAX_BYTES)
        release = await asyncio.to_thread(deb822.parse_release, data)
        self._parsed[asset.blob_sha256] = release
        while len(self._parsed) > PARSED_CACHE:
            self._parsed.popitem(last=False)
        return release

    async def _current_release(self, req: FormatRequest, suite_dir: str, *, refresh: bool = False) -> deb822.Release:
        """The ``Release`` the suite is served with now: the cached ``InRelease``, else the cached ``Release``, else
        fetched. ``refresh`` revalidates it upstream first."""
        engine = req.proxy
        if not refresh:
            for name in SIGNED_RELEASES:
                asset = await assets_db.get(engine.db, req.repo.id, _join(suite_dir, name))
                if asset is not None and await engine.blobs.exists(asset.blob_sha256):
                    return await self._parse(engine, asset)
        for name in SIGNED_RELEASES:
            spec = self._release_spec(_join(suite_dir, name), max_age_s=0 if refresh else FOREVER)
            try:
                asset = await engine.ensure(req, spec)
            except NotFound:
                continue
            return await self._parse(engine, asset)
        raise NotFound('Not Found')

    # ------------------------------------------------------------------ indexes
    def _index_spec(
        self, req: FormatRequest, suite_dir: str, rel: str, entry: deb822.IndexEntry, release: deb822.Release
    ) -> AssetSpec:
        key = _join(suite_dir, posixpath.dirname(rel), 'by-hash', 'SHA256', entry.sha256)
        return AssetSpec(
            path=key,
            kind=AssetKind.METADATA,  # buffered: verified before the first byte, so a mismatch can be retried
            upstream_path=key if release.by_hash else _join(suite_dir, rel),
            upstream_headers={'Accept-Encoding': 'identity'},  # the bytes are the file (.gz/.xz), never decoded
            expected=Expected(sha256=entry.sha256, size=entry.size),
            content_type='application/octet-stream',
            max_size=INDEX_MAX,
            max_age_s=FOREVER,
            head_passthrough=False,
            on_stored=self._on_index_stored(req, suite_dir, rel),
            artifact=rel,
        )

    async def _serve_index(self, req: FormatRequest, target: Target) -> Response:
        release = await self._current_release(req, target.suite_dir)
        entry = release.files.get(target.rel)
        if entry is None:
            raise NotFound('Not Found')
        try:
            return await req.proxy.serve(req, self._index_spec(req, target.suite_dir, target.rel, entry, release))
        except DigestMismatch:
            log.info('index does not match the Release; revalidating it', extra={'path': req.path})
        release = await self._current_release(req, target.suite_dir, refresh=True)
        retry = release.files.get(target.rel)
        if retry is None:
            raise NotFound('Not Found')
        if retry == entry:
            raise DigestMismatch('index does not match the Release')
        return await req.proxy.serve(req, self._index_spec(req, target.suite_dir, target.rel, retry, release))

    async def _serve_by_hash(self, req: FormatRequest, target: Target) -> Response:
        expected = Expected(sha256=target.digest) if target.algorithm == 'sha256' else Expected(sha512=target.digest)
        spec = AssetSpec(
            path=req.path,
            kind=AssetKind.CONTENT,
            upstream_headers={'Accept-Encoding': 'identity'},
            expected=expected,
            content_type='application/octet-stream',
            max_size=INDEX_MAX,
            max_age_s=FOREVER,
            on_stored=self._on_by_hash_stored(req, target),
        )
        return await req.proxy.serve(req, spec)

    def _on_index_stored(self, req: FormatRequest, suite_dir: str, rel: str) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            self._schedule_packages(req, asset, data, suite_dir, rel)

        return stored

    def _on_by_hash_stored(self, req: FormatRequest, target: Target) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            try:
                release = await self._current_release(req, target.suite_dir)
            except Exception as exc:  # no Release: nothing to index against
                log.info('no Release for a by-hash file', extra={'path': asset.path, 'error': type(exc).__name__})
                return
            rel = release.path_for(target.algorithm, target.digest)
            if rel is not None:
                self._schedule_packages(req, asset, data, target.suite_dir, rel)

        return stored

    def _schedule_packages(
        self, req: FormatRequest, asset: StoredAsset, data: bytes | None, suite_dir: str, rel: str
    ) -> None:
        what = packages_target(suite_dir, rel)
        if what is None:
            return
        key = (req.repo.id, asset.blob_sha256)
        if key in self._indexing:
            return
        self._indexing.add(key)
        task = asyncio.create_task(self._index_packages(req.proxy, req.repo.id, asset, data, what))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: self._indexing.discard(key))

    async def _index_packages(
        self, engine: ProxyEngine, repo_id: str, asset: StoredAsset, data: bytes | None, what: PackagesTarget
    ) -> None:
        try:
            raw = data if data is not None else await engine.read(asset, max_bytes=INDEX_MAX)
            entries = await asyncio.to_thread(deb822.parse_packages_file, raw, what.suffix)
            listings = [apt_db.Listing(e.filename, e.sha256, e.size) for e in entries]
            mismatched = await apt_db.record_listing(
                engine.db, repo_id, listings, suite=what.suite, component=what.component, arch=what.arch
            )
            for filename in mismatched:
                log.warning('cached pool file does not match its listing; dropped', extra={'path': filename})
            stanzas = [
                apt_db.Stanza(e.filename, e.sha256, e.size, e.package, e.version, e.architecture, e.section)
                for e in entries
            ]
            await apt_db.record_packages(
                engine.db,
                repo_id,
                stanzas,
                suite=browse_suite(what.suite),
                component=what.component,
                arch=what.arch,
                gen=asset.blob_sha256,
            )
            log.info('indexed Packages', extra={'path': asset.path, 'entries': len(listings)})
        except Exception as exc:
            log.warning('Packages indexing failed', extra={'path': asset.path, 'error': type(exc).__name__})

    # ------------------------------------------------------------------ pool
    async def _serve_pool(self, req: FormatRequest) -> Response:
        engine = req.proxy
        record = await apt_db.lookup(engine.db, req.repo.id, req.path)
        if record is not None and record.verified:
            spec = AssetSpec(
                path=req.path, kind=AssetKind.CONTENT, expected=Expected(sha256=record.sha256, size=record.size)
            )
            return await engine.serve(req, spec)
        repo_id = req.repo.id

        async def unverified(asset: StoredAsset, data: bytes | None) -> None:
            listed = await apt_db.record_unverified(engine.db, repo_id, asset.path, asset.blob_sha256, asset.size)
            if listed is not None and (listed.sha256 != asset.blob_sha256 or listed.size != asset.size):
                log.warning('pool file does not match its listing; dropped', extra={'path': asset.path})
                await apt_db.drop_cached(engine.db, repo_id, asset.path)

        log.info('pool file not listed in any cached Packages; serving unverified', extra={'path': req.path})
        return await engine.serve(req, AssetSpec(path=req.path, kind=AssetKind.CONTENT, on_stored=unverified))
