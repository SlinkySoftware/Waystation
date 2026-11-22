"""The Module Registry Protocol (T-P5-TFM; FORMATS.md "Terraform / OpenTofu"): pure functions on bytes, validated and
size-bounded; callers run the archive readers in a thread.

- Versions (``<modules.v1><namespace>/<name>/<system>/versions``): ``{"modules": [{"versions": [{"version"}]}]}``.
  Entries with an invalid version are skipped.
- Download (``<modules.v1><namespace>/<name>/<system>/<version>/download``): ``204`` with ``X-Terraform-Get`` (the
  Terraform registry), or ``200`` with ``{"location": ...}`` (the OpenTofu registry). The location is a go-getter
  source address; a relative one (``/``, ``./``, ``../``) is relative to the download URL.
- **Archive sources** (``archive_source``): a location Waystation can cache as one archive file. A GitHub repository
  at a ref (``git::https://github.com/<owner>/<repo>[.git][//<dir>]?ref=<ref>``, also the ``github.com/...``
  shorthand) is fetched as GitHub's tag archive (``https://github.com/<owner>/<repo>/archive/<ref>.tar.gz``, which
  redirects to its download host); an HTTP(S) archive (``.zip``, ``.tar.gz``/``.tgz`` or ``?archive=``, also
  ``s3::https://...``) is fetched as it is. Only hosts in the repository's ``archive_hosts`` qualify. Everything
  else (other git hosts, ssh, no ref, private buckets...) is passed to the client unchanged.
- **Hosted archives** (``check_module_archive``): a ``.tar.gz`` whose root is the module (at least one ``.tf``,
  ``.tf.json``, ``.tofu`` or ``.tofu.json`` file), regular files and directories only, safe relative names, bounded
  member count and size.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import SplitResult, parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from waystation_common.archives import READ_ERRORS, BoundedTarInfo
from waystation_common.errors import UpstreamError, ValidationFailed
from waystation_server.formats.terraform.paths import is_version
from waystation_server.formats.terraform.registry import newest_first

MAX_MODULE_VERSIONS = 8 * 1024 * 1024
MAX_MODULE_VERSION_COUNT = 10_000
MAX_DOWNLOAD_DOC = 64 * 1024
MAX_LOCATION = 4096
MAX_ARCHIVE = 64 * 1024 * 1024  # a cached (proxy) module archive: buffered while it is fetched
MAX_UPLOAD = 64 * 1024 * 1024  # an uploaded (hosted) module archive
MAX_MEMBERS = 10_000
MAX_UNPACKED = 512 * 1024 * 1024
MAX_NAME = 1024
MODULE_FILES = ('.tf', '.tf.json', '.tofu', '.tofu.json')
_FORCED = re.compile(r'^([a-z0-9]{1,16})::(.+)$', re.DOTALL)
_REF = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$')
_GITHUB_PART = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
_GIT_PARAMS = frozenset({'ref', 'depth'})


# ---------------------------------------------------------------------------- registry documents
def parse_module_versions(data: bytes) -> list[str]:
    """A module registry's version list: the valid versions, newest first (SemVer)."""
    if len(data) > MAX_MODULE_VERSIONS:
        raise UpstreamError('module version list is too large')
    try:
        doc = json.loads(data)
    except ValueError:
        raise UpstreamError('module version list is not JSON') from None
    modules = doc.get('modules') if isinstance(doc, dict) else None
    if not isinstance(modules, list):
        raise UpstreamError('module version list has no modules')
    found: set[str] = set()
    for module in modules[:1]:
        entries = module.get('versions') if isinstance(module, dict) else None
        if not isinstance(entries, list):
            raise UpstreamError('module version list has no versions')
        if len(entries) > MAX_MODULE_VERSION_COUNT:
            raise UpstreamError('module version list is too long')
        for entry in entries:
            version = entry.get('version') if isinstance(entry, dict) else None
            if isinstance(version, str) and is_version(version):
                found.add(version)
    return newest_first(list(found))


def render_module_versions(versions: list[str]) -> bytes:
    """The ``versions`` answer Waystation serves (newest first)."""
    body = {'modules': [{'versions': [{'version': v} for v in newest_first(versions)]}]}
    return json.dumps(body, separators=(',', ':')).encode()


