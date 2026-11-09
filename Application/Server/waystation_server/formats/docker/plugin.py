"""Docker / OCI proxy plugin (T-P2-DOCKERP, M0 slice; FORMATS.md "Docker / OCI").

Serves the pull side of the registry API for docker proxy repositories, at ``/v2/<prefix|alias|-><name>/...`` (the
router resolves the repository, SPEC §4.4) and at ``/repository/<repo>/v2/<name>/...``:

- ``GET /v2/``: API version check.
- Manifests ``GET``/``HEAD`` by **tag**: a metadata asset per tag and Accept variant, refreshed after
  ``metadata_max_age``; a stale tag is first re-checked with an upstream ``HEAD`` (same ``Docker-Content-Digest`` =
  still current, and Docker Hub does not count HEADs). The bytes are checked against the upstream's
  ``Docker-Content-Digest``, must be a JSON manifest of a manifest media type and are never converted.
- Manifests by **digest** and blobs: content assets verified against the digest before they are committed.
  Blobs are keyed by digest (a layer shared by several images is fetched once per repository).
- ``GET /v2/<name>/tags/list`` (``n``/``last``, ``Link`` rewritten): a metadata asset with a short max age.
- ``HEAD`` on anything not cached asks the upstream with ``HEAD`` (nothing downloaded, no Hub quota used).
- Upstream auth: ``upstream_auth.RegistryAuth`` (Docker Hub and any ``WWW-Authenticate: Bearer`` registry);
  Docker Hub names without a namespace get ``library/``; ``ratelimit-*`` headers are recorded for upstream health.

Browse (``browser``, ``formats.docker.browse``): every cached manifest is recorded in ``docker_manifests`` and every
cached tag manifest in ``docker_tags``, in the background after the fill (parsed in a thread). A tag resolved by an
upstream ``HEAD`` (how the Docker daemon pulls) is recorded in ``docker_tags`` from its ``Docker-Content-Digest``.

Hosted repositories (push, delete, referrers API) are served by ``formats.docker.hosted.DockerHosted``. Not yet:
group repositories (T-P2-DOCKERG) and the referrers API for proxies (404 makes clients fall back to the tag schema).
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from urllib.parse import urlencode

from starlette.responses import JSONResponse, Response

from waystation_common.errors import NotFound, WaystationError
from waystation_common.events import CacheState
from waystation_server.db import docker as docker_db
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.common.background import Background
from waystation_server.formats.docker import protocol as p
from waystation_server.formats.docker.browse import DockerBrowse, parse_manifest
from waystation_server.formats.docker.descriptor import DESCRIPTOR, DockerOptions
from waystation_server.formats.docker.hosted import DockerHosted
from waystation_server.formats.docker.upstream_auth import RegistryAuth, TokenCache
from waystation_server.problems import oci_error
from waystation_server.proxy.engine import (
    AssetKind,
    AssetSpec,
    Expected,
    OnStored,
    ProxyEngine,
    StoredAsset,
    UpstreamInfo,
)
from waystation_server.repos.model import AuthKind, RepoType, secret_aad

API_VERSION = 'Docker-Distribution-API-Version'
API_HEADERS = {API_VERSION: 'registry/2.0'}
TAGS_MAX_AGE_S = 300
NOT_FOUND_CODES = {'manifest': 'MANIFEST_UNKNOWN', 'blob': 'BLOB_UNKNOWN', 'tags': 'NAME_UNKNOWN'}


def _error(status: int, message: str, code: str, headers: dict[str, str] | None = None) -> Response:
    return oci_error(status, message, code=code, headers={**API_HEADERS, **(headers or {})})


def _expected(digest: str) -> Expected:
    algorithm, _, hexdigest = digest.partition(':')
    return Expected(sha512=hexdigest) if algorithm == 'sha512' else Expected(sha256=hexdigest)


def _same_digest(asset: StoredAsset, info: UpstreamInfo) -> bool:
    upstream = info.headers.get('docker-content-digest')
    if not upstream:
        return False
    return upstream in (f'sha256:{asset.blob_sha256}', asset.headers.get('Docker-Content-Digest'))


class _Upstream:
    """What one request learned from the upstream (for error mapping and rate limits)."""

    def __init__(self, req: FormatRequest) -> None:
        self.req = req
        self.status: int | None = None
        self.retry_after: str | None = None

    def __call__(self, info: UpstreamInfo) -> None:
        self.status = info.status
        self.retry_after = info.headers.get('retry-after')
        limit = p.rate_limit(info.headers)
        if limit is not None:
            self.req.proxy.record_rate_limit(
                self.req.repo, limit=limit.limit, remaining=limit.remaining, window_s=limit.window_s
            )


class DockerPlugin:
    descriptor = DESCRIPTOR

    def __init__(self) -> None:
        self.tokens = TokenCache()
        self.browser = DockerBrowse()
        self.hosted = DockerHosted()
        self._indexing = Background('docker-manifests')

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        await self._indexing.drain()

    async def handle(self, req: FormatRequest) -> Response:
        path = req.path
        if req.root == 'repository':
            if path != 'v2' and not path.startswith('v2/'):
                return _error(404, 'not a registry API path; use /repository/<repo>/v2/', 'NAME_UNKNOWN')
            path = path[3:]
        try:
            op = p.parse(path)
        except p.RequestRejected as exc:
            return _error(exc.status, exc.message, exc.code)
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.handle(req, op)
        if req.method not in ('GET', 'HEAD'):
            return _error(405, 'this repository is a read-only proxy', 'UNSUPPORTED')
        if op.op == 'base':
            return JSONResponse({}, headers=API_HEADERS)
        if op.op == 'referrers':
            return _error(404, 'the referrers API is not available for proxy repositories yet', 'UNSUPPORTED')
        if req.repo.proxy is None:
            return _error(404, 'repository name not known to registry', 'NAME_UNKNOWN')

        options = DockerOptions.model_validate(req.repo.format_options)
        name = p.upstream_name(op.name, docker_hub=options.docker_hub)
        upstream = _Upstream(req)
        try:
            spec = self._spec(req, op, name, upstream)
        except p.RequestRejected as exc:
            return _error(exc.status, exc.message, exc.code)
        req.log(image=name)
        try:
            response = await req.proxy.serve(req, spec)
        except NotFound:
            return _error(404, f'{op.op} unknown to registry', NOT_FOUND_CODES[op.op])
        except WaystationError as exc:
            return self._upstream_error(exc, upstream, op)
        if op.op == 'manifest' and not op.by_digest and req.is_head:
            self._record_head_tag(req, op, name, response)
        return self._finish(req, response, op)

    # -------------------------------------------------------------- specs
    def _spec(self, req: FormatRequest, op: p.Operation, name: str, upstream: _Upstream) -> AssetSpec:
        spec = self._asset(req, op, name)
        return dataclasses.replace(
            spec,
            authenticator=self._auth(req, name),
            on_upstream=upstream,
            component=name,
            on_stored=self._on_manifest_stored(req.proxy, req.repo.id, op, name) if op.op == 'manifest' else None,
        )

    def _on_manifest_stored(self, engine: ProxyEngine, repo_id: str, op: p.Operation, name: str) -> OnStored:
        """Record a cached manifest (and, fetched by tag, the tag) for browsing, off the response path."""

        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            async def index() -> None:
                raw = data if data is not None else await engine.read(asset, max_bytes=p.MANIFEST_MAX_BYTES)
                record = await asyncio.to_thread(parse_manifest, raw, asset.content_type)
                if op.by_digest:
                    digest = op.reference
                else:
                    header = asset.headers.get('Docker-Content-Digest', '')
                    digest = header if p.DIGEST_RE.fullmatch(header) else f'sha256:{asset.blob_sha256}'
                await docker_db.record_manifest(engine.db, repo_id, name, digest, record)
                if not op.by_digest:
                    authoritative = asset.path == f'{name}/manifests/{op.reference}'
                    await docker_db.record_tag(
                        engine.db, repo_id, name, op.reference, digest, authoritative=authoritative
                    )

            self._indexing.spawn((repo_id, asset.path, asset.blob_sha256), index)

        return stored

    def _record_head_tag(self, req: FormatRequest, op: p.Operation, name: str, response: Response) -> None:
        """A tag answered by an upstream ``HEAD`` (the Docker daemon resolves a tag with ``HEAD``, then pulls the
        manifest by digest, so the tag manifest itself is never cached): record the tag for browsing. The manifests
        are recorded when the client fetches them by digest."""
        if response.headers.get('x-ws-cache') != CacheState.FETCHING.value:
            return
        digest = response.headers.get('docker-content-digest', '')
        if not p.DIGEST_RE.fullmatch(digest):
            return
        types = p.accepted_types(req.request.headers.getlist('accept'))
        authoritative = p.accept_variant(types) == ''
        db, repo_id, tag = req.proxy.db, req.repo.id, op.reference

        async def index() -> None:
            await docker_db.record_tag(db, repo_id, name, tag, digest, authoritative=authoritative)

        self._indexing.spawn((repo_id, 'head', name, tag), index)

    @staticmethod
    def _asset(req: FormatRequest, op: p.Operation, name: str) -> AssetSpec:
        if op.op == 'blob':
            return AssetSpec(
                path=f'blobs/{op.reference}',
                kind=AssetKind.CONTENT,
                upstream_path=f'v2/{name}/blobs/{op.reference}',
                expected=_expected(op.reference),
                content_type=p.OCTET,
                response_headers={'Docker-Content-Digest': op.reference},
                artifact=f'{name}@{op.reference[:19]}',
            )
        if op.op == 'manifest' and op.by_digest:
            return AssetSpec(
                path=f'{name}/manifests/{op.reference}',
                kind=AssetKind.CONTENT,
                upstream_path=f'v2/{name}/manifests/{op.reference}',
                upstream_headers={'Accept': ', '.join(p.MANIFEST_TYPES)},
                expected=_expected(op.reference),
                max_size=p.MANIFEST_MAX_BYTES,
                response_headers={'Docker-Content-Digest': op.reference},
                artifact=f'{name}@{op.reference[:19]}',
            )
        if op.op == 'manifest':
            types = p.accepted_types(req.request.headers.getlist('accept'))
            return AssetSpec(
                path=f'{name}/manifests/{op.reference}{p.accept_variant(types)}',
                kind=AssetKind.METADATA,
                upstream_path=f'v2/{name}/manifests/{op.reference}',
                upstream_headers={'Accept': ', '.join(types)},
                max_size=p.MANIFEST_MAX_BYTES,
                store_headers=('Docker-Content-Digest',),
                validate=_validate_manifest,
                head_passthrough=True,
                revalidate_head=_same_digest,
                artifact=f'{name}:{op.reference}',
            )
        query = p.tags_query(req.request.query_params)
        encoded = urlencode(query)
        return AssetSpec(
            path=f'{name}/tags/list' + (f';{encoded}' if encoded else ''),
            kind=AssetKind.METADATA,
            upstream_path=f'v2/{name}/tags/list' + (f'?{encoded}' if encoded else ''),
            upstream_headers={'Accept': 'application/json'},
            content_type='application/json',
            max_size=p.TAGS_MAX_BYTES,
            max_age_s=min(TAGS_MAX_AGE_S, req.repo.proxy.metadata_max_age_s if req.repo.proxy else TAGS_MAX_AGE_S),
            store_headers=('Link',),
            validate=_validate_tags,
            artifact=f'{name} tags',
        )

    def _auth(self, req: FormatRequest, name: str) -> RegistryAuth:
        repo = req.repo
        proxy = repo.proxy
        assert proxy is not None
        auth = proxy.auth
        keyring = req.proxy.keyring

        def secret() -> str | None:
            if auth.kind is AuthKind.NONE or not auth.secret_enc or keyring is None:
                return None
            return keyring.decrypt_str(auth.secret_enc, aad=secret_aad(repo.id))

        credentials: Callable[[], tuple[str, str] | None] = lambda: None  # noqa: E731
        bearer: Callable[[], str | None] = lambda: None  # noqa: E731
        if auth.kind is AuthKind.BASIC:
            credentials = lambda: (auth.username or '', s) if (s := secret()) else None  # noqa: E731
        elif auth.kind is AuthKind.BEARER:
            bearer = secret
        return RegistryAuth(
            cache=self.tokens,
            clients=req.proxy.clients,
            repo_key=f'{repo.id}:{repo.config_version}:{repo.updated_at.isoformat()}',
            upstream_url=proxy.upstream_url,
            scope=f'repository:{name}:pull',
            allow_private=proxy.allow_private_upstream,
            credentials=credentials,
            bearer=bearer,
        )

    # -------------------------------------------------------------- responses
    def _finish(self, req: FormatRequest, response: Response, op: p.Operation) -> Response:
        headers = response.headers
        headers[API_VERSION] = 'registry/2.0'
        if 'content-type' in headers:
            headers['content-type'] = p.safe_content_type(headers['content-type'])
        if op.op in ('manifest', 'blob') and 'docker-content-digest' not in headers:
            if op.by_digest:
                headers['Docker-Content-Digest'] = op.reference
            elif 'x-ws-digest' in headers:
                headers['Docker-Content-Digest'] = headers['x-ws-digest']
        if op.op == 'tags':
            link = p.next_link(headers.get('link'), req.request.url.path)
            if 'link' in headers:
                del headers['link']
            if link:
                headers['Link'] = link
        return response

    @staticmethod
    def _upstream_error(exc: WaystationError, upstream: _Upstream, op: p.Operation) -> Response:
        if upstream.status in (401, 403):
            # Docker Hub answers 401 for names that do not exist as well as for private ones.
            return _error(404, 'repository not found upstream, or the upstream denied access', 'NAME_UNKNOWN')
        if upstream.status == 429:
            retry = upstream.retry_after if upstream.retry_after and upstream.retry_after.isdigit() else None
            return _error(
                429,
                'the upstream registry rate limit was reached',
                'TOOMANYREQUESTS',
                {'Retry-After': retry} if retry else None,
            )
        code = 'DIGEST_INVALID' if exc.code == 'digest_mismatch' else 'UNKNOWN'
        return _error(exc.status, exc.detail, code)


def _validate_manifest(data: bytes, info: UpstreamInfo) -> None:
    p.check_manifest(data, info.headers.get('content-type'), info.headers.get('docker-content-digest'))


def _validate_tags(data: bytes, info: UpstreamInfo) -> None:
    p.check_tags(data)
