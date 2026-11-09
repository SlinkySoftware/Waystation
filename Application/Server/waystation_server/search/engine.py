"""Search execution: query parsing, sources, merged keyset paging, facets, Browse deep links.

Name search
-----------
``q`` is ``<name prefix>[ <version prefix>]``, compared case-insensitively (``waystation_common.searchkeys.fold``).
The name prefix matches a document's full name or the last ``/`` segment of it (``nginx`` finds ``library/nginx``);
an empty query lists everything. Each source contributes one stream per search key (``SOURCES``); every stream is
read in ``(key, repo_id, _id)`` index order and the streams are merged in ``(key, stream rank, repo_id, id)`` order,
so a page is ``limit`` items from at most ``limit + 1`` per stream and the cursor is that tuple (exact keyset paging,
no skipping, stable under concurrent writes).

- Cached files (``assets`` of kind ``content``) of every format except docker (whose files are layer digests;
  images are found by their tags), named by their last path segment. A version prefix excludes files.
- ``cached=true`` leaves out the apt/yum package listings (what a cached index lists may not be downloaded); their
  downloaded packages are still found as files.

Digest search
-------------
``sha256:<hex>`` (8 to 64 hex digits) or a bare 64-digit hex string finds every cached file whose blob starts with
that digest, in any repository and format ("where is this blob used?"), and the docker tags pointing at it. At most
``DIGEST_MAX`` of each are considered; the page cursor is an offset into that sorted set.

Facets
------
Counts per format, repository, kind and repository type, each computed with every *other* filter applied (so a
facet shows its alternatives), from per-(source, repository) counts over the principal's visible repositories. A
stream stops counting at ``FACET_CAP`` matches; ``capped`` then says the counts are lower bounds.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from waystation_common.db.client import Database
from waystation_common.db.collections import (
    APT_PACKAGES,
    ASSETS,
    DOCKER_TAGS,
    NPM_PACKAGES,
    PYPI_PACKAGES,
    YUM_PACKAGES,
)
from waystation_common.errors import ValidationFailed
from waystation_common.searchkeys import fold
from waystation_server.browse.models import decode_cursor, encode_cursor
from waystation_server.db import npm as npm_db
from waystation_server.db import search as search_db
from waystation_server.db.search import Key, Start, Stream
from waystation_server.repos.model import RepoConfig
from waystation_server.search.models import (
    SearchBrowseLink,
    SearchFacets,
    SearchFacetValue,
    SearchHit,
    SearchKind,
    SearchPage,
)

DIGEST_MAX = 1000
KINDS: tuple[SearchKind, ...] = ('image', 'package', 'file')
_DIGEST = re.compile(r'^(?:sha256:([0-9a-f]{8,64})|([0-9a-f]{64}))$')


@dataclass(frozen=True)
class SearchQuery:
    q: str = ''
    formats: frozenset[str] = frozenset()
    repositories: frozenset[str] = frozenset()
    kinds: frozenset[str] = frozenset()
    types: frozenset[str] = frozenset()
    cached: bool = False
    cursor: str | None = None
    limit: int = 50
    facets: bool = True

    def passes(self, kind: str, repo: RepoConfig, *, skip: str = '') -> bool:
        """Whether ``kind`` results of ``repo`` pass every filter except the facet ``skip``."""
        return (
            (skip == 'format' or not self.formats or repo.format in self.formats)
            and (skip == 'repository' or not self.repositories or repo.name in self.repositories)
            and (skip == 'type' or not self.types or repo.type.value in self.types)
            and (skip == 'kind' or not self.kinds or kind in self.kinds)
        )


@dataclass(frozen=True)
class Source:
    """A searchable collection: what its documents are and how they become hits."""

    id: str
    collection: str
    kind: SearchKind
    format: str | None  # None: cached files of every format but docker
    keys: tuple[Key, ...]
    projection: Mapping[str, int]
    version_field: str | None = None
    listing: bool = False  # rows of an upstream index listing: may not be cached

    def applies(self, repo: RepoConfig) -> bool:
        return repo.format == self.format if self.format is not None else repo.format != 'docker'


SOURCES: tuple[Source, ...] = (
    Source(
        'docker',
        DOCKER_TAGS,
        'image',
        'docker',
        ('name_lc', 'base_lc'),
        {'image': 1, 'tag': 1, 'manifest_digest': 1, 'updated_at': 1},
        version_field='tag',
    ),
    Source(
        'npm',
        NPM_PACKAGES,
        'package',
        'npm',
        ('name_lc', 'base_lc'),
        {'name': 1, 'latest': 1, 'updated_at': 1},
        version_field='latest',
    ),
    Source(
        'pypi',
        PYPI_PACKAGES,
        'package',
        'pypi',
        ('name_lc',),
        {'name': 1, 'display_name': 1, 'latest': 1, 'updated_at': 1},
        version_field='latest',
    ),
    Source(
        'apt',
        APT_PACKAGES,
        'package',
        'apt',
        ('name_lc',),
        {
            'name': 1,
            'version': 1,
            'suite': 1,
            'component': 1,
            'arch': 1,
            'filename': 1,
            'size': 1,
            'sha256': 1,
            'updated_at': 1,
        },
        version_field='version',
        listing=True,
    ),
    Source(
        'yum',
        YUM_PACKAGES,
        'package',
        'yum',
        ('name_lc',),
        {'name': 1, 'epoch': 1, 'version': 1, 'release': 1, 'arch': 1, 'path': 1, 'size': 1, 'updated_at': 1},
        version_field='version',
        listing=True,
    ),
    Source('file', ASSETS, 'file', None, ('name_lc',), {'path': 1, 'size': 1, 'blob_sha256': 1, 'updated_at': 1}),
)
# Stream rank = position in this list (the merge order for equal keys).
STREAMS: tuple[tuple[Source, Key], ...] = tuple((s, k) for s in SOURCES for k in s.keys)


@dataclass
class _Entry:
    key: str
    rank: int
    repo_id: str
    id: str
    source: Source
    doc: dict[str, Any]
    hit: SearchHit | None = None


@dataclass
class _Counts:
    """Matches per (kind, repo_id), and whether any stream stopped counting."""

    by: dict[tuple[str, str], int] = field(default_factory=dict)
    capped: bool = False

    def add(self, kind: str, repo_id: str, n: int) -> None:
        self.by[(kind, repo_id)] = self.by.get((kind, repo_id), 0) + n


# ---------------------------------------------------------------------------- entry point
async def search(db: Database, query: SearchQuery, visible: list[RepoConfig]) -> SearchPage:
    """Search the repositories in ``visible`` (already RBAC-filtered by the caller)."""
    text = query.q.strip()
    digest = _DIGEST.match(text.lower())
    if digest:
        return await _digest_search(db, query, visible, digest.group(1) or digest.group(2))
    return await _name_search(db, query, visible, text)


def parse_terms(text: str) -> tuple[str, str | None]:
    """``(name prefix, version prefix)`` of a query: the first word is the name, the rest the version."""
    name, _, version = text.strip().partition(' ')
    return fold(name), (version.strip() or None)


# ---------------------------------------------------------------------------- name search
def _streams(
    query: SearchQuery, repos: list[RepoConfig], name: str, version: str | None, *, filtered: bool
) -> list[tuple[int, Source, Stream]]:
    out: list[tuple[int, Source, Stream]] = []
    for rank, (source, key) in enumerate(STREAMS):
        if key == 'base_lc' and not name:
            continue  # an empty query lists every document once, through name_lc
        if source.listing and query.cached:
            continue
        if version is not None and source.version_field is None:
            continue
        repo_ids = tuple(
            sorted(r.id for r in repos if source.applies(r) and (not filtered or query.passes(source.kind, r)))
        )
        if not repo_ids:
            continue
        stream = Stream(
            collection=source.collection,
            key=key,
            prefix=name,
            repo_ids=repo_ids,
            projection=source.projection,
            version_field=source.version_field if version is not None else None,
            version_prefix=version,
        )
        out.append((rank, source, stream))
    return out


def _position(cursor: str | None) -> tuple[str, int, str, str] | None:
    if cursor is None:
        return None
    mode, key, rank, repo_id, ident = decode_cursor(cursor, str, str, int, str, str)
    if mode != 'n':
        raise ValidationFailed('invalid cursor')
    return key, rank, repo_id, ident


def _start(position: tuple[str, int, str, str] | None, rank: int) -> Start | None:
    if position is None:
        return None
    key, at_rank, repo_id, ident = position
    if rank < at_rank:
        return Start(key)
    if rank > at_rank:
        return Start(key, inclusive=True)
    return Start(key, repo_id=repo_id, id=search_db.parse_id_token(ident))


async def _name_search(db: Database, query: SearchQuery, visible: list[RepoConfig], text: str) -> SearchPage:
    name, version = parse_terms(text)
    position = _position(query.cursor)
    streams = _streams(query, visible, name, version, filtered=True)

    async def read(rank: int, source: Source, stream: Stream) -> list[_Entry]:
        docs = await search_db.stream_page(db, stream, start=_start(position, rank), limit=query.limit + 1)
        return [
            _Entry(str(d[stream.key]), rank, str(d['repo_id']), search_db.id_token(d['_id']), source, d) for d in docs
        ]

    async def no_counts() -> _Counts | None:
        return None

    want_facets = query.facets and query.cursor is None
    chunks, counts = await asyncio.gather(
        asyncio.gather(*(read(rank, source, stream) for rank, source, stream in streams)),
        _count(db, query, visible, name, version) if want_facets else no_counts(),
    )
    entries = sorted((e for chunk in chunks for e in chunk), key=lambda e: (e.key, e.rank, e.repo_id, e.id))
    page, more = entries[: query.limit], len(entries) > query.limit
    next_cursor = encode_cursor('n', page[-1].key, page[-1].rank, page[-1].repo_id, page[-1].id) if more else None
    repos = {r.id: r for r in visible}
    items = await _hits(db, page, repos)
    facets = _facets(query, counts, repos) if counts is not None else None
    return SearchPage(mode='name', items=items, next_cursor=next_cursor, facets=facets)


async def _count(
    db: Database, query: SearchQuery, visible: list[RepoConfig], name: str, version: str | None
) -> _Counts:
    streams = _streams(query, visible, name, version, filtered=False)
    results = await asyncio.gather(*(search_db.stream_counts(db, stream) for _, _, stream in streams))
    counts = _Counts()
    for (_, source, _), (per_repo, capped) in zip(streams, results, strict=True):
        counts.capped |= capped
        for repo_id, n in per_repo.items():
            counts.add(source.kind, repo_id, n)
    return counts


# ---------------------------------------------------------------------------- digest search
async def _digest_search(db: Database, query: SearchQuery, visible: list[RepoConfig], hex_prefix: str) -> SearchPage:
    repos = {r.id: r for r in visible}
    files = await search_db.assets_by_digest(
        db, hex_prefix, sorted(repos), _source('file').projection, limit=DIGEST_MAX
    )
    docker = sorted(r.id for r in visible if r.format == 'docker')
    tags = await search_db.tags_by_digest(
        db, f'sha256:{hex_prefix}', docker, _source('docker').projection, limit=DIGEST_MAX
    )
    counts = _Counts(capped=len(files) >= DIGEST_MAX or len(tags) >= DIGEST_MAX)
    entries: list[_Entry] = []
    for source, docs in ((_source('docker'), tags), (_source('file'), files)):
        for doc in docs:
            repo = repos.get(str(doc['repo_id']))
            if repo is None:
                continue
            counts.add(source.kind, repo.id, 1)
            if query.passes(source.kind, repo):
                entries.append(_Entry('', 0, repo.id, search_db.id_token(doc['_id']), source, doc))
    hits = await _hits(db, entries, repos)
    order = {kind: i for i, kind in enumerate(KINDS)}
    hits.sort(key=lambda h: (order[h.kind], h.repository.lower(), h.name, h.version or '', h.path or ''))
    offset = 0
    if query.cursor is not None:
        mode, offset = decode_cursor(query.cursor, str, int)
        if mode != 'd' or offset < 0:
            raise ValidationFailed('invalid cursor')
    page = hits[offset : offset + query.limit]
    more = offset + query.limit < len(hits)
    facets = _facets(query, counts, repos) if query.facets and query.cursor is None else None
    return SearchPage(
        mode='digest',
        items=page,
        next_cursor=encode_cursor('d', offset + query.limit) if more else None,
        facets=facets,
    )


def _source(source_id: str) -> Source:
    return next(s for s in SOURCES if s.id == source_id)


# ---------------------------------------------------------------------------- facets
def _facets(query: SearchQuery, counts: _Counts, repos: Mapping[str, RepoConfig]) -> SearchFacets:
    def tally(skip: str, value: Callable[[str, RepoConfig], str], selected: Iterable[str]) -> list[SearchFacetValue]:
        out: dict[str, int] = dict.fromkeys(selected, 0)
        for (kind, repo_id), n in counts.by.items():
            repo = repos.get(repo_id)
            if repo is not None and n and query.passes(kind, repo, skip=skip):
                v = value(kind, repo)
                out[v] = out.get(v, 0) + n
        return [SearchFacetValue(value=v, count=c) for v, c in sorted(out.items(), key=lambda i: (-i[1], i[0]))]

    total = sum(
        n for (kind, repo_id), n in counts.by.items() if repo_id in repos and query.passes(kind, repos[repo_id])
    )
    return SearchFacets(
        format=tally('format', lambda _k, r: r.format, query.formats),
        repository=tally('repository', lambda _k, r: r.name, query.repositories),
        kind=tally('kind', lambda k, _r: k, query.kinds),
        type=tally('type', lambda _k, r: r.type.value, query.types),
        total=total,
        capped=counts.capped,
    )


# ---------------------------------------------------------------------------- hits
def _when(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parent(path: str) -> str:
    return posixpath.dirname(path)


_MANIFEST = re.compile(r'^(.+)/manifests/(sha256:[0-9a-f]{64})$')


def file_link(repo: RepoConfig, path: str) -> dict[str, str]:
    """The Browse view query showing a cached file of ``repo``."""
    if repo.format == 'npm' and '/-/' in path:
        return {'package': path.split('/-/', 1)[0]}
    if repo.format == 'docker':
        manifest = _MANIFEST.match(path)
        return {'image': manifest.group(1), 'ref': manifest.group(2)} if manifest else {}
    if repo.format == 'apt':
        return {}
    if repo.format == 'pypi' and path.startswith('packages/') and path.count('/') == 2:
        return {'project': path.split('/')[1]}
    parent = _parent(path)
    return {'path': parent} if parent else {}


def _yum_version(doc: Mapping[str, Any]) -> str | None:
    version, release, epoch = doc.get('version'), doc.get('release'), str(doc.get('epoch') or '0')
    if not version:
        return None
    evr = f'{version}-{release}' if release else str(version)
    return evr if epoch == '0' else f'{epoch}:{evr}'


def _hit(entry: _Entry, repo: RepoConfig) -> SearchHit:
    doc, kind = entry.doc, entry.source.kind
    common: dict[str, Any] = {
        'kind': kind,
        'format': repo.format,
        'repository': repo.name,
        'repository_type': repo.type,
        'updated_at': _when(doc.get('updated_at')),
    }
    match entry.source.id:
        case 'docker':
            return SearchHit(
                **common,
                name=str(doc['image']),
                version=str(doc['tag']),
                qualifier=None,
                path=None,
                size=None,
                sha256=doc.get('manifest_digest'),
                cached=True,
                browse=SearchBrowseLink(
                    repository=repo.name, query={'image': str(doc['image']), 'ref': str(doc['tag'])}
                ),
            )
        case 'npm':
            return SearchHit(
                **common,
                name=str(doc['name']),
                version=doc.get('latest'),
                qualifier=None,
                path=None,
                size=None,
                sha256=None,
                cached=True,
                browse=SearchBrowseLink(repository=repo.name, query={'package': str(doc['name'])}),
            )
        case 'pypi':
            return SearchHit(
                **common,
                name=str(doc.get('display_name') or doc['name']),
                version=doc.get('latest'),
                qualifier=None,
                path=None,
                size=None,
                sha256=None,
                cached=True,
                browse=SearchBrowseLink(repository=repo.name, query={'project': str(doc['name'])}),
            )
        case 'apt':
            suite, component, arch = str(doc.get('suite', '')), str(doc.get('component', '')), str(doc.get('arch', ''))
            return SearchHit(
                **common,
                name=str(doc['name']),
                version=doc.get('version'),
                qualifier=f'{suite}/{component} {arch}',
                path=doc.get('filename'),
                size=_int(doc.get('size')),
                sha256=doc.get('sha256'),
                cached=None,
                browse=SearchBrowseLink(
                    repository=repo.name, query={'suite': suite, 'arch': arch, 'q': str(doc['name'])}
                ),
            )
        case 'yum':
            path = str(doc.get('path', ''))
            return SearchHit(
                **common,
                name=str(doc['name']),
                version=_yum_version(doc),
                qualifier=doc.get('arch') or None,
                path=path,
                size=_int(doc.get('size')),
                sha256=None,
                cached=None,
                browse=SearchBrowseLink(repository=repo.name, query=file_link(repo, path)),
            )
        case _:
            path = str(doc['path'])
            return SearchHit(
                **common,
                name=path.rsplit('/', 1)[-1],
                version=None,
                qualifier=None,
                path=path,
                size=_int(doc.get('size')),
                sha256=doc.get('blob_sha256'),
                cached=True,
                browse=SearchBrowseLink(repository=repo.name, query=file_link(repo, path)),
            )


async def _hits(db: Database, entries: list[_Entry], repos: Mapping[str, RepoConfig]) -> list[SearchHit]:
    """Hits for ``entries``, with the per-page facts that live elsewhere: docker image sizes, npm cached tarball
    bytes, and whether a listed apt/yum package is cached."""
    kept = [e for e in entries if e.repo_id in repos]
    for e in kept:
        e.hit = _hit(e, repos[e.repo_id])
    docker = [e for e in kept if e.source.id == 'docker']
    listed = [e for e in kept if e.source.listing and e.hit is not None and e.hit.path]
    npm_by_repo: dict[str, list[_Entry]] = {}
    for e in kept:
        if e.source.id == 'npm':
            npm_by_repo.setdefault(e.repo_id, []).append(e)

    (sizes, cached), npm_stats = await asyncio.gather(
        asyncio.gather(
            search_db.manifest_sizes(
                db, [(e.repo_id, str(e.doc['image']), str(e.doc['manifest_digest'])) for e in docker]
            ),
            search_db.cached_paths(db, [(e.repo_id, e.hit.path) for e in listed if e.hit and e.hit.path]),
        ),
        asyncio.gather(
            *(
                npm_db.tarball_stats(db, repo_id, [str(e.doc['name']) for e in group])
                for repo_id, group in npm_by_repo.items()
            )
        ),
    )
    for e in docker:
        assert e.hit is not None
        e.hit.size = sizes.get((e.repo_id, str(e.doc['image']), str(e.doc['manifest_digest'])))
    for e in listed:
        assert e.hit is not None
        assert e.hit.path is not None
        e.hit.cached = (e.repo_id, e.hit.path) in cached
    for group, stats in zip(npm_by_repo.values(), npm_stats, strict=True):
        for e in group:
            assert e.hit is not None
            tarballs = stats.get(str(e.doc['name']))
            e.hit.size = tarballs.size if tarballs is not None else 0
    return [e.hit for e in kept if e.hit is not None]
