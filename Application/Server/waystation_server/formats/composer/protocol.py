"""Composer v2 repository routes, documents and URL rewriting (FORMATS.md "Composer"). Pure functions, no I/O.

Repository layout (``/repository/<repo>/``):

- ``packages.json``: the repository root (rendered per request): ``metadata-url`` = ``<repo>/p2/%package%.json``
  (Composer 2 only: no ``providers``), hosted repositories also list their ``available-packages``;
- ``p2/<vendor>/<name>.json`` (tagged versions) and ``p2/<vendor>/<name>~dev.json`` (branches): the package's
  versions, minified (``composer/2.0``), lower-case name;
- ``dists/<vendor>/<name>/<version>/<file>.zip`` (or ``.tar``): an archive. ``<version>`` is the normalised version
  (``composer.path_segment``); ``<file>`` is the archive's source reference (proxy) or ``<name>-<version>`` (hosted);
- ``packages/upload/<vendor>/<name>/<version>``: hosted ``PUT`` (upload a zip) and ``DELETE``.

Private asset key (never a route): ``_upstream/packages.json`` (where the upstream's ``p2`` files are).

A proxy's ``dist`` entries are rewritten to ``dists/...`` paths of the repository when the archive is a ``zip`` or
``tar`` on an allowed host (``dist_hosts`` plus the upstream's host); the upstream URL is kept in the entry as
``ws-upstream-url`` (Composer ignores unknown keys) so the stored document alone says where an archive comes from.
Other ``dist`` entries are left as they are (the client fetches them itself).
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin, urlsplit

from waystation_common import composer
from waystation_common.errors import UpstreamError

ROOT = 'packages.json'
P2 = 'p2'
DISTS = 'dists'
UPLOAD = 'packages/upload'
UPSTREAM_KEY = '_upstream/packages.json'
JSON_TYPE = 'application/json'
ARCHIVE_TYPES = {'zip': 'application/zip', 'tar': 'application/x-tar'}
UPSTREAM_URL_KEY = 'ws-upstream-url'
MAX_DOCUMENT = 64 * 1024 * 1024
MAX_VERSIONS = 20_000
MAX_AVAILABLE = 100_000
MAX_URL = 2048
PLACEHOLDER = '%package%'
_FILE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.(zip|tar)$')
_REFERENCE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
_PATTERN = re.compile(r'^[a-z0-9*][a-z0-9*_.-]{0,99}/[a-z0-9*][a-z0-9*_.-]{0,99}$')
_SHA1 = re.compile(r'^[0-9a-f]{40}$')
_GITHUB_ZIPBALL = re.compile(
    r'^/repos/([A-Za-z0-9][A-Za-z0-9._-]{0,99})/([A-Za-z0-9._-]{1,100})/zipball/([^/?#]{1,128})$'
)
# Keys of an upstream entry kept as they are; everything else is dropped (bounded, nothing executable for the client:
# ``scripts`` of dependencies never run anyway, but a proxy has no reason to pass them on).
ENTRY_KEYS = frozenset(
    {
        'name',
        'description',
        'keywords',
        'homepage',
        'version',
        'version_normalized',
        'license',
        'authors',
        'source',
        'dist',
        'type',
        'time',
        'support',
        'funding',
        'autoload',
        'autoload-dev',
        'require',
        'require-dev',
        'conflict',
        'replace',
        'provide',
        'suggest',
        'extra',
        'bin',
        'include-path',
        'target-dir',
        'default-branch',
        'abandoned',
        'notification-url',
        'archive',
        'php-ext',
    }
)


class Op(StrEnum):
    ROOT = 'root'
    P2 = 'p2'
    DIST = 'dist'
    UPLOAD = 'upload'


@dataclass(frozen=True)
class Route:
    op: Op
    name: str = ''  # vendor/name, lower case
    branches: bool = False  # p2: the ~dev document (branch versions)
    version: str = ''  # dist: the version path segment; upload: the version as given
    file: str = ''  # dist: the archive file name

    @property
    def key(self) -> str:
        match self.op:
            case Op.ROOT:
                return ROOT
            case Op.P2:
                return p2_path(self.name, dev=self.branches)
            case Op.DIST:
                return dist_path(self.name, self.version, self.file)
            case Op.UPLOAD:
                return f'{UPLOAD}/{self.name}/{self.version}'


def p2_path(name: str, *, dev: bool) -> str:
    return f'{P2}/{name}{"~dev" if dev else ""}.json'


def dist_path(name: str, segment: str, file: str) -> str:
    return f'{DISTS}/{name}/{segment}/{file}'


def hosted_file(name: str, segment: str) -> str:
    return f'{name.split("/", 1)[1]}-{segment}.zip'


def parse_route(path: str) -> Route | None:
    """Map a normalised repository path to a Composer operation (None: not a Composer path)."""
    if path == ROOT:
        return Route(Op.ROOT)
    parts = path.split('/')
    if parts[0] == P2 and len(parts) == 3 and parts[2].endswith('.json'):
        stem = parts[2][: -len('.json')]
        dev = stem.endswith('~dev')
        name = composer.normalise_name(f'{parts[1]}/{stem.removesuffix("~dev")}')
        return Route(Op.P2, name, branches=dev) if name else None
    if parts[0] == DISTS and len(parts) == 5:
        name = composer.normalise_name(f'{parts[1]}/{parts[2]}')
        if name is None or composer.path_segment(parts[3]) != parts[3] or not _FILE.match(parts[4]):
            return None
        return Route(Op.DIST, name, version=parts[3], file=parts[4])
    if path.startswith(UPLOAD + '/') and len(parts) == 5:
        name = composer.normalise_name(f'{parts[2]}/{parts[3]}')
        return Route(Op.UPLOAD, name, version=parts[4]) if name else None
    return None


def load(data: bytes, what: str) -> Any:
    try:
        return json.loads(data)
    except ValueError:
        raise UpstreamError(f'{what} is not JSON') from None


def dump(doc: Any) -> bytes:
    return json.dumps(doc, separators=(',', ':'), ensure_ascii=False).encode()


# ---------------------------------------------------------------------------- packages.json
def render_root(repo_url: str, *, available: Sequence[str] | None = None, patterns: Sequence[str] = ()) -> bytes:
    doc: dict[str, Any] = {'packages': [], 'metadata-url': f'{repo_url}{P2}/{PLACEHOLDER}.json'}
    if available is not None:
        doc['available-packages'] = list(available)
    if patterns:
        doc['available-package-patterns'] = list(patterns)
    return dump(doc)


@dataclass(frozen=True)
class Upstream:
    """What a proxy needs from the upstream's ``packages.json``."""

    metadata_url: str  # absolute, with %package%
    available: tuple[str, ...] | None = None
    patterns: tuple[str, ...] = ()

    def json(self) -> dict[str, Any]:
        out: dict[str, Any] = {'metadata-url': self.metadata_url}
        if self.available is not None:
            out['available-packages'] = list(self.available)
        if self.patterns:
            out['available-package-patterns'] = list(self.patterns)
        return out

    def p2_url(self, name: str, *, dev: bool) -> str:
        return self.metadata_url.replace(PLACEHOLDER, name + ('~dev' if dev else ''))

    def lists(self, name: str) -> bool:
        """False when the upstream says it does not have ``name`` (its ``available-packages`` or patterns)."""
        if self.available is None and not self.patterns:
            return True
        if self.available is not None and name in self.available:
            return True
        return any(re.fullmatch(_glob(p), name) for p in self.patterns)


