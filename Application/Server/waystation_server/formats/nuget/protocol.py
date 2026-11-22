"""NuGet v3 routes, documents and URL rewriting (FORMATS.md "NuGet v3"). Pure functions, no I/O.

Repository layout (``/repository/<repo>/``):

- ``index.json``: the service index (rendered per request: every resource points at this repository);
- ``v3-flatcontainer/<id>/index.json`` (versions), ``v3-flatcontainer/<id>/<version>/<id>.<version>.nupkg`` and
  ``.../<id>.nuspec`` (``PackageBaseAddress/3.0.0``; lower-case id, normalised lower-case version);
- ``v3/registration/<id>/index.json`` and, for proxies, the upstream's pages and leaves under the same base
  (``RegistrationsBaseUrl``, SemVer 2 included);
- ``query`` (``SearchQueryService``);
- ``api/v2/package`` (``PackagePublish/2.0.0``: ``PUT`` push, ``DELETE <id>/<version>`` unlist or delete,
  ``POST <id>/<version>`` relist).

Private asset keys (never routes): ``_upstream/index.json`` (the upstream's resources) and
``_catalog/<id>/<version>.json`` (the hash and size the upstream catalog publishes for a package).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import quote, urlsplit

from waystation_common import nuget
from waystation_common.errors import UpstreamError, ValidationFailed

SERVICE_INDEX = 'index.json'
FLAT = 'v3-flatcontainer'
REGISTRATION = 'v3/registration'
SEARCH = 'query'
PUBLISH = 'api/v2/package'
UPSTREAM_INDEX_KEY = '_upstream/index.json'
JSON_TYPE = 'application/json'
NUPKG_TYPE = 'application/octet-stream'
NUSPEC_TYPE = 'application/xml'
MAX_DOCUMENT = 64 * 1024 * 1024
MAX_VERSIONS = 100_000
MAX_REG_SEGMENTS = 4
UNLISTED_PUBLISHED = '1900-01-01T00:00:00+00:00'  # what nuget.org's registrations say for unlisted versions
_SEGMENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}$')

REGISTRATION_TYPES = (
    'RegistrationsBaseUrl',
    'RegistrationsBaseUrl/3.0.0-beta',
    'RegistrationsBaseUrl/3.0.0-rc',
    'RegistrationsBaseUrl/3.4.0',
    'RegistrationsBaseUrl/3.6.0',
    'RegistrationsBaseUrl/Versioned',
)
SEARCH_TYPES = (
    'SearchQueryService',
    'SearchQueryService/3.0.0-beta',
    'SearchQueryService/3.0.0-rc',
    'SearchQueryService/3.5.0',
)
# Upstream registration hives, best first: the SemVer 2 ones list every version.
_REGISTRATION_PREFERENCE = (
    'RegistrationsBaseUrl/3.6.0',
    'RegistrationsBaseUrl/Versioned',
    'RegistrationsBaseUrl/3.4.0',
    'RegistrationsBaseUrl/3.0.0-rc',
    'RegistrationsBaseUrl/3.0.0-beta',
    'RegistrationsBaseUrl',
)


class Op(StrEnum):
    SERVICE_INDEX = 'service-index'
    FLAT_INDEX = 'versions'
    NUPKG = 'nupkg'
    NUSPEC = 'nuspec'
    REGISTRATION = 'registration'
    REGISTRATION_PART = 'registration-part'  # a page or leaf (proxies)
    SEARCH = 'search'
    PUBLISH = 'push'
    PACKAGE = 'package'  # api/v2/package/<id>/<version>: delete (unlist), relist


@dataclass(frozen=True)
class Route:
    op: Op
    id: str = ''  # lower case
    version: str = ''  # flat container form (normalised, lower case, no metadata)
    rest: str = ''  # registration parts: the path below v3/registration/<id>/

    @property
    def key(self) -> str:
        """The canonical repository path (asset key) of the route."""
        match self.op:
            case Op.SERVICE_INDEX:
                return SERVICE_INDEX
            case Op.FLAT_INDEX:
                return f'{FLAT}/{self.id}/index.json'
            case Op.NUPKG:
                return nupkg_path(self.id, self.version)
            case Op.NUSPEC:
                return nuspec_path(self.id, self.version)
            case Op.REGISTRATION:
                return f'{REGISTRATION}/{self.id}/index.json'
            case Op.REGISTRATION_PART:
                return f'{REGISTRATION}/{self.id}/{self.rest}'
            case Op.SEARCH:
                return SEARCH
            case Op.PUBLISH:
                return PUBLISH
            case Op.PACKAGE:
                return f'{PUBLISH}/{self.id}/{self.version}'


def nupkg_path(package_id: str, version: str) -> str:
    return f'{FLAT}/{nuget.package_file(package_id, version)}'


def nuspec_path(package_id: str, version: str) -> str:
    lower = package_id.lower()
    return f'{FLAT}/{lower}/{version}/{lower}.nuspec'


def catalog_key(package_id: str, version: str) -> str:
    return f'_catalog/{package_id.lower()}/{version}.json'


def _id(text: str) -> str | None:
    return text.lower() if nuget.valid_id(text) else None


def parse_route(path: str) -> Route | None:
    """Map a normalised repository path to a NuGet operation (None: not a NuGet path)."""
    if path == SERVICE_INDEX:
        return Route(Op.SERVICE_INDEX)
    if path == SEARCH:
        return Route(Op.SEARCH)
    if path == PUBLISH:
        return Route(Op.PUBLISH)
    parts = path.split('/')
    if path.startswith(PUBLISH + '/') and len(parts) == 5:
        package_id, version = _id(parts[3]), nuget.flat_version(parts[4])
        return Route(Op.PACKAGE, package_id, version) if package_id and version else None
    if parts[0] == FLAT and len(parts) >= 3:
        package_id = _id(parts[1])
        if package_id is None:
            return None
        if len(parts) == 3:
            return Route(Op.FLAT_INDEX, package_id) if parts[2] == 'index.json' else None
        if len(parts) != 4:
            return None
        version = nuget.flat_version(parts[2])
        if version is None:
            return None
        filename = parts[3].lower()
        if filename == f'{package_id}.nuspec':
            return Route(Op.NUSPEC, package_id, version)
        if filename.endswith('.nupkg') and filename.removesuffix('.nupkg') in (
            f'{package_id}.{version}',
            f'{package_id}.{parts[2].lower()}',
        ):
            return Route(Op.NUPKG, package_id, version)
        return None
    if path.startswith(REGISTRATION + '/') and len(parts) >= 4:
        package_id = _id(parts[2])
        rest = parts[3:]
        if package_id is None or len(rest) > MAX_REG_SEGMENTS or not rest[-1].endswith('.json'):
            return None
        if rest == ['index.json']:
            return Route(Op.REGISTRATION, package_id)
        if not all(_SEGMENT.match(p) for p in rest):
            return None
        return Route(Op.REGISTRATION_PART, package_id, rest='/'.join(p.lower() for p in rest))
    return None


# ---------------------------------------------------------------------------- JSON
def load(data: bytes, what: str) -> Any:
    if len(data) > MAX_DOCUMENT:
        raise UpstreamError(f'{what} is too large')
    try:
        return json.loads(data)
    except ValueError, RecursionError:
        raise UpstreamError(f'{what} is not valid JSON') from None


def dump(doc: Any) -> bytes:
    return json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode()


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------- service index
def service_index(repo_url: str, *, publish: bool) -> bytes:
    """This repository's service index (``repo_url`` ends with ``/``)."""
    resources: list[dict[str, str]] = [
        {'@id': f'{repo_url}{FLAT}/', '@type': 'PackageBaseAddress/3.0.0', 'comment': 'Package contents'},
    ]
    resources += [
        {'@id': f'{repo_url}{REGISTRATION}/', '@type': t, 'comment': 'Package metadata (SemVer 2.0.0 included)'}
        for t in REGISTRATION_TYPES
    ]
    resources += [{'@id': f'{repo_url}{SEARCH}', '@type': t, 'comment': 'Search'} for t in SEARCH_TYPES]
    if publish:
        resources.append({'@id': f'{repo_url}{PUBLISH}', '@type': 'PackagePublish/2.0.0', 'comment': 'Push'})
    return dump({'version': '3.0.0', 'resources': resources})


