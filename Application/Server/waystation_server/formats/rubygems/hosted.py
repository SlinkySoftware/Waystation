"""RubyGems hosted repositories (T-P6-GEMS; SPECIFICATION §4.2; FORMATS.md "RubyGems").

- **Push** (``POST /repository/<repo>/api/v1/gems``, the ``.gem`` as the body: ``gem push --host
  <repository URL>``, the API token in ``Authorization``): streamed into the blob store (a live upload transfer, at
  most ``MAX_GEM_BYTES``, hard quota checked first), then read in a thread (``formats.rubygems.package``: a bounded,
  safe tar walk; the YAML specification read with a safe loader; ``checksums.yaml.gz`` checked). Stored as
  ``gems/<full name>.gem`` with the version's details on the asset (``assets.rubygems``, including its compact index
  line), plus ``quick/Marshal.4.8/<full name>.gemspec.rz`` written from the specification
  (``formats.rubygems.marshal``). Write policy: ``read_only`` refuses (403), ``disable_redeploy`` answers an existing
  version 409, ``allow_redeploy`` replaces it.
- **Yank** (``DELETE api/v1/gems/yank``, form fields ``gem_name``, ``version``, optional ``platform``): removes the
  version's files; it leaves the index.
- **Reading**: ``versions`` (one line per gem, the MD5 of its info file), ``info/<gem>`` (versions in push order),
  ``names`` and the legacy ``specs.4.8.gz`` / ``latest_specs.4.8.gz`` / ``prerelease_specs.4.8.gz`` rendered per
  request (``ETag``, ``304``, ``HEAD``, ``Repr-Digest``); gems and gemspecs by X-Accel.

Pushes need ``write`` and yanks ``delete`` (the router checked them from the method); both are audited and shown in
the activity feed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from starlette.responses import Response

from waystation_common import rubygems
from waystation_common.db.client import Database
from waystation_common.errors import (
    Conflict,
    Forbidden,
    InsufficientStorage,
    NotFound,
    PayloadTooLarge,
    ValidationFailed,
    WaystationError,
)
from waystation_common.events import CacheState, EventType
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.db import rubygems as gems_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.rubygems import marshal, package, protocol
from waystation_server.formats.rubygems.common import SAFE_HEADERS, gem_error, generated
from waystation_server.formats.rubygems.protocol import GemVersions, Op, Route
from waystation_server.proxy.engine import AssetKind
from waystation_server.repos.model import RepoConfig, StoredHosted, WritePolicy
from waystation_server.repos.quota import QuotaExceeded

MAX_GEM_BYTES = 512 * 1024 * 1024
MAX_FORM = 4096
SUMMARY_VERSIONS = 2000


def _read(path: Path) -> package.ReadGem:
    with path.open('rb') as file:
        return package.read_gem(file)


def _details(doc: Mapping[str, Any]) -> dict[str, Any]:
    found = doc.get('rubygems')
    return dict(found) if isinstance(found, Mapping) else {}


def info_lines(docs: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Each gem's info lines from its hosted assets (push order)."""
    out: dict[str, list[str]] = {}
    for doc in docs:
        details = _details(doc)
        if details.get('name') and details.get('info_line'):
            out.setdefault(str(details['name']), []).append(str(details['info_line']))
    return out


def versions_of(docs: Sequence[Mapping[str, Any]]) -> dict[str, GemVersions]:
    """The ``versions`` entries of hosted gems (MD5 of each rendered info file)."""
    out: dict[str, GemVersions] = {}
    for doc in docs:
        details = _details(doc)
        name, key = details.get('name'), details.get('key')
        if name and key:
            out.setdefault(str(name), GemVersions()).versions.append(str(key))
    for name, lines in info_lines(docs).items():
        out[name].md5 = protocol.md5(protocol.render_info(lines))
    return out


def tuples(docs: Sequence[Mapping[str, Any]], which: str) -> list[tuple[str, str, str]]:
    """``(name, version, platform)`` of the legacy index ``which`` (``specs.4.8.gz``, ``latest_``, ``prerelease_``)."""
    found = [
        (str(d['name']), str(d['version']), str(d.get('platform') or rubygems.RUBY))
        for d in (_details(doc) for doc in docs)
        if d.get('name') and d.get('version')
    ]
    if which.startswith('prerelease_'):
        return sorted(t for t in found if rubygems.is_prerelease(t[1]))
    releases = [t for t in found if not rubygems.is_prerelease(t[1])]
    if which.startswith('latest_'):
        newest: dict[tuple[str, str], tuple[str, str, str]] = {}
        for t in releases:
            current = newest.get((t[0], t[2]))
            if current is None or rubygems.sort_string(t[1]) > rubygems.sort_string(current[1]):
                newest[(t[0], t[2])] = t
        return sorted(newest.values())
    return sorted(releases)