def parse_download(data: bytes, headers: Mapping[str, str], download_url: str) -> str:
    """The location (a go-getter source address) of a module registry's download answer: ``X-Terraform-Get``, else
    the JSON body's ``location``; relative locations resolved against ``download_url``."""
    location = headers.get('x-terraform-get') or ''
    if not location and data:
        if len(data) > MAX_DOWNLOAD_DOC:
            raise UpstreamError('module download answer is too large')
        try:
            doc = json.loads(data)
        except ValueError:
            raise UpstreamError('module download answer is not JSON') from None
        value = doc.get('location') if isinstance(doc, dict) else None
        location = value if isinstance(value, str) else ''
    location = location.strip()
    if not location or len(location) > MAX_LOCATION or any(ord(c) < 0x20 for c in location):
        raise UpstreamError('module download answer has no valid location')
    if location.startswith(('/', './', '../')):
        location = urljoin(download_url, location)
    return location


# ---------------------------------------------------------------------------- archive sources
@dataclass(frozen=True)
class ArchiveSource:
    url: str  # what Waystation fetches (absolute http(s))
    filename: str  # archive.tar.gz / archive.zip
    subdir: str  # the module's directory inside the archive ('' = its root), below the top directory if any
    strip_top: bool  # the archive has one top directory to add to ``subdir`` (GitHub archives)


def split_subdir(source: str) -> tuple[str, str]:
    """go-getter's ``SourceDirSubdir``: ``<url>//<subdir>?<query>`` -> (``<url>?<query>``, ``<subdir>``)."""
    stop = source.find('?')
    stop = len(source) if stop < 0 else stop
    scheme = source.find('://', 0, stop)
    offset = scheme + 3 if scheme >= 0 else 0
    idx = source.find('//', offset, stop)
    if idx < 0:
        return source, ''
    src, subdir = source[:idx], source[idx + 2 :]
    query_at = subdir.find('?')
    if query_at >= 0:
        src += subdir[query_at:]
        subdir = subdir[:query_at]
    return src, subdir


def _safe_subdir(subdir: str) -> str | None:
    parts = [p for p in subdir.strip('/').split('/') if p not in ('', '.')]
    if any(p == '..' or '\\' in p or '*' in p for p in parts):
        return None
    return '/'.join(parts)


def _host(parts: SplitResult) -> str:
    port = parts.port
    return (parts.hostname or '').lower() + (f':{port}' if port and port not in (80, 443) else '')


def archive_source(location: str, allowed_hosts: frozenset[str]) -> ArchiveSource | None:
    """The archive Waystation caches for a module source, or None to pass the source through unchanged."""
    forced = None
    match = _FORCED.match(location)
    rest = location
    if match is not None:
        forced, rest = match.group(1), match.group(2)
    if forced not in (None, 'git', 'http', 'https', 's3'):
        return None
    src, raw_subdir = split_subdir(rest)
    subdir = _safe_subdir(raw_subdir)
    if subdir is None:
        return None
    if src.startswith('github.com/') and forced in (None, 'git'):
        src, forced = 'https://' + src, 'git'
    try:
        parts = urlsplit(src)
        host = _host(parts) if parts.hostname else ''
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') or not host or parts.username or parts.password or parts.fragment:
        return None
    if host not in allowed_hosts:
        return None
    query = parse_qsl(parts.query, keep_blank_values=True)
    if forced == 'git':
        return _github(parts, host, query, subdir)
    params = dict(query)
    kind = params.pop('archive', None)
    path = parts.path.lower()
    if kind is None:
        kind = 'zip' if path.endswith('.zip') else 'tar.gz' if path.endswith(('.tar.gz', '.tgz')) else None
    if kind not in ('zip', 'tar.gz', 'tgz'):
        return None
    url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode([(k, v) for k, v in query if k != 'archive']), '')
    )
    return ArchiveSource(
        url=url, filename='archive.zip' if kind == 'zip' else 'archive.tar.gz', subdir=subdir, strip_top=False
    )


