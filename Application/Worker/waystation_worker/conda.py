"""Hosted conda repodata generation (job ``index.conda``; FORMATS.md "Conda"; ARCHITECTURE §10).

The server records each uploaded package file in ``conda_packages`` (with the repodata entry made from the package's
``info/index.json``) and, in the same transaction, puts an ``index.conda`` job in the outbox (deletes likewise). This
job rebuilds, per subdir with packages (and always ``noarch``, which conda requires), ``<subdir>/repodata.json`` and
``<subdir>/repodata.json.zst``, then ``channeldata.json`` and the ``conda_names`` summaries. Each file is committed to
the blob store first, then its asset is switched in one document write (clients see the old or the new file).
Subdirs left without packages lose their files (the server then answers an empty repodata). Records whose package
file is gone (removed by a cleanup policy, which then asks for a rebuild) are dropped.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from waystation_common import conda
from waystation_common.conda import repodata
from waystation_common.db import conda as conda_db
from waystation_common.db.client import Database
from waystation_common.storage import BlobStore

JSON_TYPE = 'application/json'
ZST_TYPE = 'application/zstd'
FILES = ('repodata.json', 'repodata.json.zst')


@dataclass(frozen=True)
class IndexResult:
    subdirs: list[str]
    packages: int
    files: int

    def as_dict(self) -> dict[str, Any]:
        return {'subdirs': self.subdirs, 'packages': self.packages, 'files': self.files}


def build(docs: list[dict[str, Any]]) -> tuple[dict[str, bytes], dict[str, dict[str, Any]]]:
    """``(file path -> bytes, name -> summary)`` of a hosted repository's records."""
    by_subdir: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    summaries: dict[str, dict[str, Any]] = {}
    for doc in docs:
        subdir, filename = str(doc['subdir']), str(doc['filename'])
        entry = dict(doc.get('entry') or {})
        by_subdir[subdir][filename] = entry
        name, version = str(doc['name']), str(doc['version'])
        found = summaries.setdefault(name, {'latest': version, 'subdirs': [], 'counts': {}})
        if conda.compare(version, str(found['latest'])) > 0:
            found['latest'] = version
        if version == found['latest']:
            found['license'] = entry.get('license')
            found['summary'] = doc.get('summary')
        if subdir not in found['subdirs']:
            found['subdirs'].append(subdir)
        found['counts'][subdir] = found['counts'].get(subdir, 0) + 1
    by_subdir.setdefault('noarch', {})
    files: dict[str, bytes] = {}
    for subdir, entries in by_subdir.items():
        body = repodata.render(subdir, entries)
        files[f'{subdir}/repodata.json'] = body
        files[f'{subdir}/repodata.json.zst'] = repodata.compress_zst(body)
    channel = {
        name: {
            'subdirs': sorted(s['subdirs']),
            'version': s['latest'],
            **({'license': s['license']} if s.get('license') else {}),
            **({'summary': s['summary']} if s.get('summary') else {}),
        }
        for name, s in summaries.items()
    }
    files['channeldata.json'] = repodata.channeldata(channel, list(by_subdir))
    for summary in summaries.values():
        summary['latest_sort'] = conda.sort_string(str(summary['latest']))
        summary['subdirs'] = sorted(summary['subdirs'])
    return files, summaries


async def rebuild(db: Database, blobs: BlobStore, repo_id: str, *, now: datetime) -> IndexResult | None:
    """Regenerate and publish a hosted conda repository's repodata; None when the repository is gone or is not a
    hosted conda repository (nothing to do: the job succeeds)."""
    repo = await conda_db.repository(db, repo_id)
    if repo is None or repo.get('format') != 'conda' or repo.get('type') != 'hosted':
        return None
    docs = await conda_db.drop_missing(db, repo_id, await conda_db.hosted_entries(db, repo_id))
    files, summaries = await asyncio.to_thread(build, docs)
    for path, body in sorted(files.items()):
        async with await blobs.open_writer(expected_size=len(body)) as writer:
            await writer.write(body)
            ref = await blobs.commit(writer)
        content_type = ZST_TYPE if path.endswith('.zst') else JSON_TYPE
        await conda_db.publish(db, repo_id, path, sha256=ref.sha256, size=ref.size, content_type=content_type, now=now)
    for path in await conda_db.published(db, repo_id, FILES):
        if path not in files:
            await conda_db.unpublish(db, repo_id, path)
    await conda_db.replace_names(db, repo_id, summaries)
    subdirs = sorted({path.split('/', 1)[0] for path in files if '/' in path})
    return IndexResult(subdirs=subdirs, packages=len(docs), files=len(files))