def _glob(pattern: str) -> str:
    return '.*'.join(re.escape(part) for part in pattern.split('*'))


def _names(value: Any, *, patterns: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_AVAILABLE:
        raise UpstreamError('the upstream packages.json lists too many or invalid packages')
    check = _PATTERN.match if patterns else composer.normalise_name
    return tuple(item.lower() for item in value if isinstance(item, str) and check(item.lower()))


def parse_upstream_root(data: bytes, url: str) -> Upstream:
    """The upstream's ``packages.json`` (Composer 2: ``metadata-url`` required, on the upstream's host)."""
    doc = load(data, 'the upstream packages.json')
    if not isinstance(doc, dict):
        raise UpstreamError('the upstream packages.json is not an object')
    template = doc.get('metadata-url')
    if not isinstance(template, str) or PLACEHOLDER not in template or len(template) > MAX_URL:
        raise UpstreamError('the upstream is not a Composer 2 repository (packages.json has no metadata-url)')
    absolute = urljoin(url, template)
    base, target = urlsplit(url), urlsplit(absolute)
    if target.scheme not in ('http', 'https') or (target.hostname, target.port) != (base.hostname, base.port):
        raise UpstreamError('the upstream metadata-url points at another host')
    available = _names(doc['available-packages']) if 'available-packages' in doc else None
    patterns = _names(doc['available-package-patterns'], patterns=True) if 'available-package-patterns' in doc else ()
    return Upstream(absolute, available, patterns)


def upstream_from_json(doc: Any) -> Upstream:
    if not isinstance(doc, dict) or not isinstance(doc.get('metadata-url'), str):
        raise UpstreamError('the cached upstream packages.json is invalid')
    available = doc.get('available-packages')
    return Upstream(
        doc['metadata-url'],
        tuple(available) if isinstance(available, list) else None,
        tuple(doc.get('available-package-patterns') or ()),
    )


# ---------------------------------------------------------------------------- p2 documents
def parse_p2(data: bytes, name: str) -> list[dict[str, Any]]:
    """The expanded version entries of ``name`` in a ``p2`` document (validated, bounded; entries without a valid
    version are skipped)."""
    if len(data) > MAX_DOCUMENT:
        raise UpstreamError('the package metadata is too large')
    doc = load(data, 'the package metadata')
    packages = doc.get('packages') if isinstance(doc, dict) else None
    if not isinstance(packages, dict):
        raise UpstreamError('the package metadata has no packages')
    versions: Any = next((v for k, v in packages.items() if isinstance(k, str) and k.lower() == name), [])
    if not isinstance(versions, list) or len(versions) > MAX_VERSIONS:
        raise UpstreamError('the package metadata has an invalid version list')
    minified = doc.get('minified') == composer.MINIFIED
    entries = composer.expand(versions) if minified else [v for v in versions if isinstance(v, dict)]
    out: list[dict[str, Any]] = []
    for entry in entries:
        version = entry.get('version')
        if not isinstance(version, str):
            continue
        normalised = composer.normalise(version)
        given = entry.get('version_normalized')
        if isinstance(given, str) and composer.normalise(given) is not None and len(given) <= composer.MAX_VERSION:
            normalised = given
        if normalised is None:
            continue
        clean = {k: v for k, v in entry.items() if k in ENTRY_KEYS}
        clean['name'] = name
        clean['version_normalized'] = normalised
        out.append(clean)
    return out


def render_p2(name: str, entries: Sequence[Mapping[str, Any]]) -> bytes:
    """A minified ``p2`` document of ``name``."""
    return dump({'packages': {name: composer.minify([dict(e) for e in entries])}, 'minified': composer.MINIFIED})


def _host(url: str) -> str:
    return (urlsplit(url).hostname or '').lower()


def archive_type(dist: Any) -> str | None:
    kind = dist.get('type') if isinstance(dist, dict) else None
    return kind if kind in ARCHIVE_TYPES else None


def dist_file(dist: Mapping[str, Any]) -> str:
    """The file name a proxy serves an upstream archive as: its source reference (else ``dist``) and type."""
    reference = dist.get('reference')
    stem = reference if isinstance(reference, str) and _REFERENCE.match(reference) else 'dist'
    return f'{stem}.{dist["type"]}'


def shasum(dist: Mapping[str, Any]) -> str | None:
    value = dist.get('shasum')
    return value.lower() if isinstance(value, str) and _SHA1.match(value.lower()) else None


def rewrite_entries(
    entries: Iterable[dict[str, Any]], repo_url: str, hosts: Collection[str], *, strip_source: bool
) -> list[dict[str, Any]]:
    """A proxy's entries as its clients see them: archives on allowed hosts point at the repository."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        entry = dict(entry)
        if strip_source:
            entry.pop('source', None)
        dist = entry.get('dist')
        if isinstance(dist, dict):
            dist = {k: v for k, v in dist.items() if k != UPSTREAM_URL_KEY}
            url = dist.get('url')
            kind = archive_type(dist)
            if (
                kind is not None
                and isinstance(url, str)
                and len(url) <= MAX_URL
                and urlsplit(url).scheme in ('http', 'https')
                and _host(url) in hosts
            ):
                segment = composer.path_segment(str(entry['version_normalized']))
                path = dist_path(str(entry['name']), segment, dist_file(dist))
                dist = {**dist, 'url': repo_url + path, UPSTREAM_URL_KEY: url}
            entry['dist'] = dist
        out.append(entry)
    return out


@dataclass(frozen=True)
class DistTarget:
    """Where a proxy fetches an archive from and what it must be."""

    upstream_url: str
    kind: str
    shasum: str | None
    reference: str | None


def find_dist(entries: Iterable[Mapping[str, Any]], segment: str, file: str) -> DistTarget | None:
    """The rewritten archive at ``dists/<name>/<segment>/<file>`` of a proxy's stored entries."""
    for entry in entries:
        if composer.path_segment(str(entry.get('version_normalized') or '')) != segment:
            continue
        dist = entry.get('dist')
        kind = archive_type(dist)
        if kind is None or not isinstance(dist, dict) or dist_file(dist) != file:
            continue
        url = dist.get(UPSTREAM_URL_KEY)
        if not isinstance(url, str):
            continue
        reference = dist.get('reference')
        return DistTarget(url, kind, shasum(dist), reference if isinstance(reference, str) else None)
    return None


def fetch_url(url: str, hosts: Collection[str]) -> str:
    """The URL to fetch an archive from: a GitHub API zipball (which redirects to codeload.github.com, and counts
    against the API's rate limit) is fetched from codeload.github.com directly when that host is allowed."""
    parts = urlsplit(url)
    found = _GITHUB_ZIPBALL.match(parts.path) if (parts.hostname or '').lower() == 'api.github.com' else None
    if found is None or parts.query or 'codeload.github.com' not in hosts:
        return url
    owner, repo, ref = found.groups()
    return f'https://codeload.github.com/{owner}/{repo}/legacy.zip/{ref}'


def reprefix(entries: Iterable[Mapping[str, Any]], old: str, new: str) -> list[dict[str, Any]]:
    """Entries of a member with archive URLs of ``old`` (a repository URL) pointing at ``new`` (a group)."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        copy = dict(entry)
        dist = copy.get('dist')
        if isinstance(dist, dict) and isinstance(dist.get('url'), str) and dist['url'].startswith(old):
            copy['dist'] = {**dist, 'url': new + dist['url'][len(old) :]}
        out.append(copy)
    return out


def merge(members: Sequence[Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """Members' entries merged: the first member that has a version (normalised) wins it; newest first."""
    chosen: dict[str, dict[str, Any]] = {}
    for entries in members:
        for entry in entries:
            chosen.setdefault(str(entry.get('version_normalized')), dict(entry))
    return newest_first(chosen.values())


def newest_first(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda e: composer.sort_string(str(e.get('version_normalized'))), reverse=True)
