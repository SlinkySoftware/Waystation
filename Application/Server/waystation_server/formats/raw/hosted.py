"""Raw hosted repositories (T-P2-RAW; FORMATS.md "Raw"; SPECIFICATION §4.2).

- ``PUT /repository/<repo>/<path>``: the body is streamed into the blob store (``HostedStore.receive``: a live upload
  transfer, bounded by ``WAYSTATION_MAX_UPLOAD_GB`` and ``Content-Length`` when sent, free-space guard) and recorded
  as the asset ``<path>``. Checksum headers ``X-Checksum-Sha256``, ``X-Checksum-Sha1`` and ``X-Checksum-Md5`` (hex,
  as Nexus and Artifactory clients send them) are verified before anything is stored: a mismatch is a 400 and
  stores nothing. The stored Content-Type comes from the file extension (never the client's), else
  ``application/octet-stream``. Answers ``201`` with the ``ETag`` (``"sha256:<hex>"``) and ``X-Checksum-Sha256``.
- A hard repository quota refuses the upload before any byte is read (``507``, like the low-disk guard).
- Write policy (``hosted.write_policy``): ``allow_redeploy`` replaces an existing file, ``disable_redeploy`` refuses
  to (409; deleting stays allowed), ``read_only`` refuses every change (403).
- ``GET``/``HEAD``: the stored file by X-Accel (conditional requests and ranges as for cache hits), with
  ``Content-Disposition`` per the repository option and ``nosniff``. ``DELETE``: removes the file (204); blob GC
  frees the bytes once nothing references them. Paths ending in ``/`` are directory listings (``listing``).

Uploads need ``write`` and deletes ``delete`` (the router checked them from the method). Each upload and delete is
audited (``content.upload`` / ``content.delete``) and shown in the activity feed.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from starlette.responses import Response

from waystation_common.errors import (
    Conflict,
    DigestMismatch,
    Forbidden,
    NotFound,
    ValidationFailed,
)
from waystation_common.events import CacheState, EventType
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.raw import files
from waystation_server.problems import native_error
from waystation_server.repos.model import StoredHosted, WritePolicy

CHECKSUM_HEADERS = {'sha256': 'x-checksum-sha256', 'sha1': 'x-checksum-sha1', 'md5': 'x-checksum-md5'}
_HEX_LENGTH = {'sha256': 64, 'sha1': 40, 'md5': 32}
_HEX = re.compile(r'[0-9a-f]+')


def expected_checksums(headers: Any) -> dict[str, str]:
    """The checksums a client asked the server to verify (raises ``ValidationFailed`` for a malformed one)."""
    out: dict[str, str] = {}
    for alg, name in CHECKSUM_HEADERS.items():
        value = headers.get(name)
        if value is None:
            continue
        value = value.strip().lower()
        if len(value) != _HEX_LENGTH[alg] or not _HEX.fullmatch(value):
            raise ValidationFailed(f'{name} must be {_HEX_LENGTH[alg]} hexadecimal digits')
        out[alg] = value
    return out


def stored_type(path: str) -> str:
    return files.type_by_extension(path) or files.DEFAULT_TYPE


class RawHosted:
    async def handle(self, req: FormatRequest) -> Response:
        match req.method:
            case 'GET' | 'HEAD':
                return await self.get(req)
            case 'PUT':
                return await self.put(req)
            case 'DELETE':
                return await self.delete(req)
        return native_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD, PUT, DELETE'})

    # ------------------------------------------------------------------ reading
    async def get(self, req: FormatRequest) -> Response:
        options = files.options_of(req.repo)
        asset = await req.hosted.get(req.repo, req.path)
        if asset is None:
            req.hosted.account(req, CacheState.HIT, 404, 0, time.perf_counter())
            raise NotFound('Not Found')
        response = req.hosted.serve(req, asset)
        served = files.safe_type(asset.content_type, options.content_disposition)
        response.headers['Content-Type'] = served
        if 'x-ws-content-type' in response.headers:
            response.headers['X-Ws-Content-Type'] = served
        return files.finish(response, req.path, options)

    # ------------------------------------------------------------------ writing
    @staticmethod
    def _policy(req: FormatRequest) -> StoredHosted:
        hosted = req.repo.hosted or StoredHosted()
        if hosted.write_policy is WritePolicy.READ_ONLY:
            raise Forbidden('this repository is read-only')
        return hosted

    @staticmethod
    def _file_path(req: FormatRequest) -> str:
        if not req.path or req.request.url.path.endswith('/'):
            raise ValidationFailed('a file path is required (the URL names a folder)')
        return req.path

    async def put(self, req: FormatRequest) -> Response:
        hosted = self._policy(req)
        path = self._file_path(req)
        expected = expected_checksums(req.request.headers)
        existing = await req.hosted.get(req.repo, path)
        if existing is not None and hosted.write_policy is WritePolicy.DISABLE_REDEPLOY:
            raise Conflict(f'{path} exists and this repository does not allow redeploying')
        length = req.request.headers.get('content-length')
        total = int(length) if length is not None and length.isdigit() else None
        await req.hosted.admit(req.repo, total)  # a hard quota refuses before any byte is accepted (507)
        extra = {alg: hashlib.new(alg, usedforsecurity=False) for alg in expected if alg != 'sha256'}

        async def body() -> AsyncIterator[bytes]:
            async for chunk in req.request.stream():
                for hasher in extra.values():  # incremental, per chunk (ARCHITECTURE §9)
                    hasher.update(chunk)
                yield chunk

        started = time.perf_counter()
        try:
            async with await req.hosted.blobs.open_writer(
                expected_sha256=expected.get('sha256'), expected_size=total
            ) as writer:
                await req.hosted.receive(req, writer, path=path, artifact=path, total=total, body=body())
                for alg, hasher in extra.items():
                    if hasher.hexdigest() != expected[alg]:
                        raise ValidationFailed(f'the upload does not match {CHECKSUM_HEADERS[alg]}')
                ref = await req.hosted.blobs.commit(writer)
        except DigestMismatch as exc:  # from the commit: Content-Length or X-Checksum-Sha256
            sha = exc.detail.startswith('sha256')
            raise ValidationFailed('the upload does not match x-checksum-sha256' if sha else exc.detail) from None
        if hosted.write_policy is WritePolicy.DISABLE_REDEPLOY and await req.hosted.get(req.repo, path) is not None:
            raise Conflict(f'{path} exists and this repository does not allow redeploying')  # a concurrent upload won
        checksums = {alg: hasher.hexdigest() for alg, hasher in extra.items()}
        asset = await req.hosted.record(req.repo, path, ref, content_type=stored_type(path))
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('raw_file', asset.id, f'{req.repo.name}/{path}'),
            details={
                'repository': req.repo.name,
                'path': path,
                'size': ref.size,
                'sha256': ref.sha256,
                **checksums,
                'replaced': existing is not None,
            },
        )
        req.hosted.activity(req, EventType.ASSET_CREATED, path=path, artifact=path, size=ref.size)
        req.hosted.account(req, CacheState.HIT, 201, 0, started)
        headers = {'ETag': f'"sha256:{ref.sha256}"', 'X-Checksum-Sha256': ref.sha256, 'Content-Length': '0'}
        return Response(status_code=201, headers=headers)

    async def delete(self, req: FormatRequest) -> Response:
        self._policy(req)
        path = self._file_path(req)
        removed = await req.hosted.remove(req.repo, path)
        if removed is None:
            raise NotFound('Not Found')
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target('raw_file', removed.id, f'{req.repo.name}/{path}'),
            details={'repository': req.repo.name, 'path': path, 'size': removed.size, 'sha256': removed.blob_sha256},
        )
        req.hosted.activity(req, EventType.ASSET_DELETED, path=path, artifact=path, size=removed.size)
        req.hosted.account(req, CacheState.HIT, 204, 0, time.perf_counter())
        return Response(status_code=204)
