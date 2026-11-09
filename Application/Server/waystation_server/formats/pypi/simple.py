"""PyPI simple repository API helpers (FORMATS.md "PyPI"): names, file names, content negotiation, and parsing and
rendering of project pages in both representations.

- PEP 503: HTML pages, normalised project names (``[-_.]+`` → ``-``, lower case), ``#<hash>=<hex>`` fragments,
  ``data-requires-python``, ``data-yanked`` (PEP 592), ``data-core-metadata`` / ``data-dist-info-metadata``
  (PEP 658/714).
- PEP 691: the JSON representation (``application/vnd.pypi.simple.v1+json``) and ``Accept`` negotiation; PEP 700:
  ``versions``, ``size``, ``upload-time`` (API version 1.1).

A page is parsed from either representation into a ``Page`` and rendered as either one, with every file URL
pointing into this repository (``../../packages/<project>/<file>``, relative to ``simple/<project>/``, so the same
bytes work for any repository and group). The JSON rendering kept in the cache also records each file's upstream URL
under the private key ``_ws_url`` (PEP 691 reserves keys with a leading underscore for the index server); clients
ignore it, the proxy uses it to fetch the file.

Pure functions, no I/O. Parsing and rendering of large pages is CPU work: callers run them in a thread.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit

from waystation_common.cleanup.versions import pep440_key
from waystation_common.errors import UpstreamError
from waystation_server.proxy.engine import Expected

DistKind = Literal['wheel', 'sdist', 'other']
SIMPLE_JSON = 'application/vnd.pypi.simple.v1+json'
SIMPLE_HTML = 'application/vnd.pypi.simple.v1+html'
TEXT_HTML = 'text/html'
HTML_TYPE = 'text/html; charset=utf-8'
LEGACY_JSON = 'application/json'
# What the proxy asks the upstream for: JSON preferred (more information: sizes, upload times), HTML accepted.
UPSTREAM_ACCEPT = f'{SIMPLE_JSON}, {SIMPLE_HTML};q=0.2, {TEXT_HTML};q=0.01'

MAX_PROJECT_LENGTH = 200
MAX_FILES = 250_000
MAX_PROJECTS = 2_000_000
MAX_TEXT = 1024
URL_KEY = '_ws_url'

_PROJECT = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$')
_FILENAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+!~-]*$')
_SEPARATORS = re.compile(r'[-_.]+')
_HEX_LENGTH = {'md5': 32, 'sha1': 40, 'sha224': 56, 'sha256': 64, 'sha384': 96, 'sha512': 128, 'blake2b_256': 64}
_HEX = re.compile(r'^[0-9a-f]+$')
SDIST_EXTENSIONS = ('.tar.gz', '.zip', '.tar.bz2', '.tar.xz', '.tgz', '.tar')
DIST_EXTENSIONS = ('.whl', '.egg', '.exe', '.msi', '.rpm', '.dmg', *SDIST_EXTENSIONS)


# ---------------------------------------------------------------------------- names
def normalise(name: str) -> str:
    """PEP 503 normalised project name."""
    return _SEPARATORS.sub('-', name).lower()


def valid_project(name: str) -> bool:
    return len(name) <= MAX_PROJECT_LENGTH and bool(_PROJECT.match(name))


def valid_filename(filename: str) -> bool:
    return 0 < len(filename.encode()) <= 255 and bool(_FILENAME.match(filename)) and '..' not in filename


@dataclass(frozen=True)
class DistName:
    project: str  # normalised
    version: str
    kind: DistKind


def parse_filename(filename: str) -> DistName | None:
    """Project (normalised) and version from a distribution file name: wheels (PEP 427/491), sdists, eggs."""
    if filename.endswith('.whl'):
        parts = filename[:-4].split('-')
        if len(parts) not in (5, 6) or not parts[0] or not parts[1]:
            return None
        return DistName(normalise(parts[0]), parts[1], 'wheel')
    if filename.endswith('.egg'):
        parts = filename[:-4].split('-')
        return DistName(normalise(parts[0]), parts[1], 'other') if len(parts) >= 2 and parts[1] else None
    for ext in SDIST_EXTENSIONS:
        if filename.endswith(ext):
            name, _, version = filename[: -len(ext)].rpartition('-')
            return DistName(normalise(name), version, 'sdist') if name and version else None
    return None


# ---------------------------------------------------------------------------- content negotiation
_CANDIDATES = (SIMPLE_JSON, SIMPLE_HTML, TEXT_HTML)
_ALIASES = {
    'application/vnd.pypi.simple.latest+json': SIMPLE_JSON,
    'application/vnd.pypi.simple.latest+html': SIMPLE_HTML,
}


def negotiate(accept: str | None) -> str | None:
    """The representation to answer with (PEP 691): the highest ``q``; between equals an explicitly named type
    beats a wildcard, then JSON, the versioned HTML type, ``text/html``. A missing ``Accept`` or only wildcards
    answer ``text/html`` (PEP 503 clients). None when the client accepts none of them (406)."""
    if not accept or not accept.strip():
        return TEXT_HTML
    best: dict[str, tuple[float, int]] = {}
    for part in accept.split(',')[:64]:
        media, *params = (p.strip() for p in part.split(';'))
        media = _ALIASES.get(media.lower(), media.lower())
        q = 1.0
        for param in params:
            key, _, value = param.partition('=')
            if key.strip().lower() == 'q':
                try:
                    q = max(0.0, min(1.0, float(value)))
                except ValueError:
                    q = 0.0
        for candidate in _CANDIDATES:
            if media == candidate:
                specificity = 2
            elif media in ('*/*', candidate.split('/')[0] + '/*'):
                specificity = 1
            else:
                continue
            current = best.get(candidate)
            if current is None or specificity > current[1]:
                best[candidate] = (q, specificity)
    if not best:
        return None

    def rank(candidate: str) -> tuple[float, int, int]:
        q, specificity = best[candidate]
        if specificity == 1:  # wildcards: plain HTML first
            preference = {TEXT_HTML: 3, SIMPLE_HTML: 2, SIMPLE_JSON: 1}[candidate]
        else:
            preference = {SIMPLE_JSON: 3, SIMPLE_HTML: 2, TEXT_HTML: 1}[candidate]
        return (q, specificity, preference)

    chosen = max(best, key=rank)
    return chosen if best[chosen][0] > 0 else None


def is_json(media_type: str) -> bool:
    return media_type == SIMPLE_JSON


# ---------------------------------------------------------------------------- the page model
@dataclass
class DistFile:
    filename: str
    url: str = ''  # absolute upstream URL ('' for hosted files)
    hashes: dict[str, str] = field(default_factory=dict)
    requires_python: str | None = None
    yanked: bool | str = False  # True, or the reason
    core_metadata: bool | dict[str, str] = False  # PEP 658: True, or the hashes of the .metadata file
    size: int | None = None
    upload_time: str | None = None

    def expected(self) -> Expected | None:
        return expected_from(self.hashes, self.size)

    def metadata_expected(self) -> Expected | None:
        return expected_from(self.core_metadata, None) if isinstance(self.core_metadata, dict) else None

    @property
    def has_metadata(self) -> bool:
        return bool(self.core_metadata)


@dataclass
class Page:
    name: str
    files: list[DistFile] = field(default_factory=list)
    versions: list[str] | None = None


def expected_from(hashes: dict[str, str], size: int | None) -> Expected | None:
    known = {alg: hashes[alg] for alg in ('sha256', 'sha512', 'sha1', 'md5') if alg in hashes}
    if not known and size is None:
        return None
    return Expected(size=size, **known)


def _hashes(raw: Any) -> dict[str, str]:
    """Validated hash dictionary: known algorithms with well-formed hex; anything malformed refuses the page."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for alg, value in list(raw.items())[:16]:
        if not isinstance(alg, str) or not isinstance(value, str):
            continue
        alg, value = alg.lower(), value.strip().lower()
        length = _HEX_LENGTH.get(alg)
        if length is None:
            continue
        if len(value) != length or not _HEX.match(value):
            raise UpstreamError(f'upstream page has an invalid {alg} hash')
        out[alg] = value
    return out


