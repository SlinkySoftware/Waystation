"""Maven proxy repositories (FORMATS.md "Maven"; SPECIFICATION §4.1). Upstream: Maven Central or any Maven 2 layout
repository; paths map one-to-one onto the upstream.

- ``maven-metadata.xml`` (any level) is *metadata*: revalidated after the repository's metadata max age, refused
  (502, nothing cached) unless it parses (defusedxml, bounded).
- Artifacts are *content*, verified while they stream against the upstream's ``.sha1`` (or ``.sha256`` when there is
  no ``.sha1``), fetched first and cached; without either the computed SHA-256 is recorded. A non-timestamped
  SNAPSHOT file (``a-1.0-SNAPSHOT.jar``) changes in place, so it is revalidated like metadata.
- Checksum files (``.md5 .sha1 .sha256 .sha512``) of a cached file are generated from its stored digests (hashed
  from the blob when the upstream did not give that one), so they always match what Waystation serves; for metadata
  the stored document is cached first. A checksum of a file that is not cached is fetched from the upstream (and
  cached, only when it is a well-formed digest).
- ``.asc`` signatures pass through as content.
- The version policy keeps SNAPSHOT paths out of a release proxy (and releases out of a snapshot proxy): 404 without
  an upstream request. With the strict layout policy, paths that are not Maven layout paths answer 404.

Components (``components``, Maven version order) are recorded in the background when an artifact is cached.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from starlette.responses import Response

from waystation_common.errors import NotFound, ValidationFailed
from waystation_common.events import CacheState
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.maven import coords
from waystation_server.formats.maven import metadata as md
from waystation_server.formats.maven.common import XML_TYPE, Digests, generated, options_of, policy_allows
from waystation_server.formats.maven.descriptor import MavenOptions
from waystation_server.problems import native_error
from waystation_server.proxy.engine import AssetKind, AssetSpec, Expected, OnStored, StoredAsset, UpstreamInfo

CHECKSUM_MAX_BYTES = 1024
CHECKSUM_MAX_AGE_S = 10 * 365 * 86_400  # a release file's checksum never changes
EXPECT_ORDER = ('sha1', 'sha256')

ArtifactStored = Callable[[FormatRequest, coords.Artifact], OnStored]


def _validate_metadata(data: bytes, info: UpstreamInfo) -> None:
    md.validate(data)


def checksum_validator(alg: str) -> Callable[[bytes, UpstreamInfo], None]:
    def validate(data: bytes, info: UpstreamInfo) -> None:
        if coords.parse_checksum(data, alg) is None:
            raise ValidationFailed(f'not a {alg} checksum file')

    return validate


class MavenProxy:
    def __init__(self, digests: Digests, on_artifact: ArtifactStored) -> None:
        self.digests = digests
        self.on_artifact = on_artifact

    def allowed(self, path: str, options: MavenOptions) -> bool:
        if options.layout_policy == 'strict' and not coords.strict_ok(path):
            return False
        return policy_allows(options, coords.version_of_path(path))

    async def handle(self, req: FormatRequest) -> Response:
        if req.method not in ('GET', 'HEAD'):
            return native_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD'})
        path = req.path
        if not path or req.request.url.path.endswith('/'):
            raise NotFound('Not Found')  # no directory listings
        if not self.allowed(path, options_of(req.repo)):
            raise NotFound('Not Found')
        split = coords.split_checksum(path)
        if split is not None:
            return await self.checksum(req, *split)
        if coords.is_metadata(path):
            return await req.proxy.serve(req, self.metadata_spec(path))
        return await req.proxy.serve(req, self.content_spec(req, path))

    # ------------------------------------------------------------------ specs
    @staticmethod
    def metadata_spec(path: str) -> AssetSpec:
        return AssetSpec(
            path=path,
            kind=AssetKind.METADATA,
            content_type=XML_TYPE,
            validate=_validate_metadata,
            max_size=md.MAX_BYTES,
            artifact=path.rsplit('/', 2)[-2] + '/' + coords.METADATA if '/' in path else coords.METADATA,
        )

    def content_spec(self, req: FormatRequest, path: str) -> AssetSpec:
        artifact = coords.parse_artifact(path)
        verify = artifact is not None and not artifact.signature

        async def expect() -> Expected | None:
            return await self.expected(req, path)

        mutable = artifact is not None and artifact.snapshot and artifact.file_version == artifact.version
        return AssetSpec(
            path=path,
            kind=AssetKind.CONTENT,
            expect=expect if verify else None,
            content_type=coords.content_type(path),
            max_age_s=self._proxy_metadata_age(req) if mutable else None,
            on_stored=self.on_artifact(req, artifact) if artifact is not None else None,
            artifact=path.rsplit('/', 1)[-1],
            component=f'{artifact.group_id}:{artifact.artifact_id}:{artifact.version}' if artifact else None,
        )

    @staticmethod
    def _proxy_metadata_age(req: FormatRequest) -> int | None:
        return req.repo.proxy.metadata_max_age_s if req.repo.proxy is not None else None

    @staticmethod
    def checksum_spec(path: str, alg: str) -> AssetSpec:
        return AssetSpec(
            path=f'{path}.{alg}',
            kind=AssetKind.METADATA,
            content_type='text/plain',
            validate=checksum_validator(alg),
            max_size=CHECKSUM_MAX_BYTES,
            max_age_s=CHECKSUM_MAX_AGE_S,
            artifact=f'{path.rsplit("/", 1)[-1]}.{alg}',
        )

    async def expected(self, req: FormatRequest, path: str) -> Expected | None:
        """What the upstream's checksum file says (``.sha1``, else ``.sha256``); None when it publishes neither."""
        for alg in EXPECT_ORDER:
            try:
                asset = await req.proxy.ensure(req, self.checksum_spec(path, alg))
            except NotFound:
                continue
            digest = coords.parse_checksum(await req.proxy.read(asset, max_bytes=CHECKSUM_MAX_BYTES), alg)
            if digest is not None:
                return Expected(sha1=digest) if alg == 'sha1' else Expected(sha256=digest)
        return None

    # ------------------------------------------------------------------ checksum files
    async def checksum(self, req: FormatRequest, base: str, alg: str) -> Response:
        started = time.perf_counter()
        if coords.is_metadata(base):
            asset = await req.proxy.ensure(req, self.metadata_spec(base))
        else:
            cached = await req.hosted.get(req.repo, base)
            if cached is None or not await req.proxy.blobs.exists(cached.blob_sha256):
                return await req.proxy.serve(req, self.checksum_spec(base, alg))
            asset = cached
        return await self.serve_digest(req, asset, alg, started)

    async def serve_digest(self, req: FormatRequest, asset: StoredAsset, alg: str, started: float) -> Response:
        digest = await self.digests.of(asset, alg, req.proxy.blobs.blob_path(asset.blob_sha256))
        response = generated(req.request, digest.encode(), 'text/plain')
        sent = len(digest) if response.status_code == 200 and req.method == 'GET' else 0
        req.hosted.account(req, CacheState.HIT, response.status_code, sent, started)
        return response