async def summarise(db: Database, repo: RepoConfig, name: str) -> None:
    """Refresh the gem's ``rubygems_gems`` summary from its stored versions (removed when none is left)."""
    docs = await gems_db.gem_files(db, repo.id, name)
    if not docs:
        await gems_db.delete_gem(db, repo.id, name)
        return
    rows = [(_details(d), d) for d in docs]
    rows.sort(key=lambda r: rubygems.sort_string(str(r[0].get('version'))), reverse=True)
    newest = rubygems.newest(str(r[0].get('version')) for r in rows)
    top = next((r[0] for r in rows if r[0].get('version') == newest), rows[0][0])
    versions = [
        {
            'version': d.get('version'),
            'platform': d.get('platform') or rubygems.RUBY,
            'sha256': d.get('sha256'),
            'dependencies': d.get('dependencies') or [],
            'ruby': d.get('ruby'),
            'path': doc['path'],
            'published': d.get('published'),
            'uploaded_by': d.get('uploaded_by'),
        }
        for d, doc in rows[:SUMMARY_VERSIONS]
    ]
    await gems_db.upsert_gem(
        db,
        repo.id,
        name,
        {
            'versions': versions,
            'version_count': len({r[0].get('version') for r in rows}),
            'latest': newest,
            'summary': top.get('summary'),
        },
    )