@dataclass(frozen=True)
class Upstream:
    """What a proxy uses of its upstream's service index."""

    flat: str  # PackageBaseAddress, ends with '/'
    registration: str  # the registration hive fetched from, ends with '/'
    registrations: tuple[str, ...]  # every registration hive (search results may point at any)
    search: str | None
    origin: str  # scheme://host[:port] of the service index (catalog leaves must be on it or a resource's host)

    def json(self) -> dict[str, Any]:
        return {
            'flat': self.flat,
            'registration': self.registration,
            'registrations': list(self.registrations),
            'search': self.search,
            'origin': self.origin,
        }

    @classmethod
    def from_json(cls, doc: Any) -> Upstream:
        if not isinstance(doc, dict):
            raise UpstreamError('cached upstream service index is unreadable')
        return cls(
            flat=str(doc['flat']),
            registration=str(doc['registration']),
            registrations=tuple(str(r) for r in doc.get('registrations') or ()),
            search=doc.get('search') if isinstance(doc.get('search'), str) else None,
            origin=str(doc.get('origin') or ''),
        )

    def hosts(self) -> frozenset[str]:
        urls = [self.origin, self.flat, self.registration, *self.registrations, *([self.search] if self.search else [])]
        return frozenset((urlsplit(u).hostname or '').lower() for u in urls) - {''}


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parts = urlsplit(value)
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname or '@' in parts.netloc:
        return None
    return value


