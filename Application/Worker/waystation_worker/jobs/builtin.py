"""Built-in job handlers and the schedules every deployment gets (seeded into ``job_definitions`` once)."""

from __future__ import annotations

from typing import Any

from waystation_common.db.jobs import Definition
from waystation_common.jobs.model import StatsRollup
from waystation_worker import rollup
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
