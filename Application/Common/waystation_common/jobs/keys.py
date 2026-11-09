"""Valkey key names of the job system (DATA_MODEL §2)."""

from __future__ import annotations

GROUP = 'workers'  # the one consumer group of every queue stream
DEAD = 'jobs:dead'  # dead-letter stream (MAXLEN ~ DEAD_MAXLEN)
DEAD_MAXLEN = 10_000
# Hash: dead-letter entry id -> the job id its retry joined (TTL refreshed on every retry), so a repeated retry of the
# same dead letter answers with that job instead of queueing it twice.
DEAD_RETRIED = 'jobs:dead:retried'
DEAD_RETRIED_TTL_S = 7 * 24 * 3600
EPOCH = 'jobs:epoch'  # random token (no TTL); a missing/changed epoch means Valkey lost its data
SCHEDULER_LEASE = 'lease:scheduler'


def stream(queue: str) -> str:
    return f'jobs:{queue}'


def delayed(queue: str) -> str:
    """Sorted set of envelopes with ``run_after`` in the future (score = due time in ms)."""
    return f'jobs:{queue}:delayed'


def dedupe(key: str) -> str:
    return f'job:dedupe:{key}'