class RubygemsHosted:
    # ------------------------------------------------------------------ reading
    async def files(self, req: FormatRequest, repo: RepoConfig, name: str | None = None) -> list[dict[str, Any]]:
        return await gems_db.gem_files(req.hosted.db, repo.id, name)

    async def info(self, req: FormatRequest, repo: RepoConfig, name: str) -> list[str]:
        """The hosted info lines of ``name`` (push order; empty: the repository does not have it)."""
        return info_lines(await self.files(req, repo, name)).get(name, [])

    async def serve(self, req: FormatRequest, route: Route) -> Response:
        started = time.perf_counter()

        def account(status: int, sent: int) -> None:
            req.hosted.account(req, CacheState.HIT, status, sent, started)

        def missing() -> NotFound:
            account(404, 0)
            return NotFound('Not Found')

        match route.op:
            case Op.VERSIONS:
                docs = await self.files(req, req.repo)
                gems = await asyncio.to_thread(versions_of, docs)
                times = [d['created_at'] for d in docs if isinstance(d.get('created_at'), datetime)]
                created = max(times, default=datetime(2000, 1, 1, tzinfo=UTC))
                return generated(req, protocol.render_versions(gems, created), account=account)
            case Op.NAMES:
                docs = await self.files(req, req.repo)
                names = sorted({str(_details(d).get('name')) for d in docs if _details(d).get('name')})
                return generated(req, protocol.render_names(names), account=account)
            case Op.INFO:
                lines = await self.info(req, req.repo, route.name)
                if not lines:
                    raise missing()
                return generated(req, protocol.render_info(lines), account=account)
            case Op.SPECS:
                docs = await self.files(req, req.repo)
                body = await asyncio.to_thread(marshal.specs_gz, tuples(docs, route.name))
                return generated(req, body, account=account, content_type=protocol.GZIP_TYPE)
            case Op.GEM | Op.GEMSPEC:
                asset = await req.hosted.get(req.repo, route.key)
                if asset is None:
                    raise missing()
                return req.hosted.serve(req, asset, headers=SAFE_HEADERS)
        raise missing()

    # ------------------------------------------------------------------ writing
    @staticmethod
    def _policy(req: FormatRequest) -> StoredHosted:
        hosted = req.repo.hosted or StoredHosted()
        if hosted.write_policy is WritePolicy.READ_ONLY:
            raise Forbidden('this repository is read-only')
        return hosted

    async def handle_write(self, req: FormatRequest, route: Route) -> Response:
        started = time.perf_counter()
        try:
            if route.op is Op.PUSH and req.method == 'POST':
                response = await self.push(req)
            elif route.op is Op.YANK and req.method == 'DELETE':
                response = await self.yank(req)
            else:
                allow = 'POST' if route.op is Op.PUSH else 'DELETE'
                response = gem_error(405, 'Method Not Allowed', headers={'Allow': allow})
        except QuotaExceeded as exc:  # a hard repository quota (SPECIFICATION §4.8)
            response = gem_error(403, exc.detail)
        except InsufficientStorage:  # the low-disk guard
            response = gem_error(507, 'not enough free space in the blob store')
        except WaystationError as exc:
            response = gem_error(exc.status, exc.detail)
        req.hosted.account(req, CacheState.HIT, response.status_code, 0, started)
        return response

    async def push(self, req: FormatRequest) -> Response:
        hosted = self._policy(req)
        length = req.request.headers.get('content-length')
        total = int(length) if length is not None and length.isdigit() else None
        if total is not None and total > MAX_GEM_BYTES:
            raise PayloadTooLarge('the gem is too large')
        await req.hosted.admit(req.repo, total)
        writer = await req.hosted.blobs.open_writer(max_size=MAX_GEM_BYTES)
        try:
            await req.hosted.receive(req, writer, path=protocol.PUSH, artifact='gem push', total=total)
            await writer.flush()
            if writer.size == 0:
                raise ValidationFailed('the upload is empty: send the .gem as the request body')
            read = await asyncio.to_thread(_read, writer.path)
            spec = read.spec
            path = protocol.gem_path(spec.full_name)
            existing = await req.hosted.get(req.repo, path)
            if existing is not None and hosted.write_policy is WritePolicy.DISABLE_REDEPLOY:
                raise Conflict(f'Repushing of gem versions is not allowed: {spec.full_name} already exists')
            ref = await req.hosted.blobs.commit(writer)
        finally:
            if not writer.closed:
                await writer.abort()
        await req.hosted.record(req.repo, path, ref, content_type=protocol.GEM_TYPE)
        gemspec = await asyncio.to_thread(marshal.gemspec_rz, spec.marshal_fields())
        async with await req.hosted.blobs.open_writer(expected_size=len(gemspec)) as spec_writer:
            await spec_writer.write(gemspec)
            spec_ref = await req.hosted.blobs.commit(spec_writer)
        await req.hosted.record(
            req.repo,
            protocol.gemspec_path(spec.full_name),
            spec_ref,
            content_type=protocol.SPEC_TYPE,
            kind=AssetKind.METADATA,
        )
        runtime = spec.runtime
        details = {
            'name': spec.name,
            'version': spec.version,
            'version_sort': rubygems.sort_string(spec.version),
            'platform': spec.platform,
            'key': spec.version if spec.platform == rubygems.RUBY else f'{spec.version}-{spec.platform}',
            'sha256': read.sha256,
            'summary': spec.summary[:1024] or None,
            'dependencies': [[d.name, d.text()] for d in runtime],
            'ruby': ', '.join(f'{op} {v}' for op, v in spec.required_ruby_version) or None,
            'info_line': protocol.render_info_line(
                spec.version,
                spec.platform,
                [(d.name, d.requirements) for d in runtime],
                read.sha256,
                spec.required_ruby_version,
                spec.required_rubygems_version,
            ),
            'published': datetime.now(UTC),
            'uploaded_by': req.principal.display,
        }
        await gems_db.set_details(req.hosted.db, req.repo.id, path, details)
        await summarise(req.hosted.db, req.repo, spec.name)
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('rubygems_gem', None, f'{req.repo.name}/{spec.full_name}'),
            details={
                'repository': req.repo.name,
                'gem': spec.name,
                'version': spec.version,
                'platform': spec.platform,
                'sha256': read.sha256,
                'size': ref.size,
                'replaced': existing is not None,
            },
        )
        artifact = f'{spec.full_name}.gem'
        req.hosted.activity(req, EventType.ASSET_CREATED, path=path, artifact=artifact, size=ref.size)
        req.log(gem=spec.name)
        message = f'Successfully registered gem: {spec.name} ({spec.version}'
        message += ')' if spec.platform == rubygems.RUBY else f'-{spec.platform})'
        return Response(message.encode(), status_code=200, media_type=protocol.TEXT_TYPE, headers=SAFE_HEADERS)

    async def _form(self, req: FormatRequest) -> dict[str, str]:
        """The yank parameters (``gem yank`` sends a form body; query parameters are accepted too)."""
        fields = dict(req.request.query_params.items())
        body = b''
        async for chunk in req.request.stream():
            body += chunk
            if len(body) > MAX_FORM:
                raise PayloadTooLarge('the yank request is too large')
        for key, values in parse_qs(body.decode('utf-8', 'replace'), max_num_fields=10).items():
            fields[key] = values[0]
        return fields

    async def yank(self, req: FormatRequest) -> Response:
        self._policy(req)
        form = await self._form(req)
        name = form.get('gem_name', '').strip()
        version = form.get('version', '').strip()
        platform = form.get('platform', '').strip() or rubygems.RUBY
        if (
            not rubygems.valid_name(name)
            or not rubygems.valid_version(version)
            or not rubygems.valid_platform(platform)
        ):
            raise ValidationFailed('gem_name, version (and optionally platform) are required')
        full = rubygems.full_name(name, version, platform)
        path = protocol.gem_path(full)
        removed = await req.hosted.remove(req.repo, path)
        if removed is None:
            raise NotFound(f'The version {version} does not exist for {name}')
        await req.hosted.remove(req.repo, protocol.gemspec_path(full))
        await summarise(req.hosted.db, req.repo, name)
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target('rubygems_gem', None, f'{req.repo.name}/{full}'),
            details={'repository': req.repo.name, 'gem': name, 'version': version, 'platform': platform},
        )
        req.hosted.activity(req, EventType.ASSET_DELETED, path=path, artifact=f'{full}.gem', size=removed.size)
        return Response(
            f'Successfully deleted gem: {name} ({full.removeprefix(name + "-")})'.encode(),
            status_code=200,
            media_type=protocol.TEXT_TYPE,
            headers=SAFE_HEADERS,
        )
