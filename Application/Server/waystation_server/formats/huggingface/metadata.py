"""Hub metadata: repository info documents (``api/<kind>s/<id>/revision/<rev>?blobs=true``) and tree listings.

An info document names the revision's commit (``sha``) and, with ``blobs=true``, every file (``siblings``:
``rfilename``, ``size``, ``blobId`` = the git blob id, and for LFS files ``lfs.sha256`` and ``lfs.size``). That is
everything a file download needs: the commit the content is keyed by, the size and digest it is verified against,
and the ``ETag`` / ``X-Linked-Etag`` / ``X-Linked-Size`` headers huggingface_hub expects from a ``HEAD``.

Documents are checked before they are cached (JSON, a 40-hex ``sha``) and stored without ``xetHash`` fields: a
client that sees one downloads the file with the Xet protocol straight from the Hub's storage, around Waystation.
Parsing runs in a thread (documents of large repositories are megabytes) and is bounded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO

from waystation_common.errors import DigestMismatch, PathRejected, UpstreamError
from waystation_common.huggingface import is_commit
from waystation_common.paths import normalise
from waystation_server.proxy.engine import UpstreamInfo

MAX_INFO = 64 * 1024 * 1024
MAX_TREE = 64 * 1024 * 1024
MAX_FILES = 1_000_000
XET_KEYS = frozenset({'xetHash'})
_HEX40 = frozenset('0123456789abcdef')
CHUNK = 1024 * 1024


def _hex(value: Any, length: int) -> str | None:
    if isinstance(value, str) and len(value) == length and set(value) <= _HEX40:
        return value
    return None


@dataclass(frozen=True)
class FileInfo:
    path: str
    size: int
    blob_id: str | None  # git blob id (sha1 of "blob <size>\\0" + content for regular files)
    sha256: str | None  # LFS files: the content's sha256

    @property
    def lfs(self) -> bool:
        return self.sha256 is not None

    @property
    def etag(self) -> str:
        """What the Hub sends as ``X-Linked-Etag``: the sha256 of an LFS file, else the git blob id."""
        return self.sha256 or self.blob_id or ''


@dataclass(frozen=True)
class Revision:
    commit: str
    files: Mapping[str, FileInfo]
    listed: int  # siblings in the document (files with invalid entries included)

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files.values())


def _file(entry: Any) -> FileInfo | None:
    if not isinstance(entry, dict):
        return None
    path = entry.get('rfilename')
    if not isinstance(path, str) or not path:
        return None
    try:
        if normalise(path) != path:
            return None
    except PathRejected:
        return None
    lfs = entry.get('lfs')
    sha256 = None
    size = entry.get('size')
    if isinstance(lfs, dict):
        sha256 = _hex(lfs.get('sha256'), 64)
        if sha256 is None:
            return None
        size = lfs.get('size', size)
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    blob_id = _hex(entry.get('blobId'), 40)
    if sha256 is None and blob_id is None:
        return None
    return FileInfo(path, size, blob_id, sha256)


def parse_info(data: bytes) -> Revision:
    """The commit and files of an info document; raises ``UpstreamError`` when it is not one."""
    try:
        doc = json.loads(data)
    except ValueError:
        raise UpstreamError('repository info is not valid JSON') from None
    if not isinstance(doc, dict):
        raise UpstreamError('repository info is not a JSON object')
    commit = doc.get('sha')
    if not isinstance(commit, str) or not is_commit(commit):
        raise UpstreamError('repository info has no commit')
    siblings = doc.get('siblings')
    if siblings is None:
        siblings = []
    if not isinstance(siblings, list):
        raise UpstreamError('repository info has an invalid file list')
    if len(siblings) > MAX_FILES:
        raise UpstreamError('repository info lists too many files')
    files: dict[str, FileInfo] = {}
    for entry in siblings:
        found = _file(entry)
        if found is not None:
            files[found.path] = found
    return Revision(commit, files, len(siblings))


def validate_info(data: bytes, info: UpstreamInfo) -> None:
    parse_info(data)


def validate_tree(data: bytes, info: UpstreamInfo) -> None:
    try:
        doc = json.loads(data)
    except ValueError:
        raise UpstreamError('file listing is not valid JSON') from None
    if not isinstance(doc, list):
        raise UpstreamError('file listing is not a JSON array')


def _drop(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop(v) for k, v in value.items() if k not in XET_KEYS}
    if isinstance(value, list):
        return [_drop(v) for v in value]
    return value


def strip_xet(data: bytes, info: UpstreamInfo) -> bytes:
    """The document without ``xetHash`` fields (unchanged bytes when it has none)."""
    if b'"xetHash"' not in data:
        return data
    return json.dumps(_drop(json.loads(data)), separators=(',', ':'), ensure_ascii=False).encode()


def git_blob_check(blob_id: str, size: int) -> Callable[[BinaryIO], None]:
    """A ``CheckedExpected.check``: the file's git blob id (sha1 of ``blob <size>\\0`` + content) is ``blob_id``."""

    def check(file: BinaryIO) -> None:
        digest = hashlib.sha1(f'blob {size}\0'.encode(), usedforsecurity=False)
        file.seek(0)
        while chunk := file.read(CHUNK):
            digest.update(chunk)
        if digest.hexdigest() != blob_id:
            raise DigestMismatch('file does not match its git blob id')

    return check


def rewrite_link(link: str, upstream_url: str, repo_url: str) -> str:
    """A ``Link`` header (tree pages) with the upstream's URLs pointing at the repository instead."""
    base, ours = upstream_url.rstrip('/') + '/', repo_url.rstrip('/') + '/'
    return link.replace(f'<{base}', f'<{ours}').replace('</', f'<{ours}')
