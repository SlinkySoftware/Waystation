"""The Galaxy v3 / v1 documents: reading upstream answers (validated, bounded) and rendering this repository's answers.

Pure functions (no I/O); the plugin runs the parsing of large documents in a thread. Every URL a rendered document
carries points into the repository the client addressed: ``href``s and pagination ``links`` are paths on this server
(as Galaxy NG writes them; ``ansible-galaxy`` joins ``links.next`` with the server's scheme and host), the
``download_url`` of a version and of a role version is absolute.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from waystation_common import galaxy
from waystation_common.errors import ValidationFailed

MAX_DOC = 16 * 1024 * 1024
MAX_VERSIONS = 20_000
MAX_TEXT = 4096
DEFAULT_LIMIT = 10
MAX_LIMIT = 100
UPSTREAM_PAGE = 100
JSON_TYPE = 'application/json'
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
ROLE_PART = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$')
ROLE_VERSION = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}$')
ROLE_ID = re.compile(r'^[0-9]{1,18}$')
# Version detail fields kept from an upstream answer (``files``, ``manifest``, ``docs_blob`` are dropped: large and
# not used by clients).
_DETAIL_KEYS = ('version', 'created_at', 'updated_at', 'requires_ansible', 'marks', 'signatures', 'metadata')
_SIGNATURE_KEYS = ('signature', 'pubkey_fingerprint', 'signing_service', 'pulp_created')


def _text(value: Any) -> str | None:
    return value[:MAX_TEXT] if isinstance(value, str) and value else None


def load(data: bytes, what: str) -> dict[str, Any]:
    if len(data) > MAX_DOC:
        raise ValidationFailed(f'{what} is too large')
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise ValidationFailed(f'{what} is not valid JSON') from None
    if not isinstance(doc, dict):
        raise ValidationFailed(f'{what} is not a JSON object')
    return doc


def dump(doc: Mapping[str, Any]) -> bytes:
    return json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode()


def parse_time(value: Any) -> datetime | None:
    """An upstream ISO 8601 time (``...Z``) as an aware datetime, else None."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def http_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parts = urlsplit(value)
    return value if parts.scheme in ('http', 'https') and parts.hostname else None


# ---------------------------------------------------------------------------- the model
@dataclass(frozen=True)
class Version:
    """One entry of a collection's version list."""

    version: str
    created_at: str | None = None
    updated_at: str | None = None
    requires_ansible: str | None = None
    marks: tuple[Any, ...] = ()

    @property
    def sort(self) -> str:
        return galaxy.version_sort(self.version)


@dataclass(frozen=True)
class Info:
    """A collection's own fields (detail document)."""

    deprecated: bool = False
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class Detail:
    """One collection version's details (without URLs)."""

    namespace: str
    name: str
    version: str
    sha256: str
    size: int | None
    created_at: str | None = None
    updated_at: str | None = None
    requires_ansible: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    signatures: tuple[dict[str, Any], ...] = ()
    marks: tuple[Any, ...] = ()
    download_url: str = ''  # the upstream's (proxy repositories only)

    @property
    def artifact(self) -> galaxy.Artifact:
        return galaxy.Artifact(self.namespace, self.name, self.version)


def newest_first(versions: Iterable[Version]) -> list[Version]:
    return sorted(versions, key=lambda v: v.sort, reverse=True)


def highest(versions: Sequence[Version]) -> Version | None:
    """The highest release (SemVer), else the highest prerelease."""
    releases = [v for v in versions if not galaxy.is_prerelease(v.version)]
    pool = releases or list(versions)
    return max(pool, key=lambda v: v.sort) if pool else None


def merge(lists: Iterable[Sequence[Version]]) -> list[Version]:
    """Union of version lists, the first list that has a version wins it; newest first."""
    chosen: dict[str, Version] = {}
    for versions in lists:
        for v in versions:
            chosen.setdefault(v.version, v)
    return newest_first(chosen.values())


# ---------------------------------------------------------------------------- reading upstream answers
def parse_info(doc: Mapping[str, Any]) -> Info:
    return Info(
        deprecated=bool(doc.get('deprecated')),
        created_at=_text(doc.get('created_at')),
        updated_at=_text(doc.get('updated_at')),
    )


def check_collection(data: bytes, namespace: str, name: str) -> None:
    doc = load(data, 'the upstream collection document')
    if doc.get('namespace') != namespace or doc.get('name') != name:
        raise ValidationFailed('the upstream collection document names another collection')


@dataclass(frozen=True)
class VersionsPage:
    versions: list[Version]
    count: int
    has_next: bool


