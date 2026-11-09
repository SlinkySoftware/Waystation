"""Terraform/OpenTofu provider network mirror (T-P5-TFP; FORMATS.md "Terraform / OpenTofu"): proxy repositories that
serve the Provider Network Mirror Protocol for providers of the allowed origin registries.

Routes under ``/repository/<repo>/`` (``paths``):

- ``<hostname>/<namespace>/<type>/index.json``: the versions of the registry's version list (rendered per request
  from the cached list: ``ETag``, ``304``).
- ``<hostname>/<namespace>/<type>/<version>.json``: per platform the archive URL (relative) and its hashes:
  ``zh:`` (the sha256 from the version's signed ``SHA256SUMS``) for every platform, ``h1:`` too for archives this
  repository has cached (hashed while they were committed).
- ``<hostname>/<namespace>/<type>/<version>/<os>/<arch>/<filename>``: the archive (content, X-Accel once cached).

The origin is reached with the Provider Registry Protocol: service discovery (``https://<hostname>/.well-known/
terraform.json``, ``providers.v1``; cached a day), the version list (metadata, revalidated after the repository's
metadata max age), a platform's download document (kept: a release never changes), ``SHA256SUMS`` and its
signature, and the archive from wherever the download document points (GitHub release assets, HashiCorp's release
site; redirects followed, every hop through the SSRF guard; upstream credentials only to the upstream URL's host).

Trust: ``SHA256SUMS`` is cached only after its OpenPGP signature verified with a signing key the registry publishes
for the provider (``verify_signatures``, ``waystation_common.openpgp``) and it lists the download document's file
with the same sha256; an archive is committed (and its last bytes sent) only when its sha256 matches that list.
Nothing that fails is cached; the client gets 502. Host names must be allowed origins (``origins`` option, plus the
upstream URL's host), else 404.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import BinaryIO
from urllib.parse import urljoin

from starlette.responses import Response

from waystation_common import openpgp
from waystation_common.errors import DigestMismatch, NotFound, WaystationError
from waystation_common.events import CacheState
from waystation_server.db import terraform as tf_db
from waystation_server.formats.base import FormatDescriptor, FormatRequest
from waystation_server.formats.common.background import Background
from waystation_server.formats.terraform import paths, registry, ziphash
from waystation_server.formats.terraform.browse import TerraformBrowse
from waystation_server.formats.terraform.descriptor import DESCRIPTOR, TerraformOptions, allowed_origins
from waystation_server.formats.terraform.paths import Op, Provider, Route
from waystation_server.problems import native_error
from waystation_server.proxy.engine import (
    AssetKind,
    AssetSpec,
    CheckedExpected,
    Expected,
    OnStored,
    ProxyEngine,
    StoredAsset,
    UpstreamInfo,
)
from waystation_server.repos.model import RepoType

JSON = 'application/json'
TEXT = 'text/plain; charset=utf-8'
ZIP = 'application/zip'
OCTETS = 'application/octet-stream'
SAFE_HEADERS = {'X-Content-Type-Options': 'nosniff'}
DISCOVERY_MAX_AGE_S = 86_400
FOREVER_S = 100 * 365 * 86_400
MAX_ARCHIVE = 1024 * 1024 * 1024
MAX_DOWNLOAD_TRIES = 3
_PARSED_CACHE = 64


class _Lru[K, V]:
    def __init__(self, size: int) -> None:
        self.size = size
        self._items: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K) -> V | None:
        value = self._items.get(key)
        if value is not None:
            self._items.move_to_end(key)
        return value

    def put(self, key: K, value: V) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)


def tf_error(status: int, message: str, *, headers: dict[str, str] | None = None) -> Response:
    return native_error(status, message, headers=headers)


def signature_check(signature: bytes, keys: tuple[str, ...]) -> Callable[[BinaryIO], None]:
    """An ``Expected.check`` for ``SHA256SUMS``: its detached OpenPGP signature must verify with one of ``keys``
    (ASCII-armored public keys from the registry). Raises ``DigestMismatch``. Blocking."""

    def check(file: BinaryIO) -> None:
        data = file.read(registry.MAX_SUMS + 1)
        try:
            certs = [cert for armor in keys for cert in openpgp.read_certs(armor.encode('utf-8'))]
        except openpgp.OpenPgpError, UnicodeEncodeError:
            raise DigestMismatch("the registry's signing keys are malformed") from None
        if not certs:
            raise DigestMismatch('the registry publishes no signing key for this provider')
        try:
            openpgp.verify_detached(data, signature, certs)
        except openpgp.OpenPgpError as exc:
            raise DigestMismatch(f'SHA256SUMS signature: {exc}') from None

    return check


def _h1_check(found: dict[str, str]) -> Callable[[BinaryIO], None]:
    """An ``Expected.check`` that records the archive's ``h1:`` hash (never refuses: the sha256 decides)."""

    def check(file: BinaryIO) -> None:
        try:
            found['h1'] = ziphash.zip_h1(file)
        except ziphash.NotHashable:
            found.pop('h1', None)

    return check


