"""Built-in job handlers and the schedules every deployment gets (seeded into ``job_definitions`` once)."""

from __future__ import annotations

import logging
from typing import Any

from waystation_common.db.jobs import Definition
from waystation_common.events import keys
from waystation_common.events.schema import ActivityEvent, EventType
from waystation_common.jobs.model import BlobGc, CleanupRun, QuotaReconcile, StatsRollup, StorageCheck
from waystation_common.storage import BlobStore
from waystation_worker import cleanup, gc, quota, rollup
from waystation_worker.jobs.context import JobContext
from waystation_worker.jobs.registry import handles

BUILTIN_DEFINITIONS: tuple[Definition, ...] = (
    Definition(id='stats.rollup', kind=StatsRollup.kind, schedule='@every 10s', queue='stats'),
    # Blob GC every 6 hours: a blob unreferenced for the grace period (24 h) goes within about 30 hours.
    Definition(
        id='blob.gc', kind=BlobGc.kind, schedule='0 */6 * * *', params={'grace_s': 86400, 'upload_age_s': 86400}
    ),
    # Quotas (SPEC §4.8): recount usage hourly (the server counts incrementally between runs); low-disk alert.
    Definition(id='quota.reconcile', kind=QuotaReconcile.kind, schedule='7 * * * *'),
    Definition(id='storage.check', kind=StorageCheck.kind, schedule='@every 1m'),
)
log = logging.getLogger('waystation.worker.jobs')


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


@handles(BlobGc)
async def blob_gc(job: BlobGc, ctx: JobContext) -> dict[str, Any]:
    """Mark-and-sweep blob GC and the orphan upload sweep (``waystation_worker.gc``)."""
    if ctx.settings is None:
        raise RuntimeError('blob GC needs the worker settings (data directory)')
    store = BlobStore.from_settings(ctx.settings)
    result = await gc.collect(
        ctx.db, store, grace_s=job.grace_s, upload_age_s=job.upload_age_s, now=ctx.now(), progress=ctx.progress
    )
    await ctx.log(
        f'freed {result.freed_blobs} blobs ({cleanup.human_bytes(result.freed_bytes)}), marked {result.marked}, '
        f'removed {result.uploads_removed} orphan uploads, {result.missing} referenced blobs missing'
    )
    try:
        async with ctx.valkey.pipeline(transaction=False) as pipe:
            pipe.hincrby(keys.MAINTENANCE_TOTALS, 'gc_runs', 1)
            pipe.hincrby(keys.MAINTENANCE_TOTALS, 'gc_freed_blobs', result.freed_blobs)
            pipe.hincrby(keys.MAINTENANCE_TOTALS, 'gc_freed_bytes', result.freed_bytes)
            pipe.hincrby(keys.MAINTENANCE_TOTALS, 'gc_uploads_removed', result.uploads_removed)
            await pipe.execute()
    except Exception as exc:  # metrics only
        log.warning('could not record GC totals', extra={'error': repr(exc)})
    if ctx.emitter is not None and (result.freed_blobs or result.uploads_removed):
        ctx.emitter.activity(
            ActivityEvent(
                type=EventType.JOB_FINISHED,
                ts=ctx.now(),
                bytes=result.freed_bytes,
                detail={
                    'kind': BlobGc.kind,
                    'summary': f'Blob GC freed {result.freed_blobs} blobs ({cleanup.human_bytes(result.freed_bytes)})',
                },
            )
        )
    return result.as_dict()


@handles(QuotaReconcile)
async def quota_reconcile(job: QuotaReconcile, ctx: JobContext) -> dict[str, Any]:
    """Recount repository usage from ``assets`` and announce quota thresholds (``waystation_worker.quota``)."""
    result = await quota.reconcile(ctx.db, ctx.emitter, now=ctx.now())
    await ctx.log(
        f'recounted {result.repositories} repositories; {result.drifted} corrected '
        f'({cleanup.human_bytes(result.drift_bytes)} of drift), {result.announced} quota alerts'
    )
    return result.as_dict()


@handles(StorageCheck)
async def storage_check(job: StorageCheck, ctx: JobContext) -> dict[str, Any]:
    """Free space against the low-disk guard: ``storage.low`` / ``storage.ok`` (``waystation_worker.quota``)."""
    if ctx.settings is None:
        raise RuntimeError('the storage check needs the worker settings (data directory)')
    state = await quota.check_storage(
        ctx.db,
        ctx.valkey,
        BlobStore.from_settings(ctx.settings),
        ctx.emitter,
        default_min_free_bytes=ctx.settings.min_free_bytes,
        now=ctx.now(),
    )
    return state.as_dict()
