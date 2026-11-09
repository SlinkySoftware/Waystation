"""Raw file serving rules shared by proxy, hosted and group repositories (FORMATS.md "Raw"): asset kind, the
Content-Type decision, ``Content-Disposition`` and the headers every file answer carries."""

from __future__ import annotations

import mimetypes
import re
from functools import lru_cache
from urllib.parse import quote

from starlette.responses import Response

from waystation_server.formats.raw.descriptor import DESCRIPTOR, RawOptions
from waystation_server.proxy.engine import AssetKind
from waystation_server.repos.model import RepoConfig

DEFAULT_TYPE = 'application/octet-stream'
# Characters left unescaped in the upstream URL path. ':' is escaped so a first segment like ``http:`` can never be
# read as a scheme by URL joining; '?', '#' and '%' are escaped so a file name is never a query, fragment or escape.
URL_SAFE = "/-._~!$&'()*+,;=@"
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


def finish(response: Response, path: str, options: RawOptions) -> Response:
    """The headers of a raw answer: ``Content-Disposition`` on files served whole, ``nosniff`` on everything."""
    if response.status_code == 200:
        response.headers['Content-Disposition'] = content_disposition(path, options.content_disposition)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response
