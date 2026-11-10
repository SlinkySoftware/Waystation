"""Bulk-seed synthetic cached files for the API list/search scenario (tests/perf/README.md).

Runs inside the perf stack's server image (``docker compose run server python /perf/seed_assets.py``), so it uses
the application's own record model (``StoredAsset``) and search keys (``file_keys``) and writes documents exactly as
the proxy engine would, only in bulk: ``insert_many`` batches of 10 000, unordered, then one ``repo_usage`` update.
The files have no blobs (they are listed and searched, never downloaded).

Layout of the ``count`` files in the repository: ``group-<g>/pkg-<p>/<version>/artifact-<n>.tar.gz`` with 100 files
per package and 100 packages per group, so the repository root lists ``count / 10 000`` folders, a package folder
100 versions, and a name prefix like ``artifact-00012`` matches a few hundred files.
"""

from __future__ import annotations

import argparse
import hashlib
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from pymongo import MongoClient

from waystation_common.db.collections import ASSETS, REPO_USAGE, REPOSITORIES
from waystation_common.ids import new_id
from waystation_common.searchkeys import file_keys
from waystation_common.settings import get_settings
from waystation_server.proxy.records import AssetKind, StoredAsset, UpstreamMeta

BATCH = 10_000
SIZE = 65_536


def asset_path(n: int) -> str:
    return f'group-{n // 10_000:03d}/pkg-{n // 100 % 100:02d}/{n % 100}.0.0/artifact-{n:07d}.tar.gz'


def documents(repo_id: str, upstream: str, count: int, now: datetime) -> Iterator[dict[str, Any]]:
    for n in range(count):
        path = asset_path(n)
        digest = hashlib.sha256(path.encode()).hexdigest()
        asset = StoredAsset(
            id=new_id(),
            repo_id=repo_id,
            path=path,
            kind=AssetKind.CONTENT,
            blob_sha256=digest,
            size=SIZE,
            content_type='application/gzip',
            checksums={'sha256': digest},
            upstream=UpstreamMeta(url=f'{upstream.rstrip("/")}/{path}', fetched_at=now),
            created_at=now,
            updated_at=now,
        )
        yield asset.model_dump(by_alias=True) | file_keys(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--repo', required=True, help='name of an existing (empty) proxy repository')
    parser.add_argument('--count', type=int, required=True)
    args = parser.parse_args(argv)
    settings = get_settings()
    client: MongoClient[dict[str, Any]] = MongoClient(settings.mongo_uri.get_secret_value())
    db = client[settings.mongo_db]
    repo = db[REPOSITORIES].find_one({'name': args.repo})
    if repo is None:
        raise SystemExit(f'seed_assets: no repository {args.repo}')
    repo_id = str(repo['_id'])
    upstream = str((repo.get('proxy') or {}).get('upstream_url') or 'http://perf-origin:8080/')
    existing = db[ASSETS].count_documents({'repo_id': repo_id})
    if existing:
        raise SystemExit(f'seed_assets: {args.repo} already holds {existing} assets')
    started = time.monotonic()
    now = datetime.now(UTC)
    batch: list[dict[str, Any]] = []
    for doc in documents(repo_id, upstream, args.count, now):
        batch.append(doc)
        if len(batch) == BATCH:
            db[ASSETS].insert_many(batch, ordered=False)
            batch = []
    if batch:
        db[ASSETS].insert_many(batch, ordered=False)
    db[REPO_USAGE].update_one(
        {'_id': repo_id},
        {
            '$inc': {'bytes': SIZE * args.count, 'assets': args.count, 'artifacts': args.count},
            '$set': {'updated_at': now},
        },
        upsert=True,
    )
    elapsed = time.monotonic() - started
    print(f'seeded {args.count} assets into {args.repo} in {elapsed:.1f} s')
    client.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