def _base(url: str) -> str:
    return url if url.endswith('/') else url + '/'


def service_index_url(upstream_url: str) -> str:
    """The upstream service index: the URL itself when it names a ``.json`` document, else ``<url>/index.json``."""
    path = urlsplit(upstream_url).path
    return upstream_url if path.endswith('.json') else _base(upstream_url) + SERVICE_INDEX


def parse_upstream_index(data: bytes, url: str) -> Upstream:
    """The resources a proxy needs from an upstream service index; raises ``UpstreamError``."""
    doc = load(data, 'the upstream service index')
    resources = doc.get('resources') if isinstance(doc, dict) else None
    if not isinstance(resources, list):
        raise UpstreamError('the upstream is not a NuGet v3 service index')
    by_type: dict[str, str] = {}
    for item in resources[:1000]:
        if not isinstance(item, dict):
            continue
        rtype, rid = item.get('@type'), _http_url(item.get('@id'))
        if isinstance(rtype, str) and rid is not None:
            by_type.setdefault(rtype, rid)
    flat = by_type.get('PackageBaseAddress/3.0.0')
    registration = next((by_type[t] for t in _REGISTRATION_PREFERENCE if t in by_type), None)
    if flat is None or registration is None:
        raise UpstreamError('the upstream service index has no PackageBaseAddress or RegistrationsBaseUrl')
    registrations = tuple(dict.fromkeys(_base(by_type[t]) for t in _REGISTRATION_PREFERENCE if t in by_type))
    search = next((by_type[t] for t in reversed(SEARCH_TYPES) if t in by_type), None)
    parts = urlsplit(url)
    return Upstream(
        flat=_base(flat),
        registration=_base(registration),
        registrations=registrations,
        search=search,
        origin=f'{parts.scheme.lower()}://{parts.netloc.lower()}',
    )


