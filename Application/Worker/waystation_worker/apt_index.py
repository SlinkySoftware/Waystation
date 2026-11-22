"""Hosted apt index generation (job ``index.apt``; FORMATS.md "apt"; ARCHITECTURE §10).

The server records each uploaded ``.deb`` in ``apt_hosted_packages`` (with its ``Packages`` paragraph, rendered at
upload) and, in the same transaction, puts an ``index.apt`` job in the outbox (deletes likewise). This job rebuilds
the repository's distribution, as ``apt-ftparchive``/``reprepro`` would, and publishes it atomically:

1. per component and architecture (the configured ones plus those of uploaded packages; ``Architecture: all``
   packages are listed under every architecture), ``Packages``, ``Packages.gz`` and ``Packages.xz`` are streamed
   (compressed in a thread per batch) into the blob store; records whose package file is gone (a cleanup policy
   removed it) are dropped on the way;
2. each index is published at its ``by-hash/SHA256/<hash>`` path: nothing refers to it yet;
3. ``Release`` (``Acquire-By-Hash: yes``, ``MD5Sum``/``SHA256`` of every index) is signed with the repository's key
   (``waystation_common.signing``, created on first use) into ``InRelease`` (cleartext-signed) and ``Release.gpg``
   (detached); ``Release``, ``InRelease``, ``Release.gpg``, the indexes at their plain paths and the public key
   (``public.asc``, ``public.gpg``) are switched in one MongoDB transaction: clients see the old or the new set,
   never a mix (apt fetches the indexes by hash, so a client holding the previous ``InRelease`` still gets matching
   files);
4. ``dists/`` files of older generations are removed, except the previous generation's ``by-hash`` files; the
   browse listing (``apt_packages``) is replaced by the new indexes'.

Uploads that land while a rebuild runs set the job's dirty flag, so they are always picked up by one more run.
"""

from __future__ import annotations

import asyncio
import hashlib
import lzma
import posixpath
import re
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from waystation_common.db import apt_hosted as apt_db
from waystation_common.db import published as published_db
from waystation_common.db.client import Database, Document, Session
from waystation_common.signing import KeyStore, RepoKey
from waystation_common.storage import BlobStore, BlobWriter

KEY_ASC, KEY_GPG = 'public.asc', 'public.gpg'
ENCODINGS = (('', 'text/plain; charset=utf-8'), ('.gz', 'application/gzip'), ('.xz', 'application/x-xz'))
TEXT_TYPE = 'text/plain; charset=utf-8'
DEFAULT_COMPONENTS = ('main',)
DEFAULT_ARCHITECTURES = ('amd64', 'arm64')
_DIST = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
_COMPONENT = re.compile(r'^[a-z0-9][a-z0-9.+-]{0,31}$')
_ARCH = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')


@dataclass(frozen=True)
class IndexResult:
    packages: int
    distribution: str
    indexes: int
    release_sha256: str
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            'packages': self.packages,
            'distribution': self.distribution,
            'indexes': self.indexes,
            'release_sha256': self.release_sha256,
            'key': self.fingerprint,
        }


@dataclass(frozen=True)
class Options:
    distribution: str
    components: tuple[str, ...]
    architectures: tuple[str, ...]

    @classmethod
    def of(cls, raw: Any) -> Options | None:
        """The repository's options (validated by the server; anything unexpected here means nothing to build)."""
        opts = raw if isinstance(raw, dict) else {}
        dist = opts.get('distribution')
        if not isinstance(dist, str) or not _DIST.match(dist):
            return None
        components = tuple(c for c in opts.get('components') or DEFAULT_COMPONENTS if _COMPONENT.match(str(c)))
        archs = tuple(
            a for a in opts.get('architectures') or DEFAULT_ARCHITECTURES if _ARCH.match(str(a)) and a != 'all'
        )
        return cls(dist, components or DEFAULT_COMPONENTS, archs or DEFAULT_ARCHITECTURES)


@dataclass(frozen=True)
class IndexFile:
    rel: str  # below dists/<distribution>/, e.g. main/binary-amd64/Packages.xz
    sha256: str
    size: int
    md5: str
    content_type: str

    def by_hash(self, dist: str) -> str:
        return f'dists/{dist}/{posixpath.dirname(self.rel)}/by-hash/SHA256/{self.sha256}'


