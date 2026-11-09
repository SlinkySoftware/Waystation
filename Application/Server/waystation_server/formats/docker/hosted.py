"""Docker / OCI hosted repositories (T-P2-DOCKERH; FORMATS.md "Docker / OCI"; OCI Distribution Spec v1.1).

The push and pull side of the registry API for repositories of type ``hosted``. Content lives in the blob store and
is served by X-Accel like a proxy's cache (``waystation_server.hosted.store``); what a repository holds:

- **blobs** (layers, configs, artifact files): assets ``blobs/<digest>``, one per repository (an image of the
  repository can use any blob the repository holds, as with the proxy cache);
- **manifests and indexes**: assets ``<image>/manifests/<digest>`` plus a ``docker_manifests`` record (references,
  ``subject``, ``artifactType``, size, annotations: the referrers API and browsing);
- **tags**: ``docker_tags`` (``image``, ``tag`` → ``manifest_digest``, ``pushed_by``).

Uploads (``POST``/``PATCH``/``PUT``/``GET``/``DELETE /v2/<name>/blobs/uploads/...``): monolithic (``POST ?digest=``
or ``PUT ?digest=`` with the whole body), chunked (``PATCH`` with or without ``Content-Range``, in order), resumable
(``GET`` upload status answers ``Range``), cross-repository mounts (``POST ?mount=<digest>&from=<name>``: the source
is any docker repository the principal may read, proxy caches and groups (their members' copies) included; otherwise
an upload session starts). Upload
sessions are ``upload_sessions`` records naming a resumable upload file (``BlobStore.start_upload``); one request at a
time appends (session lease). Blobs are sha256-verified before they are committed; bodies are bounded by
``WAYSTATION_MAX_UPLOAD_GB``, manifests by 4 MiB.

Manifest ``PUT``: the media type (``Content-Type`` and the document's ``mediaType``) must be a manifest or index
type, the body JSON with ``schemaVersion`` 2; every blob it references (config, layers; not non-distributable layers)
must be in the repository with the size the manifest names, and every manifest an index references must be in the
image. A ``subject`` makes the manifest a referrer (answered with ``OCI-Subject``). Tags follow the repository's
write policy: ``read_only`` refuses every change, ``disable_redeploy`` never moves or deletes a tag, and tags matching
an ``immutable_tag_patterns`` regular expression never move or go away.

Quotas (SPECIFICATION §4.8): when the repository's hard quota is reached, blob uploads (``POST``, ``PATCH``, ``PUT``),
mounts and manifest pushes are refused with ``403 DENIED`` and a message naming the quota (docker shows it as
``denied: ...``); deletes and pulls still work. The low-disk guard answers ``507`` (no space left for new writes).

``DELETE``: a tag (``/manifests/<tag>``), a manifest by digest (with its tags), a blob no manifest of the repository
uses. ``GET /v2/<name>/referrers/<digest>`` (``artifactType`` filter, ``OCI-Filters-Applied``) and
``tags/list`` (``n``/``last``, ``Link``) read the records. Pushed manifests and deletes are audited
(``content.upload`` / ``content.delete``) and shown in the activity feed; blob uploads are live upload transfers.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from starlette.responses import JSONResponse, Response

from waystation_common.errors import DigestMismatch, InsufficientStorage, NotFound, PayloadTooLarge
from waystation_common.events import CacheState, EventType
from waystation_common.storage import BlobRef, BlobWriter, UploadOffsetMismatch
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.db import docker as docker_db
from waystation_server.db import uploads as uploads_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.docker import protocol as p
from waystation_server.formats.docker.browse import parse_manifest
from waystation_server.hosted.store import UploadInterrupted
from waystation_server.problems import oci_error
from waystation_server.proxy.records import StoredAsset
from waystation_server.repos.model import RepoConfig, RepoType, StoredHosted, WritePolicy
from waystation_server.repos.quota import QuotaExceeded
from waystation_server.security.actions import Action

API_HEADERS = {'Docker-Distribution-API-Version': 'registry/2.0'}
EMPTY_JSON = b'{}'
EMPTY_DIGEST = p.digest_of(EMPTY_JSON)  # the OCI empty descriptor (artifacts without a config)
NON_DISTRIBUTABLE = (
    'application/vnd.oci.image.layer.nondistributable.',
    'application/vnd.docker.image.rootfs.foreign.',
)
MAX_ANNOTATIONS = 64


def _error(status: int, message: str, code: str, headers: dict[str, str] | None = None, detail: Any = None) -> Response:
    return oci_error(status, message, code=code, detail=detail, headers={**API_HEADERS, **(headers or {})})


class Rejected(Exception):  # noqa: N818 - reads as an outcome
    def __init__(self, status: int, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail

    def response(self, headers: dict[str, str] | None = None) -> Response:
        return _error(self.status, self.message, self.code, headers, self.detail)


def hosted_config(repo: RepoConfig) -> StoredHosted:
    return repo.hosted or StoredHosted()


def tag_locked(hosted: StoredHosted, tag: str) -> bool:
    """May ``tag`` never move or go away once it exists (write policy, immutable patterns)?"""
    if hosted.write_policy is not WritePolicy.ALLOW_REDEPLOY:
        return True
    return any(re.fullmatch(pattern, tag) for pattern in hosted.immutable_tag_patterns)


def client_prefix(url_path: str, marker: str) -> str:
    """The name part of the request URL as the client wrote it (``/v2/hosted/app``), before ``marker``."""
    path = url_path.rstrip('/')
    index = path.rfind(marker)
    return path[:index] if index >= 0 else path


def client_name(prefix: str) -> str:
    """``/v2/hosted/app`` → ``hosted/app``; ``/repository/hosted/v2/app`` → ``app``."""
    if prefix.startswith('/v2/'):
        return prefix[len('/v2/') :]
    _, _, rest = prefix.partition('/v2/')
    return rest


def _parse_range(value: str | None) -> tuple[int, int] | None:
    """``Content-Range: <start>-<end>`` (OCI chunk ranges are inclusive; ``bytes `` prefix tolerated)."""
    if not value:
        return None
    match = re.fullmatch(r'\s*(?:bytes\s+)?(\d+)-(\d+)(?:/(?:\d+|\*))?\s*', value)
    if match is None:
        raise Rejected(416, 'BLOB_UPLOAD_INVALID', 'invalid Content-Range')
    start, end = int(match.group(1)), int(match.group(2))
    if end < start:
        raise Rejected(416, 'BLOB_UPLOAD_INVALID', 'invalid Content-Range')
    return start, end


def _upload_headers(location: str, session_id: str, size: int) -> dict[str, str]:
    return {
        'Location': location,
        'Range': f'0-{max(size - 1, 0)}',
        'Docker-Upload-UUID': session_id,
        'Content-Length': '0',
        **API_HEADERS,
    }


# ---------------------------------------------------------------------------- manifest checks (thread)
@dataclass(frozen=True)
class CheckedManifest:
    media_type: str
    blobs: tuple[tuple[str, int], ...]  # (digest, size) the repository must hold
    manifests: tuple[str, ...]  # index entries the image must hold
    subject: str | None
    artifact_type: str | None
    annotations: dict[str, str]
    needs_empty: bool  # references the OCI empty JSON blob


def check_pushed_manifest(data: bytes, content_type: str | None) -> CheckedManifest:
    """Validate a pushed manifest or index (pure; runs in a thread). Raises ``Rejected``."""
    media = p.media_type(content_type)
    try:
        doc = json.loads(data)
    except ValueError:
        raise Rejected(400, 'MANIFEST_INVALID', 'manifest is not valid JSON') from None
    if not isinstance(doc, dict):
        raise Rejected(400, 'MANIFEST_INVALID', 'manifest is not a JSON object')
    if doc.get('schemaVersion') != 2:
        raise Rejected(400, 'MANIFEST_INVALID', 'only schemaVersion 2 manifests are accepted')
    declared = doc.get('mediaType')
    if declared is not None and not isinstance(declared, str):
        raise Rejected(400, 'MANIFEST_INVALID', 'mediaType must be a string')
    if not media or media in ('application/json', 'application/octet-stream'):
        media = (declared or '').lower()
    if media not in p.SERVABLE_MANIFEST_TYPES:
        raise Rejected(400, 'MANIFEST_INVALID', f'unsupported manifest media type {media or "(none)"}')
    if declared is not None and declared.lower() != media:
        raise Rejected(400, 'MANIFEST_INVALID', 'Content-Type does not match the manifest mediaType')

    def descriptor(value: Any, what: str) -> tuple[str, int, str]:
        if not isinstance(value, dict):
            raise Rejected(400, 'MANIFEST_INVALID', f'{what} is not a descriptor')
        digest, size, kind = value.get('digest'), value.get('size'), value.get('mediaType', '')
        if not isinstance(digest, str) or not p.DIGEST_RE.fullmatch(digest):
            raise Rejected(400, 'MANIFEST_INVALID', f'{what} has an invalid digest')
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise Rejected(400, 'MANIFEST_INVALID', f'{what} has an invalid size')
        if not isinstance(kind, str):
            raise Rejected(400, 'MANIFEST_INVALID', f'{what} has an invalid mediaType')
        return digest, size, kind

    blobs: list[tuple[str, int]] = []
    manifests: list[str] = []
    if media in (p.OCI_INDEX, p.DOCKER_LIST):
        entries = doc.get('manifests')
        if not isinstance(entries, list):
            raise Rejected(400, 'MANIFEST_INVALID', 'an index needs a manifests list')
        for i, entry in enumerate(entries):
            manifests.append(descriptor(entry, f'manifests[{i}]')[0])
    else:
        if 'config' in doc or media != 'application/vnd.oci.artifact.manifest.v1+json':
            digest, size, _ = descriptor(doc.get('config'), 'config')
            blobs.append((digest, size))
        layers = doc.get('layers', doc.get('blobs', []))
        if not isinstance(layers, list):
            raise Rejected(400, 'MANIFEST_INVALID', 'layers must be a list')
        for i, layer in enumerate(layers):
            digest, size, kind = descriptor(layer, f'layers[{i}]')
            if not kind.startswith(NON_DISTRIBUTABLE):
                blobs.append((digest, size))
    subject: str | None = None
    if 'subject' in doc:
        subject = descriptor(doc['subject'], 'subject')[0]
    artifact_type = doc.get('artifactType')
    if artifact_type is not None and not isinstance(artifact_type, str):
        raise Rejected(400, 'MANIFEST_INVALID', 'artifactType must be a string')
    if artifact_type is None and subject is not None:
        config = doc.get('config')
        if isinstance(config, dict) and isinstance(config.get('mediaType'), str):
            artifact_type = config['mediaType']  # OCI: the referrers list falls back to the config media type
    raw_annotations = doc.get('annotations')
    annotations: dict[str, str] = {}
    if isinstance(raw_annotations, dict):
        for key, value in list(raw_annotations.items())[:MAX_ANNOTATIONS]:
            if isinstance(key, str) and isinstance(value, str):
                annotations[key[:255]] = value[:1024]
    return CheckedManifest(
        media_type=media,
        blobs=tuple(dict.fromkeys(blobs)),
        manifests=tuple(dict.fromkeys(manifests)),
        subject=subject,
        artifact_type=artifact_type[:255] if artifact_type else None,
        annotations=annotations,
        needs_empty=any(digest == EMPTY_DIGEST for digest, _ in blobs),
    )


# ---------------------------------------------------------------------------- the hosted registry
class DockerHosted:
    async def handle(self, req: FormatRequest, op: p.Operation) -> Response:
        try:
            match op.op, req.method:
                case 'base', _:
                    return JSONResponse({}, headers=API_HEADERS)
                case 'manifest', 'GET' | 'HEAD':
                    return await self.get_manifest(req, op)
                case 'manifest', 'PUT':
                    return await self.put_manifest(req, op)
                case 'manifest', 'DELETE':
                    return await self.delete_manifest(req, op)
                case 'blob', 'GET' | 'HEAD':
                    return await self.get_blob(req, op)
                case 'blob', 'DELETE':
                    return await self.delete_blob(req, op)
                case 'upload', 'POST' if not op.reference:
                    return await self.start_upload(req, op)
                case 'upload', 'PATCH' if op.reference:
                    return await self.patch_upload(req, op)
                case 'upload', 'PUT' if op.reference:
                    return await self.finish_upload(req, op)
                case 'upload', 'GET' | 'HEAD' if op.reference:
                    return await self.upload_status(req, op)
                case 'upload', 'DELETE' if op.reference:
                    return await self.cancel_upload(req, op)
                case 'tags', 'GET' | 'HEAD':
                    return await self.tags(req, op)
                case 'referrers', 'GET' | 'HEAD':
                    return await self.referrers(req, op)
        except Rejected as exc:
            return exc.response()
        except QuotaExceeded as exc:
            return _error(403, exc.detail, 'DENIED')
        except InsufficientStorage:
            return _error(507, 'not enough free space in the blob store', 'UNKNOWN')
        return _error(405, f'{req.method} is not supported here', 'UNSUPPORTED')

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _writable(req: FormatRequest) -> StoredHosted:
        hosted = hosted_config(req.repo)
        if hosted.write_policy is WritePolicy.READ_ONLY:
            raise Rejected(403, 'DENIED', 'this repository is read-only')
        return hosted

    @staticmethod
    def _manifest_path(image: str, digest: str) -> str:
        return f'{image}/manifests/{digest}'

    async def _resolve(self, req: FormatRequest, op: p.Operation) -> tuple[str, StoredAsset] | None:
        """Manifest reference (tag or digest) → (digest, stored manifest)."""
        digest = op.reference
        if not op.by_digest:
            tag = await docker_db.get_tag(req.hosted.db, req.repo.id, op.name, op.reference)
            if tag is None:
                return None
            digest = str(tag['manifest_digest'])
        asset = await req.hosted.get(req.repo, self._manifest_path(op.name, digest))
        return (digest, asset) if asset is not None else None

    # ------------------------------------------------------------------ pull side
    async def get_manifest(self, req: FormatRequest, op: p.Operation) -> Response:
        found = await self._resolve(req, op)
        if found is None:
            self._miss(req)
            return _error(404, 'manifest unknown to registry', 'MANIFEST_UNKNOWN')
        digest, asset = found
        req.log(image=op.name)
        return self._finish(req.hosted.serve(req, asset, headers={'Docker-Content-Digest': digest}), asset)

    async def get_blob(self, req: FormatRequest, op: p.Operation) -> Response:
        asset = await req.hosted.get(req.repo, f'blobs/{op.reference}')
        if asset is None:
            self._miss(req)
            return _error(404, 'blob unknown to registry', 'BLOB_UNKNOWN')
        req.log(image=op.name)
        return self._finish(req.hosted.serve(req, asset, headers={'Docker-Content-Digest': op.reference}), asset)

    @staticmethod
    def _finish(response: Response, asset: StoredAsset) -> Response:
        response.headers.update(API_HEADERS)
        response.headers['content-type'] = p.safe_content_type(asset.content_type)
        return response

    @staticmethod
    def _miss(req: FormatRequest) -> None:
        req.hosted.account(req, CacheState.HIT, 404, 0, time.perf_counter())

    async def tags(self, req: FormatRequest, op: p.Operation) -> Response:
        query = p.tags_query(req.request.query_params)
        limit = int(query['n']) if 'n' in query else p.TAGS_MAX_N  # no n: every tag (bounded)
        last = query.get('last')
        names = await docker_db.tag_names(req.hosted.db, req.repo.id, op.name, last=last, limit=limit + 1)
        if not names and last is None:
            return _error(404, 'repository name not known to registry', 'NAME_UNKNOWN')
        prefix = client_prefix(req.request.url.path, '/tags/list')
        headers = dict(API_HEADERS)
        if len(names) > limit:
            names = names[:limit]
            if names:
                link = f'{prefix}/tags/list?{urlencode({"n": limit, "last": names[-1]})}'
                headers['Link'] = f'<{link}>; rel="next"'
        return JSONResponse({'name': client_name(prefix), 'tags': names}, headers=headers)

    async def referrers(self, req: FormatRequest, op: p.Operation) -> Response:
        artifact_type = req.request.query_params.get('artifactType') or None
        if artifact_type is not None and len(artifact_type) > 255:
            return _error(400, 'invalid artifactType', 'UNSUPPORTED')
        docs = await docker_db.referrers(req.hosted.db, req.repo.id, op.name, op.reference, artifact_type=artifact_type)
        manifests = []
        for doc in docs:
            entry: dict[str, Any] = {
                'mediaType': doc.get('media_type') or p.OCI_MANIFEST,
                'digest': doc['digest'],
                'size': int(doc.get('size') or 0),
            }
            if doc.get('artifact_type'):
                entry['artifactType'] = doc['artifact_type']
            if doc.get('annotations'):
                entry['annotations'] = doc['annotations']
            manifests.append(entry)
        headers = {**API_HEADERS}
        if artifact_type is not None:
            headers['OCI-Filters-Applied'] = 'artifactType'
        body = {'schemaVersion': 2, 'mediaType': p.OCI_INDEX, 'manifests': manifests}
        return JSONResponse(body, media_type=p.OCI_INDEX, headers=headers)

    # ------------------------------------------------------------------ manifests: push and delete
    async def put_manifest(self, req: FormatRequest, op: p.Operation) -> Response:
        hosted = self._writable(req)
        started = time.perf_counter()
        length = req.request.headers.get('content-length')
        if length is not None and length.isdigit() and int(length) > p.MANIFEST_MAX_BYTES:
            raise Rejected(413, 'SIZE_INVALID', f'manifests are limited to {p.MANIFEST_MAX_BYTES} bytes')
        data = await self._read_body(req, p.MANIFEST_MAX_BYTES)
        await req.hosted.admit(req.repo, len(data))
        algorithm = op.reference.split(':', 1)[0] if op.by_digest else 'sha256'
        digest = p.digest_of(data, algorithm)
        if op.by_digest and digest != op.reference:
            raise Rejected(400, 'DIGEST_INVALID', 'the manifest does not match the digest in the URL')
        sha256 = digest if algorithm == 'sha256' else p.digest_of(data)
        checked = await asyncio.to_thread(check_pushed_manifest, data, req.request.headers.get('content-type'))
        image = op.name
        tag = None if op.by_digest else op.reference
        if tag is not None:
            current = await docker_db.get_tag(req.hosted.db, req.repo.id, image, tag)
            if current is not None and current['manifest_digest'] != digest and tag_locked(hosted, tag):
                raise Rejected(403, 'DENIED', f'tag {tag} is immutable in this repository')
        await self._check_references(req, image, checked)

        async with await req.hosted.blobs.open_writer(
            max_size=p.MANIFEST_MAX_BYTES, expected_sha256=sha256.split(':', 1)[1]
        ) as writer:
            await writer.write(data)
            ref = await req.hosted.blobs.commit(writer)
        path = self._manifest_path(image, digest)
        await req.hosted.record(
            req.repo, path, ref, content_type=checked.media_type, headers={'Docker-Content-Digest': digest}
        )
        record = await asyncio.to_thread(parse_manifest, data, checked.media_type)
        record |= {
            'size': len(data),
            'annotations': checked.annotations or None,
            'artifact_type': checked.artifact_type,
            'subject_digest': checked.subject,
        }
        await docker_db.record_manifest(req.hosted.db, req.repo.id, image, digest, record)
        if tag is not None:
            await docker_db.set_tag(req.hosted.db, req.repo.id, image, tag, digest, pushed_by=req.principal.display)
        artifact = f'{image}:{tag}' if tag else f'{image}@{digest[:19]}'
        audit.record(
            AuditAction.CONTENT_UPLOAD,
            target=audit.target('docker_manifest', digest, f'{req.repo.name}/{artifact}'),
            details={
                'repository': req.repo.name,
                'image': image,
                'tag': tag,
                'digest': digest,
                'media_type': checked.media_type,
                'size': len(data),
            },
        )
        req.hosted.activity(req, EventType.ASSET_CREATED, path=path, artifact=artifact, size=len(data))
        req.hosted.account(req, CacheState.HIT, 201, 0, started)
        req.log(image=image)
        prefix = client_prefix(req.request.url.path, '/manifests/')
        headers = {'Location': f'{prefix}/manifests/{digest}', 'Docker-Content-Digest': digest, **API_HEADERS}
        if checked.subject:
            headers['OCI-Subject'] = checked.subject
        return Response(status_code=201, headers=headers)

    async def _read_body(self, req: FormatRequest, limit: int) -> bytes:
        body = bytearray()
        async for chunk in req.request.stream():
            body += chunk
            if len(body) > limit:
                raise Rejected(413, 'SIZE_INVALID', f'manifests are limited to {limit} bytes')
        return bytes(body)

    async def _check_references(self, req: FormatRequest, image: str, checked: CheckedManifest) -> None:
        if checked.needs_empty:
            await self._ensure_empty_blob(req)
        if checked.blobs:
            paths = {f'blobs/{digest}': size for digest, size in checked.blobs}
            held = await req.hosted.sizes(req.repo, list(paths))
            for path, size in paths.items():
                digest = path.removeprefix('blobs/')
                if path not in held:
                    raise Rejected(
                        400, 'MANIFEST_BLOB_UNKNOWN', 'manifest references a blob unknown to registry', digest
                    )
                if held[path] != size:
                    raise Rejected(400, 'MANIFEST_INVALID', f'blob {digest} is {held[path]} bytes, not {size}', digest)
        if checked.manifests:
            paths_m = {self._manifest_path(image, d): d for d in checked.manifests}
            held_m = await req.hosted.sizes(req.repo, list(paths_m))
            for path, digest in paths_m.items():
                if path not in held_m:
                    raise Rejected(400, 'MANIFEST_UNKNOWN', 'index references a manifest unknown to registry', digest)

    async def _ensure_empty_blob(self, req: FormatRequest) -> None:
        """The OCI empty descriptor (``{}``) is implied: store it when a manifest uses it and it is missing."""
        path = f'blobs/{EMPTY_DIGEST}'
        if await req.hosted.get(req.repo, path) is not None:
            return
        async with await req.hosted.blobs.open_writer(expected_sha256=EMPTY_DIGEST.split(':', 1)[1]) as writer:
            await writer.write(EMPTY_JSON)
            ref = await req.hosted.blobs.commit(writer)
        await req.hosted.record(
            req.repo, path, ref, content_type=p.OCTET, headers={'Docker-Content-Digest': EMPTY_DIGEST}
        )

    async def delete_manifest(self, req: FormatRequest, op: p.Operation) -> Response:
        hosted = self._writable(req)
        db, image = req.hosted.db, op.name
        if not op.by_digest:
            tag = await docker_db.get_tag(db, req.repo.id, image, op.reference)
            if tag is None:
                return _error(404, 'manifest unknown to registry', 'MANIFEST_UNKNOWN')
            if tag_locked(hosted, op.reference):
                raise Rejected(403, 'DENIED', f'tag {op.reference} is immutable in this repository')
            await docker_db.delete_tag(db, req.repo.id, image, op.reference)
            self._deleted(req, f'{image}/tags/{op.reference}', f'{image}:{op.reference}', 'docker_tag', None)
            return Response(status_code=202, headers=API_HEADERS)
        path = self._manifest_path(image, op.reference)
        if await req.hosted.get(req.repo, path) is None:
            return _error(404, 'manifest unknown to registry', 'MANIFEST_UNKNOWN')
        tags = await docker_db.tags_of_digest(db, req.repo.id, image, op.reference)
        locked = [t for t in tags if tag_locked(hosted, t)]
        if locked:
            raise Rejected(403, 'DENIED', f'the manifest is tagged with immutable tag {locked[0]}')
        await docker_db.delete_tags_of_digest(db, req.repo.id, image, op.reference)
        await docker_db.delete_manifest(db, req.repo.id, image, op.reference)
        await req.hosted.remove(req.repo, path)
        self._deleted(req, path, f'{image}@{op.reference[:19]}', 'docker_manifest', op.reference, tags=tags)
        return Response(status_code=202, headers=API_HEADERS)

    def _deleted(
        self, req: FormatRequest, path: str, artifact: str, kind: str, digest: str | None, **details: Any
    ) -> None:
        audit.record(
            AuditAction.CONTENT_DELETE,
            target=audit.target(kind, digest, f'{req.repo.name}/{artifact}'),
            details={'repository': req.repo.name, 'path': path, **details},
        )
        req.hosted.activity(req, EventType.ASSET_DELETED, path=path, artifact=artifact)

    async def delete_blob(self, req: FormatRequest, op: p.Operation) -> Response:
        self._writable(req)
        path = f'blobs/{op.reference}'
        if await req.hosted.get(req.repo, path) is None:
            return _error(404, 'blob unknown to registry', 'BLOB_UNKNOWN')
        if await docker_db.blob_referenced(req.hosted.db, req.repo.id, op.reference):
            return _error(
                405, 'the blob is used by a manifest of this repository: delete the manifests first', 'UNSUPPORTED'
            )
        await req.hosted.remove(req.repo, path)
        self._deleted(req, path, f'{op.name}@{op.reference[:19]}', 'docker_blob', op.reference)
        return Response(status_code=202, headers=API_HEADERS)

    # ------------------------------------------------------------------ uploads
    async def start_upload(self, req: FormatRequest, op: p.Operation) -> Response:
        self._writable(req)
        length = req.request.headers.get('content-length')
        await req.hosted.admit(req.repo, int(length) if length is not None and length.isdigit() else None)
        params = req.request.query_params
        prefix = client_prefix(req.request.url.path, '/blobs/uploads')
        mount = params.get('mount')
        if mount is not None and p.DIGEST_RE.fullmatch(mount):
            mounted = await self._mount(req, mount, params.get('from'))
            if mounted:
                headers = {'Location': f'{prefix}/blobs/{mount}', 'Docker-Content-Digest': mount, **API_HEADERS}
                return Response(status_code=201, headers=headers)
        digest = params.get('digest')
        if digest is not None:
            return await self._monolithic(req, op, digest, prefix)
        upload_id = await req.hosted.blobs.start_upload()
        await uploads_db.create(
            req.hosted.db, upload_id, repo_id=req.repo.id, image=op.name, principal=req.principal.display
        )
        location = f'{prefix}/blobs/uploads/{upload_id}'
        return Response(status_code=202, headers=_upload_headers(location, upload_id, 0))

    async def _mount(self, req: FormatRequest, digest: str, source: str | None) -> bool:
        """Cross-repository mount: link the blob when the repository already has it, or when ``source`` names a
        docker repository the principal may read that holds it."""
        path = f'blobs/{digest}'
        if await req.hosted.get(req.repo, path) is not None:
            return True
        if not source:
            return False
        try:
            source_name = p.parse(f'{source}/tags/list').name
        except p.RequestRejected:
            return False
        resolved = await req.resolve_name(source_name)
        if resolved is None:
            return False
        repo, _ = resolved
        if repo.format != 'docker' or not req.allows(Action.READ, repo):
            return False
        holders = [repo]
        if repo.type is RepoType.GROUP and repo.group is not None:  # a group's content is its members' (read via it)
            holders = [m for name in repo.group.members if (m := await req.repository(name)) is not None]
        asset = None
        for holder in holders:
            if (asset := await req.hosted.get(holder, path)) is not None:
                break
        if asset is None:
            return False
        linked = await req.hosted.link(
            req.repo,
            path,
            asset.blob_sha256,
            asset.size,
            content_type=p.OCTET,
            headers={'Docker-Content-Digest': digest},
        )
        if linked is not None:
            req.log(mounted_from=repo.name)
        return linked is not None

    @staticmethod
    def _sha256(digest: str | None) -> str:
        if digest is None or not p.DIGEST_RE.fullmatch(digest):
            raise Rejected(400, 'DIGEST_INVALID', 'a valid digest parameter is required')
        algorithm, _, hexdigest = digest.partition(':')
        if algorithm != 'sha256':
            raise Rejected(400, 'DIGEST_INVALID', 'only sha256 blob uploads are supported')
        return hexdigest

    async def _monolithic(self, req: FormatRequest, op: p.Operation, digest: str, prefix: str) -> Response:
        hexdigest = self._sha256(digest)
        length = req.request.headers.get('content-length')
        expected = int(length) if length is not None and length.isdigit() else None
        async with await req.hosted.blobs.open_writer(expected_sha256=hexdigest, expected_size=expected) as writer:
            await self._receive(req, writer, op, digest, expected)
            ref = await self._commit(req, writer)
        await self._record_blob(req, digest, ref)
        headers = {'Location': f'{prefix}/blobs/{digest}', 'Docker-Content-Digest': digest, **API_HEADERS}
        return Response(status_code=201, headers=headers)

    async def _receive(
        self, req: FormatRequest, writer: BlobWriter, op: p.Operation, artifact: str, total: int | None
    ) -> int:
        try:
            return await req.hosted.receive(
                req,
                writer,
                path=f'blobs/{artifact}' if artifact.startswith('sha') else f'{op.name}/blobs/uploads',
                artifact=f'{op.name}@{artifact[:19]}' if artifact.startswith('sha') else f'{op.name} layer',
                component=op.name,
                total=total,
            )
        except PayloadTooLarge as exc:
            raise Rejected(413, 'SIZE_INVALID', exc.detail) from None
        except UploadInterrupted as exc:
            raise Rejected(400, 'BLOB_UPLOAD_INVALID', exc.detail) from None

    async def _commit(self, req: FormatRequest, writer: BlobWriter) -> BlobRef:
        try:
            ref: BlobRef = await req.hosted.blobs.commit(writer)
        except DigestMismatch:
            raise Rejected(400, 'DIGEST_INVALID', 'the uploaded content does not match the digest') from None
        return ref

    async def _record_blob(self, req: FormatRequest, digest: str, ref: BlobRef) -> None:
        await req.hosted.record(
            req.repo, f'blobs/{digest}', ref, content_type=p.OCTET, headers={'Docker-Content-Digest': digest}
        )

    async def _session(self, req: FormatRequest, op: p.Operation) -> uploads_db.UploadSession:
        session = await uploads_db.get(req.hosted.db, op.reference, req.repo.id, op.name)
        if session is None:
            raise Rejected(404, 'BLOB_UPLOAD_UNKNOWN', 'blob upload unknown to registry')
        return session

    def _location(self, req: FormatRequest, session_id: str) -> str:
        prefix = client_prefix(req.request.url.path, '/blobs/uploads')
        return f'{prefix}/blobs/uploads/{session_id}'

    async def _claim(self, req: FormatRequest, op: p.Operation, start: int | None) -> uploads_db.UploadSession:
        session = await self._session(req, op)
        if start is not None and start != session.bytes_received:
            raise _RangeRejected(self._location(req, op.reference), op.reference, session.bytes_received)
        claimed = await uploads_db.claim(req.hosted.db, op.reference, offset=session.bytes_received)
        if claimed is None:
            raise _RangeRejected(self._location(req, op.reference), op.reference, session.bytes_received)
        return claimed

    async def _append(
        self,
        req: FormatRequest,
        op: p.Operation,
        session: uploads_db.UploadSession,
        content_range: tuple[int, int] | None,
    ) -> BlobWriter:
        """Open the session's upload at its offset and stream this request's body into it. Returns the writer
        (still open); on failure the part is rolled back (or the session dropped) and the lease released."""
        db, blobs = req.hosted.db, req.hosted.blobs
        try:
            writer = await blobs.resume_writer(op.reference, offset=session.bytes_received, repair=True)
        except NotFound, UploadOffsetMismatch:
            await uploads_db.delete(db, op.reference)
            raise Rejected(404, 'BLOB_UPLOAD_UNKNOWN', 'the upload is no longer available; start again') from None
        try:
            received = await self._receive(req, writer, op, 'upload', None)
            if content_range is not None and received != content_range[1] - content_range[0] + 1:
                raise Rejected(416, 'BLOB_UPLOAD_INVALID', 'the body does not match Content-Range')
        except Rejected as exc:
            if exc.status == 413:
                await blobs.discard_upload(op.reference)
                await uploads_db.delete(db, op.reference)
            else:
                await writer.rollback()
                await uploads_db.release(db, op.reference, bytes_received=session.bytes_received)
            raise
        except BaseException:
            await writer.rollback()
            await uploads_db.release(db, op.reference, bytes_received=session.bytes_received)
            raise
        return writer

    async def patch_upload(self, req: FormatRequest, op: p.Operation) -> Response:
        self._writable(req)
        await req.hosted.admit(req.repo)
        content_range = _parse_range(req.request.headers.get('content-range'))
        session = await self._claim(req, op, content_range[0] if content_range else None)
        writer = await self._append(req, op, session, content_range)
        size = await writer.suspend()
        await uploads_db.release(req.hosted.db, op.reference, bytes_received=size)
        return Response(status_code=202, headers=_upload_headers(self._location(req, op.reference), op.reference, size))

    async def finish_upload(self, req: FormatRequest, op: p.Operation) -> Response:
        self._writable(req)
        await req.hosted.admit(req.repo)
        digest = req.request.query_params.get('digest')
        hexdigest = self._sha256(digest)
        assert digest is not None
        content_range = _parse_range(req.request.headers.get('content-range'))
        session = await self._claim(req, op, content_range[0] if content_range else None)
        writer = await self._append(req, op, session, content_range)
        writer.expected_sha256 = hexdigest
        try:
            ref = await self._commit(req, writer)
        finally:
            await uploads_db.delete(req.hosted.db, op.reference)
        await self._record_blob(req, digest, ref)
        prefix = client_prefix(req.request.url.path, '/blobs/uploads')
        headers = {'Location': f'{prefix}/blobs/{digest}', 'Docker-Content-Digest': digest, **API_HEADERS}
        return Response(status_code=201, headers=headers)

    async def upload_status(self, req: FormatRequest, op: p.Operation) -> Response:
        session = await self._session(req, op)
        headers = _upload_headers(self._location(req, op.reference), op.reference, session.bytes_received)
        return Response(status_code=204, headers=headers)

    async def cancel_upload(self, req: FormatRequest, op: p.Operation) -> Response:
        await self._session(req, op)
        await req.hosted.blobs.discard_upload(op.reference)
        await uploads_db.delete(req.hosted.db, op.reference)
        return Response(status_code=204, headers=API_HEADERS)


class _RangeRejected(Rejected):
    """416 with the upload's current ``Range`` (the client resumes from there)."""

    def __init__(self, location: str, session_id: str, size: int) -> None:
        super().__init__(416, 'BLOB_UPLOAD_INVALID', 'the chunk does not start where the upload ends')
        self.headers = _upload_headers(location, session_id, size)
        del self.headers['Content-Length']

    def response(self, headers: dict[str, str] | None = None) -> Response:
        return super().response({**self.headers, **(headers or {})})
