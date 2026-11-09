"""``/api/v1/storage/guard``: the low-disk guard (SPECIFICATION §4.8).

``GET`` (any signed-in user; the UI shows a banner while it is tripped) answers the settings and the live state: free
and total bytes of the data filesystem, the effective floor, whether new writes are refused now and since when the
worker's ``storage.check`` has reported it. ``PUT`` (``settings-admin``) changes the settings; this replica applies
them at once, the others within seconds (``repos.quota.GuardKeeper``). Audited as ``settings.update``.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from waystation_common.db import storage_guard
from waystation_common.events import keys as event_keys
from waystation_server import audit
from waystation_server.api.deps import ServicesDep
from waystation_server.audit import AuditAction
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate
from waystation_server.services import Services

router = APIRouter(tags=['storage'])

PROBLEM = {'content': {'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}}}


class StorageGuardSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')

    min_free_bytes: int = Field(ge=0, le=2**53, description='Refuse new cache fills and uploads below this many bytes')
    min_free_percent: float = Field(
        default=0, ge=0, le=90, description='... or below this percentage of the filesystem (the larger floor wins)'
    )


class StorageGuard(StorageGuardSettings):
    min_free_percent: float
    floor_bytes: int = Field(description='The effective floor: the larger of the two settings')
    free_bytes: int
    total_bytes: int
    low: bool = Field(description='Free space is below the floor: new cache fills and uploads are refused')
    low_since: datetime | None = Field(
        default=None, description='When the worker first reported low disk (null when it has not)'
    )
    updated_at: datetime | None = None


async def _state(svc: Services) -> StorageGuard:
    settings = svc.guard.current
    free, total = await svc.blobs.space()
    floor = settings.floor(total)
    since: datetime | None = None
    try:
        raw = await svc.valkey.get(event_keys.STORAGE_LOW)
        if raw:
            since = datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else str(raw))
    except Exception:  # Valkey down or a malformed value: no "since"
        since = None
    return StorageGuard(
        min_free_bytes=settings.min_free_bytes,
        min_free_percent=settings.min_free_percent,
        floor_bytes=floor,
        free_bytes=free,
        total_bytes=total,
        low=free < floor,
        low_since=since,
        updated_at=settings.updated_at,
    )


@router.get('/storage/guard', response_model=StorageGuard, operation_id='getStorageGuard')
@access.authenticated
async def get_storage_guard(request: Request, svc: ServicesDep) -> StorageGuard:
    return await _state(svc)


@router.put('/storage/guard', response_model=StorageGuard, operation_id='updateStorageGuard', responses={422: PROBLEM})
@access.requires(Action.SETTINGS_ADMIN)
async def update_storage_guard(body: StorageGuardSettings, request: Request, svc: ServicesDep) -> StorageGuard:
    principal = await authenticate(request)
    before = svc.guard.current
    saved = await storage_guard.save(
        svc.db,
        storage_guard.GuardSettings(min_free_bytes=body.min_free_bytes, min_free_percent=body.min_free_percent),
        by=principal.id,
    )
    svc.guard.apply(saved)
    audit.record(
        AuditAction.SETTINGS_UPDATE,
        target=audit.target('settings', 'storage', 'Low-disk guard'),
        changes=audit.diff(
            {'min_free_bytes': before.min_free_bytes, 'min_free_percent': before.min_free_percent},
            {'min_free_bytes': saved.min_free_bytes, 'min_free_percent': saved.min_free_percent},
        ),
    )
    return await _state(svc)
