"""Job persistence (DATA_MODEL §1, ARCHITECTURE §10): ``job_outbox``, ``job_runs``, ``job_definitions``, ``workers``.

* ``job_outbox``: durable record of event-driven jobs. ``outbox_add`` joins the caller's transaction (pass its
  session); the scheduler leader dispatches unsent entries to Valkey, records ``sent_at``/``sent_epoch``, and
  re-dispatches in-flight entries (sent, not done) when the Valkey epoch changed or ``sent_at`` is very old. The
  worker that finishes the job marks it ``done_at`` (TTL 7 d).
* ``job_runs``: one document per execution attempt with progress, a capped log and the result.
* ``job_definitions``: scheduled jobs; ``next_run_at`` is advanced with compare-and-set so one fire is enqueued
  once even if two schedulers overlap.
* ``workers``: presence (``heartbeat_at`` TTL 5 min).
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from waystation_common.db.client import Database, Document, Session
from waystation_common.db.collections import JOB_DEFINITIONS, JOB_OUTBOX, JOB_RUNS, WORKERS
from waystation_common.ids import new_id
from waystation_common.jobs.model import JobEnvelope

LOG_LINES = 500
RUN_STATUSES = ('running', 'succeeded', 'failed', 'dead', 'interrupted')


# ---------------------------------------------------------------- outbox
async def outbox_add(db: Database, envelope: JobEnvelope, *, now: datetime, session: Session | None = None) -> str:
    doc = {
        '_id': new_id(),
        'job': envelope.model_dump(mode='json'),
        'queue': envelope.queue,
        'dedupe_key': envelope.dedupe_key,
        'created_at': now,
        'sent_at': None,
        'sent_epoch': None,
        'done_at': None,
    }
    await db[JOB_OUTBOX].insert_one(doc, session=session)
    return str(doc['_id'])


async def outbox_to_dispatch(db: Database, *, epoch: str, stale_before: datetime, limit: int = 100) -> list[Document]:
    """Unsent entries, and in-flight ones sent under another Valkey epoch or longer ago than ``stale_before``."""
    query = {
        '$or': [
            {'sent_at': None, 'done_at': None},
            {'done_at': None, 'sent_at': {'$lt': stale_before}},
            {'done_at': None, 'sent_at': {'$ne': None}, 'sent_epoch': {'$ne': epoch}},
        ]
    }
    return [doc async for doc in db[JOB_OUTBOX].find(query).limit(limit)]


async def outbox_mark_sent(db: Database, outbox_id: str, *, epoch: str, now: datetime) -> None:
    await db[JOB_OUTBOX].update_one(
        {'_id': outbox_id, 'done_at': None}, {'$set': {'sent_at': now, 'sent_epoch': epoch}}
    )


async def outbox_mark_done(db: Database, outbox_ids: Sequence[str], *, now: datetime) -> int:
    if not outbox_ids:
        return 0
    result = await db[JOB_OUTBOX].update_many(
        {'_id': {'$in': list(outbox_ids)}, 'done_at': None}, {'$set': {'done_at': now}}
    )
    return result.modified_count


async def outbox_get(db: Database, outbox_id: str) -> Document | None:
    return await db[JOB_OUTBOX].find_one({'_id': outbox_id})


# ---------------------------------------------------------------- runs
async def run_start(db: Database, doc: Document, *, now: datetime) -> None:
    """Record a new attempt; earlier attempts of the same job still ``running`` lost their worker: interrupted."""
    await db[JOB_RUNS].update_many(
        {'job_id': doc['job_id'], 'status': 'running'},
        {'$set': {'status': 'interrupted', 'finished_at': now, 'error': 'worker lost'}},
    )
    await db[JOB_RUNS].insert_one({'progress': {'done': 0, 'total': None, 'message': None}, 'log': [], **doc})


async def run_progress(db: Database, run_id: str, *, done: int, total: int | None, message: str | None) -> None:
    await db[JOB_RUNS].update_one(
        {'_id': run_id}, {'$set': {'progress': {'done': done, 'total': total, 'message': message}}}
    )


async def run_log(db: Database, run_id: str, lines: Sequence[str]) -> None:
    await db[JOB_RUNS].update_one({'_id': run_id}, {'$push': {'log': {'$each': list(lines), '$slice': -LOG_LINES}}})


async def run_finish(
    db: Database,
    run_id: str,
    *,
    status: str,
    now: datetime,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    update: dict[str, Any] = {'status': status, 'finished_at': now}
    if result is not None:
        update['result'] = result
    if error is not None:
        update['error'] = error
    await db[JOB_RUNS].update_one({'_id': run_id}, {'$set': update})


async def runs_for_job(db: Database, job_id: str) -> list[Document]:
    return [doc async for doc in db[JOB_RUNS].find({'job_id': job_id}).sort('started_at', 1)]


async def recent_runs(db: Database, *, kind: str | None = None, limit: int = 100) -> list[Document]:
    query = {'kind': kind} if kind else {}
    return [doc async for doc in db[JOB_RUNS].find(query, {'log': 0}).sort('started_at', -1).limit(limit)]


# ---------------------------------------------------------------- definitions
@dataclass(frozen=True)
class Definition:
    id: str
    kind: str
    schedule: str
    params: dict[str, Any] = field(default_factory=dict)
    queue: str | None = None
    enabled: bool = True


async def ensure_definitions(db: Database, definitions: Sequence[Definition]) -> None:
    """Create built-in definitions that do not exist yet (operators may change schedule/enabled afterwards)."""
    for d in definitions:
        with contextlib.suppress(DuplicateKeyError):  # another worker inserted it concurrently
            await db[JOB_DEFINITIONS].update_one(
                {'_id': d.id},
                {
                    '$setOnInsert': {
                        'kind': d.kind,
                        'params': d.params,
                        'schedule': d.schedule,
                        'queue': d.queue,
                        'enabled': d.enabled,
                        'next_run_at': None,
                        'last_fired_at': None,
                    }
                },
                upsert=True,
            )


async def enabled_definitions(db: Database) -> list[Document]:
    return [doc async for doc in db[JOB_DEFINITIONS].find({'enabled': True})]


async def advance_definition(
    db: Database, definition_id: str, *, expected: datetime | None, next_run_at: datetime, fired_at: datetime | None
) -> bool:
    """Compare-and-set ``next_run_at``; True only for the caller that moved it (that caller enqueues the fire)."""
    update: dict[str, Any] = {'next_run_at': next_run_at}
    if fired_at is not None:
        update['last_fired_at'] = fired_at
    result = await db[JOB_DEFINITIONS].update_one({'_id': definition_id, 'next_run_at': expected}, {'$set': update})
    return result.modified_count == 1


async def get_definition(db: Database, definition_id: str) -> Document | None:
    return await db[JOB_DEFINITIONS].find_one({'_id': definition_id})


# ---------------------------------------------------------------- workers
async def worker_heartbeat(db: Database, worker_id: str, fields: dict[str, Any], *, now: datetime) -> None:
    await db[WORKERS].update_one({'_id': worker_id}, {'$set': {**fields, 'heartbeat_at': now}}, upsert=True)


async def worker_remove(db: Database, worker_id: str) -> None:
    await db[WORKERS].delete_one({'_id': worker_id})


async def list_workers(db: Database) -> list[Document]:
    return [doc async for doc in db[WORKERS].find({}).sort('started_at', 1)]


async def worker_presence(db: Database, *, alive_since: datetime) -> tuple[int, int]:
    """``(live workers, live scheduler leaders)``: workers whose heartbeat is newer than ``alive_since``."""
    query: dict[str, Any] = {'heartbeat_at': {'$gte': alive_since}}
    live = await db[WORKERS].count_documents(query)
    leaders = await db[WORKERS].count_documents({**query, 'leader': True}) if live else 0
    return live, leaders