@dataclass
class _Index:
    """One ``binary-<arch>/Packages`` in its three encodings, streamed into blob writers."""

    rel: str  # <component>/binary-<arch>/Packages
    writers: list[BlobWriter]
    md5s: list[Any] = field(default_factory=lambda: [hashlib.md5(usedforsecurity=False) for _ in ENCODINGS])
    gz: Any = field(default_factory=lambda: zlib.compressobj(9, zlib.DEFLATED, 31))  # gzip, mtime 0
    xz: Any = field(default_factory=lambda: lzma.LZMACompressor(format=lzma.FORMAT_XZ))
    listed: list[apt_db.Listed] = field(default_factory=list)

    def encode(self, text: str) -> list[bytes]:
        data = text.encode('utf-8')
        return self._hashed([data, self.gz.compress(data), self.xz.compress(data)])

    def finish(self) -> list[bytes]:
        return self._hashed([b'', self.gz.flush(), self.xz.flush()])

    def _hashed(self, chunks: list[bytes]) -> list[bytes]:
        for md5, chunk in zip(self.md5s, chunks, strict=True):
            md5.update(chunk)
        return chunks

    async def write(self, chunks: list[bytes]) -> None:
        for writer, chunk in zip(self.writers, chunks, strict=True):
            if chunk:
                await writer.write(chunk)


def _listed(doc: Document) -> apt_db.Listed:
    return apt_db.Listed(
        filename=str(doc['path']),
        sha256=str(doc['sha256']),
        size=int(doc['size']),
        name=str(doc['name']),
        version=str(doc['version']),
        architecture=str(doc['architecture']),
        section=doc.get('section'),
    )


async def _component(
    db: Database, blobs: BlobStore, repo_id: str, component: str, archs: Sequence[str]
) -> tuple[list[IndexFile], list[_Index], int]:
    """Build the ``Packages`` files of one component (one per architecture); returns them, the indexes (with their
    browse listings) and how many packages the component holds."""
    indexes: dict[str, _Index] = {}
    try:
        for arch in archs:
            indexes[arch] = _Index(
                f'{component}/binary-{arch}/Packages', [await blobs.open_writer() for _ in ENCODINGS]
            )
        count = 0
        async for batch in apt_db.records(db, repo_id, component):
            docs = await apt_db.drop_missing(db, repo_id, batch)
            texts: dict[str, list[str]] = {}
            for doc in docs:
                arch = str(doc['architecture'])
                targets = list(indexes) if arch == 'all' else [arch] if arch in indexes else []
                for target in targets:
                    texts.setdefault(target, []).append(str(doc['stanza']) + '\n')
                    indexes[target].listed.append(_listed(doc))
                count += 1 if targets else 0
            for arch, parts in texts.items():
                index = indexes[arch]
                await index.write(await asyncio.to_thread(index.encode, ''.join(parts)))
        files: list[IndexFile] = []
        for index in indexes.values():
            await index.write(await asyncio.to_thread(index.finish))
            for (suffix, content_type), writer, md5 in zip(ENCODINGS, index.writers, index.md5s, strict=True):
                ref = await blobs.commit(writer)
                files.append(IndexFile(index.rel + suffix, ref.sha256, ref.size, md5.hexdigest(), content_type))
        return files, list(indexes.values()), count
    finally:
        for index in indexes.values():
            for writer in index.writers:
                await writer.abort()


def release_text(
    name: str, opts: Options, components: Sequence[str], archs: Sequence[str], files: Sequence[IndexFile], now: datetime
) -> str:
    """The ``Release`` file (Debian's field order; sizes right-aligned as ``apt-ftparchive`` writes them)."""
    width = max((len(str(f.size)) for f in files), default=1)
    lines = [
        'Origin: Waystation',
        f'Label: {name}',
        f'Suite: {opts.distribution}',
        f'Codename: {opts.distribution}',
        f'Date: {now.astimezone(UTC).strftime("%a, %d %b %Y %H:%M:%S UTC")}',
        f'Architectures: {" ".join(archs)}',
        f'Components: {" ".join(components)}',
        f'Description: Waystation hosted repository {name}',
        'Acquire-By-Hash: yes',
        'MD5Sum:',
        *(f' {f.md5} {f.size:>{width}} {f.rel}' for f in files),
        'SHA256:',
        *(f' {f.sha256} {f.size:>{width}} {f.rel}' for f in files),
    ]
    return '\n'.join(lines) + '\n'


def by_hash_paths(release: str, dist: str) -> set[str]:
    """The ``by-hash/SHA256`` paths of the indexes a (previously published) ``Release`` lists."""
    out: set[str] = set()
    section = ''
    for line in release.split('\n'):
        if line and not line[0].isspace():
            section = line.split(':', 1)[0]
            continue
        parts = line.split()
        if section == 'SHA256' and len(parts) == 3 and re.fullmatch(r'[0-9a-f]{64}', parts[0]):
            out.add(f'dists/{dist}/{posixpath.dirname(parts[2])}/by-hash/SHA256/{parts[0]}')
    return out


