"""PyPI plugin (T-P5-PYPI; FORMATS.md "PyPI"): the simple repository API for proxy, hosted and group repositories.

Routes under ``/repository/<repo>/``:

- ``simple/`` (the project index) and ``simple/<project>/`` (a project page), HTML (PEP 503) or JSON (PEP 691) by
  ``Accept``; a non-normalised name, or a missing trailing slash, redirects (``301``) to the normalised URL.
- ``packages/<project>/<file>``: distribution files; ``packages/<project>/<file>.metadata``: the PEP 658 core
  metadata file of a distribution whose page entry announces one.
- ``pypi/<project>/json`` and ``pypi/<project>/<version>/json``: the legacy JSON API (proxies, pass-through with the
  file URLs rewritten into the repository; ``json_api`` option).
- ``POST`` to the repository root (or ``legacy/``): the upload API used by twine and ``uv publish`` (hosted, and
  groups with a writable member).

**Proxy**: each project page is cached twice, once per representation (``simple/<project>/index.json`` and
``index.html``, metadata revalidated after the repository's metadata max age), both rendered from the upstream's
page (JSON preferred, HTML accepted) with every file URL rewritten to ``../../packages/<project>/<file>`` and the
hashes, ``requires-python``, yanked state and core-metadata hashes kept. A file is fetched from the URL the cached
JSON page names for it (refreshed once when the file is not listed: published since) and verified against the page's
hashes (sha256, else sha512/sha1/md5, and the size when given) before it is committed; a file the page does not list
is 404. ``.metadata`` files are fetched from ``<file URL>.metadata`` and verified against the page's
``core-metadata`` hashes. The upstream URL is the index root (``https://pypi.org``: pages under ``simple/``), or a
simple root itself when it ends in ``/simple`` or ``/+simple``.

**Hosted** (``formats.pypi.hosted``) and **group** (``formats.pypi.group``) pages are rendered per request from the
stored files and the members' pages.

Browse (``browser``, ``formats.pypi.browse``): project summaries are recorded in ``pypi_packages`` in the background
when a project page is cached (parsed in a thread) and on every upload.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

from starlette.responses import RedirectResponse, Response

from waystation_common.errors import NotFound, UpstreamError, WaystationError
from waystation_server.db import pypi as pypi_db
from waystation_server.formats.base import FormatDescriptor, FormatRequest
from waystation_server.formats.common.background import Background
from waystation_server.formats.pypi import simple
from waystation_server.formats.pypi.browse import PypiBrowse
from waystation_server.formats.pypi.common import (
    FILE_TYPE,
    METADATA_TYPE,
    SAFE_HEADERS,
    Op,
    Route,
    file_path,
    index_key,
    media_type_of,
    page_key,
    parse_route,
    pypi_error,
    summary_fields,
)
from waystation_server.formats.pypi.descriptor import DESCRIPTOR, PypiOptions, simple_root
from waystation_server.formats.pypi.group import PypiGroup
from waystation_server.formats.pypi.hosted import PypiHosted
from waystation_server.proxy.engine import (
    AssetKind,
    AssetSpec,
    Expected,
    OnStored,
    ProxyEngine,
    StoredAsset,
    UpstreamInfo,
)
from waystation_server.repos.model import RepoType

PARSED_PAGES = 32


class PypiPlugin:
    def __init__(self, descriptor: FormatDescriptor = DESCRIPTOR) -> None:
        self.descriptor = descriptor
        self.browser = PypiBrowse()
        self.hosted = PypiHosted()
        self.group = PypiGroup(self)
        self._indexing = Background('pypi-packages')
        self._parsed: OrderedDict[str, dict[str, simple.DistFile]] = OrderedDict()

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        await self._indexing.drain()

    async def handle(self, req: FormatRequest) -> Response:
        try:
            return await self._handle(req)
        except WaystationError as exc:
            headers = getattr(exc, 'headers', None)
            return pypi_error(exc.status, exc.detail, headers=dict(headers) if headers else None)

    async def _handle(self, req: FormatRequest) -> Response:
        route = parse_route(req.path)
        if route is None:
            raise NotFound('Not Found')
        req.log(op=route.op.value)
        if route.op is Op.UPLOAD:
            if req.method != 'POST':
                if req.path == '':
                    raise NotFound('Not Found')
                return pypi_error(405, 'method not allowed', headers={'Allow': 'POST'})
            if req.repo.type is RepoType.GROUP:
                return await self.group.upload(req)
            if req.repo.type is RepoType.HOSTED:
                return await self.hosted.upload(req)
            return pypi_error(405, 'this repository is a read-only proxy: upload to a hosted repository')
        if req.method not in ('GET', 'HEAD'):
            return pypi_error(405, 'method not allowed', headers={'Allow': 'GET, HEAD'})
        if route.op in (Op.INDEX, Op.PROJECT):
            redirect = self._canonical(req, route)
            if redirect is not None:
                return redirect
        return await self.serve(req, route)

    @staticmethod
    def _canonical(req: FormatRequest, route: Route) -> Response | None:
        """PEP 503: project pages live at the normalised name, with a trailing slash."""
        slash = req.request.url.path.endswith('/')
        if route.op is Op.INDEX:
            target = f'/repository/{req.repo.name}/simple/'
            return None if slash else RedirectResponse(target, status_code=301)
        project = simple.normalise(route.project)
        if slash and project == route.project:
            return None
        query = f'?{req.request.url.query}' if req.request.url.query else ''
        target = f'/repository/{req.repo.name}/simple/{quote(project)}/{query}'
        return RedirectResponse(target, status_code=301)

    async def serve(self, req: FormatRequest, route: Route) -> Response:
        """Answer ``route`` (canonical) from ``req.repo``: a proxy, a hosted repository, or a group."""
        if route.op in (Op.INDEX, Op.PROJECT):
            chosen = simple.negotiate(req.request.headers.get('accept'))
            if chosen is None:
                return pypi_error(406, 'acceptable types: application/vnd.pypi.simple.v1+json, text/html')
        else:
            chosen = ''
        if req.repo.type is RepoType.GROUP:
            return await self.group.serve(req, route, chosen)
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.serve(req, route, chosen)
        return await self.proxy_serve(req, route, chosen)

    # ------------------------------------------------------------------ proxy
    async def proxy_serve(self, req: FormatRequest, route: Route, chosen: str) -> Response:
        match route.op:
            case Op.INDEX:
                spec = self.index_spec(req, as_json=simple.is_json(chosen))
                return self._negotiated(await req.proxy.serve(req, spec), chosen)
            case Op.PROJECT:
                spec = self.page_spec(req, simple.normalise(route.project), as_json=simple.is_json(chosen))
                return self._negotiated(await req.proxy.serve(req, spec), chosen)
            case Op.FILE | Op.METADATA:
                spec = await self.file_spec(req, route.project, route.filename, metadata=route.op is Op.METADATA)
                response = await req.proxy.serve(req, spec)
                response.headers.update(SAFE_HEADERS)
                return response
            case Op.LEGACY_JSON:
                return await req.proxy.serve(req, self.legacy_spec(req, route))
        raise NotFound('Not Found')  # pragma: no cover

    @staticmethod
    def _negotiated(response: Response, chosen: str) -> Response:
        """The cached representation answers with exactly the negotiated type (``text/html`` or PEP 691's)."""
        if response.status_code < 400:
            response.headers['content-type'] = media_type_of(chosen)
        response.headers['Vary'] = 'Accept'
        response.headers.update(SAFE_HEADERS)
        return response

    @staticmethod
    def _upstream_root(req: FormatRequest) -> str:
        if req.repo.proxy is None:
            raise NotFound('Not Found')
        return simple_root(req.repo.proxy.upstream_url)

    def page_spec(self, req: FormatRequest, project: str, *, as_json: bool, max_age_s: int | None = None) -> AssetSpec:
        """A project page in one representation (both are rendered from the upstream's page, JSON preferred)."""
        url = f'{self._upstream_root(req)}{quote(project)}/'
        found: dict[str, dict[str, Any]] = {}

        def transform(data: bytes, info: UpstreamInfo) -> bytes:
            page = simple.parse_upstream(data, info.headers.get('content-type', ''), info.url, project)
            if page.name and simple.normalise(page.name) != project:
                raise UpstreamError('upstream page is for a different project')
            found['summary'] = summary_fields(page.name or project, simple.versions_of(page))
            if as_json:
                return simple.render_json(page, project, internal=True)
            return simple.render_html(page, project)

        return AssetSpec(
            path=page_key(project, as_json=as_json),
            kind=AssetKind.METADATA,
            upstream_url=url,
            upstream_headers={'Accept': simple.UPSTREAM_ACCEPT},
            content_type=simple.SIMPLE_JSON if as_json else simple.HTML_TYPE,
            transform=transform,
            max_age_s=max_age_s,
            on_stored=self._on_page_stored(req.proxy, req.repo.id, project, found),
            artifact=project,
            component=project,
        )

    def _on_page_stored(
        self, engine: ProxyEngine, repo_id: str, project: str, found: dict[str, dict[str, Any]]
    ) -> OnStored:
        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            fields = found.get('summary')
            if fields is None:
                return

            async def index() -> None:
                await pypi_db.upsert_package(engine.db, repo_id, project, fields)

            self._indexing.spawn((repo_id, project, asset.blob_sha256), index)

        return stored

    def index_spec(self, req: FormatRequest, *, as_json: bool) -> AssetSpec:
        def transform(data: bytes, info: UpstreamInfo) -> bytes:
            names = simple.parse_index(data, info.headers.get('content-type', ''))
            return simple.render_index(names, as_json=as_json)

        return AssetSpec(
            path=index_key(as_json=as_json),
            kind=AssetKind.METADATA,
            upstream_url=self._upstream_root(req),
            upstream_headers={'Accept': simple.UPSTREAM_ACCEPT},
            content_type=simple.SIMPLE_JSON if as_json else simple.HTML_TYPE,
            transform=transform,
            artifact='simple index',
        )

    async def project_page(self, req: FormatRequest, project: str) -> simple.Page:
        """A proxy's cached (refreshed as needed) JSON page of ``project``, parsed (members of a group)."""
        asset = await req.proxy.ensure(req, self.page_spec(req, project, as_json=True))
        data = await req.proxy.read(asset)
        return await asyncio.to_thread(simple.parse_json, data, '', internal=True)

    async def index_names(self, req: FormatRequest) -> list[str]:
        asset = await req.proxy.ensure(req, self.index_spec(req, as_json=True))
        data = await req.proxy.read(asset)
        return await asyncio.to_thread(simple.parse_index, data, simple.SIMPLE_JSON)

    async def lookup(self, req: FormatRequest, project: str, filename: str) -> simple.DistFile:
        """The page entry of a file (cached JSON page; refreshed once when the file is not listed). 404 when the
        upstream does not list it."""
        for max_age_s in (None, 0):
            asset = await req.proxy.ensure(req, self.page_spec(req, project, as_json=True, max_age_s=max_age_s))
            files = self._parsed.get(asset.blob_sha256)
            if files is None:
                data = await req.proxy.read(asset)
                page = await asyncio.to_thread(simple.parse_json, data, '', internal=True)
                files = {entry.filename: entry for entry in page.files}
                self._parsed[asset.blob_sha256] = files
                while len(self._parsed) > PARSED_PAGES:
                    self._parsed.popitem(last=False)
            else:
                self._parsed.move_to_end(asset.blob_sha256)
            entry = files.get(filename)
            if entry is not None and entry.url:
                return entry
        raise NotFound('Not Found')

    async def file_spec(self, req: FormatRequest, project: str, filename: str, *, metadata: bool) -> AssetSpec:
        path = file_path(project, filename, metadata=metadata)
        cached = await req.hosted.get(req.repo, path)
        if cached is not None and cached.upstream.url:
            # Cached: no page lookup unless the engine has to fetch again (content max age).
            async def expect() -> Expected | None:
                entry = await self.lookup(req, project, filename)
                return entry.metadata_expected() if metadata else entry.expected()

            return AssetSpec(
                path=path,
                kind=AssetKind.CONTENT,
                upstream_url=cached.upstream.url,
                expect=expect,
                content_type=METADATA_TYPE if metadata else FILE_TYPE,
                artifact=filename,
                component=project,
            )
        entry = await self.lookup(req, project, filename)
        if metadata and not entry.has_metadata:
            raise NotFound('Not Found')
        return AssetSpec(
            path=path,
            kind=AssetKind.CONTENT,
            upstream_url=entry.url + '.metadata' if metadata else entry.url,
            expected=entry.metadata_expected() if metadata else entry.expected(),
            content_type=METADATA_TYPE if metadata else FILE_TYPE,
            artifact=f'{filename}.metadata' if metadata else filename,
            component=project,
        )

    def legacy_spec(self, req: FormatRequest, route: Route) -> AssetSpec:
        options = self.descriptor.parse_options(req.repo.format_options)
        if not isinstance(options, PypiOptions) or not options.json_api or req.repo.proxy is None:
            raise NotFound('Not Found')
        base = req.repo.proxy.upstream_url.rstrip('/')
        if base.endswith(('/simple', '/+simple')):
            raise NotFound('the JSON API is not available from this upstream')
        project = simple.normalise(route.project)
        repo_url = req.repo_url
        version = f'{quote(route.version, safe="")}/' if route.version else ''
        path = f'pypi/{project}/{version}json'

        def transform(data: bytes, info: UpstreamInfo) -> bytes:
            return simple.rewrite_legacy_json(data, project, repo_url)

        return AssetSpec(
            path=path,
            kind=AssetKind.METADATA,
            upstream_url=f'{base}/{path}',
            upstream_headers={'Accept': simple.LEGACY_JSON},
            content_type=simple.LEGACY_JSON,
            transform=transform,
            artifact=f'{project} JSON',
            component=project,
        )
