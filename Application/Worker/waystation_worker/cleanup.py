"""Cleanup runs (SPECIFICATION §4.8): the ``cleanup.run`` job applies one policy to its attached repositories.

Per repository it loads the rows (no cap), plans with ``waystation_common.cleanup.plan_repository`` (the code the
server's preview uses) in a thread, then removes the planned records in batches, reporting progress in ``job_runs``.
Only records go (assets, docker tags and manifests); blobs are reclaimed by blob GC after its grace period, so a
download in flight (or one that read the asset just before) keeps working. Re-running is harmless (idempotent).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from redis.asyncio import Redis

from waystation_common.cleanup import Candidate, Criteria, plan_repository
from waystation_common.db import cleanup as cleanup_db
from waystation_common.db.client import Database, Document
from waystation_common.events import EventEmitter, keys
from waystation_common.events.schema import ActivityEvent, EventType

log = logging.getLogger('waystation.worker.cleanup')

Progress = Callable[[int, int | None, str | None], Awaitable[None]]
LogLine = Callable[[str], Awaitable[None]]


@dataclass
class RepoOutcome:
    name: str
    format: str
    removed: cleanup_db.Removed
    repo_id: str = ''


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.3g} {unit}'
        size /= 1024
    raise AssertionError('unreachable')  # pragma: no cover


async def plan_for(db: Database, repo: Document, criteria: Criteria, now: datetime) -> list[Candidate]:
    data = await cleanup_db.load_repo_data(db, str(repo['_id']), str(repo['format']))
    return await asyncio.to_thread(plan_repository, data, criteria, now)


async def run_policy(
    db: Database,
    policy_id: str,
    *,
    now: datetime,
    progress: Progress | None = None,
    log_line: LogLine | None = None,
) -> tuple[Document | None, list[RepoOutcome]]:
    policy = await cleanup_db.get_policy(db, policy_id)
    if policy is None:
        return None, []
    criteria = Criteria.model_validate(policy.get('criteria') or {})
    fmt = str(policy.get('format', '*'))
    plans: list[tuple[Document, list[Candidate]]] = []
    for repo in await cleanup_db.attached_repositories(db, policy_id):
        if fmt != '*' and repo.get('format') != fmt:
            continue  # attachment checks the format; a stale attachment is skipped
        plans.append((repo, await plan_for(db, repo, criteria, now)))
    total = sum(len(plan) for _, plan in plans)
    done = 0
    outcomes: list[RepoOutcome] = []
    for repo, plan in plans:
        name = str(repo['name'])
        base = done

        async def report(n: int, *, base: int = base, name: str = name) -> None:
            if progress is not None:
                await progress(base + n, total, f'removing from {name}')

        removed = await cleanup_db.remove(db, str(repo['_id']), plan, progress=report)
        done += len(plan)
        outcomes.append(RepoOutcome(name, str(repo['format']), removed, str(repo['_id'])))
        if log_line is not None:
            await log_line(f'{name}: removed {removed.items} of {len(plan)} planned ({human_bytes(removed.bytes)})')
    return policy, outcomes


def summarise(policy: Document, outcomes: list[RepoOutcome]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for o in outcomes:
        for kind, n in o.removed.by_kind.items():
            by_kind[kind] = by_kind.get(kind, 0) + n
    return {
        'policy_id': str(policy['_id']),
        'policy': str(policy.get('name', '')),
        'removed': sum(o.removed.items for o in outcomes),
        'bytes': sum(o.removed.bytes for o in outcomes),
        'by_kind': by_kind,
        'repositories': {o.name: {'removed': o.removed.items, 'bytes': o.removed.bytes} for o in outcomes},
    }


async def record(
    valkey: Redis, emitter: EventEmitter | None, policy: Document, outcomes: list[RepoOutcome], now: datetime
) -> None:
    """Totals for /metrics and one activity event per repository that lost something."""
    removed = sum(o.removed.items for o in outcomes)
    freed = sum(o.removed.bytes for o in outcomes)
    if removed:
        try:
            async with valkey.pipeline(transaction=False) as pipe:
                pipe.hincrby(keys.MAINTENANCE_TOTALS, 'cleanup_removed', removed)
                pipe.hincrby(keys.MAINTENANCE_TOTALS, 'cleanup_bytes', freed)
                await pipe.execute()
        except Exception as exc:  # metrics only
            log.warning('could not record cleanup totals', extra={'error': repr(exc)})
    if emitter is None:
        return
    name = str(policy.get('name', ''))
    for o in outcomes:
        if not o.removed.items:
            continue
        emitter.activity(
            ActivityEvent(
                type=EventType.JOB_FINISHED,
                ts=now,
                repo=o.name,
                format=o.format,
                bytes=o.removed.bytes,
                detail={
                    'kind': 'cleanup.run',
                    'policy_id': str(policy['_id']),
                    'summary': f'Cleanup policy "{name}" removed {o.removed.items} items '
                    f'({human_bytes(o.removed.bytes)}) from {o.name}',
                },
            )
        )
