"""Hugging Face plugin (T-P6-HF; FORMATS.md "Hugging Face"): a proxy of the Hub for huggingface_hub clients
(``HF_ENDPOINT=<base>/repository/<repo>``).

- **Info** (``api/<kind>s/<id>[/revision/<rev>]``): fetched with ``blobs=true`` and cached as metadata (revalidated
  after the repository's metadata max age; a commit's document never changes), served to clients and read by the
  plugin itself: it resolves a revision to its commit and lists every file with its size, git blob id and LFS
  sha256 (``formats.huggingface.metadata``).
- **Files** (``[datasets/]<id>/resolve/<rev>/<path>``): the revision is resolved through its info document, the file
  is cached as content under ``[datasets/]<id>/resolve/<commit>/<path>`` (so a moved branch never serves another
  commit's bytes) and fetched from ``.../resolve/<commit>/<path>``, following the Hub's redirects only to its own host
  and the repository's ``cdn_hosts``. LFS files are verified against their sha256 and size, other files against
  their git blob id and size, before anything is committed (a mismatch: 502, nothing cached). Large files stream
  through the engine (tee, coalescing, fills that outlive their client).
- ``HEAD`` on a file is answered from the info document without contacting the upstream, as huggingface_hub expects:
  ``X-Repo-Commit``, ``ETag`` / ``X-Linked-Etag`` (the LFS sha256, else the git blob id), ``X-Linked-Size`` and
  ``Content-Length``, status 200 (the client then downloads the same URL from Waystation).
- **Tree** (``api/<kind>s/<id>/tree/<rev>[/<path>]``, ``snapshot_download``): cached as metadata per query, ``Link``
  pages rewritten to the repository.

Xet: ``xetHash`` fields are removed from cached documents and ``X-Xet-*`` headers are never sent, so clients with
hf_xet installed download over HTTP through Waystation. Upstream credentials (the repository's bearer token: a Hub
token for gated or private repositories) go only to the upstream URL's host, never to the CDN, and a client's
credentials are never forwarded.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from collections import OrderedDict
from urllib.parse import urlsplit

from starlette.responses import Response

from waystation_common.errors import NotFound
from waystation_common.huggingface import Kind, display_name, file_key, is_commit
from waystation_server.db import assets as assets_db
from waystation_server.db import huggingface as hf_db
from waystation_server.formats.base import FormatDescriptor, FormatRequest, WarmContext
from waystation_server.formats.huggingface import prefetch
from waystation_server.formats.huggingface.browse import HuggingfaceBrowse
from waystation_server.formats.huggingface.descriptor import DESCRIPTOR, HuggingfaceOptions, host_allowed
from waystation_server.formats.huggingface.metadata import (
    MAX_INFO,
    MAX_TREE,
    FileInfo,
    Revision,
    git_blob_check,
    parse_info,
    rewrite_link,
    strip_xet,
    validate_info,
    validate_tree,
)
from waystation_server.formats.huggingface.paths import (
    Op,
    Route,
    info_key,
    info_upstream,
    parse_route,
    resolve_upstream,
    tree_key,
    tree_query,
    tree_upstream,
)
from waystation_server.problems import native_error
from waystation_server.proxy.engine import AssetKind, AssetSpec, CheckedExpected, Expected, OnStored, StoredAsset
from waystation_server.repos.model import RepoType

log = logging.getLogger('waystation.formats.huggingface')

JSON_TYPE = 'application/json'
IMMUTABLE_S = 10 * 365 * 86400  # a commit's documents never change
PARSED = 128  # parsed info documents kept per process (by blob)
MAX_CURSOR = 1024
SAFE_HEADERS = {'X-Content-Type-Options': 'nosniff'}


def _flag(value: str | None) -> bool:
    return (value or '').lower() in ('1', 'true')


def _content_type(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path, strict=False)
    return guessed or 'application/octet-stream'


def hub_headers(commit: str, file: FileInfo) -> dict[str, str]:
    """What the Hub answers for a file (``huggingface_hub.file_download.get_hf_file_metadata`` reads them)."""
    etag = f'"{file.etag}"'
    return {'X-Repo-Commit': commit, 'ETag': etag, 'X-Linked-Etag': etag, 'X-Linked-Size': str(file.size)}


class HuggingfacePlugin:
    def __init__(self, descriptor: FormatDescriptor = DESCRIPTOR) -> None:
        self.descriptor = descriptor
        self.browser = HuggingfaceBrowse()
        self._parsed: OrderedDict[str, Revision] = OrderedDict()  # by info document blob
        # By (repository, kind, repo id, commit): a branch's document also answers for its commit, so a client that
        # resolved ``main`` and then asks for files at the commit (``snapshot_download``) needs no second document.
        self._commits: OrderedDict[tuple[str, str, str, str], Revision] = OrderedDict()

    def options(self, req: FormatRequest) -> HuggingfaceOptions:
        options = self.descriptor.parse_options(req.repo.format_options)
        return options if isinstance(options, HuggingfaceOptions) else HuggingfaceOptions()

    async def warm(self, ctx: WarmContext, item: str) -> None:
        """Prefetch (T-O12): ``formats.huggingface.prefetch``."""
        await prefetch.warm(ctx, item)

    async def handle(self, req: FormatRequest) -> Response:
        route = parse_route(req.path)
        if route is None or req.repo.type is not RepoType.PROXY or req.repo.proxy is None:
            raise NotFound('Not Found')
        if req.method not in ('GET', 'HEAD'):
            return native_error(405, 'this repository is a read-only proxy', headers={'Allow': 'GET, HEAD'})
        req.log(op=route.op.value)
        match route.op:
            case Op.INFO:
                response = await req.proxy.serve(req, self.info_spec(req, route.kind, route.repo_id, route.revision))
            case Op.TREE:
                response = await self.tree(req, route)
            case _:
                response = await self.resolve(req, route)
        response.headers.update(SAFE_HEADERS)
        return response

    # ------------------------------------------------------------------ info documents
    def info_spec(self, req: FormatRequest, kind: Kind, repo_id: str, revision: str | None) -> AssetSpec:
        immutable = revision is not None and is_commit(revision)
        return AssetSpec(
            path=info_key(kind, repo_id, revision),
            kind=AssetKind.METADATA,
            upstream_path=info_upstream(kind, repo_id, revision),
            content_type=JSON_TYPE,
            max_size=MAX_INFO,
            max_age_s=IMMUTABLE_S if immutable else None,
            validate=validate_info,
            transform=strip_xet,
            on_stored=self._on_info(req, kind, repo_id, revision),
            artifact=f'{display_name(kind, repo_id)}@{revision or "main"}',
        )

    def _on_info(self, req: FormatRequest, kind: Kind, repo_id: str, revision: str | None) -> OnStored:
        db, repo = req.proxy.db, req.repo.id

        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            if data is None:
                return
            parsed = await asyncio.to_thread(parse_info, data)
            self._remember(asset.blob_sha256, parsed, (repo, kind, repo_id))
            if revision is None or not is_commit(revision):
                await hf_db.record_revision(db, repo, kind, repo_id, revision, parsed.commit)
            else:
                await hf_db.touch(db, repo, kind, repo_id)

        return stored

    def _remember(self, blob: str, parsed: Revision, where: tuple[str, str, str]) -> None:
        commit = (*where, parsed.commit)
        self._parsed[blob] = self._commits[commit] = parsed
        self._parsed.move_to_end(blob)
        self._commits.move_to_end(commit)
        while len(self._parsed) > PARSED:
            self._parsed.popitem(last=False)
        while len(self._commits) > PARSED:
            self._commits.popitem(last=False)

    async def revision(self, req: FormatRequest, kind: Kind, repo_id: str, revision: str) -> Revision:
        """The commit and files of ``revision`` (its info document, fetched or revalidated as needed; a commit
        another revision's document already named is answered from memory)."""
        where = (req.repo.id, kind, repo_id)
        if is_commit(revision):
            known = self._commits.get((*where, revision)) or await self._from_refs(req, kind, repo_id, revision)
            if known is not None:
                return known
        asset = await req.proxy.ensure(req, self.info_spec(req, kind, repo_id, revision))
        parsed = self._parsed.get(asset.blob_sha256)
        if parsed is None:
            data = await req.proxy.read(asset, max_bytes=MAX_INFO)
            parsed = await asyncio.to_thread(parse_info, data)
        self._remember(asset.blob_sha256, parsed, where)
        return parsed

    async def _from_refs(self, req: FormatRequest, kind: Kind, repo_id: str, commit: str) -> Revision | None:
        """A commit's files from the cached document of a revision that resolved to it (``huggingface_repos``): no
        upstream request, and an offline repository still answers for the commits its cached revisions name."""
        db = req.proxy.db
        summary = await hf_db.get(db, req.repo.id, display_name(kind, repo_id))
        if summary is None:
            return None
        keys = [info_key(kind, repo_id, None)] if summary.get('latest') == commit else []
        keys += [
            info_key(kind, repo_id, str(ref['revision']))
            for ref in summary.get('refs') or []
            if isinstance(ref, dict) and ref.get('commit') == commit and ref.get('revision')
        ]
        for key in keys:
            asset = await assets_db.get(db, req.repo.id, key)
            if asset is None or not await req.proxy.blobs.exists(asset.blob_sha256):
                continue
            parsed = self._parsed.get(asset.blob_sha256)
            if parsed is None:
                parsed = await asyncio.to_thread(parse_info, await req.proxy.read(asset, max_bytes=MAX_INFO))
            if parsed.commit == commit:
                self._remember(asset.blob_sha256, parsed, (req.repo.id, kind, repo_id))
                return parsed
        return None

    # ------------------------------------------------------------------ tree listings
    async def tree(self, req: FormatRequest, route: Route) -> Response:
        params = req.request.query_params
        limit_raw = params.get('limit')
        limit = int(limit_raw) if limit_raw and limit_raw.isdigit() and 0 < int(limit_raw) <= 10_000 else None
        cursor = params.get('cursor')
        if cursor is not None and len(cursor) > MAX_CURSOR:
            return native_error(400, 'cursor too long')
        query = tree_query(_flag(params.get('recursive')), _flag(params.get('expand')), cursor, limit)
        spec = AssetSpec(
            path=tree_key(route, query),
            kind=AssetKind.METADATA,
            upstream_path=tree_upstream(route, query),
            content_type=JSON_TYPE,
            max_size=MAX_TREE,
            max_age_s=IMMUTABLE_S if route.immutable else None,
            validate=validate_tree,
            transform=strip_xet,
            store_headers=('Link',),
            artifact=f'{display_name(route.kind, route.repo_id)}@{route.revision} tree',
        )
        response = await req.proxy.serve(req, spec)
        link = response.headers.get('link')
        proxy = req.repo.proxy
        if link and proxy is not None:
            response.headers['Link'] = rewrite_link(link, proxy.upstream_url, req.repo_url)
        return response

    # ------------------------------------------------------------------ files
    async def resolve(self, req: FormatRequest, route: Route) -> Response:
        assert route.revision is not None
        revision = await self.revision(req, route.kind, route.repo_id, route.revision)
        file = revision.files.get(route.path)
        if file is None:
            return native_error(
                404,
                'Entry not found',
                headers={'X-Error-Code': 'EntryNotFound', 'X-Repo-Commit': revision.commit},
            )
        headers = hub_headers(revision.commit, file)
        if req.is_head:
            return Response(
                status_code=200,
                headers={
                    **headers,
                    'Content-Length': str(file.size),
                    'Content-Type': _content_type(file.path),
                    'Accept-Ranges': 'bytes',
                },
            )
        response = await req.proxy.serve(req, self.file_spec(req, route.kind, route.repo_id, revision.commit, file))
        response.headers.update({k: v for k, v in headers.items() if k != 'ETag'})
        return response

    def file_spec(self, req: FormatRequest, kind: Kind, repo_id: str, commit: str, file: FileInfo) -> AssetSpec:
        proxy = req.repo.proxy
        upstream_host = (urlsplit(proxy.upstream_url).hostname or '').lower() if proxy is not None else ''
        cdn_hosts = self.options(req).cdn_hosts

        def allowed(url: str) -> bool:
            host = (urlsplit(url).hostname or '').lower()
            return bool(host) and (host == upstream_host or host_allowed(host, cdn_hosts))

        expected: Expected
        if file.sha256 is not None:
            expected = Expected(sha256=file.sha256, size=file.size)
        elif file.blob_id is not None:
            expected = CheckedExpected(size=file.size, check=git_blob_check(file.blob_id, file.size))
        else:  # pragma: no cover - parse_info keeps only files with one of both
            expected = Expected(size=file.size)
        db, repo = req.proxy.db, req.repo.id

        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            await hf_db.touch(db, repo, kind, repo_id)

        return AssetSpec(
            path=file_key(kind, repo_id, commit, file.path),
            kind=AssetKind.CONTENT,
            upstream_path=resolve_upstream(kind, repo_id, commit, file.path),
            expected=expected,
            content_type=_content_type(file.path),
            redirect_allowed=allowed,
            on_stored=stored,
            artifact=file.path.rsplit('/', 1)[-1],
            component=f'{display_name(kind, repo_id)}@{commit[:12]}',
        )
