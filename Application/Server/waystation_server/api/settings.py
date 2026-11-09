"""``/api/v1/settings`` and ``/api/v1/system`` (SPECIFICATION §4.12 "Settings", "System").

- ``GET/PUT /settings`` (``settings-admin``): the instance settings stored in MongoDB (``waystation_server.instance``;
  now the instance name shown in the top bar, on the sign-in page and in ``GET /config``, and the security policy
  "require two-factor sign-in for local administrators"), and, read-only, the limits
  set with environment variables. Saving is audited as ``settings.update`` with the field changes.
- ``GET /system`` (``admin``): version, Python, when the answering process started, MongoDB and Valkey health (a
  ping with a 2-second limit each, round trip and server version; a failure reports only the error class) and the
  data directory's size, use and free space (``statvfs`` in a thread).
"""

from __future__ import annotations

import asyncio
import os
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from waystation_common.db.collections import STATS_MINUTE_RETENTION_DAYS
from waystation_server import audit
from waystation_server.api.auth import VERSION
from waystation_server.api.deps import ServicesDep
from waystation_server.api.rbac import PROBLEM
from waystation_server.audit import AuditAction
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate
from waystation_server.services import Services

router = APIRouter(tags=['admin'])

STARTED_AT = datetime.now(UTC)
CHECK_TIMEOUT_S = 2.0

InstanceName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class GeneralSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')

    instance_name: InstanceName = Field(
        description='Shown in the top bar, the sign-in page and the browser title (default `WAYSTATION_INSTANCE_NAME`)'
    )


class EnvironmentLimits(BaseModel):
    session_idle_minutes: int
    session_max_hours: int
    login_rate_limit: int = Field(description='Sign-in attempts per client address per 5 minutes')
    login_lockout_failures: int
    login_lockout_minutes: int
    token_failure_limit: int = Field(description='Rejected API tokens per client address per 5 minutes')
    audit_retention_days: int
    stats_minute_retention_days: int = Field(description='Per-minute statistics are kept this long')
    max_upload_bytes: int = Field(description='Largest single upload or cached file')
    min_free_bytes: int = Field(description='New cache and upload writes are refused below this much free space')


class SecuritySettings(BaseModel):
    model_config = ConfigDict(extra='forbid')

    require_totp_for_admins: bool = Field(
        description='Local administrators must set up two-factor sign-in (TOTP) before they can do anything else'
    )


class InstanceSettings(BaseModel):
    general: GeneralSettings
    security: SecuritySettings
    limits: EnvironmentLimits
    updated_at: datetime | None = Field(description='null until first saved')
    updated_by: str | None = Field(description='Username of the last change')


class InstanceSettingsWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')

    general: GeneralSettings
    security: SecuritySettings | None = Field(None, description='Omit to keep the current security settings')


class SystemComponent(BaseModel):
    name: Literal['mongodb', 'valkey']
    status: Literal['ok', 'unavailable']
    version: str | None
    latency_ms: float | None = Field(description='Round trip of a ping')
    detail: str | None = Field(description='Why it is unavailable (the error class only)')


class SystemStorage(BaseModel):
    path: str = Field(description='The data directory (`WAYSTATION_DATA_DIR`)')
    total_bytes: int | None = Field(description='null when the volume cannot be read')
    used_bytes: int | None
    free_bytes: int | None
    min_free_bytes: int = Field(description='New writes are refused below this much free space')


class SystemInfo(BaseModel):
    version: str
    python: str = Field(description='Python version of the server process')
    started_at: datetime = Field(description='When the answering server process started')
    components: list[SystemComponent]
    storage: SystemStorage


# ---------------------------------------------------------------------------- settings
async def _view(svc: Services) -> InstanceSettings:
    general = await svc.instance.general()
    security = await svc.instance.security()
    s = svc.settings
    updated_at, updated_by = general.updated_at, general.updated_by
    if security.updated_at is not None and (updated_at is None or security.updated_at > updated_at):
        updated_at, updated_by = security.updated_at, security.updated_by
    return InstanceSettings(
        general=GeneralSettings(instance_name=general.instance_name),
        security=SecuritySettings(require_totp_for_admins=security.require_totp_for_admins),
        limits=EnvironmentLimits(
            session_idle_minutes=s.session_idle_minutes,
            session_max_hours=s.session_max_hours,
            login_rate_limit=s.login_rate_limit,
            login_lockout_failures=s.login_lockout_failures,
            login_lockout_minutes=s.login_lockout_minutes,
            token_failure_limit=s.token_failure_limit,
            audit_retention_days=s.audit_retention_days,
            stats_minute_retention_days=STATS_MINUTE_RETENTION_DAYS,
            max_upload_bytes=s.max_upload_bytes,
            min_free_bytes=s.min_free_bytes,
        ),
        updated_at=updated_at,
        updated_by=updated_by,
    )