def _github(parts: SplitResult, host: str, query: list[tuple[str, str]], subdir: str) -> ArchiveSource | None:
    if host != 'github.com' or parts.scheme != 'https':
        return None
    names = parts.path.strip('/').split('/')
    if len(names) != 2:
        return None
    owner, repo = names[0], names[1].removesuffix('.git')
    if not _GITHUB_PART.match(owner) or not _GITHUB_PART.match(repo):
        return None
    params = dict(query)
    ref = params.get('ref', '')
    if set(params) - _GIT_PARAMS or not _REF.match(ref):
        return None  # no ref (a moving branch), or options only git understands
    url = f'https://github.com/{owner}/{repo}/archive/{quote(ref, safe="")}.tar.gz'
    return ArchiveSource(url=url, filename='archive.tar.gz', subdir=subdir, strip_top=True)


def client_location(archive_url: str, source: ArchiveSource, top: str | None) -> str:
    """``X-Terraform-Get`` for a cached archive: its URL (relative to the download URL, maybe with a signed download
    grant in its query) and the module's directory inside it (go-getter's ``//`` syntax, before the query)."""
    subdir = '/'.join(p for p in (top or '', source.subdir) if p)
    if not subdir:
        return archive_url
    url, sep, query = archive_url.partition('?')
    return f'{url}//{subdir}{sep}{query}'


def archive_top(file: BinaryIO, filename: str) -> str | None:
    """The single top directory of an archive (GitHub's ``<repo>-<ref>/``), from its first member. Blocking."""
    try:
        if filename.endswith('.zip'):
            with zipfile.ZipFile(file) as archive:
                names = archive.namelist()[:1]
        else:
            with tarfile.open(fileobj=file, mode='r:gz', tarinfo=BoundedTarInfo) as archive:
                names = []
                for member in archive:
                    names = [member.name]
                    break
    except READ_ERRORS as exc:
        raise UpstreamError(f'the module archive is not readable ({type(exc).__name__})') from None
    if not names:
        raise UpstreamError('the module archive is empty')
    top = names[0].lstrip('./').split('/', 1)[0]
    if not top or top == '..' or len(top) > 255:
        raise UpstreamError('the module archive has no top directory')
    return top


def check_fetched_archive(data: bytes, filename: str) -> None:
    """A fetched (proxy) archive must at least be a readable archive. Blocking."""
    archive_top(io.BytesIO(data), filename)


# ---------------------------------------------------------------------------- hosted archives
@dataclass(frozen=True)
class ModuleArchive:
    files: int
    unpacked: int
    root_files: tuple[str, ...]  # the module's own configuration files (at the archive root)


def _member_name(name: str) -> str:
    clean = name
    while clean.startswith('./'):
        clean = clean[2:]
    clean = clean.rstrip('/')
    if (
        not clean
        or len(clean) > MAX_NAME
        or clean.startswith('/')
        or '\x00' in clean
        or '\\' in clean
        or any(part in ('', '.', '..') for part in clean.split('/'))
    ):
        raise ValidationFailed(f'the module archive has an unsafe file name: {name[:200]!r}')
    return clean


def check_module_archive(path: Path) -> ModuleArchive:
    """Validate an uploaded module archive (``.tar.gz`` whose root is the module) without extracting it. Raises
    ``ValidationFailed``. Blocking: run in a thread."""
    files = unpacked = members = 0
    root: list[str] = []
    try:
        with path.open('rb') as fh, tarfile.open(fileobj=fh, mode='r:gz', tarinfo=BoundedTarInfo) as archive:
            for member in archive:
                members += 1
                if members > MAX_MEMBERS:
                    raise ValidationFailed(f'the module archive has more than {MAX_MEMBERS} entries')
                if member.name in ('.', './'):
                    continue
                name = _member_name(member.name)
                if member.isdir():
                    continue
                if not member.isreg():
                    raise ValidationFailed(f'the module archive may hold only files and directories: {name[:200]}')
                files += 1
                unpacked += member.size
                if unpacked > MAX_UNPACKED:
                    raise ValidationFailed('the module archive unpacks to more than 512 MiB')
                if '/' not in name:
                    root.append(name)
    except ValidationFailed:
        raise
    except READ_ERRORS as exc:
        raise ValidationFailed(f'not a gzip-compressed tar archive ({type(exc).__name__})') from None
    configs = tuple(sorted(n for n in root if n.endswith(MODULE_FILES)))
    if not configs:
        raise ValidationFailed(
            "the module archive has no .tf file at its root: archive the module directory's contents "
            '(tar -czf module.tar.gz -C <dir> .)'
        )
    return ModuleArchive(files=files, unpacked=unpacked, root_files=configs[:50])
