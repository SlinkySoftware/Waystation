"""Built-in job handlers and the schedules every deployment gets (seeded into ``job_definitions`` once)."""

from __future__ import annotations

from typing import Any

from waystation_common.db.jobs import Definition
from waystation_common.jobs.model import CleanupRun, StatsRollup
from waystation_worker import cleanup, rollup
from waystation_worker.jobs.context import JobContext
from waystation_worker.jobs.registry import handles

BUILTIN_DEFINITIONS: tuple[Definition, ...] = (
    Definition(id='stats.rollup', kind=StatsRollup.kind, schedule='@every 10s', queue='stats'),
)


@handles(StatsRollup)
async def stats_rollup(job: StatsRollup, ctx: JobContext) -> dict[str, Any]:
    """Closed minutes -> ``stats_minute`` exactly once, plus the stale-transfer sweep (``waystation_worker.rollup``)."""
    inserted = await rollup.rollup_once(ctx.valkey, ctx.db, ctx.worker_id, now=ctx.clock())
    if inserted is None:
        return {'skipped': 'another rollup holds lease:rollup'}
    return {'minutes': inserted}


@handles(CleanupRun)
async def cleanup_run(job: CleanupRun, ctx: JobContext) -> dict[str, Any]:
    """Apply a cleanup policy to its attached repositories (``waystation_worker.cleanup``)."""
    now = ctx.now()
    policy, outcomes = await cleanup.run_policy(ctx.db, job.policy_id, now=now, progress=ctx.progress, log_line=ctx.log)
    if policy is None:
        return {'policy_id': job.policy_id, 'skipped': 'the policy was deleted', 'removed': 0, 'bytes': 0}
    await cleanup.record(ctx.valkey, ctx.emitter, policy, outcomes, now)
    return cleanup.summarise(policy, outcomes)