@router.get('/settings', response_model=InstanceSettings, operation_id='getSettings')
@access.requires(Action.SETTINGS_ADMIN)
async def get_settings(svc: ServicesDep) -> InstanceSettings:
    return await _view(svc)


@router.put('/settings', response_model=InstanceSettings, operation_id='updateSettings', responses={422: PROBLEM})
@access.requires(Action.SETTINGS_ADMIN)
async def update_settings(body: InstanceSettingsWrite, request: Request, svc: ServicesDep) -> InstanceSettings:
    principal = await authenticate(request)
    before = await svc.instance.general()
    after = await svc.instance.save_general(body.general.instance_name, principal.name)
    audit.record(
        AuditAction.SETTINGS_UPDATE,
        target=audit.target('settings', 'general', 'general'),
        changes=audit.diff({'instance_name': before.instance_name}, {'instance_name': after.instance_name}),
    )
    current = await svc.instance.security()
    wanted = body.security
    if wanted is not None and wanted.require_totp_for_admins != current.require_totp_for_admins:
        saved = await svc.instance.save_security(wanted.require_totp_for_admins, principal.name)
        audit.record(
            AuditAction.SETTINGS_UPDATE,
            target=audit.target('settings', 'security', 'security'),
            changes=audit.diff(
                {'require_totp_for_admins': current.require_totp_for_admins},
                {'require_totp_for_admins': saved.require_totp_for_admins},
            ),
        )
    return await _view(svc)


# ---------------------------------------------------------------------------- system
def _error(exc: BaseException) -> str:
    return 'timeout' if isinstance(exc, TimeoutError) else type(exc).__name__


async def _mongodb(svc: Services) -> SystemComponent:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            start = time.perf_counter()
            await svc.db.command('ping')
            latency = (time.perf_counter() - start) * 1000
            info = await svc.db.command('buildInfo')
    except Exception as exc:
        return SystemComponent(name='mongodb', status='unavailable', version=None, latency_ms=None, detail=_error(exc))
    version = info.get('version')
    return SystemComponent(
        name='mongodb',
        status='ok',
        version=version if isinstance(version, str) else None,
        latency_ms=round(latency, 2),
        detail=None,
    )


async def _valkey(svc: Services) -> SystemComponent:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            start = time.perf_counter()
            await svc.valkey.ping()
            latency = (time.perf_counter() - start) * 1000
            info = await svc.valkey.info('server')
    except Exception as exc:
        return SystemComponent(name='valkey', status='unavailable', version=None, latency_ms=None, detail=_error(exc))
    version = info.get('valkey_version') or info.get('redis_version')
    return SystemComponent(
        name='valkey', status='ok', version=str(version) if version else None, latency_ms=round(latency, 2), detail=None
    )


def _volume(path: Path) -> tuple[int, int, int] | None:
    """(total, used, free) bytes of the volume holding ``path`` (free = available to the unprivileged process)."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return total, total - st.f_bfree * st.f_frsize, free


@router.get('/system', response_model=SystemInfo, operation_id='getSystem')
@access.requires(Action.ADMIN)
async def get_system(svc: ServicesDep) -> SystemInfo:
    mongo, valkey, volume = await asyncio.gather(
        _mongodb(svc), _valkey(svc), asyncio.to_thread(_volume, svc.settings.data_dir)
    )
    total, used, free = volume if volume else (None, None, None)
    return SystemInfo(
        version=VERSION,
        python=platform.python_version(),
        started_at=STARTED_AT,
        components=[mongo, valkey],
        storage=SystemStorage(
            path=str(svc.settings.data_dir),
            total_bytes=total,
            used_bytes=used,
            free_bytes=free,
            min_free_bytes=svc.settings.min_free_bytes,
        ),
    )
