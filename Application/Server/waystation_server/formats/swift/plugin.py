"""Swift Package Registry repositories (T-P5-SWIFT; FORMATS.md "Swift Package Registry"; SE-0292, SE-0391).

Routes under ``/repository/<repo>/`` (SE-0292 §4):

- ``<scope>/<name>`` (``.json`` optional): the release list (removed releases carry a ``410`` problem), ``Link``
  ``latest-version`` and ``canonical`` (the source repository);
- ``<scope>/<name>/<version>`` (``.json`` optional): the release (``resources[source-archive].checksum`` and
  signature, ``metadata``, ``publishedAt``; ``Link`` latest/successor/predecessor); ``PUT`` publishes, ``DELETE``
  removes (hosted, ``formats.swift.hosted``);
- ``<scope>/<name>/<version>/Package.swift`` (``?swift-version=`` → that ``Package@swift-<v>.swift`` or ``303`` to
  the unqualified manifest; ``Link`` alternates for every version-specific manifest);
- ``<scope>/<name>/<version>.zip``: the source archive (``Digest``, ``Content-Disposition``, signature headers);
- ``identifiers?url=``: the package identifiers of a source repository URL;
- ``POST login`` (or the repository URL itself): ``swift package-registry login`` (200 for valid credentials).

Every answer carries ``Content-Version: 1``; errors are problem details. An ``Accept`` asking for another API version
is refused (``400`` invalid, ``415`` unsupported). Files are served by X-Accel like every other format.

**SCM-backed proxy**: the upstream URL is a Git host; ``<scope>.<name>`` is the Git repository
``<upstream>/<owner>/<name>`` (owner = the scope, or the ``scopes`` option's mapping; ``mapped_scopes_only`` makes the
mapping an allowlist). Releases are the repository's SemVer tags, read from the smart-HTTP ref advertisement
(cached as metadata, revalidated after the metadata max age). The first request for a release's information,
manifest or archive fetches the tag's tarball (cached content, size-bounded, SSRF-guarded like every upstream fetch)
and repackages it **deterministically** in a thread (``archive.build_from_tarball``); the archive's checksum is then
pinned in ``swift_releases`` and never changes: should the archive have to be rebuilt (its files gone), a different
result is refused (``502``) instead of served. Concurrent first requests in one process build once; across
processes the release record's unique index decides and the bytes are identical anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Hashable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from starlette.responses import Response

from waystation_common.errors import NotFound, UpstreamError, WaystationError
from waystation_common.events import CacheState
from waystation_common.ids import new_id
from waystation_common.storage import BlobRef, BlobStore
from waystation_common.swift import PackageId, valid_name, valid_scope, valid_version, version_of_tag
from waystation_common.versionsort import version_sort
from waystation_server.db import swift as swift_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.swift import api, archive, gitrefs
from waystation_server.formats.swift.browse import SwiftBrowse
from waystation_server.formats.swift.descriptor import DESCRIPTOR, SwiftOptions, options_of
from waystation_server.formats.swift.hosted import MAX_ARCHIVE_BYTES, SwiftHosted, record_release_files
from waystation_server.problems import BASIC_CHALLENGE
from waystation_server.proxy.engine import AssetKind, AssetSpec, StoredAsset, UpstreamInfo
from waystation_server.repos.model import RepoType

log = logging.getLogger('waystation.formats.swift')

MAX_REFS = 16 * 1024 * 1024
MAX_SOURCE = 256 * 1024 * 1024
PARSED_KEPT = 512
REFUSED_KEPT = 256
CHUNK = 1024 * 1024
GIT_MISSING = frozenset({401, 404, 410})  # Git hosts answer 401 for repositories that do not exist
_SWIFT_VERSION = re.compile(r'^\d{1,4}(?:\.\d{1,4}){0,2}$')

Tags = dict[str, tuple[str, str]]  # version -> (tag, commit)


@dataclass(frozen=True)
class Route:
    kind: str  # root | login | identifiers | releases | release | manifest | archive
    scope: str = ''
    name: str = ''
    version: str = ''

    @property
    def package(self) -> PackageId:
        return PackageId(self.scope, self.name)


def parse_route(path: str) -> Route | None:
    seg = path.split('/') if path else []
    if not seg:
        return Route('root')
    if seg == ['login']:
        return Route('login')
    if seg == ['identifiers']:
        return Route('identifiers')
    if len(seg) < 2 or not valid_scope(seg[0]):
        return None
    scope, name = seg[0], seg[1]
    if len(seg) == 2:
        name = name.removesuffix('.json')
        return Route('releases', scope, name) if valid_name(name) else None
    if not valid_name(name):
        return None
    third = seg[2]
    if len(seg) == 3:
        if valid_version(third):
            return Route('release', scope, name, third)
        for suffix, kind in (('.zip', 'archive'), ('.json', 'release')):
            stem = third.removesuffix(suffix)
            if third.endswith(suffix) and valid_version(stem):
                return Route(kind, scope, name, stem)
        return None
    if len(seg) == 4 and seg[3] == 'Package.swift' and valid_version(third):
        return Route('manifest', scope, name, third)
    return None


def map_tags(tags: dict[str, str]) -> Tags:
    """Versions of a repository's tags. When several tags name one version, ``1.2.3`` wins over ``v1.2.3`` over
    ``1.2``/``v1.2``."""
    ranked: dict[str, tuple[int, str, str]] = {}
    for tag, commit in tags.items():
        version = version_of_tag(tag)
        if version is None:
            continue
        rank = 0 if tag == version else 1 if tag == f'v{version}' else 2
        found = ranked.get(version)
        if found is None or (rank, tag) < found[:2]:
            ranked[version] = (rank, tag, commit)
    return {v: (tag, commit) for v, (_rank, tag, commit) in ranked.items()}


def _sorted(versions: dict[str, bool]) -> list[tuple[str, bool]]:
    return sorted(versions.items(), key=lambda item: version_sort('swift', item[0]), reverse=True)


class _Locks:
    """One asyncio lock per key while anyone holds or waits for it."""

    def __init__(self) -> None:
        self._locks: dict[Hashable, tuple[asyncio.Lock, int]] = {}

    @contextlib.asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


async def store_file(blobs: BlobStore, path: Path, max_size: int) -> BlobRef:
    """Stream a local file (a built archive) into the blob store."""
    writer = await blobs.open_writer(max_size=max_size)
    try:
        handle = await asyncio.to_thread(path.open, 'rb')
        try:
            while chunk := await asyncio.to_thread(handle.read, CHUNK):
                await writer.write(chunk)
        finally:
            await asyncio.to_thread(handle.close)
        return await blobs.commit(writer)
    finally:
        if not writer.closed:
            await writer.abort()


class SwiftPlugin:
    descriptor = DESCRIPTOR

    def __init__(self) -> None:
        self.browser = SwiftBrowse()
        self.hosted = SwiftHosted()
        self._locks = _Locks()
        self._parsed: OrderedDict[str, Tags] = OrderedDict()
        self._refused: OrderedDict[str, str] = OrderedDict()  # tarball sha256 -> why it is not a package

    # ------------------------------------------------------------------ dispatch
    async def handle(self, req: FormatRequest) -> Response:
        try:
            return await self._handle(req)
        except api.Problem as exc:
            return api.problem(exc.status, exc.detail, headers=exc.headers)
        except WaystationError as exc:
            headers = {'WWW-Authenticate': BASIC_CHALLENGE} if exc.status == 401 else {}
            detail = 'not found' if isinstance(exc, NotFound) else exc.detail
            return api.problem(exc.status, detail, headers=headers)

    async def _handle(self, req: FormatRequest) -> Response:
        route = parse_route(req.path)
        if route is None:
            raise api.Problem(404, 'not found')
        api.check_accept(req.request.headers.get('accept'))
        req.log(op=route.kind)
        method = req.method
        if route.kind in ('root', 'login'):
            if method == 'POST':
                return self._login(req)
            if route.kind == 'root':
                raise api.Problem(404, 'not found')
            raise api.Problem(405, 'sign in with POST', headers={'Allow': 'POST'})
        if route.kind == 'release' and method in ('PUT', 'DELETE'):
            return await self._write(req, route)
        if method not in ('GET', 'HEAD'):
            allow = 'GET, HEAD, PUT, DELETE' if route.kind == 'release' else 'GET, HEAD'
            raise api.Problem(405, 'method not allowed', headers={'Allow': allow})
        urls = api.Urls(req.repo_url)
        started = time.perf_counter()
        if route.kind == 'identifiers':
            return await self._identifiers(req, started)
        if route.kind == 'releases':
            return await self._releases(req, urls, route.package, started)
        if route.kind == 'release':
            return await self._release_info(req, urls, route.package, route.version, started)
        if route.kind == 'manifest':
            return await self._manifest(req, urls, route.package, route.version)
        return await self._archive(req, route.package, route.version)

    def _json(
        self, req: FormatRequest, doc: dict[str, Any], started: float, *, headers: dict[str, str] | None = None
    ) -> Response:
        """A JSON document built per request: ``ETag``, conditional ``304``, ``HEAD``."""
        body = api.dump(doc)
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        out = {**api.VERSION_HEADERS, 'ETag': etag, 'Cache-Control': 'no-cache', **(headers or {})}
        match = req.request.headers.get('if-none-match')
        if match and etag in {m.strip() for m in match.split(',')}:
            req.hosted.account(req, CacheState.HIT, 304, 0, started)
            return Response(status_code=304, headers=out)
        sent = 0 if req.is_head else len(body)
        req.hosted.account(req, CacheState.HIT, 200, sent, started)
        if req.is_head:
            out['Content-Length'] = str(len(body))
            return Response(status_code=200, headers=out, media_type=api.JSON_TYPE)
        return Response(body, status_code=200, headers=out, media_type=api.JSON_TYPE)

    @staticmethod
    def _login(req: FormatRequest) -> Response:
        if req.principal.is_anonymous:
            raise api.Problem(
                401, 'sign in with an API token or a password', headers={'WWW-Authenticate': BASIC_CHALLENGE}
            )
        return Response(status_code=200, headers=api.VERSION_HEADERS)

    async def _write(self, req: FormatRequest, route: Route) -> Response:
        if req.repo.type is not RepoType.HOSTED:
            what = "publishing isn't supported" if req.method == 'PUT' else "removing releases isn't supported"
            raise api.Problem(405, f'{what}: this is a proxy repository', headers={'Allow': 'GET, HEAD'})
        if req.method == 'PUT':
            return await self.hosted.publish(req, route.package, route.version, api.Urls(req.repo_url))
        return await self.hosted.remove(req, route.package, route.version)

    # ------------------------------------------------------------------ SCM source (proxy)
    @staticmethod
    def _options(req: FormatRequest) -> SwiftOptions:
        return options_of(DESCRIPTOR.parse_options(req.repo.format_options))

    def _source(self, req: FormatRequest, pkg: PackageId) -> str | None:
        """The Git repository URL of a package on the proxy's Git host; None when the scope is not served."""
        if req.repo.type is not RepoType.PROXY or req.repo.proxy is None:
            return None
        options = self._options(req)
        owner = options.scopes.get(pkg.scope_key)
        if owner is None:
            if options.mapped_scopes_only:
                return None
            owner = pkg.scope
        return f'{req.repo.proxy.upstream_url.rstrip("/")}/{owner}/{pkg.name}'

    @staticmethod
    def _archive_url(options: SwiftOptions, source: str, pkg: PackageId, tag: str) -> str:
        quoted = quote(tag, safe='')
        if options.archive_layout == 'gitlab':
            return f'{source}/-/archive/{quoted}/{quote(pkg.name)}-{quoted}.tar.gz'
        if options.archive_layout == 'gitea':
            return f'{source}/archive/{quoted}.tar.gz'
        return f'{source}/archive/refs/tags/{quoted}.tar.gz'

    def _refs_spec(self, pkg: PackageId, source: str) -> AssetSpec:
        def transform(data: bytes, info: UpstreamInfo) -> bytes:
            try:
                return gitrefs.compact(gitrefs.parse_advertisement(data))
            except ValueError as exc:
                raise UpstreamError(f'the Git host answered no usable ref advertisement ({exc})') from None

        return AssetSpec(
            path=f'_scm/{pkg.prefix}/refs',
            kind=AssetKind.METADATA,
            upstream_url=f'{source}.git/info/refs?service=git-upload-pack',
            upstream_headers={'Accept': 'application/x-git-upload-pack-advertisement'},
            content_type=api.JSON_TYPE,
            transform=transform,
            max_size=MAX_REFS,
            artifact=f'{pkg.id} tags',
            component=pkg.id,
            missing_statuses=GIT_MISSING,
        )

    async def tags(self, req: FormatRequest, pkg: PackageId) -> Tags | None:
        """The package's versions on the Git host (version → tag, commit); None when the repository is unknown."""
        source = self._source(req, pkg)
        if source is None:
            return None
        try:
            asset = await req.proxy.ensure(req, self._refs_spec(pkg, source))
        except NotFound:
            return None
        found = self._parsed.get(asset.blob_sha256)
        if found is not None:
            self._parsed.move_to_end(asset.blob_sha256)
            return found
        data = await req.proxy.read(asset, max_bytes=MAX_REFS)
        try:
            raw = await asyncio.to_thread(gitrefs.load_compact, data)
        except ValueError:
            raise UpstreamError('the cached tag list is unusable') from None
        tags = map_tags(raw)
        self._parsed[asset.blob_sha256] = tags
        while len(self._parsed) > PARSED_KEPT:
            self._parsed.popitem(last=False)
        return tags

    # ------------------------------------------------------------------ releases
    async def listing(self, req: FormatRequest, pkg: PackageId) -> tuple[list[tuple[str, bool]], PackageId, str | None]:
        """(versions highest first with their removed flag, the package's display identity, its source URL)."""
        records = await swift_db.releases(req.hosted.db, req.repo.id, pkg.scope_key, pkg.name_key)
        versions = {str(r['version']): r.get('state') == swift_db.REMOVED for r in records}
        display = PackageId(str(records[-1]['scope']), str(records[-1]['name'])) if records else pkg
        canonical: str | None = None
        if req.repo.type is RepoType.PROXY:
            tags = await self.tags(req, pkg)
            if tags is not None:
                canonical = self._source(req, pkg)
                for version in tags:
                    versions.setdefault(version, False)
        else:
            for record in records:
                urls = (record.get('metadata') or {}).get('repositoryURLs') or []
                https = [u for u in urls if isinstance(u, str) and u.startswith('https://')]
                if record.get('state') == swift_db.PUBLISHED and https:
                    canonical = https[0]
                    break
        return _sorted(versions), display, canonical

    async def _releases(self, req: FormatRequest, urls: api.Urls, pkg: PackageId, started: float) -> Response:
        versions, display, canonical = await self.listing(req, pkg)
        if not versions:
            raise api.Problem(404, 'package not found')
        links: list[tuple[str, str, dict[str, str]]] = []
        published = [v for v, removed in versions if not removed]
        if published:
            links.append((urls.release(display.scope, display.name, published[0]), 'latest-version', {}))
        if canonical:
            links.append((canonical, 'canonical', {}))
        doc = api.releases_doc(urls, display.scope, display.name, versions)
        return self._json(req, doc, started, headers={'Link': api.link_header(links)} if links else None)

    async def release(self, req: FormatRequest, pkg: PackageId, version: str, *, files: bool) -> dict[str, Any] | None:
        """A release's record; a proxy pins it on first use. ``files``: also make sure its archive and manifests are
        stored (rebuilt from the tag when a proxy's were lost)."""
        record = await swift_db.get_release(req.hosted.db, req.repo.id, pkg.scope_key, pkg.name_key, version)
        if record is not None:
            if record.get('state') == swift_db.REMOVED or not files or await self._stored(req, record):
                return record
            if record.get('source') == 'scm' and req.repo.type is RepoType.PROXY:
                return await self._pin(req, pkg, version, str(record['tag']), str(record.get('commit') or ''), record)
            raise api.Problem(404, f'the files of release {version} are missing')
        if req.repo.type is not RepoType.PROXY:
            return None
        tags = await self.tags(req, pkg)
        found = tags.get(version) if tags is not None else None
        if found is None:
            return None
        return await self._pin(req, pkg, version, found[0], found[1], None)

    async def _stored(self, req: FormatRequest, record: dict[str, Any]) -> bool:
        paths = [str(record['archive_path']), *(str(m['path']) for m in record.get('manifests') or [])]
        if not record.get('manifests'):
            return False
        return len(await req.hosted.sizes(req.repo, paths)) == len(paths)

    async def _pin(
        self,
        req: FormatRequest,
        pkg: PackageId,
        version: str,
        tag: str,
        commit: str,
        pinned: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the release's archive from its tag; record it (first time) or check it against the pinned checksum
        (rebuild), then store its files."""
        async with self._locks.hold((req.repo.id, pkg.scope_key, pkg.name_key, version)):
            current = await swift_db.get_release(req.hosted.db, req.repo.id, pkg.scope_key, pkg.name_key, version)
            if current is not None and (current.get('state') == swift_db.REMOVED or await self._stored(req, current)):
                return current  # pinned (or repaired) meanwhile
            pinned = current or pinned
            display = PackageId(str(pinned['scope']), str(pinned['name'])) if pinned is not None else pkg
            source = self._source(req, pkg)
            if pinned is not None:
                url = str(pinned['upstream_url'])
            elif source is not None:
                url = self._archive_url(self._options(req), source, pkg, tag)
            else:
                raise api.Problem(404, 'package not found')
            tarball = await req.proxy.ensure(
                req,
                AssetSpec(
                    path=f'_scm/{pkg.prefix}/{quote(tag, safe="")}.tar.gz',
                    kind=AssetKind.CONTENT,
                    upstream_url=url,
                    content_type='application/gzip',
                    max_size=MAX_SOURCE,
                    head_passthrough=False,
                    artifact=f'{pkg.name}-{tag}.tar.gz',
                    component=pkg.id,
                ),
            )
            ref, summary = await self._build(req, tarball, display.name, tag)
            if pinned is not None:
                if ref.sha256 != pinned['checksum']:
                    log.warning('rebuilt archive differs from its pinned checksum', extra={'package': pkg.id})
                    raise UpstreamError(
                        f'the source archive of {pkg.id} {version} rebuilt from tag {tag} does not match its pinned '
                        'checksum (the tag changed upstream): refusing to serve different bytes'
                    )
                record = pinned
            else:
                record = self._record(req, display, version, tag, commit, url, source or url, ref, summary)
                if not await swift_db.insert_release(req.hosted.db, record):
                    existing = await swift_db.get_release(
                        req.hosted.db, req.repo.id, pkg.scope_key, pkg.name_key, version
                    )
                    if existing is None or existing.get('checksum') != ref.sha256:
                        raise UpstreamError(f'{pkg.id} {version} was pinned concurrently with different bytes')
                    record = existing
            manifests = await record_release_files(req, req.repo, display, version, ref, summary)
            await swift_db.set_manifests(req.hosted.db, req.repo.id, pkg.scope_key, pkg.name_key, version, manifests)
            await swift_db.refresh_package(req.hosted.db, req.repo.id, display.scope, display.name)
            return {**record, 'manifests': manifests}

    def _record(
        self,
        req: FormatRequest,
        pkg: PackageId,
        version: str,
        tag: str,
        commit: str,
        url: str,
        source: str,
        ref: BlobRef,
        summary: archive.ArchiveSummary,
    ) -> dict[str, Any]:
        main = summary.manifest()
        key = api.url_key(source)
        return {
            '_id': new_id(),
            'repo_id': req.repo.id,
            'scope': pkg.scope,
            'name': pkg.name,
            'scope_key': pkg.scope_key,
            'name_key': pkg.name_key,
            'version': version,
            'version_sort': version_sort('swift', version),
            'state': swift_db.PUBLISHED,
            'source': 'scm',
            'archive_path': pkg.archive_path(version),
            'checksum': ref.sha256,
            'size': ref.size,
            'manifests': [],
            'tools_version': main.tools_version if main is not None else None,
            'metadata': {'repositoryURLs': [source]},
            'repository_urls': [key] if key else [],
            'signature': None,
            'tag': tag,
            'commit': commit,
            'upstream_url': url,
            'published_at': datetime.now(UTC),
            'published_by': req.principal.display,
        }

    async def _build(
        self, req: FormatRequest, tarball: StoredAsset, top: str, tag: str
    ) -> tuple[BlobRef, archive.ArchiveSummary]:
        refused = self._refused.get(tarball.blob_sha256)
        if refused is not None:
            raise api.Problem(404, f'tag {tag}: {refused}')
        blobs = req.hosted.blobs
        out = blobs.uploads_dir / f'swift-{uuid.uuid4().hex}.zip'
        try:
            try:
                summary = await asyncio.to_thread(
                    archive.build_from_tarball, blobs.blob_path(tarball.blob_sha256), out, top, blobs.uploads_dir
                )
            except archive.ArchiveRefused as exc:
                self._refused[tarball.blob_sha256] = exc.message
                while len(self._refused) > REFUSED_KEPT:
                    self._refused.popitem(last=False)
                raise api.Problem(404, f'tag {tag}: {exc.message}') from None
            ref = await store_file(blobs, out, MAX_ARCHIVE_BYTES)
        finally:
            await asyncio.to_thread(archive.remove, out)
        return ref, summary

    async def _release_info(
        self, req: FormatRequest, urls: api.Urls, pkg: PackageId, version: str, started: float
    ) -> Response:
        record = await self.release(req, pkg, version, files=False)
        if record is None:
            raise api.Problem(404, 'release not found')
        if record.get('state') == swift_db.REMOVED:
            raise api.Problem(410, f'release {version} was removed from this registry')
        display = PackageId(str(record['scope']), str(record['name']))
        try:
            versions, _display, _canonical = await self.listing(req, pkg)
        except WaystationError:  # the Git host is unreachable: the pinned release is still served
            versions = [(version, False)]
        published = [v for v, removed in versions if not removed]
        links = api.neighbours(urls, display.scope, display.name, published, version)
        return self._json(req, api.release_doc(record), started, headers={'Link': api.link_header(links)})

    async def _file(self, req: FormatRequest, pkg: PackageId, version: str) -> dict[str, Any]:
        record = await self.release(req, pkg, version, files=True)
        if record is None:
            raise api.Problem(404, 'release not found')
        if record.get('state') == swift_db.REMOVED:
            raise api.Problem(410, f'release {version} was removed from this registry')
        return record

    async def _serve(self, req: FormatRequest, path: str, headers: dict[str, str]) -> Response:
        asset = await req.hosted.get(req.repo, path)
        if asset is None:
            raise api.Problem(404, 'not found')
        return req.hosted.serve(req, asset, headers=headers)

    async def _manifest(self, req: FormatRequest, urls: api.Urls, pkg: PackageId, version: str) -> Response:
        record = await self._file(req, pkg, version)
        display = PackageId(str(record['scope']), str(record['name']))
        manifests = {str(m['filename']): m for m in record.get('manifests') or []}
        cache = {**api.VERSION_HEADERS, 'Cache-Control': 'public, immutable'}
        wanted = req.request.query_params.get('swift-version')
        if wanted is not None:
            found = manifests.get(f'Package@swift-{wanted}.swift') if _SWIFT_VERSION.match(wanted) else None
            if found is None:
                location = urls.manifest(display.scope, display.name, version)
                return Response(status_code=303, headers={**api.VERSION_HEADERS, 'Location': location})
            return await self._serve(req, str(found['path']), cache)
        main = manifests.get('Package.swift')
        if main is None:
            raise api.Problem(404, 'the release has no Package.swift')
        links = [
            (
                urls.manifest(display.scope, display.name, version, str(m['swift_version'])),
                'alternate',
                {'filename': name, 'swift-tools-version': str(m.get('tools_version') or m['swift_version'])},
            )
            for name, m in sorted(manifests.items())
            if name != 'Package.swift' and m.get('swift_version')
        ]
        headers = {**cache, 'Link': api.link_header(links)} if links else cache
        return await self._serve(req, str(main['path']), headers)

    async def _archive(self, req: FormatRequest, pkg: PackageId, version: str) -> Response:
        record = await self._file(req, pkg, version)
        headers = {
            **api.VERSION_HEADERS,
            'Cache-Control': 'public, immutable',
            'Digest': api.digest_header(str(record['checksum'])),
        }
        signature = record.get('signature')
        if isinstance(signature, dict) and signature.get('base64'):
            headers['X-Swift-Package-Signature-Format'] = str(signature.get('format') or 'cms-1.0.0')
            headers['X-Swift-Package-Signature'] = str(signature['base64'])
        return await self._serve(req, str(record['archive_path']), headers)

    # ------------------------------------------------------------------ identifiers
    async def _identifiers(self, req: FormatRequest, started: float) -> Response:
        url = req.request.query_params.get('url')
        if not url:
            raise api.Problem(400, 'the url query parameter is required')
        parts = api.url_parts(url)
        if parts is None:
            raise api.Problem(400, 'the url query parameter is not a source repository URL')
        if req.repo.type is RepoType.PROXY:
            found = await self._proxy_identifiers(req, parts)
        else:
            found = await swift_db.identifiers(req.hosted.db, req.repo.id, f'{parts[0]}/{parts[1].lower()}')
        if not found:
            raise api.Problem(404, 'no package identifiers are registered for this URL')
        return self._json(req, {'identifiers': found}, started)

    async def _proxy_identifiers(self, req: FormatRequest, parts: tuple[str, str]) -> list[str]:
        """A URL on the proxy's Git host names ``<owner path>/<repository>``: its scope is the mapped one (or the
        owner itself), its name the repository's. Only repositories that exist on the host have an identifier."""
        host, path = parts
        upstream = req.repo.proxy.upstream_url if req.repo.proxy is not None else ''
        if host != (urlsplit(upstream).hostname or '').lower() or '/' not in path:
            return []
        owner, _, name = path.rpartition('/')
        options = self._options(req)
        scope = next((s for s, o in options.scopes.items() if o.lower() == owner.lower()), None)
        if scope is None and not options.mapped_scopes_only and '/' not in owner and valid_scope(owner):
            scope = owner
        if scope is None or not valid_name(name):
            return []
        pkg = PackageId(scope, name)
        return [pkg.id] if await self.tags(req, pkg) is not None else []
