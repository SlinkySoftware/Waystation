"""Job protocol shared by server (producer) and worker (consumer): ARCHITECTURE §10, DATA_MODEL §1-§2.

Server side::

    jobs = JobClient(valkey, db)
    async with await client.start_session() as s, s.start_transaction():
        ...  # the triggering write, with session=s
        await jobs.enqueue_durable(RebuildAptIndex(repo_id=repo.id), session=s)   # outbox, same transaction
    await jobs.enqueue(StatsRollup())                                            # best effort, straight to Valkey

Pieces: ``model`` (typed payloads, ``JobEnvelope``, ``QUEUES``), ``queue`` (the Valkey Streams state machine),
``schedule`` (cron / ``@every``), ``keys`` (Valkey key names), ``client`` (``JobClient``). Persistence lives in
``waystation_common.db.jobs``; the runner, dispatcher and scheduler in ``waystation_worker.jobs``.
"""

from waystation_common.jobs.client import JobClient
from waystation_common.jobs.model import (
    JOB_TYPES,
    QUEUES,
    CleanupRun,
    JobEnvelope,
    JobPayload,
    Queue,
    StatsRollup,
)
from waystation_common.jobs.queue import Delivery, EnqueueResult, JobQueue
from waystation_common.jobs.schedule import parse_schedule

__all__ = [
    'JOB_TYPES',
    'QUEUES',
    'CleanupRun',
    'Delivery',
    'EnqueueResult',
    'JobClient',
    'JobEnvelope',
    'JobPayload',
    'JobQueue',
    'Queue',
    'StatsRollup',
    'parse_schedule',
]
