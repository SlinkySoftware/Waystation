"""Blob garbage collection status and "run now" (SPECIFICATION §4.8; OpenAPI ``getBlobGc``, ``runBlobGc``).

GC itself is the worker job ``blob.gc`` (``waystation_worker.gc``), scheduled by the built-in ``job_definitions``
entry ``blob.gc`` (schedule and grace period are its ``schedule`` and ``params``). Both routes need ``admin``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from waystation_common.db import blobs as blobs_db
from waystation_common.db import jobs as jobs_db
from waystation_common.db.client import Document
from waystation_common.jobs import BlobGc
from waystation_common.jobs.schedule import parse_schedule
from waystation_server.api.cleanup import JobAccepted, JobProgress, RunStatus, run_trigger
from waystation_server.api.deps import ServicesDep
from waystation_server.security import access
from waystation_server.security.access import Action

router = APIRouter(tags=['cleanup'])

DEFINITION_ID = 'blob.gc'
DEFAULT_SCHEDULE = '0 */6 * * *'


class GcRun(BaseModel):
    id: str
    status: RunStatus
    trigger: Literal['schedule', 'manual']
    started_at: datetime
    finished_at: datetime | None
    progress: JobProgress
    freed_blobs: int
    freed_bytes: int
    marked: int = Field(description='Blobs newly found unreferenced (deleted once unreferenced for the grace period)')
    uploads_removed: int
    error: str | None


class GcStatus(BaseModel):
    enabled: bool
    schedule: str
    grace_hours: float = Field(description='A blob is deleted only after being unreferenced this long')
    next_run_at: datetime | None
    pending_blobs: int
    pending_bytes: int
    runs: list[GcRun]


def _run_view(doc: Document) -> GcRun:
    result = doc.get('result') or {}
    progress = doc.get('progress') or {}
    return GcRun(
        id=str(doc['_id']),
        status=doc.get('status', 'running'),
        trigger='schedule' if run_trigger(doc) == 'schedule' else 'manual',
        started_at=doc['started_at'],
        finished_at=doc.get('finished_at'),
        progress=JobProgress(
            done=int(progress.get('done') or 0), total=progress.get('total'), message=progress.get('message')
        ),
        freed_blobs=int(result.get('freed_blobs') or 0),
        freed_bytes=int(result.get('freed_bytes') or 0),
        marked=int(result.get('marked') or 0),
        uploads_removed=int(result.get('uploads_removed') or 0),
        error=doc.get('error'),
    )


def _payload(definition: Document | None) -> BlobGc:
    params = (definition or {}).get('params') or {}
    try:
        return BlobGc.model_validate(params)
    except ValueError:
        return BlobGc()


@router.get('/maintenance/gc', response_model=GcStatus, operation_id='getBlobGc')
@access.requires(Action.ADMIN)
async def get_gc(request: Request, svc: ServicesDep) -> GcStatus:
    definition = await jobs_db.get_definition(svc.db, DEFINITION_ID)
    schedule = str((definition or {}).get('schedule') or DEFAULT_SCHEDULE)
    enabled = bool((definition or {}).get('enabled', True))
    next_run: datetime | None = (definition or {}).get('next_run_at')
    if enabled and next_run is None:
        try:
            next_run = datetime.fromtimestamp(parse_schedule(schedule).next_after(datetime.now(UTC).timestamp()), UTC)
        except ValueError:
            next_run = None
    pending_blobs, pending_bytes = await blobs_db.pending(svc.db)
    runs = await jobs_db.recent_runs(svc.db, kind=BlobGc.kind, limit=20)
    return GcStatus(
        enabled=enabled,
        schedule=schedule,
        grace_hours=round(_payload(definition).grace_s / 3600, 3),
        next_run_at=next_run if enabled else None,
        pending_blobs=pending_blobs,
        pending_bytes=pending_bytes,
        runs=[_run_view(d) for d in runs],
    )


@router.post('/maintenance/gc/run', status_code=202, response_model=JobAccepted, operation_id='runBlobGc')
@access.requires(Action.ADMIN)
async def run_gc(request: Request, svc: ServicesDep) -> JobAccepted:
    definition = await jobs_db.get_definition(svc.db, DEFINITION_ID)
    result = await svc.jobs.enqueue(_payload(definition))
    return JobAccepted(job_id=result.job_id, status=result.status)
