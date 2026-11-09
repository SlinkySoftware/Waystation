"""Raw directory listings (FORMATS.md "Raw"): ``GET``/``HEAD /repository/<repo>/`` and any path ending in ``/``.

What a repository holds under the folder (hosted: uploaded files; proxy: cached files; group: the members' listings
merged, first member wins for a file name) as an HTML index for browsers, or as JSON (``Accept: application/json``)
in the shape of the browse API's folder listing (``FolderListing``: ``path``, ``entries``, ``next_cursor``).
Large folders are paged (``?cursor=`` from ``next_cursor``, ``PAGE`` entries per page; a group lists the first page
of each member). A folder with nothing under it is a 404; the repository root answers an empty listing.

Listing needs ``browse`` on the repository besides ``read`` (the router checked ``read``). The HTML is built from
escaped names only and served with a ``default-src 'none'`` policy, so a file name can never run script.
"""

from __future__ import annotations

import html
import json
import time
from collections.abc import Iterable
from urllib.parse import quote

from starlette.responses import Response

from waystation_common.errors import NotFound
from waystation_common.events import CacheState
from waystation_server.browse import tree
from waystation_server.browse.models import FolderEntry, FolderListing
from waystation_server.formats.base import FormatRequest
from waystation_server.repos.model import RepoConfig
from waystation_server.security.actions import Action

PAGE = 1000
HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'",
    'Cache-Control': 'no-cache',
}


def wants_json(accept: str | None) -> bool:
    return 'application/json' in (accept or '').lower()


async def folder(req: FormatRequest, repo: RepoConfig) -> FolderListing:
    cursor = req.request.query_params.get('cursor') or None
    return await tree.list_folder(req.hosted.db, repo.id, req.path, cursor=cursor, limit=PAGE)


def merge(listings: Iterable[FolderListing], path: str) -> FolderListing:
    """Members' listings of one folder as one: folders united, a file name taken from the first member that has it."""
    folders: dict[str, FolderEntry] = {}
    files: dict[str, FolderEntry] = {}
    for listing in listings:
        for entry in listing.entries:
            (folders if entry.kind == 'folder' else files).setdefault(entry.name, entry)
    entries = [folders[n] for n in sorted(folders)] + [files[n] for n in sorted(files)]
    return FolderListing(path=path, entries=entries, next_cursor=None)


def respond(req: FormatRequest, listing: FolderListing, started: float) -> Response:
    if wants_json(req.request.headers.get('accept')):
        body = json.dumps(listing.model_dump(mode='json'), separators=(',', ':')).encode()
        media = 'application/json'
    else:
        body = render_html(req.repo.name, listing).encode()
        media = 'text/html; charset=utf-8'
    headers = {**HEADERS, 'Content-Length': str(len(body)), 'Vary': 'Accept'}
    req.hosted.account(req, CacheState.HIT, 200, 0, started)
    return Response(b'' if req.is_head else body, media_type=media, headers=headers)


async def serve(req: FormatRequest, listings: Iterable[RepoConfig] | None = None) -> Response:
    """The listing of ``req.path`` in ``req.repo`` (or merged over ``listings``, a group's members)."""
    req.require(Action.BROWSE)
    started = time.perf_counter()
    if listings is None:
        return respond(req, await folder(req, req.repo), started)
    found: list[FolderListing] = []
    for member in listings:
        try:
            found.append(await tree.list_folder(req.hosted.db, member.id, req.path, cursor=None, limit=PAGE))
        except NotFound:
            continue
    if req.path and not any(listing.entries for listing in found):
        raise NotFound('folder not found')
    return respond(req, merge(found, req.path), started)


def render_html(repo: str, listing: FolderListing) -> str:
    title = html.escape(f'{repo}/{listing.path}'.rstrip('/') + '/')
    rows = []
    if listing.path:
        rows.append('<tr><td><a href="../">../</a></td><td></td><td></td></tr>')
    for entry in listing.entries:
        name = entry.name + ('/' if entry.kind == 'folder' else '')
        href = quote(name, safe='/')
        size = '' if entry.size is None else str(entry.size)
        modified = entry.last_modified.strftime('%Y-%m-%d %H:%M') if entry.last_modified else ''
        rows.append(
            f'<tr><td><a href="./{html.escape(href)}">{html.escape(name)}</a></td>'
            f'<td class="n">{size}</td><td>{modified}</td></tr>'
        )
    more = ''
    if listing.next_cursor:
        more = f'<p><a href="?cursor={quote(listing.next_cursor, safe="")}">Next page</a></p>'
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>Index of {title}</title>'
        '<style>body{font-family:system-ui,sans-serif;margin:2rem}td{padding:.15rem 1.5rem .15rem 0}'
        '.n{text-align:right;font-variant-numeric:tabular-nums}th{text-align:left}</style></head><body>'
        f'<h1>Index of {title}</h1><table><tr><th>Name</th><th>Size</th><th>Last modified</th></tr>'
        + ''.join(rows)
        + f'</table>{more}</body></html>\n'
    )
