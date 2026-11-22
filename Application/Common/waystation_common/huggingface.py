"""Hugging Face Hub names and cached-file keys (FORMATS.md "Hugging Face"), shared by the server (proxy, browse,
search) and the cleanup planner and component index.

- A **repo id** is ``<name>`` (legacy canonical models: ``gpt2``) or ``<namespace>/<name>``: letters, digits, ``-``,
  ``_``, ``.``; at most 96 characters per part, no leading or trailing ``-``/``.``, no ``--`` or ``..``.
- A **revision** is a commit (40 hex digits), a branch or tag name without ``/``, or ``refs/pr/<n>`` /
  ``refs/convert/<name>`` (clients send them percent-encoded, the router has decoded them).
- A cached **file** has the asset key ``[datasets/]<repo_id>/resolve/<commit>/<path>``: the client's URL with the
  revision resolved to its commit (content addressed by commit and path).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal['model', 'dataset']
DATASETS = 'datasets'
RESOLVE = 'resolve'
COMMIT = re.compile(r'^[0-9a-f]{40}$')
_PART = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$')
_REV = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9._+-]{0,254}$')
_REF_PREFIXES = ('refs/pr/', 'refs/convert/')


def valid_part(part: str) -> bool:
    return bool(_PART.match(part)) and '--' not in part and '..' not in part


def valid_repo_id(repo_id: str) -> bool:
    parts = repo_id.split('/')
    return 1 <= len(parts) <= 2 and all(valid_part(p) for p in parts)


def is_commit(revision: str) -> bool:
    return bool(COMMIT.match(revision))


def valid_revision(revision: str) -> bool:
    for prefix in _REF_PREFIXES:
        if revision.startswith(prefix):
            return bool(_REV.match(revision[len(prefix) :]))
    return bool(_REV.match(revision)) and not revision.endswith('.lock')


def split_revision(parts: list[str]) -> tuple[str, list[str]] | None:
    """A revision at the start of ``parts`` (``refs/pr/1`` takes three segments) and the rest."""
    if len(parts) >= 3 and parts[0] == 'refs' and parts[1] in ('pr', 'convert'):
        revision, rest = '/'.join(parts[:3]), parts[3:]
    elif parts:
        revision, rest = parts[0], parts[1:]
    else:
        return None
    return (revision, rest) if valid_revision(revision) else None


def prefix(kind: Kind) -> str:
    """The URL prefix of a repository type (``datasets/`` for datasets, '' for models)."""
    return f'{DATASETS}/' if kind == 'dataset' else ''


def display_name(kind: Kind, repo_id: str) -> str:
    """How browse and search name a Hub repository: the model id, ``datasets/<id>`` for a dataset."""
    return f'{prefix(kind)}{repo_id}'


def parse_display_name(name: str) -> tuple[Kind, str] | None:
    kind: Kind = 'model'
    if name.startswith(f'{DATASETS}/'):
        kind, name = 'dataset', name[len(DATASETS) + 1 :]
    return (kind, name) if valid_repo_id(name) else None


@dataclass(frozen=True)
class FileKey:
    kind: Kind
    repo_id: str
    commit: str
    path: str

    @property
    def name(self) -> str:
        return display_name(self.kind, self.repo_id)

    @property
    def key(self) -> str:
        return file_key(self.kind, self.repo_id, self.commit, self.path)


def file_key(kind: Kind, repo_id: str, commit: str, path: str) -> str:
    return f'{prefix(kind)}{repo_id}/{RESOLVE}/{commit}/{path}'


def commit_prefix(kind: Kind, repo_id: str, commit: str | None = None) -> str:
    """Asset key prefix of a repository's cached files (of one commit)."""
    base = f'{prefix(kind)}{repo_id}/{RESOLVE}/'
    return f'{base}{commit}/' if commit else base


def parse_file_key(path: str) -> FileKey | None:
    """``[datasets/]<repo_id>/resolve/<commit>/<path>`` → its parts; None for any other asset key."""
    kind: Kind = 'model'
    parts = path.split('/')
    if parts[0] == DATASETS:
        kind, parts = 'dataset', parts[1:]
    for size in (2, 1):
        if len(parts) > size + 2 and parts[size] == RESOLVE and is_commit(parts[size + 1]):
            repo_id = '/'.join(parts[:size])
            if valid_repo_id(repo_id):
                return FileKey(kind, repo_id, parts[size + 1], '/'.join(parts[size + 2 :]))
    return None


__all__ = [
    'COMMIT',
    'FileKey',
    'Kind',
    'commit_prefix',
    'display_name',
    'file_key',
    'is_commit',
    'parse_display_name',
    'parse_file_key',
    'prefix',
    'split_revision',
    'valid_repo_id',
    'valid_revision',
]