# ---------------------------------------------------------------------------- URL rewriting
def rewrite_urls(doc: Any, prefixes: Sequence[tuple[str, str]]) -> Any:
    """Every string in ``doc`` starting with an ``old`` prefix gets the ``new`` one (first match wins)."""
    if isinstance(doc, str):
        for old, new in prefixes:
            if doc.startswith(old):
                return new + doc[len(old) :]
        return doc
    if isinstance(doc, list):
        return [rewrite_urls(v, prefixes) for v in doc]
    if isinstance(doc, dict):
        return {k: rewrite_urls(v, prefixes) for k, v in doc.items()}
    return doc


def proxy_prefixes(upstream: Upstream, repo_url: str) -> list[tuple[str, str]]:
    """Upstream resource bases → this repository's (registration hives, then the flat container)."""
    reg = f'{repo_url}{REGISTRATION}/'
    out = [(base, reg) for base in upstream.registrations]
    out.append((upstream.flat, f'{repo_url}{FLAT}/'))
    return out


def rewrite_registration(data: bytes, upstream: Upstream, repo_url: str) -> bytes:
    """An upstream registration index, page or leaf with its URLs pointing at this repository. Catalog entry ids stay
    the upstream's (identifiers; clients do not fetch them to restore)."""
    doc = load(data, 'the upstream registration')
    if not isinstance(doc, dict):
        raise UpstreamError('the upstream registration is not a JSON object')
    try:
        return dump(rewrite_urls(doc, proxy_prefixes(upstream, repo_url)))
    except RecursionError:
        raise UpstreamError('the upstream registration is nested too deeply') from None


def rewrite_search(data: bytes, upstream: Upstream, repo_url: str) -> bytes:
    doc = load(data, 'the upstream search answer')
    if not isinstance(doc, dict) or not isinstance(doc.get('data', []), list):
        raise UpstreamError('the upstream search answer is not a NuGet search result')
    try:
        return dump(rewrite_urls(doc, proxy_prefixes(upstream, repo_url)))
    except RecursionError:
        raise UpstreamError('the upstream search answer is nested too deeply') from None


def reprefix(data: bytes, old_repo_url: str, new_repo_url: str) -> bytes:
    """A document a member rendered or cached, as its group serves it (JSON written by ``dump``: no escaped ``/``)."""
    return data.replace(old_repo_url.encode(), new_repo_url.encode())


# ---------------------------------------------------------------------------- flat container
def parse_versions(data: bytes) -> list[str]:
    """Versions of a flat container ``index.json`` (normalised to the flat form, de-duplicated, upstream order)."""
    doc = load(data, 'the version list')
    versions = doc.get('versions') if isinstance(doc, dict) else None
    if not isinstance(versions, list):
        raise UpstreamError('the version list has no "versions" array')
    if len(versions) > MAX_VERSIONS:
        raise UpstreamError('the version list is too long')
    out: dict[str, None] = {}
    for item in versions:
        flat = nuget.flat_version(item) if isinstance(item, str) else None
        if flat is not None:
            out.setdefault(flat)
    return list(out)


def render_versions(versions: Iterable[str]) -> bytes:
    """A flat container ``index.json``: versions ascending (NuGet order), de-duplicated."""
    unique = {v for v in versions if v}
    return dump({'versions': sorted(unique, key=nuget.sort_string)})


# ---------------------------------------------------------------------------- registrations
def leaf_version(leaf: Any) -> str | None:
    """The flat version of a registration leaf (or None)."""
    if not isinstance(leaf, dict):
        return None
    entry = leaf.get('catalogEntry')
    version = entry.get('version') if isinstance(entry, dict) else None
    return nuget.flat_version(version) if isinstance(version, str) else None


def in_page(page: Mapping[str, Any], version: str) -> bool:
    lower, upper = page.get('lower'), page.get('upper')
    if not isinstance(lower, str) or not isinstance(upper, str):
        return True
    key = nuget.sort_string(version)
    return nuget.sort_string(lower) <= key <= nuget.sort_string(upper)


def registration_pages(doc: Any) -> list[dict[str, Any]]:
    """The pages of a registration index (validated shape)."""
    items = doc.get('items') if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise UpstreamError('the registration has no "items" array')
    return [p for p in items if isinstance(p, dict)]


