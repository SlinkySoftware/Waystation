"""npm hosted repositories (T-P5-NPMH; SPECIFICATION §4.2; FORMATS.md "npm").

- **Publish** (``PUT /<pkg>`` with ``_attachments``, what ``npm publish`` / ``pnpm publish`` send): the body
  (≤ ``publish.MAX_PUT_BYTES``) is parsed in a thread; the tarball is decoded, checked (``dist.integrity``,
  ``dist.shasum`` and ``length`` when given, ``package.json`` inside it against the version's name and version,
  bounded archive read) and streamed into the blob store as a live upload transfer. The stored version keeps the
  manifest with ``dist`` computed here (``integrity`` sha512, ``shasum`` sha1, ``fileCount``, ``unpackedSize``).
  Write policy: ``read_only`` refuses (403); ``disable_redeploy`` (the npm registry's own rule) refuses a version
  that exists (403 "cannot publish over"); ``allow_redeploy`` replaces it.
- **Packuments** are generated from the stored versions (``npm_versions``) and the package document
  (``npm_packages``: dist-tags, readme, ``rev``) after every change and stored as the repository's metadata assets
  ``<pkg>/-/packument.json`` and ``<pkg>/-/packument.install-v1.json`` (the keys a proxy caches them under), so they
  are served by X-Accel with ``ETag``/``304`` and browse reads them like a proxy's. Changes move the package's
  ``rev`` by compare-and-set; a regeneration that finds the ``rev`` moved while it ran runs again, so the last
  writer always leaves the newest state. Versions whose tarball a cleanup policy removed are left out (and dropped
  when a client asks for the missing tarball).
- **Packument updates** (``PUT /<pkg>`` or ``PUT /<pkg>/-rev/<rev>`` without attachments: ``npm deprecate``,
  ``npm unpublish <pkg>@<version>``): deprecation messages of held versions are applied, held versions missing from
  the body are unpublished (needs ``delete``), dist-tags are taken from the body (only tags on held versions; a
  ``latest`` is always kept). Versions the repository does not hold are ignored.
- **Unpublish**: ``DELETE /<pkg>/-rev/<rev>`` (or ``/<pkg>``) removes every version; ``DELETE`` of a tarball (also
  behind the ``repository/<repo>/`` prefix npm sends, with ``/-rev/<rev>``) removes it and its version.
- **dist-tags**: ``GET -/package/<pkg>/dist-tags``, ``PUT``/``DELETE -/package/<pkg>/dist-tags/<tag>`` (body: the
  version as a JSON string; ``latest`` cannot be removed; ``write`` permission).
- **Search**: ``/-/v1/search?text=`` over the repository's packages (name substring).

Uploads are audited (``content.upload``), deprecations and dist-tag changes ``content.update``, unpublishes
``content.delete`` (each also fires webhooks), and shown in the activity feed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse, Response

from waystation_common.errors import Conflict, NotFound, PayloadTooLarge, ValidationFailed
from waystation_common.events import CacheState, EventType
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.db import npm as npm_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.npm import metadata, publish
from waystation_server.formats.npm.metadata import Op, Route
from waystation_server.proxy.engine import AssetKind
from waystation_server.repos.model import RepoConfig, StoredHosted, WritePolicy
from waystation_server.security.actions import Action

if TYPE_CHECKING:
    from waystation_server.formats.npm.plugin import NpmPlugin

log = logging.getLogger('waystation.formats.npm')

TARBALL_TYPE = 'application/octet-stream'
MAX_TAG_BODY = 4096
CAS_ROUNDS = 20
REGENERATE_ROUNDS = 8
CHUNK = 256 * 1024
SEARCH_MAX = 250
REPAIR_AFTER = timedelta(minutes=10)
_KEEP: Any = object()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def npm_json(body: dict[str, Any], status: int = 200) -> Response:
    return JSONResponse(body, status_code=status, headers={'Cache-Control': 'no-store'})


def _npm_error(status: int, message: str) -> Response:
    return JSONResponse({'error': message}, status_code=status)


async def read_body(req: FormatRequest, limit: int) -> bytes:
    """The request body, bounded (``PayloadTooLarge`` past ``limit``)."""
    length = req.request.headers.get('content-length')
    if length is not None and length.isdigit() and int(length) > limit:
        raise PayloadTooLarge(f'the request body exceeds {limit} bytes')
    chunks: list[bytes] = []
    size = 0
    async for chunk in req.request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLarge(f'the request body exceeds {limit} bytes')
        chunks.append(chunk)
    return b''.join(chunks)


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    view = memoryview(data)
    for start in range(0, len(view), CHUNK):
        yield bytes(view[start : start + CHUNK])


class NpmHosted:
    def __init__(self, plugin: NpmPlugin) -> None:
        self._plugin = plugin

    # ------------------------------------------------------------------ dispatch
    async def handle(self, req: FormatRequest, route: Route) -> Response:
        method = req.method
        reading = method in ('GET', 'HEAD')
        match route.op:
            case Op.PACKUMENT if reading:
                return await self.serve_packument(req, req.repo, route.package)
            case Op.PACKUMENT | Op.PACKAGE_REV if method == 'PUT':
                return await self.put(req, route.package)
            case Op.PACKUMENT | Op.PACKAGE_REV if method == 'DELETE':
                return await self.unpublish_all(req, route.package)
            case Op.TARBALL if reading:
                return await self.serve_tarball(req, route)
            case Op.TARBALL if method == 'DELETE':
                return await self.delete_tarball(req, route)
            case Op.DIST_TAGS if reading:
                return await self.dist_tags(req, route.package)
            case Op.DIST_TAG if method == 'PUT':
                return await self.set_tag(req, route)
            case Op.DIST_TAG if method == 'DELETE':
                return await self.remove_tag(req, route)
            case Op.SEARCH if reading:
                return await self.search(req)
            case Op.AUDIT:
                raise NotFound('audit is not available from a hosted repository')
        allowed = {
            Op.PACKUMENT: 'GET, HEAD, PUT, DELETE',
            Op.PACKAGE_REV: 'PUT, DELETE',
            Op.TARBALL: 'GET, HEAD, DELETE',
            Op.DIST_TAGS: 'GET, HEAD',
            Op.DIST_TAG: 'PUT, DELETE',
        }.get(route.op, 'GET, HEAD')
        return JSONResponse({'error': 'method not allowed'}, status_code=405, headers={'Allow': allowed})

    # ------------------------------------------------------------------ reading
    async def serve_packument(self, req: FormatRequest, repo: RepoConfig, package: str) -> Response:
        started = time.perf_counter()
        abbreviated = self._plugin.wants_abbreviated(req) and req.request.query_params.get('write') != 'true'
        asset = await req.hosted.get(repo, metadata.packument_key(package, abbreviated=abbreviated))
        if asset is None:
            req.hosted.account(req, CacheState.HIT, 404, 0, started)
            raise NotFound('Not found')
        return req.hosted.serve(req, asset)

    async def serve_tarball(self, req: FormatRequest, route: Route) -> Response:
        started = time.perf_counter()
        asset = await req.hosted.get(req.repo, metadata.tarball_path(route.package, route.filename))
        if asset is None:
            req.hosted.account(req, CacheState.HIT, 404, 0, started)
            await self._repair(req, route)
            raise NotFound('Not found')
        return req.hosted.serve(req, asset)

    async def _repair(self, req: FormatRequest, route: Route) -> None:
        """A listed version whose tarball is gone (a cleanup policy removed it): drop the version and regenerate the
        packuments, so clients stop being offered it."""
        state = await self.state(req, req.repo, route.package)
        record = next((r for r in state.versions if r.filename == route.filename), None) if state else None
        if record is None or datetime.now(UTC) - _aware(record.published_at) < REPAIR_AFTER:
            return  # (a publish in progress records its tarball moments after the version)
        log.info('dropping an npm version whose tarball is gone', extra={'repo': req.repo.name, 'path': req.path})
        await npm_db.delete_version(req.hosted.db, req.repo.id, route.package, record.version)
        await self.commit(req, req.repo, route.package)

    async def state(self, req: FormatRequest, repo: RepoConfig, package: str) -> publish.PackageState | None:
        """The package as stored (None: the repository has neither a package document nor versions of it)."""
        db = req.hosted.db
        doc = await npm_db.get(db, repo.id, package)
        docs = await npm_db.versions(db, repo.id, package)
        if doc is None and not docs:
            return None
        records = await asyncio.to_thread(publish.records_of, docs)
        doc = doc or {}
        tags = doc.get('dist_tags')
        return publish.PackageState(
            name=package,
            rev=int(doc.get('rev') or 0),
            dist_tags=dict(tags) if isinstance(tags, dict) else {},
            readme=doc.get('readme'),
            created=doc.get('created_at'),
            modified=doc.get('modified'),
            versions=records,
        )

    async def dist_tags(self, req: FormatRequest, package: str) -> Response:
        state = await self.state(req, req.repo, package)
        if state is None or not state.versions:
            raise NotFound('Not found')
        return npm_json(publish.effective_tags(state.dist_tags, [r.version for r in state.versions]))

    async def search(self, req: FormatRequest) -> Response:
        """``/-/v1/search``: the repository's packages whose name contains ``text`` (npm's result shape)."""
        text, size, offset = search_params(req)
        docs = await self.search_docs(req, req.repo, text, limit=offset + size)
        return JSONResponse(search_body(docs[offset:]))

    @staticmethod
    async def search_docs(req: FormatRequest, repo: RepoConfig, text: str, *, limit: int) -> list[dict[str, Any]]:
        return await npm_db.page(req.hosted.db, repo.id, after=None, q=text or None, limit=limit)

    # ------------------------------------------------------------------ publishing and updates
    async def put(self, req: FormatRequest, package: str) -> Response:
        hosted = req.repo.hosted or StoredHosted()
        if hosted.write_policy is WritePolicy.READ_ONLY:
            return _npm_error(403, 'this repository is read-only')
        length = req.request.headers.get('content-length')
        await req.hosted.admit(req.repo, int(length) if length is not None and length.isdigit() else None)
        body = await read_body(req, publish.MAX_PUT_BYTES)
        parsed = await asyncio.to_thread(publish.parse_put, body, package)
        del body
        if isinstance(parsed, publish.Published):
            return await self._publish(req, hosted, parsed)
        return await self._update(req, package, parsed)

    async def _publish(self, req: FormatRequest, hosted: StoredHosted, item: publish.Published) -> Response:
        repo, db, package = req.repo, req.hosted.db, item.package
        path = metadata.tarball_path(package, item.filename)
        existing = await npm_db.get_version(db, repo.id, package, item.version)
        redeploy = existing is not None and hosted.write_policy is WritePolicy.DISABLE_REDEPLOY
        if redeploy and await req.hosted.get(repo, path) is not None:
            return self._over(item.version)
        if existing is None and await npm_db.count_versions(db, repo.id, package) >= npm_db.MAX_VERSIONS:
            raise ValidationFailed(f'a package holds at most {npm_db.MAX_VERSIONS} versions')
        writer = await req.hosted.blobs.open_writer(expected_size=len(item.tarball), expected_sha256=item.sha256)
        try:
            await req.hosted.receive(
                req,
                writer,
                path=path,
                artifact=item.filename,
                component=package,
                total=len(item.tarball),
                body=_chunks(item.tarball),
            )
            ref = await req.hosted.blobs.commit(writer)
        finally:
            if not writer.closed:
                await writer.abort()
        now = datetime.now(UTC)
        fields = {
            'manifest': json.dumps(item.manifest, ensure_ascii=False, separators=(',', ':')),
            'tarball': item.filename,
            'size': ref.size,
            'sha256': ref.sha256,
            'published_at': now,
            'published_by': req.principal.display,
        }
        if hosted.write_policy is WritePolicy.DISABLE_REDEPLOY:
            if not await npm_db.claim_version(db, repo.id, package, item.version, fields):
                if await req.hosted.get(repo, path) is not None:  # published meanwhile by another request
                    return self._over(item.version)
                await npm_db.put_version(db, repo.id, package, item.version, fields)  # left by a failed publish
        else:
            await npm_db.put_version(db, repo.id, package, item.version, fields)
        await req.hosted.record(repo, path, ref, content_type=TARBALL_TYPE)
        tags = item.dist_tags
        await self.commit(req, repo, package, tags=lambda current: {**current, **tags}, readme=item.readme or _KEEP)
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('npm_package', ref.sha256, f'{repo.name}/{package}@{item.version}'),
            details={
                'repository': repo.name,
                'package': package,
                'version': item.version,
                'filename': item.filename,
                'sha256': ref.sha256,
                'size': ref.size,
                'dist_tags': sorted(tags),
                'replaced': existing is not None,
            },
        )
        req.hosted.activity(req, EventType.ASSET_CREATED, path=path, artifact=item.filename, size=ref.size)
        req.log(package=package, version=item.version)
        return npm_json({'ok': True, 'id': package}, 201)

    @staticmethod
    def _over(version: str) -> Response:
        return _npm_error(403, f'You cannot publish over the previously published versions: {version}.')

    async def _update(self, req: FormatRequest, package: str, update: publish.Update) -> Response:
        repo, db = req.repo, req.hosted.db
        state = await self.state(req, repo, package)
        if state is None or not state.versions:
            raise NotFound('Not found')
        held = {r.version: r for r in state.versions}
        removed = [v for v in held if v not in update.versions]
        if removed and not req.allows_member(Action.DELETE, repo):
            return _npm_error(403, f'delete permission required on {repo.name} to unpublish versions')
        if len(removed) == len(held):
            return await self.unpublish_all(req, package)
        deprecations: dict[str, str | None] = {}
        for version, meta in update.versions.items():
            record = held.get(version)
            if record is None:
                continue
            wanted = publish.deprecation(meta)
            current = record.manifest.get('deprecated')
            if wanted != (current if isinstance(current, str) and current else None):
                manifest = dict(record.manifest)
                if wanted is None:
                    manifest.pop('deprecated', None)
                else:
                    manifest['deprecated'] = wanted
                text = json.dumps(manifest, ensure_ascii=False, separators=(',', ':'))
                await npm_db.set_manifest(db, repo.id, package, version, text)
                deprecations[version] = wanted
        for version in removed:
            await self._remove_version(req, repo, package, held[version])
        new_tags = update.dist_tags
        tags_changed = new_tags is not None and publish.effective_tags(
            new_tags, [v for v in held if v not in removed]
        ) != publish.effective_tags(state.dist_tags, list(held))
        if deprecations or removed or tags_changed:
            await self.commit(req, repo, package, tags=(lambda _: dict(new_tags)) if new_tags is not None else None)
        for version, message in deprecations.items():
            audit.record(
                AuditAction.CONTENT_UPDATE,
                target=audit.target('npm_package', None, f'{repo.name}/{package}@{version}'),
                details={'repository': repo.name, 'package': package, 'version': version, 'deprecated': message},
            )
        if removed:
            audit.record(
                AuditAction.CONTENT_DELETE,
                target=audit.target('npm_package', None, f'{repo.name}/{package}'),
                details={'repository': repo.name, 'package': package, 'versions': sorted(removed)},
            )
        if tags_changed and new_tags is not None:
            audit.record(
                AuditAction.CONTENT_UPDATE,
                target=audit.target('npm_package', None, f'{repo.name}/{package}'),
                details={'repository': repo.name, 'package': package, 'dist_tags': dict(sorted(new_tags.items()))},
            )
        req.log(package=package)
        return npm_json({'ok': True, 'id': package})

    # ------------------------------------------------------------------ unpublishing
    async def _remove_version(
        self, req: FormatRequest, repo: RepoConfig, package: str, record: publish.VersionRecord
    ) -> None:
        await npm_db.delete_version(req.hosted.db, repo.id, package, record.version)
        path = metadata.tarball_path(package, record.filename)
        removed = await req.hosted.remove(repo, path)
        if removed is not None:
            req.hosted.activity(req, EventType.ASSET_DELETED, path=path, artifact=record.filename, size=removed.size)

    async def unpublish_all(self, req: FormatRequest, package: str) -> Response:
        repo = req.repo
        state = await self.state(req, repo, package)
        if state is None:
            raise NotFound('Not found')
        for record in state.versions:
            await self._remove_version(req, repo, package, record)
        await self.commit(req, repo, package)
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target('npm_package', None, f'{repo.name}/{package}'),
            details={
                'repository': repo.name,
                'package': package,
                'versions': sorted(r.version for r in state.versions),
                'all': True,
            },
        )
        req.log(package=package)
        return npm_json({'ok': True, 'id': package})

    async def delete_tarball(self, req: FormatRequest, route: Route) -> Response:
        repo, package = req.repo, route.package
        state = await self.state(req, repo, package)
        record = next((r for r in state.versions if r.filename == route.filename), None) if state else None
        if record is not None:
            await self._remove_version(req, repo, package, record)
            await self.commit(req, repo, package)
            audit.record(
                AuditAction.CONTENT_DELETE,
                target=audit.target('npm_package', None, f'{repo.name}/{package}'),
                details={'repository': repo.name, 'package': package, 'versions': [record.version]},
            )
            return npm_json({'ok': True})
        path = metadata.tarball_path(package, route.filename)
        removed = await req.hosted.remove(repo, path)
        if removed is not None:
            req.hosted.activity(req, EventType.ASSET_DELETED, path=path, artifact=route.filename, size=removed.size)
            audit.record(
                AuditAction.CONTENT_DELETE,
                target=audit.target('npm_package', removed.blob_sha256, f'{repo.name}/{path}'),
                details={'repository': repo.name, 'package': package, 'filename': route.filename},
            )
            return npm_json({'ok': True})
        if state is not None:
            return npm_json({'ok': True})  # npm unpublish deletes the tarball after the packument update removed it
        raise NotFound('Not found')

    # ------------------------------------------------------------------ dist-tags
    async def set_tag(self, req: FormatRequest, route: Route) -> Response:
        raw = await read_body(req, MAX_TAG_BODY)
        try:
            version = json.loads(raw)
        except ValueError:
            version = raw.decode('utf-8', 'replace').strip()
        if not isinstance(version, str) or not metadata.valid_version(version):
            raise ValidationFailed('the body must be a version as a JSON string')
        repo, package, tag = req.repo, route.package, route.arg
        state = await self.state(req, repo, package)
        if state is None or all(r.version != version for r in state.versions):
            raise NotFound(f'version not found: {package}@{version}')
        await self.commit(req, repo, package, tags=lambda current: {**current, tag: version})
        audit.record(
            AuditAction.CONTENT_UPDATE,
            target=audit.target('npm_package', None, f'{repo.name}/{package}'),
            details={'repository': repo.name, 'package': package, 'dist_tag': tag, 'version': version},
        )
        return npm_json({'ok': True})

    async def remove_tag(self, req: FormatRequest, route: Route) -> Response:
        repo, package, tag = req.repo, route.package, route.arg
        if tag == 'latest':
            raise ValidationFailed('the latest dist-tag cannot be removed')
        state = await self.state(req, repo, package)
        if state is None or tag not in state.dist_tags:
            raise NotFound(f'dist-tag not found: {tag}')
        await self.commit(req, repo, package, tags=lambda current: {k: v for k, v in current.items() if k != tag})
        audit.record(
            AuditAction.CONTENT_UPDATE,
            target=audit.target('npm_package', None, f'{repo.name}/{package}'),
            details={'repository': repo.name, 'package': package, 'dist_tag': tag, 'version': None},
        )
        return npm_json({'ok': True})

    # ------------------------------------------------------------------ package state and packuments
    async def commit(
        self,
        req: FormatRequest,
        repo: RepoConfig,
        package: str,
        *,
        tags: Callable[[dict[str, str]], dict[str, str]] | None = None,
        readme: Any = _KEEP,
    ) -> None:
        """Record a change of the package (its versions are already written): dist-tags (``tags`` maps the stored
        ones to the new ones), readme, summary; by compare-and-set on ``rev``. Then regenerate its packuments. A
        package without versions is removed."""
        db = req.hosted.db
        for _ in range(CAS_ROUNDS):
            state = await self.state(req, repo, package)
            if state is None:
                break
            if not state.versions:
                if state.rev == 0 or await npm_db.drop_package(db, repo.id, package, state.rev):
                    break
                continue
            held = [r.version for r in state.versions]
            current = publish.effective_tags(state.dist_tags, held)
            state.dist_tags = publish.effective_tags(tags(dict(current)) if tags else current, held)
            if readme is not _KEEP:
                state.readme = readme
            state.modified = datetime.now(UTC)
            fields = {**publish.summary(state), 'readme': state.readme}
            if await npm_db.bump(db, repo.id, package, state.rev, fields):
                break
        else:
            raise Conflict('the package is being changed by another request; try again')
        await self.regenerate(req, repo, package)

    async def regenerate(self, req: FormatRequest, repo: RepoConfig, package: str) -> None:
        """Write the package's packuments from its stored state (see the module docstring)."""
        db = req.hosted.db
        base = f'{req.base_url}/repository/{repo.name}/'
        keys = {abbreviated: metadata.packument_key(package, abbreviated=abbreviated) for abbreviated in (False, True)}
        for _ in range(REGENERATE_ROUNDS):
            state = await self.state(req, repo, package)
            if state is None or (state.rev == 0 and not state.versions):
                for key in keys.values():
                    await req.hosted.remove(repo, key)
                return
            if state.rev == 0:
                return  # a publish is between its version and its package document: it regenerates next
            paths = {r.version: metadata.tarball_path(package, r.filename) for r in state.versions}
            present = await req.hosted.sizes(repo, list(paths.values()))
            state.versions = [r for r in state.versions if paths[r.version] in present]
            full, corgi = await asyncio.to_thread(publish.build_packuments, state, base)
            for abbreviated, body in ((False, full), (True, corgi)):
                async with await req.hosted.blobs.open_writer(expected_size=len(body)) as writer:
                    await writer.write(body)
                    ref = await req.hosted.blobs.commit(writer)
                content_type = metadata.ABBREVIATED_TYPE if abbreviated else metadata.JSON_TYPE
                await req.hosted.record(
                    repo, keys[abbreviated], ref, content_type=content_type, kind=AssetKind.METADATA
                )
            again = await npm_db.get(db, repo.id, package)
            if again is not None and int(again.get('rev') or 0) == state.rev:
                return
        log.warning('npm packument regeneration kept racing', extra={'repo': repo.name, 'package': package})


def search_params(req: FormatRequest) -> tuple[str, int, int]:
    """``text``, ``size`` (1-250, default 20) and ``from`` of a search request."""
    params = req.request.query_params
    text = (params.get('text') or '')[:214]
    size = min(max(int(params['size']), 1) if params.get('size', '').isdigit() else 20, SEARCH_MAX)
    offset = min(int(params['from']), 10_000) if params.get('from', '').isdigit() else 0
    return text, size, offset


def search_body(docs: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    objects = []
    for doc in docs:
        modified = doc.get('modified')
        objects.append(
            {
                'package': {
                    'name': doc['name'],
                    'version': doc.get('latest') or '',
                    'description': doc.get('description') or '',
                    'date': modified.isoformat() if isinstance(modified, datetime) else None,
                    'links': {},
                },
                'score': {'final': 1.0, 'detail': {'quality': 1.0, 'popularity': 1.0, 'maintenance': 1.0}},
                'searchScore': 1.0,
            }
        )
    return {'objects': objects, 'total': len(objects), 'time': now}