class TerraformPlugin:
    def __init__(self, descriptor: FormatDescriptor = DESCRIPTOR) -> None:
        self.descriptor = descriptor
        self.browser = TerraformBrowse()
        self._indexing = Background('terraform-providers')
        self._parsed: _Lru[str, dict[str, registry.VersionInfo]] = _Lru(_PARSED_CACHE)

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        await self._indexing.drain()

    def options(self, req: FormatRequest) -> TerraformOptions:
        options = self.descriptor.parse_options(req.repo.format_options)
        return options if isinstance(options, TerraformOptions) else TerraformOptions()

    async def handle(self, req: FormatRequest) -> Response:
        try:
            response = await self._handle(req)
        except WaystationError as exc:
            headers = getattr(exc, 'headers', None)
            return tf_error(exc.status, exc.detail, headers=dict(headers) if headers else None)
        response.headers.update(SAFE_HEADERS)
        return response

    async def _handle(self, req: FormatRequest) -> Response:
        route = paths.parse_route(req.path)
        if route is None:
            raise NotFound('not found: not a provider network mirror path')
        if req.method not in ('GET', 'HEAD'):
            return tf_error(405, 'method not allowed', headers={'Allow': 'GET, HEAD'})
        if req.repo.type is not RepoType.PROXY or req.repo.proxy is None:
            raise NotFound('Not Found')
        hostname = route.provider.hostname
        if hostname not in allowed_origins(self.options(req), req.repo.proxy.upstream_url):
            raise NotFound(f'not found: {hostname} is not an origin registry of this mirror')
        req.log(op=route.op.value)
        match route.op:
            case Op.INDEX:
                return await self._index(req, route.provider)
            case Op.VERSION:
                return await self._version(req, route)
            case Op.ARCHIVE:
                return await self._archive(req, route)
        raise NotFound('Not Found')  # pragma: no cover

    # ------------------------------------------------------------------ origin documents
    async def registry_base(self, req: FormatRequest, hostname: str) -> str:
        """The origin's provider registry base URL (``providers.v1``, from its cached service discovery)."""

        def validate(data: bytes, info: UpstreamInfo) -> None:
            registry.parse_discovery(data, hostname)

        spec = AssetSpec(
            path=paths.discovery_key(hostname),
            kind=AssetKind.METADATA,
            upstream_url=registry.discovery_url(hostname),
            content_type=JSON,
            max_size=registry.MAX_DISCOVERY,
            max_age_s=DISCOVERY_MAX_AGE_S,
            validate=validate,
            head_passthrough=False,
            artifact=f'{hostname} service discovery',
            component=hostname,
        )
        asset = await req.proxy.ensure(req, spec)
        return registry.parse_discovery(await req.proxy.read(asset, max_bytes=registry.MAX_DISCOVERY), hostname)

    async def versions(self, req: FormatRequest, provider: Provider) -> dict[str, registry.VersionInfo]:
        """The registry's version list for ``provider`` (cached metadata, parsed once per content)."""
        base = await self.registry_base(req, provider.hostname)
        spec = AssetSpec(
            path=paths.versions_key(provider),
            kind=AssetKind.METADATA,
            upstream_url=urljoin(base, f'{provider.namespace}/{provider.type}/versions'),
            content_type=JSON,
            max_size=registry.MAX_VERSIONS,
            validate=_validate_versions,
            on_stored=self._on_versions_stored(req.proxy, req.repo.id, provider),
            head_passthrough=False,
            artifact=f'{provider.key} versions',
            component=provider.key,
        )
        asset = await req.proxy.ensure(req, spec)
        return await self.parsed_versions(req.proxy, asset)

    async def parsed_versions(self, engine: ProxyEngine, asset: StoredAsset) -> dict[str, registry.VersionInfo]:
        parsed = self._parsed.get(asset.blob_sha256)
        if parsed is None:
            data = await engine.read(asset, max_bytes=registry.MAX_VERSIONS)
            parsed = await asyncio.to_thread(registry.parse_versions, data)
            self._parsed.put(asset.blob_sha256, parsed)
        return parsed

    async def download(
        self, req: FormatRequest, provider: Provider, version: str, os_: str, arch: str
    ) -> registry.Download:
        """A platform's download document (kept: a release does not change)."""
        base = await self.registry_base(req, provider.hostname)

        def validate(data: bytes, info: UpstreamInfo) -> None:
            registry.parse_download(data, os_, arch)

        spec = AssetSpec(
            path=paths.download_key(provider, version, os_, arch),
            kind=AssetKind.METADATA,
            upstream_url=urljoin(base, f'{provider.namespace}/{provider.type}/{version}/download/{os_}/{arch}'),
            content_type=JSON,
            max_size=registry.MAX_DOWNLOAD,
            max_age_s=FOREVER_S,
            validate=validate,
            head_passthrough=False,
            artifact=f'{provider.key} {version} {os_}_{arch} download',
            component=provider.key,
        )
        asset = await req.proxy.ensure(req, spec)
        data = await req.proxy.read(asset, max_bytes=registry.MAX_DOWNLOAD)
        return registry.parse_download(data, os_, arch)

    async def sums(
        self, req: FormatRequest, provider: Provider, version: str, known: registry.Download
    ) -> dict[str, str]:
        """The version's ``SHA256SUMS`` (file name -> sha256), cached only once its signature verified and it lists
        ``known``'s archive with the registry's shasum."""
        verify = self.options(req).verify_signatures

        def validate(data: bytes, info: UpstreamInfo) -> None:
            listed = registry.parse_sums(data)
            if listed.get(known.filename) != known.shasum:
                raise DigestMismatch(f"SHA256SUMS does not list {known.filename} with the registry's shasum")

        async def expect() -> Expected | None:
            if not verify:
                req.log(signature='unverified')
                return None
            signature = await self._signature(req, provider, version, known)
            req.log(signature='verified')
            return CheckedExpected(check=signature_check(signature, known.keys))

        spec = AssetSpec(
            path=paths.sums_key(provider, version),
            kind=AssetKind.METADATA,
            upstream_url=known.shasums_url,
            content_type=TEXT,
            max_size=registry.MAX_SUMS,
            max_age_s=FOREVER_S,
            expect=expect,
            validate=validate,
            head_passthrough=False,
            artifact=f'{provider.key} {version} SHA256SUMS',
            component=provider.key,
        )
        asset = await req.proxy.ensure(req, spec)
        return registry.parse_sums(await req.proxy.read(asset, max_bytes=registry.MAX_SUMS))

    async def _signature(self, req: FormatRequest, provider: Provider, version: str, known: registry.Download) -> bytes:
        spec = AssetSpec(
            path=paths.signature_key(provider, version),
            kind=AssetKind.METADATA,
            upstream_url=known.shasums_signature_url,
            content_type=OCTETS,
            max_size=registry.MAX_SIGNATURE,
            head_passthrough=False,
            artifact=f'{provider.key} {version} SHA256SUMS.sig',
            component=provider.key,
        )
        asset = await req.proxy.ensure(req, spec)
        return await req.proxy.read(asset, max_bytes=registry.MAX_SIGNATURE)

    async def _any_download(
        self, req: FormatRequest, provider: Provider, version: str, platforms: tuple[tuple[str, str], ...]
    ) -> registry.Download:
        """A download document of any platform of the version (they all name the same ``SHA256SUMS``)."""
        preferred = sorted(platforms, key=lambda p: p != ('linux', 'amd64')) or [('linux', 'amd64')]
        missing: NotFound | None = None
        for os_, arch in preferred[:MAX_DOWNLOAD_TRIES]:
            try:
                return await self.download(req, provider, version, os_, arch)
            except NotFound as exc:
                missing = exc
        raise missing or NotFound('Not Found')

    # ------------------------------------------------------------------ mirror routes
    def _generated(self, req: FormatRequest, body: bytes, started: float) -> Response:
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        headers = {'ETag': etag, 'Cache-Control': 'no-cache'}
        match = req.request.headers.get('if-none-match')
        if match and etag in {m.strip() for m in match.split(',')}:
            req.hosted.account(req, CacheState.HIT, 304, 0, started)
            return Response(status_code=304, headers=headers)
        if req.is_head:
            req.hosted.account(req, CacheState.HIT, 200, 0, started)
            headers['Content-Length'] = str(len(body))
            return Response(status_code=200, headers=headers, media_type=JSON)
        req.hosted.account(req, CacheState.HIT, 200, len(body), started)
        return Response(body, status_code=200, headers=headers, media_type=JSON)

    async def _index(self, req: FormatRequest, provider: Provider) -> Response:
        started = time.perf_counter()
        versions = await self.versions(req, provider)
        return self._generated(req, registry.render_index(versions), started)

    async def _version(self, req: FormatRequest, route: Route) -> Response:
        started = time.perf_counter()
        provider, version = route.provider, route.version
        info = (await self.versions(req, provider)).get(version)
        if info is None:
            raise NotFound(f'not found: {provider.key} has no version {version}')
        known = await self._any_download(req, provider, version, info.platforms)
        listed = await self.sums(req, provider, version, known)
        items = registry.archives(listed, known, info.platforms)
        h1 = await tf_db.h1_hashes(req.proxy.db, req.repo.id, provider.key, version)
        return self._generated(req, registry.render_version(version, items, h1), started)

    async def _archive(self, req: FormatRequest, route: Route) -> Response:
        provider, version = route.provider, route.version
        known = await self.download(req, provider, version, route.os, route.arch)
        if known.filename != route.filename:
            raise NotFound(
                f'not found: the {route.os}_{route.arch} archive of {provider.key} {version} is {known.filename}'
            )
        found: dict[str, str] = {}

        async def expect() -> Expected | None:
            listed = await self.sums(req, provider, version, known)
            if listed.get(known.filename) != known.shasum:
                raise DigestMismatch(f"SHA256SUMS does not list {known.filename} with the registry's shasum")
            return CheckedExpected(sha256=known.shasum, check=_h1_check(found))

        spec = AssetSpec(
            path=paths.archive_key(provider, version, route.os, route.arch, known.filename),
            kind=AssetKind.CONTENT,
            upstream_url=known.download_url,
            content_type=ZIP,
            max_size=MAX_ARCHIVE,
            expect=expect,
            on_stored=self._on_archive_stored(req.proxy, req.repo.id, provider, known, version, found),
            artifact=f'{provider.key} {version} {route.os}_{route.arch}',
            component=provider.key,
        )
        return await req.proxy.serve(req, spec)

    # ------------------------------------------------------------------ browse summaries
    def _on_versions_stored(self, engine: ProxyEngine, repo_id: str, provider: Provider) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            if data is None:
                return
            raw = data

            async def index() -> None:
                versions = await asyncio.to_thread(registry.parse_versions, raw)
                latest = registry.latest_of(list(versions))
                await tf_db.record_versions(engine.db, repo_id, provider.key, latest, len(versions))

            self._indexing.spawn((repo_id, provider.key, asset.blob_sha256), index)

        return stored

    def _on_archive_stored(
        self,
        engine: ProxyEngine,
        repo_id: str,
        provider: Provider,
        known: registry.Download,
        version: str,
        found: dict[str, str],
    ) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            async def index() -> None:
                await tf_db.record_archive(
                    engine.db,
                    repo_id,
                    provider.key,
                    version=version,
                    os=known.os,
                    arch=known.arch,
                    filename=known.filename,
                    sha256=known.shasum,
                    h1=found.get('h1'),
                )

            self._indexing.spawn((repo_id, provider.key, version, known.os, known.arch), index)

        return stored


def _validate_versions(data: bytes, info: UpstreamInfo) -> None:
    registry.parse_versions(data)
