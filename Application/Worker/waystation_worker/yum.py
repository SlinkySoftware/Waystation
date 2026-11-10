"""Hosted yum repodata generation (job ``index.yum``; FORMATS.md "yum / dnf"; ARCHITECTURE §10).

The server records each uploaded RPM in ``yum_packages`` (with its ``primary``/``filelists``/``other`` XML entries,
rendered from the package header at upload) and, in the same transaction, puts an ``index.yum`` job in the outbox
(deletes likewise). This job rebuilds the repository's whole repodata, createrepo style, and publishes it atomically:

1. records whose package file is gone (a cleanup policy removed it) are dropped; the set of packages is fixed;
2. ``repodata/<sha256>-primary.xml.gz``, ``-filelists.xml.gz`` and ``-other.xml.gz`` are streamed (gzip, in a
   thread per batch) into the blob store and published as new assets: nothing refers to them yet;
3. ``repodata/repomd.xml`` (checksums and sizes of the three files, compressed and open) is signed with the
   repository's key (``waystation_common.signing``: created on first use) into ``repomd.xml.asc``, and both, plus the
   public key (``RPM-GPG-KEY`` at the repository root and ``repodata/repomd.xml.key``), are switched in one MongoDB
   transaction: clients see the old or the new set, never a mix;
4. repodata files of older generations are removed, except the previous one (a client that fetched the previous
   ``repomd.xml`` moments ago can still get its files).

Uploads that land while a rebuild runs set the job's dirty flag, so they are always picked up by one more run.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from waystation_common.db import yum as yum_db
from waystation_common.db.client import Database, Document, Session
from waystation_common.signing import KeyStore, RepoKey
from waystation_common.storage import BlobStore, BlobWriter

KINDS = ('primary', 'filelists', 'other')
HEADS = {
    'primary': '<metadata xmlns="http://linux.duke.edu/metadata/common" '
    'xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="{n}">\n',
    'filelists': '<filelists xmlns="http://linux.duke.edu/metadata/filelists" packages="{n}">\n',
    'other': '<otherdata xmlns="http://linux.duke.edu/metadata/other" packages="{n}">\n',
}
TAILS = {'primary': '</metadata>\n', 'filelists': '</filelists>\n', 'other': '</otherdata>\n'}
XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'
REPOMD = 'repodata/repomd.xml'
SIGNATURE = 'repodata/repomd.xml.asc'
KEY_PATHS = ('RPM-GPG-KEY', 'repodata/repomd.xml.key')
_HREF = re.compile(r'<location href="(repodata/[^"]+)"')


@dataclass(frozen=True)
class IndexResult:
    packages: int
    revision: int
    repomd_sha256: str
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            'packages': self.packages,
            'revision': self.revision,
            'repomd_sha256': self.repomd_sha256,
            'key': self.fingerprint,
        }


class _Stream:
    """One repodata file: gzip-compressed into a blob writer, hashing the open (uncompressed) bytes too."""

    def __init__(self, writer: BlobWriter) -> None:
        self.writer = writer
        self.gz = zlib.compressobj(6, zlib.DEFLATED, 31)  # gzip container
        self.open_sha = hashlib.sha256()
        self.open_size = 0

    def compress(self, text: str) -> bytes:
        data = text.encode('utf-8')
        self.open_sha.update(data)
        self.open_size += len(data)
        return self.gz.compress(data)

    def finish(self) -> bytes:
        return self.gz.flush()


@dataclass(frozen=True)
class _File:
    kind: str
    sha256: str
    size: int
    open_sha256: str
    open_size: int

    @property
    def href(self) -> str:
        return f'repodata/{self.sha256}-{self.kind}.xml.gz'


def _fragments(docs: list[Document]) -> dict[str, str]:
    """The XML entries of a batch of records, by repodata file."""
    out = {kind: [] for kind in KINDS}  # type: dict[str, list[str]]
    for doc in docs:
        meta = json.loads(gzip.decompress(bytes(doc['meta'])))
        for kind in KINDS:
            out[kind].append(str(meta[kind]))
    return {kind: ''.join(parts) for kind, parts in out.items()}


def repomd_xml(files: list[_File], revision: int) -> bytes:
    data = ''.join(
        f'<data type="{f.kind}">'
        f'<checksum type="sha256">{f.sha256}</checksum>'
        f'<open-checksum type="sha256">{f.open_sha256}</open-checksum>'
        f'<location href="{f.href}"/>'
        f'<timestamp>{revision}</timestamp>'
        f'<size>{f.size}</size>'
        f'<open-size>{f.open_size}</open-size>'
        '</data>\n'
        for f in files
    )
    return (
        f'{XML_DECL}<repomd xmlns="http://linux.duke.edu/metadata/repo" xmlns:rpm="http://linux.duke.edu/metadata/rpm">\n'
        f'<revision>{revision}</revision>\n{data}</repomd>\n'
    ).encode()


async def _commit(blobs: BlobStore, data: bytes) -> tuple[str, int]:
    async with await blobs.open_writer(expected_size=len(data)) as writer:
        await writer.write(data)
        ref = await blobs.commit(writer)
    return ref.sha256, ref.size


async def _build(db: Database, blobs: BlobStore, repo_id: str, paths: set[str]) -> list[_File]:
    writers = [await blobs.open_writer() for _ in KINDS]
    try:
        streams = {kind: _Stream(w) for kind, w in zip(KINDS, writers, strict=True)}
        for kind, stream in streams.items():
            await stream.writer.write(stream.compress(XML_DECL + HEADS[kind].format(n=len(paths))))
        async for batch in yum_db.hosted_packages(db, repo_id):
            docs = [d for d in batch if d['path'] in paths]
            if not docs:
                continue
            parts = await asyncio.to_thread(_fragments, docs)
            for kind, stream in streams.items():
                await stream.writer.write(await asyncio.to_thread(stream.compress, parts[kind]))
        files = []
        for kind, stream in streams.items():
            await stream.writer.write(stream.compress(TAILS[kind]) + stream.finish())
            ref = await blobs.commit(stream.writer)
            files.append(_File(kind, ref.sha256, ref.size, stream.open_sha.hexdigest(), stream.open_size))
        return files
    finally:
        for writer in writers:
            await writer.abort()


async def _current_files(db: Database, blobs: BlobStore, repo_id: str) -> set[str]:
    """The repodata files the currently published ``repomd.xml`` lists."""
    asset = await yum_db.get_asset(db, repo_id, REPOMD)
    if asset is None:
        return set()
    try:
        data = await asyncio.to_thread(blobs.blob_path(str(asset['blob_sha256'])).read_bytes)
    except OSError:
        return set()
    return set(_HREF.findall(data.decode('utf-8', 'replace')))


async def rebuild(db: Database, blobs: BlobStore, keys: KeyStore, repo_id: str, *, now: datetime) -> IndexResult | None:
    """Regenerate, sign and publish a hosted yum repository's repodata; None when the repository is gone or is not a
    hosted yum repository (nothing to do: the job succeeds)."""
    repo = await yum_db.repository(db, repo_id)
    if repo is None or repo.get('format') != 'yum' or repo.get('type') != 'hosted':
        return None
    user_id = f'Waystation repository {repo.get("name")} ({repo_id})'
    key: RepoKey = await asyncio.to_thread(keys.load_or_create, repo_id, user_id)

    paths: set[str] = set()
    async for batch in yum_db.hosted_packages(db, repo_id):
        paths.update(str(d['path']) for d in await yum_db.drop_missing(db, repo_id, batch))
    files = await _build(db, blobs, repo_id, paths)
    previous = await _current_files(db, blobs, repo_id)
    deltas = await yum_db.publish(
        db,
        repo_id,
        [yum_db.Published(f.href, f.sha256, f.size, 'application/gzip') for f in files],
        now=now,
    )

    revision = int(now.timestamp())
    repomd = repomd_xml(files, revision)
    signature = (await asyncio.to_thread(key.sign, repomd, created=revision)).encode()
    repomd_sha, repomd_size = await _commit(blobs, repomd)
    asc_sha, asc_size = await _commit(blobs, signature)
    cert_sha, cert_size = await _commit(blobs, key.certificate.encode())
    swap = [
        yum_db.Published(REPOMD, repomd_sha, repomd_size, 'application/xml'),
        yum_db.Published(SIGNATURE, asc_sha, asc_size, 'application/pgp-signature'),
        *(yum_db.Published(path, cert_sha, cert_size, 'application/pgp-keys') for path in KEY_PATHS),
    ]

    async def switch(session: Session) -> list[tuple[int, int]]:
        return await yum_db.publish(db, repo_id, swap, now=now, session=session)

    async with db.client.start_session() as session:
        deltas += await session.with_transaction(switch)
    await yum_db.count_usage(db, repo_id, deltas)

    keep = {f.href for f in files} | previous | {REPOMD, SIGNATURE, KEY_PATHS[1]}
    stale = [p for p in await yum_db.repodata_paths(db, repo_id) if p not in keep]
    await yum_db.remove_assets(db, repo_id, stale)
    return IndexResult(packages=len(paths), revision=revision, repomd_sha256=repomd_sha, fingerprint=key.fingerprint)
