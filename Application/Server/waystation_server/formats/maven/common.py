"""Helpers shared by the Maven proxy, hosted and group handlers: options, version policy, generated responses
(checksum files, merged metadata) and digests of stored files."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from pathlib import Path

from redis.asyncio import Redis
from starlette.requests import Request
from starlette.responses import Response

from waystation_common.errors import WaystationError
from waystation_common.ids import new_id
from waystation_common.leases import hold_lease, release_lease
from waystation_server.dataplane.xaccel import _etag_matches
from waystation_server.formats.maven.descriptor import DESCRIPTOR, MavenOptions
from waystation_server.formats.maven.version import is_snapshot
from waystation_server.proxy.records import StoredAsset
from waystation_server.repos.model import RepoConfig

log = logging.getLogger('waystation.formats.maven')

XML_TYPE = 'text/xml'
TEXT_TYPE = 'text/plain'
HASH_CHUNK = 1024 * 1024
DIGEST_CACHE = 4096
LOCK_TTL_MS = 30_000
LOCK_WAIT_S = 20.0
LOCK_POLL_S = 0.02


class MetadataBusy(WaystationError):  # noqa: N818 - reads as a state
    code = 'metadata_busy'
    status = 503


def options_of(repo: RepoConfig) -> MavenOptions:
    options = DESCRIPTOR.parse_options(repo.format_options)
    assert isinstance(options, MavenOptions)
    return options


def policy_allows(options: MavenOptions, version: str | None) -> bool:
    """Does the repository's version policy admit ``version`` (None: not a versioned path, always)?"""
    if version is None or options.version_policy == 'mixed':
        return True
    return is_snapshot(version) == (options.version_policy == 'snapshot')


def generated(request: Request, data: bytes, content_type: str, *, cache: str = 'HIT') -> Response:
    """A small document built per request (a checksum file, merged metadata): strong ETag, conditional GET, HEAD."""
    etag = f'"sha256:{hashlib.sha256(data).hexdigest()}"'
    headers = {'ETag': etag, 'X-Ws-Cache': cache, 'X-Content-Type-Options': 'nosniff'}
    if_none_match = request.headers.get('if-none-match')
    if if_none_match is not None and _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    if request.method == 'HEAD':
        headers['Content-Length'] = str(len(data))
        return Response(status_code=200, headers=headers, media_type=content_type)
    return Response(content=data, status_code=200, headers=headers, media_type=content_type)


def _hash_file(path: Path, alg: str) -> str:
    digest = hashlib.new(alg)
    with path.open('rb') as fh:
        while chunk := fh.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class Digests:
    """Checksums of stored files: the asset's recorded digests, else hashed from the blob in a thread (kept in a
    small in-process cache by blob and algorithm; blobs never change)."""

    def __init__(self) -> None:
        self._cache: OrderedDict[tuple[str, str], str] = OrderedDict()

    async def of(self, asset: StoredAsset, alg: str, blob_path: Path) -> str:
        known = asset.checksums.get(alg)
        if known:
            return known.lower()
        key = (asset.blob_sha256, alg)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        value = await asyncio.to_thread(_hash_file, blob_path, alg)
        self._cache[key] = value
        while len(self._cache) > DIGEST_CACHE:
            self._cache.popitem(last=False)
        return value


@contextlib.asynccontextmanager
async def artifact_lock(valkey: Redis, repo_id: str, group_id: str, artifact_id: str) -> AsyncIterator[None]:
    """Serialise ``maven-metadata.xml`` regeneration for one groupId:artifactId across replicas (Valkey lease
    ``lock:maven:<repo_id>:<sha1(g:a)>``, 30 s; waits up to 20 s). Valkey down: runs unlocked (regeneration reads
    the complete state each time, so only two regenerations racing can leave an older document)."""
    key = (
        f'lock:maven:{repo_id}:{hashlib.sha1(f"{group_id}:{artifact_id}".encode(), usedforsecurity=False).hexdigest()}'
    )
    owner = new_id()
    held = False
    deadline = time.monotonic() + LOCK_WAIT_S
    try:
        while True:
            if await hold_lease(valkey, owner, key=key, ttl_ms=LOCK_TTL_MS):
                held = True
                break
            if time.monotonic() > deadline:
                raise MetadataBusy('maven-metadata.xml is being updated by another deploy; try again')
            await asyncio.sleep(LOCK_POLL_S)
    except MetadataBusy:
        raise
    except Exception as exc:  # Valkey unavailable: degrade to unlocked
        log.warning('maven metadata lock unavailable', extra={'error': type(exc).__name__})
    try:
        yield
    finally:
        if held:
            try:
                await release_lease(valkey, owner, key=key)
            except Exception as exc:
                log.warning('maven metadata lock release failed', extra={'error': type(exc).__name__})
