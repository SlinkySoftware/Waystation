"""Raw (generic files) proxy plugin (FORMATS.md "Raw"; SPECIFICATION §4.1).

``GET``/``HEAD /repository/<repo>/<path>`` maps one-to-one onto ``<upstream_url>/<path>`` and is cached by path:

- **Kind**: paths matching the repository's ``metadata_path_regex`` (searched in ``/<path>``, so ``(^|/)latest$``
  also matches a root-level ``latest``) are mutable *metadata*, revalidated after ``metadata_max_age_s``; everything
  else is immutable *content* (kept for ``content_max_age_s``, default forever).
- **Content-Type**: decided by the file extension from Python's built-in table (never the host's mime.types), so a
  wrong upstream type cannot change how a known file is served; otherwise the upstream's type when it is a well-formed
  media type; otherwise ``application/octet-stream``. With ``content_disposition: inline`` the types a browser would
  execute on this origin (HTML, XHTML, SVG, XML) are served as ``text/plain``.
- **Headers**: ``Content-Disposition`` (``attachment`` by default, the repository option) on file answers and
  ``X-Content-Type-Options: nosniff`` on every answer.
- Upstream 404/410 → native 404, remembered in the negative cache for ``negative_ttl_s``; other upstream failures →
  502/504 (the engine). Conditional requests on cached files (``If-None-Match``/``If-Modified-Since``) → 304.
- Not in this slice: directory listings (a path ending in ``/`` or the repository root answers 404), hosted
  ``PUT``/``DELETE`` (405 here) and group repositories.
"""

from __future__ import annotations

import mimetypes
import re
from functools import lru_cache
from urllib.parse import quote

from starlette.responses import Response

from waystation_common.errors import NotFound
from waystation_server.formats.base import FormatRequest
from waystation_server.formats.raw.descriptor import DESCRIPTOR, RawOptions
from waystation_server.problems import native_error
from waystation_server.proxy.engine import AssetKind, AssetSpec
from waystation_server.repos.model import RepoConfig

DEFAULT_TYPE = 'application/octet-stream'
# Characters left unescaped in the upstream URL path. ':' is escaped so a first segment like ``http:`` can never be
# read as a scheme by URL joining; '?', '#' and '%' are escaped so a file name is never a query, fragment or escape.
_URL_SAFE = "/-._~!$&'()*+,;=@"
_MEDIA_TYPE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$')
_ACTIVE_TYPES = frozenset(
    {'text/html', 'application/xhtml+xml', 'image/svg+xml', 'text/xml', 'application/xml', 'text/xsl'}
)
# Built-in extension table only: deterministic across hosts (mimetypes.guess_type would read /etc/mime.types).
_TYPES = mimetypes.MimeTypes(filenames=())
_EXTRA_TYPES = {
    '.deb': 'application/vnd.debian.binary-package',
    '.rpm': 'application/x-rpm',
    '.iso': 'application/x-iso9660-image',
    '.gz': 'application/gzip',
    '.tgz': 'application/gzip',
    '.xz': 'application/x-xz',
    '.zst': 'application/zstd',
    '.bz2': 'application/x-bzip2',
    '.asc': 'text/plain',
    '.sig': 'application/pgp-signature',
    '.sha256': 'text/plain',
    '.sha512': 'text/plain',
    '.md': 'text/markdown',
    '.yaml': 'application/yaml',
    '.yml': 'application/yaml',
    '.sh': 'text/x-shellscript',
}
_COMPRESSED = {'gzip': 'application/gzip', 'bzip2': 'application/x-bzip2', 'xz': 'application/x-xz'}


@lru_cache(maxsize=256)
def _pattern(regex: str) -> re.Pattern[str]:
    return re.compile(regex)


def asset_kind(path: str, options: RawOptions) -> AssetKind:
    """METADATA when the repository's regex matches ``/<path>``, else CONTENT."""
    if options.metadata_path_regex and _pattern(options.metadata_path_regex).search('/' + path):
        return AssetKind.METADATA
    return AssetKind.CONTENT


def type_by_extension(path: str) -> str | None:
    name = path.rsplit('/', 1)[-1].lower()
    dot = name.rfind('.')
    if dot <= 0:
        return None
    ext = name[dot:]
    if ext in _EXTRA_TYPES:
        return _EXTRA_TYPES[ext]
    guessed, encoding = _TYPES.guess_type(name, strict=False)
    if encoding is not None:  # .tar.gz: the file on the wire is the compressed one
        return _COMPRESSED.get(encoding, DEFAULT_TYPE)
    return guessed


def upstream_type(header: str | None) -> str | None:
    """The upstream's Content-Type when it is a well-formed media type (parameters kept only for ``charset``)."""
    if not header:
        return None
    main, _, params = header.partition(';')
    main = main.strip().lower()
    if not _MEDIA_TYPE.match(main):
        return None
    for param in params.split(';'):
        key, _, value = param.partition('=')
        value = value.strip().strip('"')
        if key.strip().lower() == 'charset' and re.fullmatch(r'[A-Za-z0-9._-]{1,40}', value):
            return f'{main}; charset={value}'
    return main


def safe_type(content_type: str, disposition: str) -> str:
    if disposition == 'inline' and content_type.partition(';')[0].strip() in _ACTIVE_TYPES:
        return 'text/plain; charset=utf-8'
    return content_type


def content_disposition(path: str, disposition: str) -> str:
    name = path.rsplit('/', 1)[-1]
    ascii_name = ''.join(c if 0x20 <= ord(c) < 0x7F and c not in '"\\' else '_' for c in name)
    value = f'{disposition}; filename="{ascii_name}"'
    if ascii_name != name:
        value += f"; filename*=UTF-8''{quote(name, safe='')}"
    return value


def options_of(repo: RepoConfig) -> RawOptions:
    options = DESCRIPTOR.parse_options(repo.format_options)
    assert isinstance(options, RawOptions)
    return options


class RawPlugin:
    descriptor = DESCRIPTOR

    async def handle(self, req: FormatRequest) -> Response:
        if req.method not in ('GET', 'HEAD'):
            return native_error(405, 'Method Not Allowed', headers={'Allow': 'GET, HEAD'})
        if not req.path or req.request.url.path.endswith('/'):
            raise NotFound('Not Found')  # directory listings are a later task
        options = options_of(req.repo)
        disposition = options.content_disposition
        by_extension = type_by_extension(req.path)
        spec = AssetSpec(
            path=req.path,
            kind=asset_kind(req.path, options),
            upstream_path=quote(req.path, safe=_URL_SAFE),
            content_type=safe_type(by_extension, disposition) if by_extension else None,
        )
        response = await req.proxy.serve(req, spec)
        if by_extension is None and 'content-type' in response.headers:
            # No known extension: the engine kept the upstream's type; serve only a sanitised form of it.
            served = safe_type(upstream_type(response.headers['content-type']) or DEFAULT_TYPE, disposition)
            response.headers['Content-Type'] = served
            if 'x-ws-content-type' in response.headers:
                response.headers['X-Ws-Content-Type'] = served
        if response.status_code == 200:
            response.headers['Content-Disposition'] = content_disposition(req.path, disposition)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