def _metadata_flag(raw: Any) -> bool | dict[str, str]:
    if isinstance(raw, dict):
        return _hashes(raw) or True
    return raw is True


def _text(value: Any) -> str | None:
    return value[:MAX_TEXT] if isinstance(value, str) and value else None


def _file_url(href: str, page_url: str) -> tuple[str, dict[str, str]]:
    """Absolute URL without fragment, and the hash of a ``#<alg>=<hex>`` fragment."""
    absolute, fragment = urldefrag(urljoin(page_url, href.strip()))
    if urlsplit(absolute).scheme not in ('http', 'https'):
        raise UpstreamError('upstream page links a file with an unsupported URL')
    hashes: dict[str, str] = {}
    alg, sep, value = fragment.partition('=')
    if sep:
        hashes = _hashes({alg: value})
    return absolute, hashes


def _filename_of(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit('/', 1)[-1])


def _add(page: Page, entry: DistFile, seen: set[str]) -> None:
    if not valid_filename(entry.filename) or entry.filename in seen:
        return
    if len(page.files) >= MAX_FILES:
        raise UpstreamError('upstream page lists too many files')
    seen.add(entry.filename)
    page.files.append(entry)


def parse_json(data: bytes, page_url: str, *, internal: bool = False) -> Page:
    """A PEP 691 project page. ``internal``: a page this server rendered (upstream URLs under ``_ws_url``)."""
    try:
        doc = json.loads(data)
    except ValueError:
        raise UpstreamError('upstream page is not valid JSON') from None
    if not isinstance(doc, dict) or not isinstance(doc.get('files', []), list):
        raise UpstreamError('upstream page is not a PEP 691 project page')
    page = Page(name=str(doc.get('name') or '')[:MAX_PROJECT_LENGTH])
    seen: set[str] = set()
    for raw in doc.get('files', []):
        if not isinstance(raw, dict) or not isinstance(raw.get('filename'), str):
            continue
        hashes = _hashes(raw.get('hashes'))
        if internal:
            url = str(raw.get(URL_KEY) or '')
        elif isinstance(raw.get('url'), str):
            url, fragment_hashes = _file_url(raw['url'], page_url)
            hashes = hashes or fragment_hashes
        else:
            continue
        yanked = raw.get('yanked', False)
        size = raw.get('size')
        metadata = raw.get('core-metadata', raw.get('data-dist-info-metadata', raw.get('dist-info-metadata', False)))
        entry = DistFile(
            filename=raw['filename'],
            url=url,
            hashes=hashes,
            requires_python=_text(raw.get('requires-python')),
            yanked=(_text(yanked) or True) if isinstance(yanked, str) else yanked is True,
            core_metadata=_metadata_flag(metadata),
            size=size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
            upload_time=_text(raw.get('upload-time')),
        )
        _add(page, entry, seen)
    versions = doc.get('versions')
    if isinstance(versions, list):
        page.versions = [v[:MAX_TEXT] for v in versions[:MAX_FILES] if isinstance(v, str)]
    return page


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[dict[str, str | None], str]] = []
        self._current: dict[str, str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'a':
            if len(self.anchors) >= MAX_FILES:
                raise UpstreamError('upstream page lists too many files')
            self._current, self._text = dict(attrs), []

    def handle_data(self, data: str) -> None:
        if self._current is not None and sum(len(t) for t in self._text) < MAX_TEXT:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == 'a' and self._current is not None:
            self.anchors.append((self._current, ''.join(self._text).strip()))
            self._current = None


def _anchors(data: bytes) -> list[tuple[dict[str, str | None], str]]:
    parser = _Anchors()
    parser.feed(data.decode('utf-8', errors='replace'))
    parser.close()
    return parser.anchors


def _html_metadata(value: str | None) -> bool | dict[str, str]:
    if value is None:
        return False
    alg, sep, digest = value.partition('=')
    if sep:
        return _hashes({alg: digest}) or True
    return value.strip().lower() != 'false'


def parse_html(data: bytes, page_url: str, name: str = '') -> Page:
    """A PEP 503 project page (every ``<a>`` is a file)."""
    page = Page(name=name)
    seen: set[str] = set()
    for attrs, text in _anchors(data):
        href = attrs.get('href')
        if not href:
            continue
        url, hashes = _file_url(href, page_url)
        filename = _filename_of(url) or text
        yanked = attrs.get('data-yanked')
        metadata = attrs.get('data-core-metadata', attrs.get('data-dist-info-metadata'))
        entry = DistFile(
            filename=filename,
            url=url,
            hashes=hashes,
            requires_python=_text(attrs.get('data-requires-python')),
            yanked=False if yanked is None else (_text(yanked) or True),
            core_metadata=_html_metadata(metadata),
        )
        _add(page, entry, seen)
    return page


def parse_upstream(data: bytes, content_type: str, page_url: str, name: str) -> Page:
    """An upstream project page in whichever representation it answered with."""
    media = content_type.split(';', 1)[0].strip().lower()
    page = parse_json(data, page_url) if media.endswith('json') else parse_html(data, page_url, name)
    page.name = page.name or name
    return page


def versions_of(page: Page) -> list[str]:
    """The page's versions (PEP 700 ``versions``, else from the file names), PEP 440 order."""
    found = set(page.versions or ())
    for entry in page.files:
        dist = parse_filename(entry.filename)
        if dist is not None:
            found.add(dist.version)
    return sorted(found, key=pep440_key)


# ---------------------------------------------------------------------------- rendering
def file_href(project: str, filename: str) -> str:
    """A file's URL relative to ``simple/<project>/``."""
    return f'../../packages/{project}/{quote(filename, safe="")}'


def _sha256_fragment(hashes: dict[str, str]) -> str:
    for alg in ('sha256', 'sha512', 'sha384', 'sha224', 'sha1', 'md5'):
        if alg in hashes:
            return f'#{alg}={hashes[alg]}'
    return ''


def render_json(page: Page, project: str, *, internal: bool = False) -> bytes:
    """PEP 691 (API 1.1 when every file has a size). ``internal`` keeps the upstream URLs (``_ws_url``)."""
    files: list[dict[str, Any]] = []
    for entry in page.files:
        item: dict[str, Any] = {
            'filename': entry.filename,
            'url': file_href(project, entry.filename),
            'hashes': entry.hashes,
        }
        if entry.requires_python:
            item['requires-python'] = entry.requires_python
        item['yanked'] = entry.yanked
        if entry.core_metadata:
            item['core-metadata'] = entry.core_metadata
            item['data-dist-info-metadata'] = entry.core_metadata
        if entry.size is not None:
            item['size'] = entry.size
        if entry.upload_time:
            item['upload-time'] = entry.upload_time
        if internal and entry.url:
            item[URL_KEY] = entry.url
        files.append(item)
    complete = all(entry.size is not None for entry in page.files)
    doc = {
        'meta': {'api-version': '1.1' if complete else '1.0'},
        'name': page.name or project,
        'versions': versions_of(page),
        'files': files,
    }
    return json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode()


def _attr(value: str) -> str:
    return html.escape(value, quote=True)


def render_html(page: Page, project: str) -> bytes:
    """PEP 503 page (with the PEP 629 version meta tag)."""
    name = _attr(page.name or project)
    lines = [
        '<!DOCTYPE html>',
        '<html>',
        '  <head>',
        '    <meta name="pypi:repository-version" content="1.1">',
        f'    <title>Links for {name}</title>',
        '  </head>',
        '  <body>',
        f'    <h1>Links for {name}</h1>',
    ]
    for entry in page.files:
        attrs = [f'href="{_attr(file_href(project, entry.filename) + _sha256_fragment(entry.hashes))}"']
        if entry.requires_python:
            attrs.append(f'data-requires-python="{_attr(entry.requires_python)}"')
        if entry.core_metadata:
            value = 'true'
            if isinstance(entry.core_metadata, dict) and entry.core_metadata:
                fragment = _sha256_fragment(entry.core_metadata)
                value = fragment[1:] if fragment else 'true'
            attrs.append(f'data-dist-info-metadata="{_attr(value)}"')
            attrs.append(f'data-core-metadata="{_attr(value)}"')
        if entry.yanked:
            reason = entry.yanked if isinstance(entry.yanked, str) else ''
            attrs.append(f'data-yanked="{_attr(reason)}"')
        lines.append(f'    <a {" ".join(attrs)}>{_attr(entry.filename)}</a><br />')
    lines += ['  </body>', '</html>', '']
    return '\n'.join(lines).encode()


# ---------------------------------------------------------------------------- the root index
def parse_index(data: bytes, content_type: str) -> list[str]:
    """Project names of an upstream ``/simple/`` index (either representation)."""
    names: list[str] = []
    if content_type.split(';', 1)[0].strip().lower().endswith('json'):
        try:
            doc = json.loads(data)
        except ValueError:
            raise UpstreamError('upstream index is not valid JSON') from None
        projects = doc.get('projects') if isinstance(doc, dict) else None
        for item in projects if isinstance(projects, list) else []:
            if isinstance(item, dict) and isinstance(item.get('name'), str):
                names.append(item['name'])
    else:
        names = [text for _, text in _anchors(data)]
    if len(names) > MAX_PROJECTS:
        raise UpstreamError('upstream index lists too many projects')
    return [n for n in names if valid_project(n)]


def render_index(names: list[str], *, as_json: bool) -> bytes:
    """``/simple/`` in either representation, one entry per normalised name, sorted."""
    unique: dict[str, str] = {}
    for name in names:
        unique.setdefault(normalise(name), name)
    ordered = sorted(unique.items())
    if as_json:
        doc = {'meta': {'api-version': '1.0'}, 'projects': [{'name': name} for _, name in ordered]}
        return json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode()
    lines = [
        '<!DOCTYPE html>',
        '<html>',
        '  <head>',
        '    <meta name="pypi:repository-version" content="1.0">',
        '    <title>Simple index</title>',
        '  </head>',
        '  <body>',
    ]
    lines += [f'    <a href="{_attr(key)}/">{_attr(name)}</a><br />' for key, name in ordered]
    lines += ['  </body>', '</html>', '']
    return '\n'.join(lines).encode()


# ---------------------------------------------------------------------------- legacy JSON API
def rewrite_legacy_json(data: bytes, project: str, repo_url: str) -> bytes:
    """``/pypi/<project>/json`` (and ``/<version>/json``) with every file URL pointing into this repository."""
    try:
        doc = json.loads(data)
    except ValueError:
        raise UpstreamError('upstream JSON is not valid') from None
    if not isinstance(doc, dict):
        raise UpstreamError('upstream JSON is not an object')
    info = doc.get('info')
    if isinstance(info, dict) and isinstance(info.get('name'), str) and normalise(info['name']) != project:
        raise UpstreamError('upstream JSON is for a different project')

    def rewrite(files: Any) -> None:
        for item in files if isinstance(files, list) else []:
            if isinstance(item, dict) and isinstance(item.get('filename'), str) and valid_filename(item['filename']):
                item['url'] = f'{repo_url}packages/{project}/{quote(item["filename"], safe="")}'

    rewrite(doc.get('urls'))
    releases = doc.get('releases')
    if isinstance(releases, dict):
        for files in releases.values():
            rewrite(files)
    return json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode()