@dataclass(frozen=True)
class CatalogHash:
    sha512_hex: str
    size: int | None


def parse_catalog_leaf(data: bytes) -> dict[str, Any]:
    """The fields of a catalog leaf Waystation keeps (``packageHash``, its algorithm, ``packageSize``, id, version)."""
    doc = load(data, 'the catalog entry')
    if not isinstance(doc, dict):
        raise UpstreamError('the catalog entry is not a JSON object')
    out = {k: doc.get(k) for k in ('id', 'version', 'packageHash', 'packageHashAlgorithm', 'packageSize')}
    return {k: v for k, v in out.items() if isinstance(v, str | int) and not isinstance(v, bool)}


def catalog_hash(doc: Mapping[str, Any]) -> CatalogHash | None:
    """The package's SHA-512 (hex) and size from a kept catalog leaf; None when it publishes none."""
    algorithm = str(doc.get('packageHashAlgorithm') or 'SHA512').upper()
    value = doc.get('packageHash')
    if algorithm != 'SHA512' or not isinstance(value, str):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except binascii.Error, ValueError:
        raise UpstreamError('the catalog entry has an invalid packageHash') from None
    if len(raw) != 64:
        raise UpstreamError('the catalog entry has an invalid packageHash')
    size = doc.get('packageSize')
    return CatalogHash(raw.hex(), size if isinstance(size, int) and size >= 0 else None)


@dataclass(frozen=True)
class HostedVersion:
    """One hosted package version, as registrations and search render it."""

    details: Mapping[str, Any]  # ``assets.nuget`` (``Nuspec.details`` + listed, published, uploaded_by)
    size: int
    sha512_b64: str | None

    @property
    def id(self) -> str:
        return str(self.details.get('id') or '')

    @property
    def version(self) -> str:
        return str(self.details.get('version') or '')

    @property
    def flat(self) -> str:
        return nuget.flat_version(self.version) or self.version.lower()

    @property
    def listed(self) -> bool:
        return self.details.get('listed') is not False


def _catalog_entry(repo_url: str, v: HostedVersion) -> dict[str, Any]:
    d = v.details
    reg = f'{repo_url}{REGISTRATION}/'
    lower = v.id.lower()
    published = d.get('published')
    groups = []
    for group in d.get('dependency_groups') or ():
        deps = [
            {
                '@id': f'{reg}{lower}/{v.flat}.json#dependency/{str(dep.get("id", "")).lower()}',
                '@type': 'PackageDependency',
                'id': dep.get('id'),
                'range': dep.get('range') or '(, )',
                'registration': f'{reg}{quote(str(dep.get("id", "")).lower())}/index.json',
            }
            for dep in group.get('dependencies') or ()
        ]
        out: dict[str, Any] = {'@type': 'PackageDependencyGroup', 'dependencies': deps}
        if group.get('target_framework'):
            out['targetFramework'] = group['target_framework']
        groups.append(out)
    entry: dict[str, Any] = {
        '@id': f'{reg}{lower}/{v.flat}.json#catalogEntry',
        '@type': 'PackageDetails',
        'id': v.id,
        'version': v.version,
        'authors': d.get('authors') or '',
        'description': d.get('description') or '',
        'summary': d.get('summary') or '',
        'title': d.get('title') or '',
        'tags': list(d.get('tags') or ()),
        'iconUrl': d.get('icon_url') or '',
        'licenseUrl': d.get('license_url') or '',
        'licenseExpression': d.get('license_expression') or '',
        'projectUrl': d.get('project_url') or '',
        'language': d.get('language') or '',
        'listed': v.listed,
        'requireLicenseAcceptance': bool(d.get('require_license_acceptance')),
        'minClientVersion': d.get('min_client_version') or '',
        'published': iso(published) if isinstance(published, datetime) and v.listed else UNLISTED_PUBLISHED,
        'packageContent': f'{repo_url}{nupkg_path(v.id, v.flat)}',
        'dependencyGroups': groups,
    }
    if v.sha512_b64:
        entry['packageHash'] = v.sha512_b64
        entry['packageHashAlgorithm'] = 'SHA512'
        entry['packageSize'] = v.size
    return entry