def parse_versions_page(data: bytes) -> VersionsPage:
    doc = load(data, 'the upstream version list')
    items = doc.get('data')
    meta = doc.get('meta')
    if not isinstance(items, list) or not isinstance(meta, dict) or len(items) > MAX_LIMIT * 10:
        raise ValidationFailed('the upstream version list has no data')
    count = meta.get('count')
    if not isinstance(count, int) or count < 0:
        raise ValidationFailed('the upstream version list has no count')
    out: list[Version] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('version'), str):
            raise ValidationFailed('the upstream version list has an entry without a version')
        if not galaxy.valid_version(item['version']):
            continue  # not SemVer: ansible-galaxy cannot use it either
        marks = item.get('marks')
        out.append(
            Version(
                version=item['version'],
                created_at=_text(item.get('created_at')),
                updated_at=_text(item.get('updated_at')),
                requires_ansible=_text(item.get('requires_ansible')),
                marks=tuple(marks) if isinstance(marks, list) else (),
            )
        )
    links = doc.get('links')
    has_next = isinstance(links, dict) and bool(links.get('next'))
    return VersionsPage(out, count, has_next)


def parse_detail(data: bytes, namespace: str, name: str, version: str) -> Detail:
    doc = load(data, 'the upstream version document')
    ns = doc.get('namespace')
    ns_name = ns.get('name') if isinstance(ns, dict) else ns
    coll = doc.get('collection')
    coll_name = coll.get('name') if isinstance(coll, dict) else doc.get('name')
    if ns_name != namespace or coll_name != name or doc.get('version') != version:
        raise ValidationFailed('the upstream version document names another collection version')
    artifact = doc.get('artifact')
    if not isinstance(artifact, dict):
        raise ValidationFailed('the upstream version document has no artifact')
    sha256 = str(artifact.get('sha256') or '').lower()
    if not _HEX64.match(sha256):
        raise ValidationFailed('the upstream version document has no valid artifact sha256')
    size = artifact.get('size')
    url = http_url(doc.get('download_url'))
    if url is None:
        raise ValidationFailed('the upstream version document has no http(s) download_url')
    metadata = doc.get('metadata')
    meta: dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}
    deps = meta.get('dependencies')
    meta['dependencies'] = deps if isinstance(deps, dict) else {}
    signatures = doc.get('signatures')
    sigs = tuple(
        {k: s[k] for k in _SIGNATURE_KEYS if k in s}
        for s in (signatures if isinstance(signatures, list) else [])
        if isinstance(s, dict)
    )
    marks = doc.get('marks')
    return Detail(
        namespace=namespace,
        name=name,
        version=version,
        sha256=sha256,
        size=size if isinstance(size, int) and size >= 0 else None,
        created_at=_text(doc.get('created_at')),
        updated_at=_text(doc.get('updated_at')),
        requires_ansible=_text(doc.get('requires_ansible')),
        metadata=meta,
        signatures=sigs,
        marks=tuple(marks) if isinstance(marks, list) else (),
        download_url=url,
    )


def slim_detail(data: bytes, info: Any) -> bytes:
    """What is cached of an upstream version document: the fields clients use (``files``, ``manifest`` and
    ``docs_blob`` dropped)."""
    doc = load(data, 'the upstream version document')
    keep = {k: doc[k] for k in (*_DETAIL_KEYS, 'artifact', 'download_url', 'namespace', 'collection', 'name') if k in doc}
    meta = keep.get('metadata')
    if isinstance(meta, dict):
        keep['metadata'] = {k: v for k, v in meta.items() if k != 'contents'}
    return dump(keep)


# ---------------------------------------------------------------------------- rendering
@dataclass(frozen=True)
class Urls:
    """Where the addressed repository lives: ``root`` = absolute repository URL (trailing slash), ``path`` = the
    same as a server path."""

    root: str

    @property
    def path(self) -> str:
        return urlsplit(self.root).path

    def collection(self, namespace: str, name: str) -> str:
        return f'{self.path}api/v3/collections/{namespace}/{name}/'

    def versions(self, namespace: str, name: str) -> str:
        return self.collection(namespace, name) + 'versions/'

    def version(self, namespace: str, name: str, version: str) -> str:
        return self.versions(namespace, name) + quote(version, safe='+') + '/'

    def download(self, artifact: galaxy.Artifact) -> str:
        return self.root + quote(artifact.path, safe='/+')

    def task(self, task_id: str) -> str:
        return f'{self.path}api/v3/imports/collections/{task_id}/'

    def role_archive(self, role_id: str, version: str) -> str:
        return f'{self.root}roles/{role_id}/{quote(version, safe="+")}.tar.gz'


def discovery(*, prefix: str = '', roles: bool) -> dict[str, Any]:
    versions = {'v3': f'{prefix}v3/'}
    if roles:
        versions['v1'] = f'{prefix}v1/'
    return {'available_versions': versions, 'current_version': 'v3'}


def render_collection(urls: Urls, namespace: str, name: str, info: Info, versions: Sequence[Version]) -> dict[str, Any]:
    top = highest(versions)
    return {
        'href': urls.collection(namespace, name),
        'namespace': namespace,
        'name': name,
        'deprecated': info.deprecated,
        'versions_url': urls.versions(namespace, name),
        'highest_version': (
            {'href': urls.version(namespace, name, top.version), 'version': top.version} if top is not None else None
        ),
        'created_at': info.created_at,
        'updated_at': info.updated_at,
        'download_count': 0,
    }


