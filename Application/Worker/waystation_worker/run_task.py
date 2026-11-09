"""Run a scheduled task now from the command line, inside a worker container (no API token needed)::

    docker exec waystation-worker python -m waystation_worker.run_task blob.verify --wait

Enqueues the task like "Run now" on the Tasks page (trigger ``manual``, the task's own parameters and dedupe key)
through the durable job outbox, so it runs on whichever worker picks it up and appears in the Tasks page's run list.
With ``--wait`` it waits for that run to finish, prints its result as JSON and exits 0 when it succeeded, 1 when it
failed, 2 on a usage error or a timeout. The restore procedure uses it to run ``blob.verify`` (BACKUP.md).

A freshly restored database may not have the task yet (it was backed up by an older release): workers seed the
built-in tasks when they start, so the command waits up to ``--timeout`` for it to appear.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from waystation_common.db import jobs as jobs_db
from waystation_common.db.client import Database, create_client, database
from waystation_common.db.migrations import check_schema
from waystation_common.jobs.model import JOB_TYPES, QUEUES, JobEnvelope
from waystation_common.settings import get_settings

POLL_S = 1.0
FINISHED = {'succeeded', 'dead'}


class TaskError(Exception):
    """A usage problem: unknown task, bad parameters, timeout."""


async def enqueue(db: Database, task_id: str, *, now: datetime) -> JobEnvelope:
    doc = await jobs_db.get_definition(db, task_id)
    if doc is None:
        raise LookupError(task_id)
    payload_type = JOB_TYPES.get(str(doc.get('kind') or ''))
    if payload_type is None:
        raise TaskError(f'this version cannot run tasks of kind {doc.get("kind")!r}')
    try:
        payload = payload_type.model_validate(doc.get('params') or {})
    except ValueError:
        raise TaskError(f'task {task_id} has invalid parameters') from None
    queue = doc.get('queue')
    envelope = JobEnvelope.for_payload(
        payload,
        dedupe_key=payload.dedupe_key() or f'def:{task_id}',
        queue=queue if queue in QUEUES else None,
        definition_id=task_id,
        trigger='manual',
        now=now,
    )
    await jobs_db.outbox_add(db, envelope, now=now)
    return envelope


async def finished_run(db: Database, envelope: JobEnvelope) -> dict[str, Any] | None:
    """The first finished run of this request, or of the pending job it merged into (same dedupe key, started after
    the request). A failed attempt that will be retried is not finished; a dead one is."""
    for doc in await jobs_db.runs_by_dedupe(db, envelope.dedupe_key, limit=20):
        started = doc.get('started_at')
        if not isinstance(started, datetime) or _aware(started) < envelope.enqueued_at:
            continue
        if doc.get('status') in FINISHED:
            return doc
    return None


async def run(
    db: Database,
    task_id: str,
    *,
    wait: bool,
    timeout_s: float,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    poll_s: float = POLL_S,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        try:
            envelope = await enqueue(db, task_id, now=clock())
            break
        except LookupError:
            if loop.time() > deadline:
                raise TaskError(f'no scheduled task {task_id!r} (is a worker running?)') from None
            await asyncio.sleep(poll_s)
    out: dict[str, Any] = {'task': task_id, 'job_id': envelope.id, 'status': 'queued'}
    if not wait:
        return out
    while (doc := await finished_run(db, envelope)) is None:
        if loop.time() > deadline:
            raise TaskError(f'task {task_id} did not finish within {timeout_s:.0f} s (it keeps running)')
        await asyncio.sleep(poll_s)
    out.update(status=doc.get('status'), run_id=doc.get('_id'), result=doc.get('result') or {})
    if doc.get('error'):
        out['error'] = doc['error']
    return out


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _main(args: argparse.Namespace) -> int:  # pragma: no cover - process entry point
    settings = get_settings()
    client = create_client(settings, app_name='waystation-run-task')
    try:
        db = database(client, settings)
        await check_schema(db)
        try:
            out = await run(db, args.task, wait=args.wait, timeout_s=args.timeout)
        except TaskError as exc:
            sys.stderr.write(f'run_task: {exc}\n')
            return 2
    finally:
        await client.close()
    sys.stdout.write(json.dumps(out, default=str, indent=2) + '\n')
    return 0 if out['status'] in {'queued', 'succeeded'} else 1


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entry point
    parser = argparse.ArgumentParser(prog='python -m waystation_worker.run_task', description=__doc__.split('\n')[0])
    parser.add_argument('task', help='scheduled task id, e.g. blob.verify or blob.gc')
    parser.add_argument('--wait', action='store_true', help='wait for the run to finish and print its result')
    parser.add_argument('--timeout', type=float, default=3600.0, help='seconds to wait (default 3600)')
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
