"""Built-in job handlers and the schedules every deployment gets (seeded into ``job_definitions`` once)."""

from __future__ import annotations

import logging
from typing import Any

from waystation_common.db import galaxy as galaxy_db
from waystation_common.db import jobs as jobs_db
from waystation_common.db.blobs import MongoBlobGuard
from waystation_common.db.jobs import Definition
from waystation_common.events import keys
from waystation_common.events.schema import ActivityEvent, EventType
from waystation_common.jobs.model import (
    BlobGc,
    BlobVerify,
    CleanupRun,
    GalaxyImport,
    HelmIndex,
    JobEnvelope,
    QuotaReconcile,
    StatsRollup,
    StorageCheck,
    WebhookDeliver,
    WebhookEvent,
    WebhookSweep,
)
from waystation_common.storage import BlobStore
from waystation_worker import cleanup, galaxy, gc, helm, quota, rollup, verify, webhooks
from waystation_worker.jobs.context import JobContext
from waystation_worker.jobs.registry import handles

BUILTIN_DEFINITIONS: tuple[Definition, ...] = (
    Definition(id='stats.rollup', kind=StatsRollup.kind, schedule='@every 10s', queue='stats'),
    # Blob GC every 6 hours: a blob unreferenced for the grace period (24 h) goes within about 30 hours.
    Definition(
        id='blob.gc', kind=BlobGc.kind, schedule='0 */6 * * *', params={'grace_s': 86400, 'upload_age_s': 86400}
    ),
    # Blob verify weekly (Sunday 03:30 UTC) and after every restore (SPEC §8.5): missing/damaged blobs.
    Definition(id='blob.verify', kind=BlobVerify.kind, schedule='30 3 * * 0', params={'deep': False, 'repair': True}),
    # Quotas (SPEC §4.8): recount usage hourly (the server counts incrementally between runs); low-disk alert.
    Definition(id='quota.reconcile', kind=QuotaReconcile.kind, schedule='7 * * * *'),
    Definition(id='storage.check', kind=StorageCheck.kind, schedule='@every 1m'),
    # Webhook retries that are due (and deliveries whose job was lost), SPEC §4.12.
    Definition(id='webhook.sweep', kind=WebhookSweep.kind, schedule='@every 15s'),
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
    for outcome in outcomes:  # hosted Helm charts removed: their index must drop them (waystation_worker.helm)
        if outcome.format == 'helm' and outcome.removed.items and outcome.repo_id:
            envelope = JobEnvelope.for_payload(HelmIndex(repo_id=outcome.repo_id), now=now)
            await jobs_db.outbox_add(ctx.db, envelope, now=now)
        # Galaxy collection versions whose artifact a policy removed are no longer listed (waystation_worker.galaxy)
        if outcome.format == 'galaxy' and outcome.removed.items and outcome.repo_id:
            await galaxy_db.drop_missing(ctx.db, outcome.repo_id)
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


@handles(BlobVerify)
async def blob_verify(job: BlobVerify, ctx: JobContext) -> dict[str, Any]:
    """Missing or damaged blobs: proxies marked for re-fetch, hosted losses reported (``waystation_worker.verify``)."""
    if ctx.settings is None:
        raise RuntimeError('blob verify needs the worker settings (data directory)')
    store = BlobStore.from_settings(ctx.settings)
    result = await verify.verify(
        ctx.db, store, deep=job.deep, repair=job.repair, now=ctx.now(), progress=ctx.progress, log_line=ctx.log
    )
    await ctx.log(result.summary())
    try:
        async with ctx.valkey.pipeline(transaction=False) as pipe:
            pipe.hincrby(keys.MAINTENANCE_TOTALS, 'verify_runs', 1)
            pipe.hincrby(keys.MAINTENANCE_TOTALS, 'verify_missing_blobs', result.missing_blobs)
            pipe.hincrby(keys.MAINTENANCE_TOTALS, 'verify_damaged_blobs', result.damaged_blobs)
            pipe.hincrby(keys.MAINTENANCE_TOTALS, 'verify_refetch_marked', result.proxy_marked)
            pipe.hincrby(keys.MAINTENANCE_TOTALS, 'verify_hosted_lost', result.hosted_lost)
            await pipe.execute()
    except Exception as exc:  # metrics only
        log.warning('could not record verify totals', extra={'error': repr(exc)})
    if ctx.emitter is not None and (result.hosted_lost or result.proxy_marked):
        lost = ', '.join(f'{name} ({n})' for name, n in sorted(result.lost_repositories.items())[:5])
        summary = (
            f'Blob verify: {result.hosted_lost} hosted assets lost ({lost}); '
            if result.hosted_lost
            else 'Blob verify: '
        ) + f'{result.proxy_marked} cached assets will be re-fetched'
        ctx.emitter.activity(
            ActivityEvent(
                type=EventType.BLOBS_MISSING,
                ts=ctx.now(),
                detail={
                    'kind': BlobVerify.kind,
                    'summary': summary,
                    'hosted_lost': result.hosted_lost,
                    'proxy_marked': result.proxy_marked,
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


@handles(HelmIndex)
async def helm_index(job: HelmIndex, ctx: JobContext) -> dict[str, Any]:
    """Regenerate and publish a hosted Helm repository's ``index.yaml`` (``waystation_worker.helm``)."""
    if ctx.settings is None:
        raise RuntimeError('the Helm index needs the worker settings (data directory)')
    store = BlobStore.from_settings(ctx.settings)
    store.guard = MongoBlobGuard(ctx.db)  # the commit claims the blob against GC (ARCHITECTURE §5)
    result = await helm.rebuild(ctx.db, store, job.repo_id, now=ctx.now())
    if result is None:
        return {'repo_id': job.repo_id, 'skipped': 'not a hosted Helm repository (deleted?)'}
    await ctx.log(f'published index.yaml: {result.charts} charts, {result.versions} versions, {result.size} bytes')
    return {'repo_id': job.repo_id, **result.as_dict()}


@handles(GalaxyImport)
async def galaxy_import(job: GalaxyImport, ctx: JobContext) -> dict[str, Any]:
    """Validate and publish an uploaded Ansible collection (``waystation_worker.galaxy``)."""
    if ctx.settings is None:
        raise RuntimeError('the Galaxy import needs the worker settings (data directory)')
    store = BlobStore.from_settings(ctx.settings)
    store.guard = MongoBlobGuard(ctx.db)  # the artifact is claimed against GC before it is published
    result = await galaxy.run_import(ctx.db, store, job.task_id, now=ctx.now())
    await ctx.log(f'import {result.task_id}: {result.state} ({result.detail})')
    return result.as_dict()


@handles(WebhookEvent)
async def webhook_event(job: WebhookEvent, ctx: JobContext) -> dict[str, Any]:
    """One event -> a delivery per webhook that wants it, each attempted at once (``waystation_worker.webhooks``)."""
    async with webhooks.Sender(ctx.settings) as sender:
        deliverer = webhooks.deliverer(ctx.db, ctx.valkey, ctx.settings, sender, ctx.now)
        return await deliverer.fan_out(
            event_id=job.event_id, event=job.event, repository=job.repository, document=job.document
        )


@handles(WebhookDeliver)
async def webhook_deliver(job: WebhookDeliver, ctx: JobContext) -> dict[str, Any]:
    """Attempt one delivery now (a test event or a redelivery)."""
    async with webhooks.Sender(ctx.settings) as sender:
        deliverer = webhooks.deliverer(ctx.db, ctx.valkey, ctx.settings, sender, ctx.now)
        status = await deliverer.attempt(job.delivery_id)
    return {'delivery_id': job.delivery_id, 'status': status or 'not due'}


@handles(WebhookSweep)
async def webhook_sweep(job: WebhookSweep, ctx: JobContext) -> dict[str, Any]:
    """Attempt the deliveries whose retry is due."""
    async with webhooks.Sender(ctx.settings) as sender:
        return await webhooks.deliverer(ctx.db, ctx.valkey, ctx.settings, sender, ctx.now).sweep()