def page_params(query: Mapping[str, str]) -> tuple[int, int]:
    """``limit`` (default 10, at most 100) and ``offset`` of a list request (Galaxy's limit/offset pagination)."""

    def number(key: str, default: int) -> int:
        raw = query.get(key)
        return int(raw) if raw is not None and raw.isdigit() and len(raw) <= 9 else default

    return max(1, min(number('limit', DEFAULT_LIMIT), MAX_LIMIT)), number('offset', 0)


def render_versions(
    urls: Urls, namespace: str, name: str, versions: Sequence[Version], *, limit: int, offset: int
) -> dict[str, Any]:
    base = urls.versions(namespace, name)
    count = len(versions)

    def link(at: int) -> str:
        return base + '?' + urlencode({'limit': limit, 'offset': at})

    last = max(0, (count - 1) // limit * limit)
    return {
        'meta': {'count': count},
        'links': {
            'first': link(0),
            'previous': link(max(0, offset - limit)) if offset > 0 else None,
            'next': link(offset + limit) if offset + limit < count else None,
            'last': link(last),
        },
        'data': [
            {
                'version': v.version,
                'href': urls.version(namespace, name, v.version),
                'created_at': v.created_at,
                'updated_at': v.updated_at,
                'requires_ansible': v.requires_ansible,
                'marks': list(v.marks),
            }
            for v in versions[offset : offset + limit]
        ],
    }


def render_detail(urls: Urls, detail: Detail) -> dict[str, Any]:
    artifact = detail.artifact
    return {
        'version': detail.version,
        'href': urls.version(detail.namespace, detail.name, detail.version),
        'created_at': detail.created_at,
        'updated_at': detail.updated_at,
        'requires_ansible': detail.requires_ansible,
        'marks': list(detail.marks),
        'artifact': {'filename': artifact.filename, 'sha256': detail.sha256, 'size': detail.size},
        'collection': {'name': detail.name, 'href': urls.collection(detail.namespace, detail.name)},
        'download_url': urls.download(artifact),
        'name': detail.name,
        'namespace': {'name': detail.namespace},
        'signatures': list(detail.signatures),
        'metadata': detail.metadata,
        'git_url': None,
        'git_commit_sha': None,
    }


def render_task(urls: Urls, task: Mapping[str, Any]) -> dict[str, Any]:
    def when(value: Any) -> str | None:
        return value.isoformat().replace('+00:00', 'Z') if hasattr(value, 'isoformat') else None

    return {
        'id': str(task['_id']),
        'href': urls.task(str(task['_id'])),
        'state': task.get('state', 'waiting'),
        'created_at': when(task.get('created_at')),
        'started_at': when(task.get('started_at')),
        'finished_at': when(task.get('finished_at')),
        'error': task.get('error') or None,
        'messages': [
            {'level': m.get('level', 'INFO'), 'message': m.get('message', ''), 'time': when(m.get('time'))}
            for m in task.get('messages') or []
            if isinstance(m, dict)
        ],
        'namespace': {'name': task.get('namespace')},
        'name': task.get('name'),
        'version': task.get('version'),
    }


def error_doc(status: int, detail: str, code: str) -> dict[str, Any]:
    return {'errors': [{'status': str(status), 'code': code, 'title': detail, 'detail': detail}]}


# ---------------------------------------------------------------------------- roles (legacy v1)
def role_search_doc(data: bytes) -> dict[str, Any]:
    doc = load(data, 'the upstream role search')
    results = doc.get('results')
    if not isinstance(results, list):
        raise ValidationFailed('the upstream role search has no results')
    return doc


def render_role_search(data: bytes) -> dict[str, Any]:
    doc = role_search_doc(data)
    results = [r for r in doc['results'] if isinstance(r, dict) and ROLE_ID.match(str(r.get('id', '')))]
    return {'count': len(results), 'next': None, 'next_link': None, 'previous': None, 'results': results}


@dataclass(frozen=True)
class RoleVersionsPage:
    results: list[dict[str, Any]]
    next_link: str | None


def parse_role_versions(data: bytes) -> RoleVersionsPage:
    doc = load(data, 'the upstream role versions')
    results = doc.get('results')
    if not isinstance(results, list):
        raise ValidationFailed('the upstream role versions have no results')
    next_link = doc.get('next_link')
    return RoleVersionsPage(
        [r for r in results if isinstance(r, dict) and isinstance(r.get('name'), str)],
        next_link if isinstance(next_link, str) and next_link else None,
    )


def render_role_versions(urls: Urls, role_id: str, versions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for v in versions:
        item = dict(v)
        item.pop('source', None)
        name = str(v['name'])
        if ROLE_VERSION.match(name) and http_url(v.get('download_url')):
            item['download_url'] = urls.role_archive(role_id, name)
        else:
            item.pop('download_url', None)
        out.append(item)
    return {'count': len(out), 'next': None, 'next_link': None, 'previous': None, 'previous_link': None, 'results': out}


def role_download(versions: Sequence[Mapping[str, Any]], version: str) -> str | None:
    """The upstream download URL of a role version, when the list has one."""
    for v in versions:
        if v.get('name') == version:
            return http_url(v.get('download_url'))
    return None

