"""PyPI routes, asset keys and generated responses shared by the proxy, hosted and group handlers."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from starlette.responses import Response

from waystation_common.cleanup.versions import pep440_is_prerelease, pep440_key
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.pypi import simple
from waystation_server.problems import native_error

FILE_TYPE = 'application/octet-stream'
METADATA_TYPE = 'text/plain; charset=utf-8'
SAFE_HEADERS = {'X-Content-Type-Options': 'nosniff'}


class Op(StrEnum):
    UPLOAD = 'upload'
    INDEX = 'index'
    PROJECT = 'project'
    FILE = 'file'
    METADATA = 'metadata'
    LEGACY_JSON = 'legacy-json'


@dataclass(frozen=True)
class Route:
    op: Op
    project: str = ''  # pages: as requested (may need normalising); files: the normalised name
    filename: str = ''  # files: the distribution file name (without ``.metadata``)
    version: str | None = None  # legacy JSON for one version


def parse_route(path: str) -> Route | None:
    """Map a normalised repository path to a PyPI operation."""
    if path in ('', 'legacy'):
        return Route(Op.UPLOAD)
    segments = path.split('/')
    head, rest = segments[0], segments[1:]
    if head == 'simple':
        if not rest:
            return Route(Op.INDEX)
        if len(rest) == 1 and simple.valid_project(rest[0]):
            return Route(Op.PROJECT, rest[0])
        return None
    if head == 'packages' and len(rest) == 2:
        project, filename = rest
        if project != simple.normalise(project) or not simple.valid_project(project):
            return None
        if filename.endswith('.metadata'):
            base = filename.removesuffix('.metadata')
            return Route(Op.METADATA, project, base) if simple.valid_filename(base) else None
        return Route(Op.FILE, project, filename) if simple.valid_filename(filename) else None
    if head == 'pypi' and len(rest) in (2, 3) and rest[-1] == 'json' and simple.valid_project(rest[0]):
        version = rest[1] if len(rest) == 3 else None
        if version is not None and not simple.valid_filename(version):
            return None
        return Route(Op.LEGACY_JSON, rest[0], version=version)
    return None


def pypi_error(status: int, message: str, *, headers: dict[str, str] | None = None) -> Response:
    """PyPI clients show the plain-text body of an error (twine, pip, uv)."""
    return native_error(status, message, headers=headers)


def file_path(project: str, filename: str, *, metadata: bool = False) -> str:
    return f'packages/{project}/{filename}{".metadata" if metadata else ""}'


def page_key(project: str, *, as_json: bool) -> str:
    return f'simple/{project}/index.{"json" if as_json else "html"}'


def index_key(*, as_json: bool) -> str:
    return f'simple/index.{"json" if as_json else "html"}'


def media_type_of(chosen: str) -> str:
    """The Content-Type of a negotiated page (``text/html`` with its charset)."""
    return simple.HTML_TYPE if chosen == simple.TEXT_HTML else chosen


def body_etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def generated(req: FormatRequest, body: bytes, chosen: str, *, account: Callable[[int, int], None]) -> Response:
    """A page rendered per request (hosted, group): ``ETag``, conditional ``304``, ``HEAD``. ``account(status,
    bytes sent)`` records the request."""
    etag = body_etag(body)
    headers = {'ETag': etag, 'Vary': 'Accept', 'Cache-Control': 'no-cache', **SAFE_HEADERS}
    content_type = media_type_of(chosen)
    match = req.request.headers.get('if-none-match')
    if match and etag in {m.strip() for m in match.split(',')}:
        account(304, 0)
        return Response(status_code=304, headers=headers)
    if req.is_head:
        account(200, 0)
        headers['Content-Length'] = str(len(body))
        return Response(status_code=200, headers=headers, media_type=content_type)
    account(200, len(body))
    return Response(body, status_code=200, headers=headers, media_type=content_type)


def page_from_assets(docs: list[dict[str, Any]], project: str) -> simple.Page:
    """A hosted project page from its ``packages/<project>/`` assets (``pypi_db.project_files``)."""
    sidecars = {str(d['path']).removesuffix('.metadata'): d for d in docs if str(d['path']).endswith('.metadata')}
    page = simple.Page(name=project)
    for doc in docs:
        path = str(doc['path'])
        if path.endswith('.metadata'):
            continue
        details: dict[str, Any] = doc.get('pypi') or {}
        filename = path.rsplit('/', 1)[-1]
        sidecar = sidecars.get(path)
        yanked: bool | str = False
        if details.get('yanked'):
            yanked = str(details.get('yanked_reason') or '') or True
        created = doc.get('created_at')
        page.files.append(
            simple.DistFile(
                filename=filename,
                hashes={'sha256': str(doc['blob_sha256'])},
                requires_python=details.get('requires_python'),
                yanked=yanked,
                core_metadata={'sha256': str(sidecar['blob_sha256'])} if sidecar else False,
                size=int(doc['size']),
                upload_time=iso_time(created) if isinstance(created, datetime) else None,
            )
        )
        if details.get('name') and page.name == project:
            page.name = str(details['name'])
    return page


def iso_time(value: datetime) -> str:
    """PEP 700 ``upload-time``: ISO 8601 UTC with a ``Z``."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def latest_of(versions: list[str]) -> str | None:
    """The newest final release (PEP 440), else the newest version."""
    finals = [v for v in versions if not pep440_is_prerelease(v)]
    pool = finals or versions
    return max(pool, key=pep440_key) if pool else None


def summary_fields(display_name: str, versions: list[str]) -> dict[str, Any]:
    """The ``pypi_packages`` fields a project page provides."""
    return {
        'display_name': display_name[: simple.MAX_PROJECT_LENGTH],
        'latest': latest_of(versions),
        'version_count': len(versions),
    }
