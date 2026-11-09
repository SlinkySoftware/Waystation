"""Go modules plugin (T-P5-GO; FORMATS.md "Go modules"): the GOPROXY protocol and the checksum database proxy for
proxy and group repositories.

Routes under ``/repository/<repo>/`` (module paths and versions case-encoded, ``modpath``):

- ``<module>/@v/list``: the module's tagged versions (metadata: revalidated after the repository's metadata max age;
  validated as a version list).
- ``<module>/@latest``: the latest version's ``.info`` (metadata; validated JSON).
- ``<module>/@v/<version>.info``: the version's JSON ``{"Version", "Time"}``. Content (immutable) for canonical
  versions, checked to name that version; metadata for queries the upstream resolves (``master.info``).
- ``<module>/@v/<version>.mod`` / ``.zip``: content, canonical versions only (else 404). Before they are committed
  (and before the client gets the last bytes) they are checked against the module's ``go.sum`` lines from the
  checksum database (``verify_checksums``, ``gosum``): the database's lookup record is fetched (and cached) through
  the repository first. A module the database does not know (404/410, e.g. a private one) is cached unverified; one
  matching ``nosumdb`` is not looked up; a database that cannot be reached fails the download (502).
- ``sumdb/<name>/...``: the checksum database proxy (``sumdb``).

Every path is decoded, validated and re-encoded, so the asset key and the upstream URL use the canonical encoding (an
upper-case letter in a request is accepted as its ``!`` form). Upstream ``404`` and ``410`` both answer ``404`` (the
go command treats them alike: "not found", try the next proxy). Size limits: lists 4 MiB, ``.info`` 1 MiB, ``.mod``
16 MiB, ``.zip`` 500 MiB (the go command's own limits).

Groups (``formats.go.group``) merge version lists and answer files from the first member that has them.

Browse (``browser``, ``formats.go.browse``): module summaries are recorded in ``go_modules`` in the background when a
version list or a module file is cached.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import BinaryIO

from starlette.responses import Response

from waystation_common.errors import DigestMismatch, NotFound, UpstreamError, WaystationError
from waystation_server.db import go as go_db
from waystation_server.formats.base import FormatDescriptor, FormatRequest
from waystation_server.formats.common.background import Background
from waystation_server.formats.go import gosum, modpath, sumdb
from waystation_server.formats.go.browse import GoBrowse
from waystation_server.formats.go.common import (
    JSON,
    MAX_LIST,
    SAFE_HEADERS,
    TEXT,
    ZIP,
    go_error,
    latest_of,
    matches_prefix_patterns,
    parse_list,
)
from waystation_server.formats.go.descriptor import DESCRIPTOR, GoOptions
from waystation_server.formats.go.group import GoGroup
from waystation_server.formats.go.modpath import Op, Route
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

MAX_INFO = 1024 * 1024
MAX_MOD = 16 * 1024 * 1024
MAX_ZIP = 500 * 1024 * 1024


def _info_check(version: str | None) -> Callable[[BinaryIO], None]:
    def check(file: BinaryIO) -> None:
        validate_info(file.read(MAX_INFO + 1), version)

    return check


def validate_info(data: bytes, version: str | None) -> None:
    """A ``.info`` / ``@latest`` answer: a JSON object whose ``Version`` is canonical (and ``version`` when given)."""
    if len(data) > MAX_INFO:
        raise DigestMismatch('version info is too large')
    try:
        doc = json.loads(data)
    except ValueError:
        raise DigestMismatch('version info is not JSON') from None
    found = doc.get('Version') if isinstance(doc, dict) else None
    if not isinstance(found, str) or not modpath.is_canonical(found):
        raise DigestMismatch('version info has no valid Version')
    if version is not None and found != version:
        raise DigestMismatch('version info is for another version')


class GoPlugin:
    def __init__(self, descriptor: FormatDescriptor = DESCRIPTOR) -> None:
        self.descriptor = descriptor
        self.browser = GoBrowse()
        self.group = GoGroup(self)
        self._indexing = Background('go-modules')

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        await self._indexing.drain()

    def options(self, req: FormatRequest) -> GoOptions:
        options = self.descriptor.parse_options(req.repo.format_options)
        return options if isinstance(options, GoOptions) else GoOptions()

    async def handle(self, req: FormatRequest) -> Response:
        try:
            return await self._handle(req)
        except WaystationError as exc:
            headers = getattr(exc, 'headers', None)
            return go_error(exc.status, exc.detail, headers=dict(headers) if headers else None)

    async def _handle(self, req: FormatRequest) -> Response:
        route = modpath.parse_route(req.path)
        if route is None:
            raise NotFound('not found: not a Go module proxy path')
        if req.method not in ('GET', 'HEAD'):
            return go_error(405, 'method not allowed', headers={'Allow': 'GET, HEAD'})
        req.log(op=route.op.value)
        return await self.serve(req, route)

    async def serve(self, req: FormatRequest, route: Route) -> Response:
        if req.repo.type is RepoType.GROUP:
            return await self.group.serve(req, route)
        response = await self.proxy_serve(req, route)
        response.headers.update(SAFE_HEADERS)
        return response

    # ------------------------------------------------------------------ proxy
    async def proxy_serve(self, req: FormatRequest, route: Route) -> Response:
        if req.repo.proxy is None:
            raise NotFound('Not Found')
        if route.op is Op.SUMDB:
            return await self.sumdb_serve(req, route)
        return await req.proxy.serve(req, self.module_spec(req, route))

    def module_spec(self, req: FormatRequest, route: Route) -> AssetSpec:
        """The asset of a GOPROXY path (``route.op`` not ``SUMDB``) of a proxy repository."""
        key = route.key
        module = route.module
        match route.op:
            case Op.LIST:
                return AssetSpec(
                    path=key,
                    component=module,
                    kind=AssetKind.METADATA,
                    content_type=TEXT,
                    max_size=MAX_LIST,
                    validate=_validate_list,
                    on_stored=self._on_list_stored(req.proxy, req.repo.id, module),
                    artifact=f'{module} versions',
                )
            case Op.LATEST:
                return AssetSpec(
                    path=key,
                    component=module,
                    kind=AssetKind.METADATA,
                    content_type=JSON,
                    max_size=MAX_INFO,
                    validate=_validate_latest,
                    artifact=f'{module}@latest',
                )
            case Op.INFO:
                canonical = modpath.is_canonical(route.version)
                indexed = self._on_version_stored(req.proxy, req.repo.id, module, route.version)
                return AssetSpec(
                    path=key,
                    component=module,
                    kind=AssetKind.CONTENT if canonical else AssetKind.METADATA,
                    content_type=JSON,
                    max_size=MAX_INFO,
                    expected=CheckedExpected(check=_info_check(route.version if canonical else None)),
                    on_stored=indexed if canonical else None,
                    head_passthrough=False,
                    artifact=f'{module}@{route.version}.info',
                )
            case Op.MOD | Op.ZIP:
                is_zip = route.op is Op.ZIP
                return AssetSpec(
                    path=key,
                    component=module,
                    kind=AssetKind.CONTENT,
                    content_type=ZIP if is_zip else TEXT,
                    max_size=MAX_ZIP if is_zip else MAX_MOD,
                    expect=self._expect(req, module, route.version, is_zip=is_zip),
                    on_stored=self._on_version_stored(req.proxy, req.repo.id, module, route.version),
                    artifact=f'{module}@{route.version}.{route.op.value}',
                )
        raise NotFound('Not Found')  # pragma: no cover

    def _expect(
        self, req: FormatRequest, module: str, version: str, *, is_zip: bool
    ) -> Callable[[], Awaitable[Expected | None]]:
        async def expect() -> Expected | None:
            record = await self.sum_record(req, module, version)
            if record is None:
                req.log(sumdb='unverified')
                return None
            h1 = record.zip_h1 if is_zip else record.mod_h1
            if h1 is None:
                raise UpstreamError('the checksum database has no hash for this file')
            req.log(sumdb='verified')
            return CheckedExpected(check=gosum.zip_check(h1) if is_zip else gosum.mod_check(h1))

        return expect

    async def sum_record(self, req: FormatRequest, module: str, version: str) -> gosum.SumRecord | None:
        """The checksum database's ``go.sum`` lines for ``module@version`` (cached lookup), or None when the
        repository does not verify this module or the database does not know it."""
        options = self.options(req)
        if not options.verify_checksums or matches_prefix_patterns(options.nosumdb, module):
            return None
        path = sumdb.SumdbPath(
            'lookup',
            f'lookup/{modpath.escape_path(module)}@{modpath.escape_version(version)}',
            module=module,
            version=version,
        )
        try:
            asset = await req.proxy.ensure(req, sumdb.lookup_spec(options.sumdb_url, options.sumdb_name, path))
        except NotFound:
            return None
        data = await req.proxy.read(asset, max_bytes=gosum.MAX_LOOKUP)
        return gosum.parse_lookup(data, module, version)

    async def sumdb_serve(self, req: FormatRequest, route: Route) -> Response:
        options = self.options(req)
        if not options.sumdb_proxy or route.sumdb != options.sumdb_name:
            raise NotFound(f'not found: checksum database {route.sumdb} is not proxied here')
        path = sumdb.parse(route.rest)
        if path is None:
            raise NotFound('not found: not a checksum database path')
        if path.kind == 'supported':
            return Response(b'', status_code=200, media_type=TEXT)
        return await req.proxy.serve(req, sumdb.spec(options.sumdb_url, options.sumdb_name, path))

    # ------------------------------------------------------------------ browse summaries
    def _on_list_stored(self, engine: ProxyEngine, repo_id: str, module: str) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            if data is None:
                return
            versions = parse_list(data)

            async def index() -> None:
                await go_db.record_list(engine.db, repo_id, module, latest_of(versions), len(versions))

            self._indexing.spawn((repo_id, module, asset.blob_sha256), index)

        return stored

    def _on_version_stored(self, engine: ProxyEngine, repo_id: str, module: str, version: str) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            async def index() -> None:
                await go_db.record_version(engine.db, repo_id, module, version)

            self._indexing.spawn((repo_id, module, 'version'), index)

        return stored


def _validate_list(data: bytes, info: UpstreamInfo) -> None:
    parse_list(data)


def _validate_latest(data: bytes, info: UpstreamInfo) -> None:
    try:
        validate_info(data, None)
    except DigestMismatch as exc:
        raise UpstreamError(exc.detail) from None