def _leaf(repo_url: str, v: HostedVersion) -> dict[str, Any]:
    reg = f'{repo_url}{REGISTRATION}/'
    lower = v.id.lower()
    return {
        '@id': f'{reg}{lower}/{v.flat}.json',
        '@type': 'Package',
        'catalogEntry': _catalog_entry(repo_url, v),
        'packageContent': f'{repo_url}{nupkg_path(v.id, v.flat)}',
        'registration': f'{reg}{lower}/index.json',
    }


def render_registration(repo_url: str, package_id: str, versions: Sequence[HostedVersion]) -> bytes:
    """A registration index with every version inlined in one page (ascending)."""
    ordered = sorted(versions, key=lambda v: nuget.sort_string(v.version))
    reg = f'{repo_url}{REGISTRATION}/{package_id.lower()}/index.json'
    page = {
        '@id': f'{reg}#page/{ordered[0].flat}/{ordered[-1].flat}',
        '@type': 'catalog:CatalogPage',
        'count': len(ordered),
        'items': [_leaf(repo_url, v) for v in ordered],
        'lower': ordered[0].version,
        'upper': ordered[-1].version,
        'parent': reg,
    }
    return dump(
        {
            '@id': reg,
            '@type': ['catalog:CatalogRoot', 'PackageRegistration', 'catalog:Permalink'],
            'count': 1,
            'items': [page],
        }
    )


def render_leaf(repo_url: str, v: HostedVersion) -> bytes:
    leaf = _leaf(repo_url, v)
    return dump(
        {
            '@id': leaf['@id'],
            '@type': ['Package', 'http://schema.nuget.org/catalog#Permalink'],
            'catalogEntry': leaf['catalogEntry']['@id'],
            'listed': v.listed,
            'packageContent': leaf['packageContent'],
            'published': leaf['catalogEntry']['published'],
            'registration': leaf['registration'],
        }
    )


def merge_registrations(documents: Sequence[Any]) -> dict[str, Any] | None:
    """A group's registration index: the members' leaves (fully inlined documents, in member order), the first member
    that has a version winning it; one page, ascending. None when no member has a leaf."""
    chosen: dict[str, dict[str, Any]] = {}
    first: dict[str, Any] | None = None
    for doc in documents:
        for page in registration_pages(doc):
            for leaf in page.get('items') or ():
                version = leaf_version(leaf)
                if version is not None and version not in chosen:
                    chosen[version] = leaf
                    first = first or doc
    if first is None:
        return None
    ordered = [chosen[v] for v in sorted(chosen, key=nuget.sort_string)]
    reg = str(first.get('@id') or '')
    lower_v = ordered[0]['catalogEntry'].get('version')
    upper_v = ordered[-1]['catalogEntry'].get('version')
    page = {
        '@id': f'{reg}#page/{leaf_version(ordered[0])}/{leaf_version(ordered[-1])}',
        '@type': 'catalog:CatalogPage',
        'count': len(ordered),
        'items': ordered,
        'lower': lower_v,
        'upper': upper_v,
        'parent': reg,
    }
    return {'@id': reg, '@type': first.get('@type', 'PackageRegistration'), 'count': 1, 'items': [page]}


