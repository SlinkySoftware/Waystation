"""Hugging Face request routes under ``/repository/<repo>/`` (the client's ``HF_ENDPOINT``).

- ``api/models/<repo_id>`` and ``api/models/<repo_id>/revision/<rev>``: repository info (``INFO``; ``sha`` is the
  revision's commit, ``siblings`` its files).
- ``api/models/<repo_id>/tree/<rev>[/<path>]``: a file listing (``TREE``; ``recursive``, ``expand``, ``cursor`` and
  ``limit`` query parameters, paged with a ``Link`` header).
- ``<repo_id>/resolve/<rev>/<path>``: a file (``RESOLVE``).

Datasets: ``api/datasets/...`` and ``datasets/<repo_id>/resolve/...``. A repo id has one or two segments (``gpt2``,
``openai-community/gpt2``); where both readings fit, the two-segment one wins (as on the Hub). Revisions: a commit,
a branch or tag without ``/``, ``refs/pr/<n>`` or ``refs/convert/<name>``. Anything else is not a route (404).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import quote

from waystation_common.huggingface import (
    DATASETS,
    RESOLVE,
    Kind,
    is_commit,
    prefix,
    split_revision,
    valid_repo_id,
)

API_KINDS: dict[str, Kind] = {'models': 'model', 'datasets': 'dataset'}


class Op(Enum):
    INFO = 'info'
    TREE = 'tree'
    RESOLVE = 'resolve'


@dataclass(frozen=True)
class Route:
    op: Op
    kind: Kind
    repo_id: str
    revision: str | None  # INFO without a revision: the default branch
    path: str = ''  # TREE: the folder ('' = root); RESOLVE: the file

    @property
    def immutable(self) -> bool:
        return self.revision is not None and is_commit(self.revision)


def _api(kind: Kind, rest: list[str]) -> Route | None:
    for size in (2, 1):
        if len(rest) < size:
            continue
        repo_id, tail = '/'.join(rest[:size]), rest[size:]
        if not valid_repo_id(repo_id):
            continue
        if not tail:
            return Route(Op.INFO, kind, repo_id, None)
        found = split_revision(tail[1:])
        if found is None:
            continue
        revision, more = found
        if tail[0] == 'revision' and not more:
            return Route(Op.INFO, kind, repo_id, revision)
        if tail[0] == 'tree':
            return Route(Op.TREE, kind, repo_id, revision, '/'.join(more))
    return None


def parse_route(path: str) -> Route | None:
    parts = path.split('/')
    if parts[0] == 'api':
        api_kind = API_KINDS.get(parts[1]) if len(parts) > 2 else None
        return _api(api_kind, parts[2:]) if api_kind is not None else None
    kind: Kind = 'model'
    if parts[0] == DATASETS:
        kind, parts = 'dataset', parts[1:]
    for size in (2, 1):
        if len(parts) > size + 2 and parts[size] == RESOLVE:
            repo_id = '/'.join(parts[:size])
            found = split_revision(parts[size + 1 :]) if valid_repo_id(repo_id) else None
            if found is not None and found[1]:
                return Route(Op.RESOLVE, kind, repo_id, found[0], '/'.join(found[1]))
    return None


# ---------------------------------------------------------------------------- keys and upstream URLs
def info_key(kind: Kind, repo_id: str, revision: str | None) -> str:
    base = f'api/{kind}s/{repo_id}'
    return f'{base}/revision/{revision}' if revision is not None else base


def info_upstream(kind: Kind, repo_id: str, revision: str | None) -> str:
    """Repository info with every file's size, blob id and LFS sha256 (``blobs=true``)."""
    base = f'api/{kind}s/{quote(repo_id)}'
    if revision is not None:
        base += f'/revision/{quote(revision, safe="")}'
    return f'{base}?blobs=true'


def tree_query(recursive: bool, expand: bool, cursor: str | None, limit: int | None) -> str:
    query = f'recursive={str(recursive).lower()}&expand={str(expand).lower()}'
    if limit is not None:
        query += f'&limit={limit}'
    if cursor:
        query += f'&cursor={quote(cursor, safe="")}'
    return query


def tree_key(route: Route, query: str) -> str:
    folder = f'/{route.path}' if route.path else ''
    return f'api/{route.kind}s/{route.repo_id}/tree/{route.revision}{folder}?{query}'


def tree_upstream(route: Route, query: str) -> str:
    folder = f'/{quote(route.path)}' if route.path else ''
    return f'api/{route.kind}s/{quote(route.repo_id)}/tree/{quote(route.revision or "main", safe="")}{folder}?{query}'


def resolve_upstream(kind: Kind, repo_id: str, commit: str, path: str) -> str:
    return f'{prefix(kind)}{quote(repo_id)}/{RESOLVE}/{commit}/{quote(path)}'
