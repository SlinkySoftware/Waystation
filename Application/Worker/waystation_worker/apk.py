"""Hosted apk index generation (job ``index.apk``; FORMATS.md "Alpine apk"; ARCHITECTURE §10).

The server records each uploaded ``.apk`` in ``apk_packages`` (with its ``APKINDEX`` stanza, rendered at upload) and,
in the same transaction, puts an ``index.apk`` job in the outbox (deletes likewise). This job rebuilds the
``APKINDEX.tar.gz`` of every ``<branch>/<repository>/<arch>`` directory of the repository from the records:

1. records whose package file is gone (a cleanup policy removed it) are dropped;
2. each directory's index (``DESCRIPTION`` + ``APKINDEX``, tar.gz) is signed with the repository's RSA key
   (``waystation_common.signing``, created on first use; ``.SIGN.RSA256.<key name>``, adr/0007 amendment) and
   committed to the blob store;
3. every index and the public key (``keys/<key name>``, PEM, for ``/etc/apk/keys/``) are switched in one MongoDB
   transaction; a directory left without packages is published with an empty index (``apk update`` keeps working).

Uploads that land while a rebuild runs set the job's dirty flag, so they are always picked up by one more run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from waystation_common import apk
from waystation_common.db import apk as apk_db
from waystation_common.db.client import Database, Session
from waystation_common.signing import KeyStore, RepoKey
from waystation_common.storage import BlobStore


@dataclass(frozen=True)
class IndexResult:
    directories: int
    packages: int
    key: str

    def as_dict(self) -> dict[str, Any]:
        return {'directories': self.directories, 'packages': self.packages, 'key': self.key}


async def _commit(blobs: BlobStore, data: bytes) -> tuple[str, int]:
    async with await blobs.open_writer(expected_size=len(data)) as writer:
        await writer.write(data)
        ref = await blobs.commit(writer)
    return ref.sha256, ref.size


async def rebuild(db: Database, blobs: BlobStore, keys: KeyStore, repo_id: str, *, now: datetime) -> IndexResult | None:
    """Regenerate, sign and publish a hosted apk repository's indexes; None when the repository is gone or is not a
    hosted apk repository (nothing to do: the job succeeds)."""
    repo = await apk_db.repository(db, repo_id)
    if repo is None or repo.get('format') != 'apk' or repo.get('type') != 'hosted':
        return None
    name = str(repo.get('name'))
    user_id = f'Waystation repository {name} ({repo_id})'
    key: RepoKey = await asyncio.to_thread(keys.load_or_create, repo_id, user_id)
    signer_name = apk.key_name(name, repo_id)
    mtime = int(now.timestamp())

    files: list[apk_db.Published] = []
    total = 0
    for directory in sorted(set(await apk_db.hosted_dirs(db, repo_id)) | set(await apk_db.published_dirs(db, repo_id))):
        records = await apk_db.hosted_records(db, repo_id, directory)
        total += len(records)
        stanzas = [str(r['stanza']) for r in records]
        description = f'{name} {directory} (Waystation)'
        index = await asyncio.to_thread(
            apk.build_index, stanzas, description, key_name=signer_name, sign=key.sign_pkcs1_sha256, mtime=mtime
        )
        sha, size = await _commit(blobs, index)
        files.append(apk_db.Published(f'{directory}/{apk.INDEX_NAME}', sha, size, 'application/gzip'))
    pem_sha, pem_size = await _commit(blobs, key.public_pem)
    files.append(apk_db.Published(f'{apk.KEYS_DIR}/{signer_name}', pem_sha, pem_size, 'application/x-pem-file'))

    async def switch(session: Session) -> list[tuple[int, int]]:
        return await apk_db.publish(db, repo_id, files, now=now, session=session)

    async with db.client.start_session() as session:
        deltas = await session.with_transaction(switch)
    await apk_db.count_usage(db, repo_id, deltas)
    return IndexResult(directories=len(files) - 1, packages=total, key=signer_name)
