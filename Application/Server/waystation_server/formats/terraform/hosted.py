"""The hosted Terraform repository (T-P5-TFM; FORMATS.md "Terraform / OpenTofu"; SPECIFICATION §4.2): a private
module registry and a private provider registry. One per instance: ``/.well-known/terraform.json`` points at it.

**Modules** (Module Registry Protocol below ``v1/modules/``):

- ``PUT v1/modules/<namespace>/<name>/<system>/<version>`` (``curl --upload-file module.tar.gz``): a ``.tar.gz`` whose
  root is the module (``modules.check_module_archive``: read in a thread without extracting, bounded, tar-slip and link
  checks), stored as ``.../<version>/archive.tar.gz``. Versions are immutable (``409``; delete first); ``read_only``
  refuses every change (``403``).
- ``GET .../versions``, ``GET .../<version>/download`` (``204`` with ``X-Terraform-Get: ./archive.tar.gz``, relative to
  the download URL, with a signed download grant for a signed-in caller: adr/0009, proposed) and the archive itself
  (X-Accel).
- ``DELETE v1/modules/<namespace>/<name>/<system>/<version>``.

**Providers** (Provider Registry Protocol below ``v1/providers/``):

- ``POST v1/providers/<namespace>/<type>/<version>`` (multipart): one ``archive`` part per platform, named
  ``terraform-provider-<type>_<version>_<os>_<arch>.zip``, the ``shasums`` (``SHA256SUMS``) listing every uploaded
  archive with its sha256, its detached OpenPGP ``signature`` (binary or armored) and the ASCII-armored public
  ``signing_key`` it verifies with (``waystation_common.openpgp``, as Terraform will check it), optional ``protocols``
  (default ``5.0``). Releases are immutable (``409``).
- ``GET .../versions``, ``GET .../<version>/download/<os>/<arch>`` (the download document: file URLs relative to it,
  with download grants; ``signing_keys`` = the uploaded key) and the files (X-Accel).
- ``DELETE v1/providers/<namespace>/<type>/<version>``: the release and its files.

Uploads need ``write``, deletes ``delete`` (checked by the router from the method); both are audited
(``content.upload`` / ``content.delete``) and shown in the activity feed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse, Response

from waystation_common import openpgp
from waystation_common.errors import Conflict, Forbidden, NotFound, PayloadTooLarge, UpstreamError, ValidationFailed
from waystation_common.events import CacheState, EventType
from waystation_common.storage import BlobRef
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.db import terraform as tf_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.common.multipart import MultipartError, MultipartReader, boundary_of
from waystation_server.formats.terraform import modules, paths, registry, ziphash
from waystation_server.formats.terraform.paths import HostedProvider, Module, RegistryOp, RegistryRoute
from waystation_server.problems import native_error
from waystation_server.repos.model import StoredHosted, WritePolicy

JSON = 'application/json'
TARGZ = 'application/gzip'
ZIP = 'application/zip'
TEXT = 'text/plain; charset=utf-8'
OCTETS = 'application/octet-stream'
MAX_PROVIDER_ARCHIVE = 1024 * 1024 * 1024
MAX_ARCHIVES = 64
MAX_KEY = 256 * 1024
_PROTOCOL = re.compile(r'^[0-9]{1,2}\.[0-9]{1,2}$')
DEFAULT_PROTOCOLS = ('5.0',)


def _etag_response(req: FormatRequest, body: bytes, started: float) -> Response:
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {'ETag': etag, 'Cache-Control': 'no-cache'}
    match = req.request.headers.get('if-none-match')
    if match and etag in {m.strip() for m in match.split(',')}:
        req.hosted.account(req, CacheState.HIT, 304, 0, started)
        return Response(status_code=304, headers=headers)
    req.hosted.account(req, CacheState.HIT, 200, 0 if req.is_head else len(body), started)
    if req.is_head:
        headers['Content-Length'] = str(len(body))
        return Response(status_code=200, headers=headers, media_type=JSON)
    return Response(body, status_code=200, headers=headers, media_type=JSON)


def latest_of(versions: list[str]) -> str | None:
    return registry.latest_of(versions)


@dataclass
class _Upload:
    archives: dict[str, BlobRef]  # file name -> stored blob
    shasums: bytes | None = None
    signature: bytes | None = None
    signing_key: bytes | None = None
    protocols: tuple[str, ...] = DEFAULT_PROTOCOLS


class TerraformHosted:
    async def handle(self, req: FormatRequest, route: RegistryRoute) -> Response:
        method, op = req.method, route.op
        reading = method in ('GET', 'HEAD')
        match op:
            case RegistryOp.MODULE_VERSIONS if reading:
                return await self.module_versions(req, _module(route))
            case RegistryOp.MODULE_DOWNLOAD if reading:
                return await self.module_download(req, _module(route), route.version)
            case RegistryOp.MODULE_ARCHIVE if reading:
                return await self.serve_file(req, paths.module_archive_key(_module(route), route.version))
            case RegistryOp.MODULE_VERSION if method == 'PUT':
                return await self.upload_module(req, _module(route), route.version)
            case RegistryOp.MODULE_VERSION if method == 'DELETE':
                return await self.delete_module(req, _module(route), route.version)
            case RegistryOp.PROVIDER_VERSIONS if reading:
                return await self.provider_versions(req, _provider(route))
            case RegistryOp.PROVIDER_DOWNLOAD if reading:
                return await self.provider_download(req, route)
            case RegistryOp.PROVIDER_FILE if reading:
                key = paths.provider_file_key(_provider(route), route.version, route.filename)
                return await self.serve_file(req, key)
            case RegistryOp.PROVIDER_VERSION if method == 'POST':
                return await self.upload_provider(req, _provider(route), route.version)
            case RegistryOp.PROVIDER_VERSION if method == 'DELETE':
                return await self.delete_provider(req, _provider(route), route.version)
        allow = {
            RegistryOp.MODULE_VERSION: 'PUT, DELETE',
            RegistryOp.PROVIDER_VERSION: 'POST, DELETE',
        }.get(op, 'GET, HEAD')
        return native_error(405, 'method not allowed', headers={'Allow': allow})

    # ------------------------------------------------------------------ reading
    async def serve_file(self, req: FormatRequest, path: str) -> Response:
        asset = await req.hosted.get(req.repo, path)
        if asset is None:
            req.hosted.account(req, CacheState.HIT, 404, 0, time.perf_counter())
            raise NotFound('Not Found')
        return req.hosted.serve(req, asset)

    async def module_versions(self, req: FormatRequest, module: Module) -> Response:
        started = time.perf_counter()
        doc = await tf_db.get_module(req.hosted.db, req.repo.id, module.key)
        versions = [str(v['version']) for v in (doc or {}).get('versions') or []]
        if not versions:
            raise NotFound(f'not found: no module {module.key}')
        return _etag_response(req, modules.render_module_versions(versions), started)

    async def module_download(self, req: FormatRequest, module: Module, version: str) -> Response:
        started = time.perf_counter()
        doc = await tf_db.get_module(req.hosted.db, req.repo.id, module.key)
        entry = next((v for v in (doc or {}).get('versions') or [] if v.get('version') == version), None)
        if entry is None:
            raise NotFound(f'not found: {module.key} has no version {version}')
        # the grant names the path the client will ask for (the download URL's, as it was written)
        location = f'./archive.tar.gz{req.download_query(sibling(req.path, paths.ARCHIVES[0]))}'
        req.hosted.account(req, CacheState.HIT, 204, 0, started)
        return Response(status_code=204, headers={'X-Terraform-Get': location, 'Cache-Control': 'no-store'})

    async def provider_versions(self, req: FormatRequest, provider: HostedProvider) -> Response:
        started = time.perf_counter()
        doc = await tf_db.get_provider(req.hosted.db, req.repo.id, provider.key)
        releases = (doc or {}).get('releases') or []
        if not releases:
            raise NotFound(f'not found: no provider {provider.key}')
        by_version = {str(r['version']): r for r in releases}
        items = [
            {
                'version': version,
                'protocols': list(by_version[version].get('protocols') or DEFAULT_PROTOCOLS),
                'platforms': [{'os': p['os'], 'arch': p['arch']} for p in by_version[version].get('platforms') or []],
            }
            for version in registry.newest_first(list(by_version))
        ]
        return _etag_response(req, json.dumps({'versions': items}, separators=(',', ':')).encode(), started)

    async def provider_download(self, req: FormatRequest, route: RegistryRoute) -> Response:
        started = time.perf_counter()
        provider, version = _provider(route), route.version
        doc = await tf_db.get_provider(req.hosted.db, req.repo.id, provider.key)
        release = next((r for r in (doc or {}).get('releases') or [] if r.get('version') == version), None)
        platform = next(
            (p for p in (release or {}).get('platforms') or [] if p['os'] == route.os and p['arch'] == route.arch),
            None,
        )
        if release is None or platform is None:
            raise NotFound(f'not found: {provider.key} {version} has no {route.os}_{route.arch} package')
        sums = paths.provider_filename(provider, version, sums='SHA256SUMS')
        sig = paths.provider_filename(provider, version, sums='SHA256SUMS.sig')

        release_path = req.path.rsplit('/', 3)[0]  # .../<version>, as the client wrote it

        def url(filename: str) -> str:  # relative to .../<version>/download/<os>/<arch>
            return f'../../{filename}{req.download_query(f"{release_path}/{filename}")}'

        body = {
            'protocols': list(release.get('protocols') or DEFAULT_PROTOCOLS),
            'os': route.os,
            'arch': route.arch,
            'filename': platform['filename'],
            'download_url': url(platform['filename']),
            'shasums_url': url(sums),
            'shasums_signature_url': url(sig),
            'shasum': platform['sha256'],
            'signing_keys': {
                'gpg_public_keys': [
                    {
                        'key_id': release.get('key_id', ''),
                        'ascii_armor': release.get('signing_key', ''),
                        'trust_signature': '',
                        'source': '',
                        'source_url': None,
                    }
                ]
            },
        }
        data = json.dumps(body, separators=(',', ':')).encode()
        req.hosted.account(req, CacheState.HIT, 200, 0 if req.is_head else len(data), started)
        headers = {'Cache-Control': 'no-store', 'Content-Length': str(len(data))}
        return Response(b'' if req.is_head else data, status_code=200, media_type=JSON, headers=headers)

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

    async def upload_module(self, req: FormatRequest, module: Module, version: str) -> Response:
        self._policy(req)
        db, repo = req.hosted.db, req.repo
        existing = await tf_db.get_module(db, repo.id, module.key)
        if any(v.get('version') == version for v in (existing or {}).get('versions') or []):
            raise Conflict(f'{module.key} {version} exists: versions cannot be replaced (delete it first)')
        total = self._length(req)
        if total is not None and total > modules.MAX_UPLOAD:
            raise PayloadTooLarge('the module archive is too large (64 MiB at most)')
        await req.hosted.admit(repo, total)
        key = paths.module_archive_key(module, version)
        async with await req.hosted.blobs.open_writer(max_size=modules.MAX_UPLOAD, expected_size=total) as writer:
            await req.hosted.receive(
                req, writer, path=key, artifact=f'{module.key} {version}', component=module.key, total=total
            )
            ref = await req.hosted.blobs.commit(writer)
        info = await asyncio.to_thread(modules.check_module_archive, req.hosted.blobs.blob_path(ref.sha256))
        entry = {
            'version': version,
            'filename': paths.ARCHIVES[0],
            'sha256': ref.sha256,
            'size': ref.size,
            'files': info.files,
            'uploaded_by': req.principal.display,
            'created_at': datetime.now(UTC),
        }
        if not await tf_db.add_module_version(db, repo.id, module.key, entry):
            raise Conflict(f'{module.key} {version} exists: versions cannot be replaced (delete it first)')
        await req.hosted.record(repo, key, ref, content_type=TARGZ)
        await self._module_summary(req, module)
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('terraform_module', None, f'{repo.name}/{module.key}:{version}'),
            details={
                'repository': repo.name,
                'module': module.key,
                'version': version,
                'size': ref.size,
                'sha256': ref.sha256,
            },
        )
        req.hosted.activity(req, EventType.ASSET_CREATED, path=key, artifact=f'{module.key} {version}', size=ref.size)
        req.hosted.account(req, CacheState.HIT, 201, 0, time.perf_counter())
        body = {'module': module.key, 'version': version, 'sha256': ref.sha256, 'size': ref.size}
        return JSONResponse(body, status_code=201, headers={'X-Checksum-Sha256': ref.sha256})

    async def _module_summary(self, req: FormatRequest, module: Module) -> None:
        doc = await tf_db.get_module(req.hosted.db, req.repo.id, module.key)
        versions = [str(v['version']) for v in (doc or {}).get('versions') or []]
        if versions:
            await tf_db.set_module_summary(
                req.hosted.db, req.repo.id, module.key, latest=latest_of(versions), count=len(versions)
            )

    async def delete_module(self, req: FormatRequest, module: Module, version: str) -> Response:
        self._policy(req)
        repo = req.repo
        removed = await tf_db.remove_module_version(req.hosted.db, repo.id, module.key, version)
        if removed is None:
            raise NotFound(f'not found: {module.key} has no version {version}')
        key = paths.module_archive_key(module, version)
        await req.hosted.remove(repo, key)
        await self._module_summary(req, module)
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target('terraform_module', None, f'{repo.name}/{module.key}:{version}'),
            details={'repository': repo.name, 'module': module.key, 'version': version, 'size': removed.get('size')},
        )
        req.hosted.activity(
            req, EventType.ASSET_DELETED, path=key, artifact=f'{module.key} {version}', size=removed.get('size')
        )
        req.hosted.account(req, CacheState.HIT, 204, 0, time.perf_counter())
        return Response(status_code=204)

    # ------------------------------------------------------------------ providers
    async def upload_provider(self, req: FormatRequest, provider: HostedProvider, version: str) -> Response:
        self._policy(req)
        db, repo = req.hosted.db, req.repo
        existing = await tf_db.get_provider(db, repo.id, provider.key)
        if any(r.get('version') == version for r in (existing or {}).get('releases') or []):
            raise Conflict(f'{provider.key} {version} exists: releases cannot be replaced (delete it first)')
        await req.hosted.admit(repo, self._length(req))
        upload = await self._read_provider_upload(req, provider, version)
        release = await self._check_provider_upload(req, provider, version, upload)
        if not await tf_db.add_provider_release(db, repo.id, provider.key, release):
            raise Conflict(f'{provider.key} {version} exists: releases cannot be replaced (delete it first)')
        for filename, ref in upload.archives.items():
            await req.hosted.record(repo, paths.provider_file_key(provider, version, filename), ref, content_type=ZIP)
        for suffix, data, kind in (('SHA256SUMS', upload.shasums, TEXT), ('SHA256SUMS.sig', upload.signature, OCTETS)):
            name = paths.provider_filename(provider, version, sums=suffix)
            await self._store_small(req, paths.provider_file_key(provider, version, name), data or b'', kind)
        await self._provider_summary(req, provider)
        size = sum(ref.size for ref in upload.archives.values())
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('terraform_provider', None, f'{repo.name}/{provider.key}:{version}'),
            details={
                'repository': repo.name,
                'provider': provider.key,
                'version': version,
                'platforms': [f'{p["os"]}_{p["arch"]}' for p in release['platforms']],
                'size': size,
                'key_id': release['key_id'],
            },
        )
        req.hosted.activity(
            req,
            EventType.ASSET_CREATED,
            path=paths.provider_prefix(provider) + version,
            artifact=f'{provider.key} {version}',
            size=size,
        )
        req.hosted.account(req, CacheState.HIT, 201, 0, time.perf_counter())
        body = {
            'provider': provider.key,
            'version': version,
            'platforms': [f'{p["os"]}_{p["arch"]}' for p in release['platforms']],
            'key_id': release['key_id'],
        }
        return JSONResponse(body, status_code=201)

    async def _read_provider_upload(self, req: FormatRequest, provider: HostedProvider, version: str) -> _Upload:
        try:
            boundary = boundary_of(req.request.headers.get('content-type'))
        except MultipartError as exc:
            raise ValidationFailed(f'{exc.detail}: send the release as multipart/form-data') from None
        reader = MultipartReader(req.request.stream(), boundary)
        upload = _Upload(archives={})
        limits = {'shasums': registry.MAX_SUMS, 'signature': registry.MAX_SIGNATURE, 'signing_key': MAX_KEY}
        async for part in reader.parts():
            if part.name == 'archive':
                filename = (part.filename or '').rsplit('/', 1)[-1]
                match = paths.PROVIDER_FILE.match(filename)
                if match is None or not match['os'] or match['type'] != provider.type or match['version'] != version:
                    raise ValidationFailed(
                        f'archive {filename[:200]!r} is not named '
                        f'{paths.provider_filename(provider, version, "<os>", "<arch>")}'
                    )
                if filename in upload.archives or len(upload.archives) >= MAX_ARCHIVES:
                    raise ValidationFailed(f'archive {filename} is sent twice, or too many archives')
                key = paths.provider_file_key(provider, version, filename)
                async with await req.hosted.blobs.open_writer(max_size=MAX_PROVIDER_ARCHIVE) as writer:
                    await req.hosted.receive(
                        req, writer, path=key, artifact=filename, component=provider.key, body=reader.chunks()
                    )
                    upload.archives[filename] = await req.hosted.blobs.commit(writer)
            elif part.name in limits:
                data = bytearray()
                async for chunk in reader.chunks():
                    data += chunk
                    if len(data) > limits[part.name]:
                        raise PayloadTooLarge(f'the {part.name} part is too large')
                setattr(upload, part.name, bytes(data))
            elif part.name == 'protocols':
                text = (await reader.text()).replace(',', ' ').split()
                if not text or len(text) > 8 or not all(_PROTOCOL.match(p) for p in text):
                    raise ValidationFailed('protocols: plugin protocol versions such as "5.0" or "6.0"')
                upload.protocols = tuple(dict.fromkeys(text))
            else:
                await reader.drain()
        if not upload.archives:
            raise ValidationFailed('no archive part: send each platform package as "archive"')
        for name in ('shasums', 'signature', 'signing_key'):
            if not getattr(upload, name):
                raise ValidationFailed(f'the "{name}" part is missing')
        return upload

    async def _check_provider_upload(
        self, req: FormatRequest, provider: HostedProvider, version: str, upload: _Upload
    ) -> dict[str, Any]:
        """The release record, once ``SHA256SUMS`` lists every archive with its sha256 and its signature verifies
        with the uploaded key (as Terraform and OpenTofu will check them). Raises ``ValidationFailed``."""
        try:
            listed = registry.parse_sums(upload.shasums or b'')
        except UpstreamError as exc:
            raise ValidationFailed(f'shasums: {exc.detail}') from None
        for filename, ref in upload.archives.items():
            if listed.get(filename) != ref.sha256:
                raise ValidationFailed(f'shasums does not list {filename} with its sha256 ({ref.sha256})')
        verified = await asyncio.to_thread(_verify_signature, upload.shasums or b'', upload)
        platforms = []
        for filename, ref in sorted(upload.archives.items()):
            match = paths.PROVIDER_FILE.match(filename)
            assert match is not None  # checked when the part arrived
            h1 = await asyncio.to_thread(_zip_h1, req.hosted.blobs.blob_path(ref.sha256), filename)
            platforms.append(
                {
                    'os': match['os'],
                    'arch': match['arch'],
                    'filename': filename,
                    'sha256': ref.sha256,
                    'size': ref.size,
                    'h1': h1,
                }
            )
        return {
            'version': version,
            'protocols': list(upload.protocols),
            'platforms': platforms,
            'key_id': verified,
            'signing_key': (upload.signing_key or b'').decode('ascii', errors='replace'),
            'uploaded_by': req.principal.display,
            'created_at': datetime.now(UTC),
        }

    async def _store_small(self, req: FormatRequest, path: str, data: bytes, content_type: str) -> None:
        async with await req.hosted.blobs.open_writer(max_size=len(data) + 1, expected_size=len(data)) as writer:
            await writer.write(data)
            ref = await req.hosted.blobs.commit(writer)
        await req.hosted.record(req.repo, path, ref, content_type=content_type)

    async def _provider_summary(self, req: FormatRequest, provider: HostedProvider) -> None:
        doc = await tf_db.get_provider(req.hosted.db, req.repo.id, provider.key)
        versions = [str(r['version']) for r in (doc or {}).get('releases') or []]
        if versions:
            await tf_db.set_provider_summary(
                req.hosted.db, req.repo.id, provider.key, latest=latest_of(versions), count=len(versions)
            )

    async def delete_provider(self, req: FormatRequest, provider: HostedProvider, version: str) -> Response:
        self._policy(req)
        repo = req.repo
        removed = await tf_db.remove_provider_release(req.hosted.db, repo.id, provider.key, version)
        if removed is None:
            raise NotFound(f'not found: {provider.key} has no version {version}')
        files = [p['filename'] for p in removed.get('platforms') or []]
        files += [paths.provider_filename(provider, version, sums=s) for s in ('SHA256SUMS', 'SHA256SUMS.sig')]
        for filename in files:
            await req.hosted.remove(repo, paths.provider_file_key(provider, version, filename))
        await self._provider_summary(req, provider)
        size = sum(int(p.get('size') or 0) for p in removed.get('platforms') or [])
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target('terraform_provider', None, f'{repo.name}/{provider.key}:{version}'),
            details={'repository': repo.name, 'provider': provider.key, 'version': version, 'size': size},
        )
        req.hosted.activity(
            req,
            EventType.ASSET_DELETED,
            path=paths.provider_prefix(provider) + version,
            artifact=f'{provider.key} {version}',
            size=size,
        )
        req.hosted.account(req, CacheState.HIT, 204, 0, time.perf_counter())
        return Response(status_code=204)


def sibling(path: str, name: str) -> str:
    """``path`` with its last segment replaced by ``name`` (``.../<version>/download`` -> ``.../<version>/<name>``)."""
    return f'{path.rsplit("/", 1)[0]}/{name}'


def _module(route: RegistryRoute) -> Module:
    assert route.module is not None
    return route.module


def _provider(route: RegistryRoute) -> HostedProvider:
    assert route.provider is not None
    return route.provider


def _verify_signature(shasums: bytes, upload: _Upload) -> str:
    """The signing key's ID (the primary key's, 16 hex digits) once the signature verifies. Blocking."""
    try:
        certs = openpgp.read_certs(upload.signing_key or b'')
    except openpgp.OpenPgpError as exc:
        raise ValidationFailed(f'signing_key: {exc}') from None
    if not certs:
        raise ValidationFailed('signing_key: no OpenPGP public key found')
    try:
        verified = openpgp.verify_detached(shasums, upload.signature or b'', certs)
        upload.signature = openpgp.dearmor(upload.signature or b'')  # clients expect the binary signature
    except openpgp.OpenPgpError as exc:
        raise ValidationFailed(f'signature: {exc}') from None
    return verified.cert_fingerprint[-16:]


def _zip_h1(path: Path, filename: str) -> str:
    """The archive's ``h1:`` hash (Terraform's package hash); refuses a file that is not a readable zip. Blocking."""
    try:
        with path.open('rb') as fh:
            return ziphash.zip_h1(fh)
    except (ziphash.NotHashable, zipfile.BadZipFile, OSError) as exc:
        raise ValidationFailed(f'{filename} is not a readable zip archive ({type(exc).__name__})') from None