async def _previous(db: Database, blobs: BlobStore, repo_id: str, dist: str) -> set[str]:
    asset = await published_db.get_asset(db, repo_id, f'dists/{dist}/Release')
    if asset is None:
        return set()
    try:
        data = await asyncio.to_thread(blobs.blob_path(str(asset['blob_sha256'])).read_bytes)
    except OSError:
        return set()
    return by_hash_paths(data.decode('utf-8', 'replace'), dist)


async def _commit(blobs: BlobStore, data: bytes) -> tuple[str, int]:
    async with await blobs.open_writer(expected_size=len(data)) as writer:
        await writer.write(data)
        ref = await blobs.commit(writer)
    return ref.sha256, ref.size


async def rebuild(db: Database, blobs: BlobStore, keys: KeyStore, repo_id: str, *, now: datetime) -> IndexResult | None:
    """Regenerate, sign and publish a hosted apt repository's ``dists/``; None when the repository is gone or is not
    a hosted apt repository (nothing to do: the job succeeds)."""
    repo = await apt_db.repository(db, repo_id)
    if repo is None or repo.get('format') != 'apt' or repo.get('type') != 'hosted':
        return None
    opts = Options.of(repo.get('format_options'))
    if opts is None:
        return None
    name = str(repo.get('name'))
    key: RepoKey = await asyncio.to_thread(keys.load_or_create, repo_id, f'Waystation repository {name} ({repo_id})')
    dist = opts.distribution

    present = await apt_db.components(db, repo_id)
    components = [*opts.components, *(c for c in present if c not in opts.components and _COMPONENT.match(c))]
    present_archs = await apt_db.architectures(db, repo_id)
    archs = [*opts.architectures, *(a for a in present_archs if a not in opts.architectures and a != 'all')]
    files: list[IndexFile] = []
    indexes: list[_Index] = []
    packages = 0
    for component in components:
        built, built_indexes, count = await _component(db, blobs, repo_id, component, archs)
        files += built
        indexes += built_indexes
        packages += count

    deltas = await published_db.publish(
        db, repo_id, [published_db.Published(f.by_hash(dist), f.sha256, f.size, f.content_type) for f in files], now=now
    )
    previous = await _previous(db, blobs, repo_id, dist)

    revision = int(now.timestamp())
    text = release_text(name, opts, components, archs, files, now)
    release = text.encode('utf-8')
    inrelease = (await asyncio.to_thread(key.clearsign, text, created=revision)).encode('utf-8')
    signature = (await asyncio.to_thread(key.sign, release, created=revision)).encode('ascii')
    release_sha, release_size = await _commit(blobs, release)
    in_sha, in_size = await _commit(blobs, inrelease)
    gpg_sha, gpg_size = await _commit(blobs, signature)
    asc_sha, asc_size = await _commit(blobs, key.certificate.encode('ascii'))
    bin_sha, bin_size = await _commit(blobs, key.certificate_binary)
    base = f'dists/{dist}'
    swap = [
        *(published_db.Published(f'{base}/{f.rel}', f.sha256, f.size, f.content_type) for f in files),
        published_db.Published(f'{base}/Release', release_sha, release_size, TEXT_TYPE),
        published_db.Published(f'{base}/InRelease', in_sha, in_size, TEXT_TYPE),
        published_db.Published(f'{base}/Release.gpg', gpg_sha, gpg_size, 'application/pgp-signature'),
        published_db.Published(KEY_ASC, asc_sha, asc_size, 'application/pgp-keys'),
        published_db.Published(KEY_GPG, bin_sha, bin_size, 'application/pgp-keys'),
    ]

    async def switch(session: Session) -> list[tuple[int, int]]:
        return await published_db.publish(db, repo_id, swap, now=now, session=session)

    async with db.client.start_session() as session:
        deltas += await session.with_transaction(switch)
    await published_db.count_usage(db, repo_id, deltas)

    keep = {f.by_hash(dist) for f in files} | previous | {p.path for p in swap}
    stale = [p for p in await apt_db.dists_paths(db, repo_id) if p not in keep]
    await published_db.remove_assets(db, repo_id, stale)

    for index in indexes:
        component, _, arch = posixpath.dirname(index.rel).partition('/binary-')
        gen = next(f.sha256 for f in files if f.rel == index.rel)
        await apt_db.record_listing(
            db, repo_id, index.listed, suite=dist, component=component, arch=arch, gen=gen, now=now
        )
    await apt_db.drop_listings(db, repo_id, suite=dist, components=components, archs=archs)
    return IndexResult(
        packages=packages,
        distribution=dist,
        indexes=len(files),
        release_sha256=release_sha,
        fingerprint=key.fingerprint,
    )
