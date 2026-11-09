"""Docker Registry HTTP API v2 / OCI Distribution v1.1 request grammar (FORMATS.md "Docker / OCI").

Pure functions: parse a request path into an operation, validate names, references and digests, choose the
upstream ``Accept`` list, check upstream manifests and parse Docker Hub rate-limit headers. No I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

from waystation_common.errors import DigestMismatch, UpstreamError

# Manifest media types a client can ask for (the order is the canonical upstream Accept order).
OCI_INDEX = 'application/vnd.oci.image.index.v1+json'
OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
DOCKER_LIST = 'application/vnd.docker.distribution.manifest.list.v2+json'
DOCKER_MANIFEST = 'application/vnd.docker.distribution.manifest.v2+json'
MANIFEST_TYPES: tuple[str, ...] = (OCI_INDEX, OCI_MANIFEST, DOCKER_LIST, DOCKER_MANIFEST)
_SHORT = {OCI_INDEX: 'oi', OCI_MANIFEST: 'om', DOCKER_LIST: 'dl', DOCKER_MANIFEST: 'dm'}
# Types an upstream manifest response may carry (never converted, served as is).
SERVABLE_MANIFEST_TYPES = frozenset({*MANIFEST_TYPES, 'application/vnd.oci.artifact.manifest.v1+json'})
# What Waystation itself may label a response with; anything else from upstream is served as octet-stream so
# upstream-chosen types (text/html...) never render on the registry's origin.
SAFE_TYPES = frozenset({*SERVABLE_MANIFEST_TYPES, 'application/json', 'application/octet-stream'})
OCTET = 'application/octet-stream'

MANIFEST_MAX_BYTES = 4 * 1024 * 1024  # OCI distribution: registries should accept manifests up to 4 MiB
TAGS_MAX_BYTES = 16 * 1024 * 1024
TAGS_MAX_N = 10_000

# OCI distribution-spec name and reference grammar.
_COMPONENT = r'[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*'
NAME_RE = re.compile(rf'{_COMPONENT}(?:/{_COMPONENT})*')
TAG_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]{0,127}')
DIGEST_RE = re.compile(r'(sha256:[a-f0-9]{64}|sha512:[a-f0-9]{128})')
NAME_MAX = 255

Op = Literal['base', 'manifest', 'blob', 'tags', 'referrers', 'upload']
UPLOAD_ID_RE = re.compile(r'[0-9a-f]{32}')


@dataclass(frozen=True)
class Operation:
    op: Op
    name: str = ''  # the image name as the client wrote it (after the repository prefix/alias)
    reference: str = ''  # tag or digest (manifests), digest (blobs, referrers), session id (uploads; '' = start)

    @property
    def by_digest(self) -> bool:
        return DIGEST_RE.fullmatch(self.reference) is not None


class RequestRejected(Exception):  # noqa: N818 - reads as an outcome
    """The request does not match the registry API grammar (``code`` is the OCI error code)."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


_OPS = ('manifests', 'blobs', 'tags', 'referrers')


def parse(path: str) -> Operation:
    """``library/nginx/manifests/latest`` → ``Operation('manifest', 'library/nginx', 'latest')``. '' → base."""
    if path == '':
        return Operation('base')
    parts = path.split('/')
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] not in _OPS:
            continue
        name, op, rest = '/'.join(parts[:i]), parts[i], parts[i + 1 :]
        if op == 'tags' and rest == ['list']:
            return Operation('tags', _name(name))
        if op == 'blobs' and rest[:1] == ['uploads'] and len(rest) <= 2:
            session = rest[1] if len(rest) == 2 else ''
            if session and not UPLOAD_ID_RE.fullmatch(session):
                raise RequestRejected('BLOB_UPLOAD_UNKNOWN', 'blob upload unknown to registry', status=404)
            return Operation('upload', _name(name), session)
        if len(rest) != 1:
            continue
        ref = rest[0]
        if op == 'manifests':
            if not (TAG_RE.fullmatch(ref) or DIGEST_RE.fullmatch(ref)):
                raise RequestRejected('MANIFEST_INVALID', 'invalid tag or digest')
            return Operation('manifest', _name(name), ref)
        if op in ('blobs', 'referrers'):
            if not DIGEST_RE.fullmatch(ref):
                raise RequestRejected('DIGEST_INVALID', 'invalid digest')
            return Operation('blob' if op == 'blobs' else 'referrers', _name(name), ref)
    raise RequestRejected('NAME_UNKNOWN', 'repository name not known to registry', status=404)


def _name(name: str) -> str:
    if len(name) > NAME_MAX or not NAME_RE.fullmatch(name):
        raise RequestRejected('NAME_INVALID', 'invalid repository name')
    return name


def upstream_name(name: str, *, docker_hub: bool) -> str:
    """Docker Hub keeps official images under ``library/``: ``nginx`` → ``library/nginx``."""
    return f'library/{name}' if docker_hub and '/' not in name else name