# ---------------------------------------------------------------------------- search
@dataclass(frozen=True)
class SearchQuery:
    q: str
    skip: int
    take: int
    prerelease: bool
    semver2: bool

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> SearchQuery:
        def number(name: str, default: int, limit: int) -> int:
            raw = params.get(name, '')
            if not raw:
                return default
            if not raw.isdigit():
                raise ValidationFailed(f'{name} must be a non-negative integer')
            return min(int(raw), limit)

        return cls(
            q=(params.get('q') or '').strip()[:256],
            skip=number('skip', 0, 3000),
            take=number('take', 20, 1000),
            prerelease=(params.get('prerelease') or '').lower() == 'true',
            semver2=(params.get('semVerLevel') or '').startswith('2'),
        )

    def upstream_query(self) -> str:
        parts = [f'q={quote(self.q, safe="")}', f'skip={self.skip}', f'take={self.take}']
        parts.append(f'prerelease={"true" if self.prerelease else "false"}')
        if self.semver2:
            parts.append('semVerLevel=2.0.0')
        return '&'.join(parts)

    def visible(self, version: str) -> bool:
        found = nuget.parse_version(version)
        if found is None:
            return False
        return (self.prerelease or not found.is_prerelease) and (self.semver2 or not found.is_semver2)

    def matches(self, package_id: str, details: Mapping[str, Any]) -> bool:
        """Every term of ``q`` (``id:`` / ``packageid:`` exact id, else a substring of the id, title, tags or
        description), case-insensitively."""
        for term in self.q.lower().split():
            field, sep, value = term.partition(':')
            if sep and field in ('id', 'packageid'):
                if package_id.lower() != value:
                    return False
                continue
            haystack = ' '.join(
                [
                    package_id,
                    str(details.get('title') or ''),
                    ' '.join(details.get('tags') or ()),
                    str(details.get('description') or ''),
                ]
            ).lower()
            if term not in haystack:
                return False
        return True


def search_hit(repo_url: str, versions: Sequence[HostedVersion]) -> dict[str, Any]:
    """One package of a search answer (``versions``: the visible ones, any order; the newest is described)."""
    ordered = sorted(versions, key=lambda v: nuget.sort_string(v.version))
    latest = ordered[-1]
    d = latest.details
    reg = f'{repo_url}{REGISTRATION}/{latest.id.lower()}/'
    authors = [a.strip() for a in str(d.get('authors') or '').split(',') if a.strip()]
    return {
        '@id': f'{reg}index.json',
        '@type': 'Package',
        'registration': f'{reg}index.json',
        'id': latest.id,
        'version': latest.version,
        'description': d.get('description') or '',
        'summary': d.get('summary') or '',
        'title': d.get('title') or latest.id,
        'iconUrl': d.get('icon_url') or '',
        'licenseUrl': d.get('license_url') or '',
        'projectUrl': d.get('project_url') or '',
        'tags': list(d.get('tags') or ()),
        'authors': authors,
        'owners': [o.strip() for o in str(d.get('owners') or '').split(',') if o.strip()],
        'totalDownloads': 0,
        'verified': False,
        'packageTypes': [{'name': t} for t in d.get('package_types') or ('Dependency',)],
        'versions': [{'version': v.version, 'downloads': 0, '@id': f'{reg}{v.flat}.json'} for v in ordered],
    }


def render_search(total: int, hits: Sequence[Mapping[str, Any]]) -> bytes:
    return dump({'totalHits': total, 'data': list(hits)})


def merge_search(answers: Sequence[Any], query: SearchQuery) -> bytes:
    """A group's search answer: the members' hits (each asked for ``skip + take`` from 0), first member wins per id,
    then the ``skip``/``take`` window."""
    seen: set[str] = set()
    hits: list[Any] = []
    total = 0
    for doc in answers:
        if not isinstance(doc, dict):
            continue
        count = doc.get('totalHits')
        total += count if isinstance(count, int) else 0
        for hit in doc.get('data') or ():
            package_id = hit.get('id') if isinstance(hit, dict) else None
            if isinstance(package_id, str) and package_id.lower() not in seen:
                seen.add(package_id.lower())
                hits.append(hit)
    return render_search(max(total, len(hits)), hits[query.skip : query.skip + query.take])


def sha512_b64(hex_digest: str | None) -> str | None:
    if not hex_digest:
        return None
    try:
        return base64.b64encode(bytes.fromhex(hex_digest)).decode('ascii')
    except ValueError:
        return None
