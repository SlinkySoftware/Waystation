"""Hosted Helm index generation (job ``index.helm``; FORMATS.md "Helm"; ARCHITECTURE §10).

The server records each uploaded chart version in ``helm_charts`` and, in the same transaction, puts an
``index.helm`` job in the outbox (deletes likewise). This job rebuilds the repository's whole ``index.yaml`` from
those records and publishes it atomically: the new index is committed to the blob store first, then the
``index.yaml`` asset is switched to it in one document write. Uploads that land while a rebuild runs set the job's
dirty flag, so they are always picked up by one more run. Records whose chart file is gone (removed by a cleanup
policy, which then asks for a rebuild) are dropped.

The index is written as JSON (valid YAML; Helm 3.11+ detects and parses JSON indexes directly, older clients and
other tools read it as YAML): entries by chart name, newest version first, each with the chart's ``Chart.yaml``
fields, ``urls`` (the chart file, relative to the repository), ``digest`` (sha256) and ``created``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from waystation_common.db import helm as helm_db
from waystation_common.db.client import Database, Document
from waystation_common.storage import BlobStore


@dataclass(frozen=True)
class IndexResult:
    charts: int
    versions: int
    size: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {'charts': self.charts, 'versions': self.versions, 'bytes': self.size, 'sha256': self.sha256}


def rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def build_index(docs: Iterable[Document], now: datetime) -> tuple[bytes, int, int]:
    """``(index bytes, charts, versions)`` from ``helm_charts`` records sorted by name, newest version first."""
    entries: dict[str, list[dict[str, Any]]] = {}
    versions = 0
    for doc in docs:
        entry: dict[str, Any] = dict(doc.get('entry') or {})
        entry['name'] = doc['name']
        entry['version'] = doc['version']
        entry['urls'] = [doc['path']]
        if doc.get('digest'):
            entry['digest'] = doc['digest']
        created = doc.get('created')
        if isinstance(created, datetime):
            entry['created'] = rfc3339(created)
        entries.setdefault(str(doc['name']), []).append(entry)
        versions += 1
    index = {'apiVersion': 'v1', 'entries': entries, 'generated': rfc3339(now)}
    body = json.dumps(index, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
    return body, len(entries), versions


async def rebuild(db: Database, blobs: BlobStore, repo_id: str, *, now: datetime) -> IndexResult | None:
    """Regenerate and publish a hosted Helm repository's ``index.yaml``; None when the repository is gone or is not
    a hosted Helm repository (nothing to do: the job succeeds)."""
    repo = await helm_db.repository(db, repo_id)
    if repo is None or repo.get('format') != 'helm' or repo.get('type') != 'hosted':
        return None
    docs = await helm_db.drop_missing(db, repo_id, await helm_db.hosted_entries(db, repo_id))
    body, charts, versions = await asyncio.to_thread(build_index, docs, now)
    async with await blobs.open_writer(expected_size=len(body)) as writer:
        await writer.write(body)
        ref = await blobs.commit(writer)
    await helm_db.publish_index(db, repo_id, sha256=ref.sha256, size=ref.size, now=now)
    return IndexResult(charts=charts, versions=versions, size=ref.size, sha256=ref.sha256)
