"""Hosted Helm repositories (T-P5-HELM; FORMATS.md "Helm"; SPECIFICATION §4.2).

- **Upload**: ``PUT /repository/<repo>/<file>.tgz`` (``curl --upload-file``; the body is the chart package) or the
  ChartMuseum API ``POST /repository/<repo>/api/charts`` (``helm cm-push``: multipart fields ``chart`` and optional
  ``prov``, or the package as the raw body). The package is streamed into the blob store (bounded by
  ``MAX_CHART_BYTES``, hard quota checked first), then read safely (``formats.helm.chart``: tar-slip guards, size
  limits, safe YAML) and stored as ``<name>-<version>.tgz`` whatever the request path was. Write policy:
  ``allow_redeploy`` replaces a version, ``disable_redeploy`` refuses an existing one (409), ``read_only`` refuses
  every change (403).
- **Provenance**: ``PUT <name>-<version>.tgz.prov`` (or the multipart ``prov`` field) next to an existing chart.
- **Delete**: ``DELETE <name>-<version>.tgz`` or ``DELETE api/charts/<name>/<version>``: the chart, its provenance
  and its index entry.
- **Index**: each upload or delete writes the chart's ``helm_charts`` record and an ``index.helm`` job into the
  outbox in one MongoDB transaction; a worker regenerates ``index.yaml`` and publishes it atomically
  (``waystation_worker.helm``), usually within a few seconds. Before the first index exists ``index.yaml`` answers
  an empty index.

Uploads need ``write``, deletes ``delete`` (the router checked them from the method); both are audited
(``content.upload`` / ``content.delete``) and shown in the activity feed.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from starlette.responses import JSONResponse, Response

from waystation_common.db.client import Session
from waystation_common.errors import Conflict, Forbidden, NotFound, PayloadTooLarge, ValidationFailed
from waystation_common.events import CacheState, EventType
from waystation_common.jobs import HelmIndex
from waystation_common.storage import BlobRef
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.db import helm as helm_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.helm import chart, index
from waystation_server.problems import native_error
from waystation_server.repos.model import RepoConfig, StoredHosted, WritePolicy

MAX_CHART_BYTES = 64 * 1024 * 1024
MAX_PROV_BYTES = 1024 * 1024
CHART_TYPE = 'application/gzip'
PROV_TYPE = 'application/pgp-signature'
_API_CHARTS = re.compile(r'^api/(?:.+/)?charts$')
_API_DELETE = re.compile(r'^api/(?:.+/)?charts/([^/]+)/([^/]+)$')
_BOUNDARY = re.compile(r'boundary="?([^";]{1,200})"?', re.IGNORECASE)


def parse_multipart(content_type: str, body: bytes) -> dict[str, bytes]:
    """The parts of a ``multipart/form-data`` body by field name (the first of each name)."""
    match = _BOUNDARY.search(content_type)
    if match is None:
        raise ValidationFailed('multipart body without a boundary')
    delimiter = b'--' + match.group(1).encode('latin-1')
    out: dict[str, bytes] = {}
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b'--'):
            break
        head, sep, content = chunk.partition(b'\r\n\r\n')
        if not sep:
            raise ValidationFailed('malformed multipart body')
        if content.endswith(b'\r\n'):
            content = content[:-2]
        name = re.search(rb'(?im)^content-disposition:.*?\bname="([^"]*)"', head)
        if name is not None:
            out.setdefault(name.group(1).decode('latin-1'), content)
    return out


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


class HelmHosted:
    async def handle(self, req: FormatRequest) -> Response:
        method, path = req.method, req.path
        if method in ('GET', 'HEAD'):
            return await self.get(req)
        if method == 'PUT' and path.endswith('.tgz.prov'):
            return await self.put_prov(req)
        if method == 'PUT' and path.endswith('.tgz'):
            return await self.upload(req)
        if method == 'POST' and _API_CHARTS.match(path):
            return await self.upload(req, api=True)
        if method == 'DELETE' and (path.endswith('.tgz') or _API_DELETE.match(path)):
            return await self.delete(req)
        return native_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD, PUT, POST, DELETE'})

    # ------------------------------------------------------------------ reading
    async def get(self, req: FormatRequest) -> Response:
        asset = await req.hosted.get(req.repo, req.path)
        if asset is not None:
            return req.hosted.serve(req, asset)
        started = time.perf_counter()
        if req.path == index.INDEX_PATH:  # nothing published yet: an empty index
            body = index.empty_index(datetime.now(UTC))
            req.hosted.account(req, CacheState.HIT, 200, 0 if req.is_head else len(body), started)
            headers = {'Content-Length': str(len(body)), 'Cache-Control': 'no-cache'}
            return Response(b'' if req.is_head else body, media_type=index.INDEX_TYPE, headers=headers)
        req.hosted.account(req, CacheState.HIT, 404, 0, started)
        raise NotFound('Not Found')

    # ------------------------------------------------------------------ writing
    @staticmethod
    def _policy(req: FormatRequest) -> StoredHosted:
        hosted = req.repo.hosted or StoredHosted()
        if hosted.write_policy is WritePolicy.READ_ONLY:
            raise Forbidden('this repository is read-only')
        return hosted

    @staticmethod
    def _length(req: FormatRequest) -> int | None:
        length = req.request.headers.get('content-length')
        return int(length) if length is not None and length.isdigit() else None

    async def _buffer(self, req: FormatRequest, limit: int) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in req.request.stream():
            size += len(chunk)
            if size > limit:
                raise PayloadTooLarge('the upload is too large')
            chunks.append(chunk)
        return b''.join(chunks)

    async def upload(self, req: FormatRequest, *, api: bool = False) -> Response:
        hosted = self._policy(req)
        total = self._length(req)
        if total is not None and total > MAX_CHART_BYTES:
            raise PayloadTooLarge('the chart package is too large')
        await req.hosted.admit(req.repo, total)  # a hard quota refuses before any byte is accepted (507)
        prov: bytes | None = None
        content_type = req.request.headers.get('content-type', '')
        body: AsyncIterator[bytes] | None = None
        if api and content_type.lower().startswith('multipart/form-data'):
            parts = parse_multipart(content_type, await self._buffer(req, MAX_CHART_BYTES + MAX_PROV_BYTES + 65536))
            if 'chart' not in parts:
                raise ValidationFailed('the multipart body has no "chart" field')
            prov = parts.get('prov')
            if prov is not None and len(prov) > MAX_PROV_BYTES:
                raise PayloadTooLarge('the provenance file is too large')
            body, total = _once(parts['chart']), len(parts['chart'])
        artifact = req.path.rsplit('/', 1)[-1] if not api else 'chart.tgz'
        async with await req.hosted.blobs.open_writer(max_size=MAX_CHART_BYTES, expected_size=total) as writer:
            await req.hosted.receive(req, writer, path=req.path, artifact=artifact, total=total, body=body)
            ref = await req.hosted.blobs.commit(writer)
        meta = await asyncio.to_thread(chart.read_chart, req.hosted.blobs.blob_path(ref.sha256))
        existing = await helm_db.get_version(req.hosted.db, req.repo.id, meta.name, meta.version)
        if existing is not None and hosted.write_policy is WritePolicy.DISABLE_REDEPLOY:
            raise Conflict(f'{meta.name} {meta.version} exists and this repository does not allow redeploying')
        await self.save(req, meta, ref)
        if prov is not None:
            await self._store_prov(req, meta.filename + '.prov', prov)
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('helm_chart', None, f'{req.repo.name}/{meta.name}:{meta.version}'),
            details={
                'repository': req.repo.name,
                'chart': meta.name,
                'version': meta.version,
                'size': ref.size,
                'sha256': ref.sha256,
                'replaced': existing is not None,
            },
        )
        req.hosted.activity(req, EventType.ASSET_CREATED, path=meta.filename, artifact=meta.filename, size=ref.size)
        req.hosted.account(req, CacheState.HIT, 201, 0, time.perf_counter())
        headers = {'ETag': f'"sha256:{ref.sha256}"', 'X-Checksum-Sha256': ref.sha256, 'Location': meta.filename}
        return JSONResponse({'saved': True}, status_code=201, headers=headers)

    async def save(self, req: FormatRequest, meta: chart.ChartMeta, ref: BlobRef) -> None:
        """Record the chart file, then its ``helm_charts`` entry and the index job in one transaction."""
        repo = req.repo
        await req.hosted.record(repo, meta.filename, ref, content_type=CHART_TYPE)
        record: dict[str, Any] = {
            'name': meta.name,
            'version': meta.version,
            'version_sort': index.version_sort(meta.version),
            'path': meta.filename,
            'digest': ref.sha256,
            'size': ref.size,
            'created': datetime.now(UTC),
            'entry': meta.entry,
            'uploaded_by': req.principal.display,
            **index.summary(meta.entry),
        }

        async def write(session: Session) -> None:
            await helm_db.save_hosted(req.hosted.db, repo.id, record, session=session)
            await self._reindex(req, repo, session)

        await req.hosted.transaction(write)

    @staticmethod
    async def _reindex(req: FormatRequest, repo: RepoConfig, session: Session) -> None:
        if req.hosted.jobs is not None:
            await req.hosted.jobs.enqueue_durable(HelmIndex(repo_id=repo.id), session=session)

    async def _store_prov(self, req: FormatRequest, path: str, data: bytes) -> None:
        async with await req.hosted.blobs.open_writer(max_size=MAX_PROV_BYTES, expected_size=len(data)) as writer:
            await writer.write(data)
            ref = await req.hosted.blobs.commit(writer)
        await req.hosted.record(req.repo, path, ref, content_type=PROV_TYPE)

    async def put_prov(self, req: FormatRequest) -> Response:
        hosted = self._policy(req)
        path = req.path
        if await helm_db.by_path(req.hosted.db, req.repo.id, path.removesuffix('.prov')) is None:
            raise NotFound('upload the chart before its provenance file')
        if hosted.write_policy is WritePolicy.DISABLE_REDEPLOY and await req.hosted.get(req.repo, path) is not None:
            raise Conflict(f'{path} exists and this repository does not allow redeploying')
        await req.hosted.admit(req.repo, self._length(req))
        data = await self._buffer(req, MAX_PROV_BYTES)
        await self._store_prov(req, path, data)
        req.hosted.activity(req, EventType.ASSET_CREATED, path=path, artifact=path, size=len(data))
        req.hosted.account(req, CacheState.HIT, 201, 0, time.perf_counter())
        return Response(status_code=201, headers={'Content-Length': '0'})

    async def delete(self, req: FormatRequest) -> Response:
        self._policy(req)
        db, repo = req.hosted.db, req.repo
        api = _API_DELETE.match(req.path)
        if api is not None:
            found = await helm_db.get_version(db, repo.id, api.group(1), api.group(2))
        else:
            found = await helm_db.by_path(db, repo.id, req.path)
        if found is None:
            raise NotFound('Not Found')

        async def write(session: Session) -> None:
            await helm_db.delete_version(db, repo.id, found['name'], found['version'], session=session)
            await self._reindex(req, repo, session)

        await req.hosted.transaction(write)
        removed = await req.hosted.remove(repo, found['path'])
        await req.hosted.remove(repo, found['path'] + '.prov')
        size = removed.size if removed is not None else found.get('size')
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target('helm_chart', None, f'{repo.name}/{found["name"]}:{found["version"]}'),
            details={'repository': repo.name, 'chart': found['name'], 'version': found['version'], 'size': size},
        )
        req.hosted.activity(req, EventType.ASSET_DELETED, path=found['path'], artifact=found['path'], size=size)
        req.hosted.account(req, CacheState.HIT, 200 if api else 204, 0, time.perf_counter())
        if api is not None:
            return JSONResponse({'deleted': True})
        return Response(status_code=204)
