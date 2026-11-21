"""apt proxy refresh (job ``apt.refresh``; FORMATS.md "apt"; ARCHITECTURE §6.2, §6.4, §10).

The apt proxy indexes a ``Packages`` file when a client fetches it, and validates pool files against those listings.
That leaves gaps only a background pass can close: a pool file fetched before any listing named it stays
``unverified`` until a client happens to download a ``Packages`` file listing it (apt skips indexes it already has),
a ``Packages`` file whose background indexing was lost (process stopped) is never indexed, and a suite's listings
age until a client asks again. Per apt proxy repository the job:

1. **Release**: for every suite with a cached ``InRelease``/``Release`` (flat repositories: every directory with
   one) whose copy is older than ``metadata_max_age_s``, revalidates the suite's cached ``InRelease``, ``Release``
   and ``Release.gpg`` upstream (conditional ``GET``; ``304`` only moves ``fetched_at``). New ``InRelease``/``Release``
   bytes must parse (bounded, ``deb822``) before they replace the cached copy; bytes are never changed (clients
   verify the signature). Skipped for offline repositories and while the auto-block breaker blocks the upstream.
2. **Packages**: for the suite's current ``Release`` (the cached ``InRelease``, else ``Release``), each
   ``(component, arch)`` ``Packages`` index it lists is
   - indexed from the cache when one of its variants is cached under the listed hash but that generation is not the
     recorded listing yet (``apt_pool_index`` + ``apt_packages``);
   - else fetched when the upstream may be called and either clients used that index before (its
     ``(component, arch)`` has a listing in ``apt_packages`` for the suite) or it could list an unverified pool file
     (``pool/<component>/...`` with that architecture, or ``all``; only until no such file is left): one variant
     (``.xz``, else ``.gz``, ...), from ``by-hash`` when the ``Release`` says ``Acquire-By-Hash: yes``, verified
     against the listed SHA256 and size while it streams into the blob store (nothing unverified is committed),
     stored under its ``by-hash/SHA256/<h>`` key exactly as a client fetch would, then indexed. Other indexes are
     never fetched (the cache does not grow on its own).
3. **Pool files**: every ``unverified`` entry is checked against the recorded listings (``apt_packages``): a match
   becomes ``verified``; a listing that disagrees replaces the entry and the cached copy that was checked is dropped
   (the next request fetches and validates it); unlisted entries stay unverified.

Idempotent: a second run finds the releases fresh, the indexes indexed and nothing left to verify, and calls no
upstream. Safe alongside the server: index blobs are content-addressed (a concurrent client fill stores the same
bytes), ``Release`` replacement is what a server revalidation does, and pool-index updates are compare-and-set on
the unverified hash. Not coalesced with server fills (ARCH §6.3): at worst one extra upstream request.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from waystation_common.apt import deb822, suites
from waystation_common.apt.indexes import (
    INDEX_MAX,
    RELEASE_GPG,
    RELEASE_GPG_MAX,
    SIGNED_RELEASES,
    PackagesTarget,
    browse_suite,
    by_hash_key,
    join,
    packages_target,
)
from waystation_common.db import apt as apt_db
from waystation_common.db.client import Database, Document
from waystation_common.errors import WaystationError
from waystation_common.storage import BlobStore
from waystation_worker.upstream import Fetcher, Upstream, blocked, copy_bounded, read_bounded

log = logging.getLogger('waystation.worker.apt')

DEFAULT_METADATA_MAX_AGE_S = 1800
SUFFIX_PREFERENCE = ('.xz', '.gz', '.bz2', '.lzma', '.zst', '')
RELEASE_FILES = (*SIGNED_RELEASES, RELEASE_GPG)

LogLine = Callable[[str], Awaitable[None]]


async def _no_log(line: str) -> None:
    return None


@dataclass
class RefreshResult:
    repo_id: str
    name: str = ''
    skipped: str | None = None
    upstream: bool = True  # False: offline / blocked: only local work was done
    suites: int = 0
    releases_checked: int = 0
    releases_changed: int = 0
    indexes_fetched: int = 0
    indexes_indexed: int = 0
    verified: int = 0
    dropped: int = 0
    unverified: int = 0  # left unverified (no listing names them)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            'repo_id': self.repo_id,
            'name': self.name,
            'suites': self.suites,
            'releases_checked': self.releases_checked,
            'releases_changed': self.releases_changed,
            'indexes_fetched': self.indexes_fetched,
            'indexes_indexed': self.indexes_indexed,
            'verified': self.verified,
            'dropped': self.dropped,
            'unverified': self.unverified,
            'upstream': self.upstream,
        }
        if self.skipped:
            out['skipped'] = self.skipped
        if self.errors:
            out['errors'] = self.errors[:20]
        return out

    def summary(self) -> str:
        if self.skipped:
            return f'{self.name or self.repo_id}: skipped ({self.skipped})'
        return (
            f'{self.name}: {self.suites} suites, {self.releases_checked} releases checked '
            f'({self.releases_changed} changed), {self.indexes_fetched} indexes fetched, '
            f'{self.indexes_indexed} indexed, {self.verified} pool files verified, {self.dropped} dropped, '
            f'{self.unverified} still unverified' + (f', {len(self.errors)} errors' if self.errors else '')
        )


def _age_s(doc: Document, now: datetime) -> float:
    upstream = doc.get('upstream') or {}
    fetched = upstream.get('fetched_at')
    if not isinstance(fetched, datetime):
        return float('inf')
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    return (now - fetched).total_seconds()


def suites_of(release_docs: list[Document], *, flat: bool) -> dict[str, dict[str, Document]]:
    """Cached release files grouped by suite directory (``dists/<suite>``; flat: the directory, ``''`` = root)."""
    out: dict[str, dict[str, Document]] = {}
    for doc in release_docs:
        path = str(doc['path'])
        directory, name = posixpath.split(path)
        if name not in RELEASE_FILES:
            continue
        if flat:
            suite_dir: str | None = directory
        else:
            suite_dir = suites.release_suite_dir(path)
        if suite_dir is None:
            continue
        out.setdefault(suite_dir, {})[name] = doc
    return out


def packages_lists(release: deb822.Release, suite_dir: str) -> dict[tuple[str, str], list[tuple[str, PackagesTarget]]]:
    """The ``Packages`` variants a ``Release`` lists, per ``(component, arch)``, most preferred first."""
    groups: dict[tuple[str, str], list[tuple[str, PackagesTarget]]] = {}
    for rel in release.files:
        target = packages_target(suite_dir, rel)
        if target is not None:
            groups.setdefault((target.component, target.arch), []).append((rel, target))
    for variants in groups.values():
        variants.sort(key=lambda v: SUFFIX_PREFERENCE.index(v[1].suffix))
    return groups


BINARY_SUFFIXES = ('.deb', '.udeb', '.ddeb')


def binary_arch(filename: str) -> str | None:
    """The architecture of a binary package under ``pool/`` from its Debian file name
    (``pool/main/h/hello/hello_2.10-5_amd64.deb`` → ``amd64``); None for anything else."""
    if not filename.startswith('pool/'):
        return None
    name = posixpath.basename(filename)
    suffix = next((s for s in BINARY_SUFFIXES if name.endswith(s)), None)
    if suffix is None:
        return None
    stem = name.removesuffix(suffix)
    parts = stem.split('_')
    if len(parts) < 3 or not parts[-1]:
        return None
    return parts[-1]


def would_list(pair: tuple[str, str], pending: list[tuple[str, str]]) -> bool:
    """Whether the ``Packages`` index of ``(component, arch)`` could list one of the ``pending`` unverified pool
    files: same component directory under ``pool/``, same architecture (``all`` packages are in every one)."""
    component, arch = pair
    prefix = f'pool/{component}/'
    return any(filename.startswith(prefix) and file_arch in (arch, 'all') for filename, file_arch in pending)


class Refresher:
    def __init__(
        self,
        db: Database,
        blobs: BlobStore,
        fetcher: Fetcher,
        *,
        valkey: Redis | None = None,
        now: datetime,
        log_line: LogLine = _no_log,
    ) -> None:
        self.db = db
        self.blobs = blobs
        self.fetcher = fetcher
        self.valkey = valkey
        self.now = now
        self.log_line = log_line

    async def _read_blob(self, sha256: str, size: int, max_bytes: int) -> bytes:
        if size > max_bytes:
            raise WaystationError(f'cached file exceeds {max_bytes} bytes')
        data = await asyncio.to_thread(self.blobs.blob_path(sha256).read_bytes)
        if len(data) > max_bytes:
            raise WaystationError(f'cached file exceeds {max_bytes} bytes')
        return data

    # ------------------------------------------------------------------ one repository
    async def refresh(self, repo: Document) -> RefreshResult:
        repo_id = str(repo['_id'])
        result = RefreshResult(repo_id=repo_id, name=str(repo.get('name') or ''))
        if repo.get('format') != 'apt' or repo.get('type') != 'proxy' or not repo.get('proxy'):
            result.skipped = 'not an apt proxy repository'
            return result
        proxy: dict[str, Any] = repo['proxy']
        flat = bool((repo.get('format_options') or {}).get('flat'))
        max_age = int(proxy.get('metadata_max_age_s') or DEFAULT_METADATA_MAX_AGE_S)
        upstream = Upstream.of(repo)
        if not repo.get('online', True) or proxy.get('offline'):
            result.upstream = False
        elif await blocked(self.valkey, repo_id, now_ms=int(self.now.timestamp() * 1000)):
            result.upstream = False
            await self.log_line(f'{result.name}: the upstream is auto-blocked; refreshing from the cache only')

        grouped = suites_of(await apt_db.release_assets(self.db, repo_id), flat=flat)
        result.suites = len(grouped)
        pending = await self._pending(repo_id)
        for suite_dir, files in sorted(grouped.items()):
            try:
                await self._suite(repo_id, upstream, suite_dir, files, max_age, pending, result)
            except WaystationError as exc:
                result.errors.append(f'{suite_dir or "."}: {exc.detail}')
            except Exception as exc:  # one suite never stops the others
                log.warning('apt refresh of a suite failed', extra={'repo': repo_id, 'error': repr(exc)})
                result.errors.append(f'{suite_dir or "."}: {type(exc).__name__}')
        await self._reverify(repo_id, result)
        return result

    async def _suite(
        self,
        repo_id: str,
        upstream: Upstream,
        suite_dir: str,
        files: dict[str, Document],
        max_age: int,
        pending: list[tuple[str, str]],
        result: RefreshResult,
    ) -> None:
        primary_name = next((n for n in SIGNED_RELEASES if n in files), None)
        if primary_name is None:
            return  # only a Release.gpg: nothing to index against
        if result.upstream and _age_s(files[primary_name], self.now) >= max_age:
            for name in RELEASE_FILES:
                if name in files:
                    result.releases_checked += 1
                    if await self._revalidate(repo_id, upstream, files[name], result):
                        result.releases_changed += 1
            fresh = await apt_db.get_asset(self.db, repo_id, join(suite_dir, primary_name))
            if fresh is not None:
                files[primary_name] = fresh
        primary = files[primary_name]
        data = await self._read_blob(str(primary['blob_sha256']), int(primary['size']), deb822.RELEASE_MAX_BYTES)
        release = await asyncio.to_thread(deb822.parse_release, data)
        groups = packages_lists(release, suite_dir)
        if not groups:
            return
        keys = {
            rel: by_hash_key(suite_dir, rel, release.files[rel].sha256)
            for variants in groups.values()
            for rel, _ in variants
        }
        cached = await apt_db.existing_paths(self.db, repo_id, list(keys.values()))
        suite_name = next(iter(groups.values()))[0][1].suite
        used = await apt_db.indexed_lists(self.db, repo_id, browse_suite(suite_name))
        for pair, variants in sorted(groups.items()):
            hit = next(((rel, t) for rel, t in variants if keys[rel] in cached), None)
            if hit is not None:
                rel, target = hit
                entry = release.files[rel]
                if not await apt_db.is_indexed(self.db, repo_id, target, gen=entry.sha256):
                    raw = await self._read_blob(entry.sha256, entry.size, INDEX_MAX)
                    await self._index(repo_id, raw, target, entry.sha256, keys[rel], result)
                    pending[:] = await self._pending(repo_id) if pending else []
                continue
            if not result.upstream or not (pair in used or would_list(pair, pending)):
                continue
            rel, target = variants[0]
            try:
                raw = await self._fetch_index(repo_id, upstream, suite_dir, rel, release, keys[rel])
            except WaystationError as exc:
                result.errors.append(f'{join(suite_dir, rel)}: {exc.detail}')
                continue
            result.indexes_fetched += 1
            await self._index(repo_id, raw, target, release.files[rel].sha256, keys[rel], result)
            pending[:] = await self._pending(repo_id) if pending else []

    async def _pending(self, repo_id: str) -> list[tuple[str, str]]:
        """``(filename, arch)`` of the unverified binary packages under ``pool/`` (what a ``Packages`` index of the
        matching component and architecture would list)."""
        out: list[tuple[str, str]] = []
        async for batch in apt_db.unverified(self.db, repo_id):
            out += [(r.filename, arch) for r in batch if (arch := binary_arch(r.filename)) is not None]
        return out

    async def _index(
        self, repo_id: str, raw: bytes, target: PackagesTarget, gen: str, path: str, result: RefreshResult
    ) -> None:
        indexed = await apt_db.index_packages(self.db, repo_id, raw, target, gen=gen)
        result.indexes_indexed += 1
        result.dropped += len(indexed.dropped)
        for filename in indexed.dropped:
            await self.log_line(f'{result.name}: {filename} does not match its listing; dropped from the cache')
        log.info('indexed Packages', extra={'repo': repo_id, 'path': path, 'entries': indexed.entries})

    # ------------------------------------------------------------------ upstream
    async def _revalidate(self, repo_id: str, upstream: Upstream, doc: Document, result: RefreshResult) -> bool:
        """Conditional ``GET`` of one cached release file; True when its content changed."""
        path = str(doc['path'])
        name = posixpath.basename(path)
        cached_up: dict[str, Any] = doc.get('upstream') or {}
        headers: dict[str, str] = {}
        if cached_up.get('etag'):
            headers['If-None-Match'] = str(cached_up['etag'])
        if cached_up.get('last_modified'):
            headers['If-Modified-Since'] = str(cached_up['last_modified'])
        max_bytes = RELEASE_GPG_MAX if name == RELEASE_GPG else deb822.RELEASE_MAX_BYTES
        try:
            async with self.fetcher.get(upstream, path, headers=headers) as response:
                fetched = apt_db.Fetched(
                    url=str(response.url),
                    etag=response.headers.get('etag'),
                    last_modified=response.headers.get('last-modified'),
                    status=response.status_code,
                )
                if response.status_code == 304:
                    await apt_db.mark_confirmed(self.db, str(doc['_id']), fetched, self.now)
                    return False
                if response.status_code != 200:
                    result.errors.append(f'{path}: upstream answered {response.status_code}')
                    return False
                data = await read_bounded(response, max_bytes)
                content_type = response.headers.get('content-type') or 'application/octet-stream'
        except WaystationError as exc:
            result.errors.append(f'{path}: {exc.detail}')
            return False
        if name in SIGNED_RELEASES:
            try:
                await asyncio.to_thread(deb822.parse_release, data)
            except WaystationError as exc:
                result.errors.append(f'{path}: upstream Release is invalid: {exc.detail}')
                return False
        async with await self.blobs.open_writer(max_size=max_bytes, expected_size=len(data)) as writer:
            await writer.write(data)
            ref = await self.blobs.commit(writer)
        await apt_db.save_asset(
            self.db,
            repo_id,
            path,
            sha256=ref.sha256,
            size=ref.size,
            content_type=content_type,
            fetched=fetched,
            now=self.now,
        )
        return ref.sha256 != doc.get('blob_sha256')

    async def _fetch_index(
        self, repo_id: str, upstream: Upstream, suite_dir: str, rel: str, release: deb822.Release, key: str
    ) -> bytes:
        """Fetch one index file the ``Release`` lists, verified against its SHA256 and size, and store it under its
        ``by-hash`` key. Returns its bytes (for indexing)."""
        entry = release.files[rel]
        if entry.size > INDEX_MAX:
            raise WaystationError(f'index listed with {entry.size} bytes, more than {INDEX_MAX}')
        upstream_path = key if release.by_hash else join(suite_dir, rel)
        async with self.fetcher.get(upstream, upstream_path) as response:
            if response.status_code != 200:
                raise WaystationError(f'upstream answered {response.status_code}')
            fetched = apt_db.Fetched(
                url=str(response.url),
                etag=response.headers.get('etag'),
                last_modified=response.headers.get('last-modified'),
                status=response.status_code,
            )
            async with await self.blobs.open_writer(
                max_size=INDEX_MAX, expected_size=entry.size, expected_sha256=entry.sha256
            ) as writer:
                await copy_bounded(response, writer)
                ref = await self.blobs.commit(writer)  # DigestMismatch unless sha256 and size match
        await apt_db.save_asset(
            self.db,
            repo_id,
            key,
            sha256=ref.sha256,
            size=ref.size,
            content_type='application/octet-stream',
            fetched=fetched,
            now=self.now,
        )
        await self.log_line(f'fetched {join(suite_dir, rel)} ({ref.size} bytes, verified)')
        return await self._read_blob(ref.sha256, ref.size, INDEX_MAX)

    # ------------------------------------------------------------------ pool files
    async def _reverify(self, repo_id: str, result: RefreshResult) -> None:
        async for batch in apt_db.unverified(self.db, repo_id):
            listings = await apt_db.listings_of(self.db, repo_id, [r.filename for r in batch])
            for record in batch:
                listing = listings.get(record.filename)
                if listing is None:
                    result.unverified += 1
                elif (listing.sha256, listing.size) == (record.sha256, record.size):
                    if await apt_db.mark_verified(self.db, repo_id, record, listing):
                        result.verified += 1
                elif await apt_db.replace_unverified(self.db, repo_id, record, listing):
                    result.dropped += 1
                    await self.log_line(
                        f'{result.name}: {record.filename} does not match its listing; dropped from the cache'
                    )


ProgressFn = Callable[[int, int | None, str | None], Awaitable[None]]


async def run(
    db: Database,
    blobs: BlobStore,
    fetcher: Fetcher,
    repo_id: str | None,
    *,
    valkey: Redis | None = None,
    now: datetime,
    log_line: LogLine = _no_log,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Refresh one apt proxy repository (``repo_id``) or every one (None)."""
    refresher = Refresher(db, blobs, fetcher, valkey=valkey, now=now, log_line=log_line)
    if repo_id is not None:
        repo = await apt_db.repository(db, repo_id)
        if repo is None:
            return {'repo_id': repo_id, 'skipped': 'the repository was deleted'}
        repos = [repo]
    else:
        repos = await apt_db.apt_proxies(db)
    results: list[dict[str, Any]] = []
    for i, repo in enumerate(repos):
        if progress is not None:
            await progress(i, len(repos), str(repo.get('name') or ''))
        outcome = await refresher.refresh(repo)
        await log_line(outcome.summary())
        results.append(outcome.as_dict())
    if progress is not None:
        await progress(len(repos), len(repos), None)
    if repo_id is not None:
        return results[0]
    return {
        'repositories': len(results),
        'verified': sum(r.get('verified', 0) for r in results),
        'dropped': sum(r.get('dropped', 0) for r in results),
        'indexes_fetched': sum(r.get('indexes_fetched', 0) for r in results),
        'errors': sum(len(r.get('errors', [])) for r in results),
        'results': results[:50],
    }