# ---------------------------------------------------------------------------- Accept
def accepted_types(accept_headers: Iterable[str]) -> tuple[str, ...]:
    """The known manifest types the client accepts, in canonical order. No Accept or ``*/*`` = all of them."""
    offered: set[str] = set()
    wildcard = False
    seen_any = False
    for header in accept_headers:
        for item in header.split(','):
            media = item.split(';', 1)[0].strip().lower()
            if not media:
                continue
            seen_any = True
            if media in ('*/*', 'application/*'):
                wildcard = True
            offered.add(media)
    if wildcard or not seen_any:
        return MANIFEST_TYPES
    picked = tuple(t for t in MANIFEST_TYPES if t in offered)
    return picked or MANIFEST_TYPES


def accept_variant(types: tuple[str, ...]) -> str:
    """Asset-key suffix for a tag manifest fetched with a narrower Accept list ('' for the full list)."""
    if types == MANIFEST_TYPES:
        return ''
    return '@accept=' + '+'.join(_SHORT[t] for t in types)


# ---------------------------------------------------------------------------- validation
def media_type(content_type: str | None) -> str:
    return (content_type or '').split(';', 1)[0].strip().lower()


def safe_content_type(content_type: str | None) -> str:
    media = media_type(content_type)
    return media if media in SAFE_TYPES else OCTET


def digest_of(data: bytes, algorithm: str = 'sha256') -> str:
    return f'{algorithm}:{hashlib.new(algorithm, data).hexdigest()}'


def check_manifest(data: bytes, content_type: str | None, upstream_digest: str | None) -> None:
    """Refuse an upstream manifest that is not a JSON object of a manifest media type, or whose bytes do not match
    the upstream's own ``Docker-Content-Digest``. Runs in a thread (engine ``validate``)."""
    media = media_type(content_type)
    if media not in SERVABLE_MANIFEST_TYPES and media != 'application/json':
        raise UpstreamError(f'upstream manifest has an unexpected media type {media or "(none)"}')
    if upstream_digest:
        algorithm = upstream_digest.split(':', 1)[0]
        if algorithm not in ('sha256', 'sha512') or not DIGEST_RE.fullmatch(upstream_digest):
            raise UpstreamError('upstream sent an invalid Docker-Content-Digest')
        if digest_of(data, algorithm) != upstream_digest:
            raise DigestMismatch('manifest does not match the upstream Docker-Content-Digest')
    try:
        doc = json.loads(data)
    except ValueError:
        raise UpstreamError('upstream manifest is not valid JSON') from None
    if not isinstance(doc, dict):
        raise UpstreamError('upstream manifest is not a JSON object')


def check_tags(data: bytes) -> None:
    try:
        doc = json.loads(data)
    except ValueError:
        raise UpstreamError('upstream tag list is not valid JSON') from None
    if not isinstance(doc, dict) or not isinstance(doc.get('tags') or [], list):
        raise UpstreamError('upstream tag list has an unexpected shape')


# ---------------------------------------------------------------------------- tag list paging
def tags_query(params: Mapping[str, str]) -> dict[str, str]:
    """Validated ``n`` / ``last`` of ``GET /v2/<name>/tags/list``."""
    out: dict[str, str] = {}
    n = params.get('n')
    if n is not None:
        if not n.isdigit() or int(n) > TAGS_MAX_N:
            raise RequestRejected('PAGINATION_NUMBER_INVALID', 'invalid number of results requested')
        out['n'] = str(int(n))
    last = params.get('last')
    if last is not None:
        if not TAG_RE.fullmatch(last):
            raise RequestRejected('UNSUPPORTED', 'invalid last tag')
        out['last'] = last
    return out


_LINK_RE = re.compile(r'<([^>]*)>\s*;\s*rel="?next"?')


def next_link(upstream_link: str | None, client_path: str) -> str | None:
    """Rewrite the upstream's ``Link: <...?last=x&n=y>; rel="next"`` to the client-facing tags/list path."""
    if not upstream_link:
        return None
    match = _LINK_RE.search(upstream_link)
    if match is None:
        return None
    query = dict(parse_qsl(urlsplit(match.group(1)).query))
    try:
        clean = tags_query(query)
    except RequestRejected:
        return None
    if 'last' not in clean:
        return None
    return f'<{client_path}?{urlencode(clean)}>; rel="next"'


# ---------------------------------------------------------------------------- rate limits
@dataclass(frozen=True)
class RateLimit:
    limit: int
    remaining: int
    window_s: int | None


def _limit_value(raw: str) -> tuple[int, int | None] | None:
    """``100;w=21600`` → (100, 21600)."""
    head, *params = (p.strip() for p in raw.split(';'))
    if not head.isdigit():
        return None
    window: int | None = None
    for param in params:
        key, _, value = param.partition('=')
        if key.strip() == 'w' and value.strip().isdigit():
            window = int(value.strip())
    return int(head), window


def rate_limit(headers: Mapping[str, str]) -> RateLimit | None:
    """Docker Hub's ``ratelimit-limit`` / ``ratelimit-remaining`` (lower-case header names)."""
    limit_raw, remaining_raw = headers.get('ratelimit-limit'), headers.get('ratelimit-remaining')
    if not limit_raw or not remaining_raw:
        return None
    limit, remaining = _limit_value(limit_raw), _limit_value(remaining_raw)
    if limit is None or remaining is None:
        return None
    return RateLimit(limit=limit[0], remaining=remaining[0], window_s=limit[1] or remaining[1])
