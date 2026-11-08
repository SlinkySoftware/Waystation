"""Event and live-state models (SPECIFICATION §4.7, ARCHITECTURE §8). JSON on the wire (``model_dump_json``)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CacheState(StrEnum):
    """One vocabulary everywhere (API, events, metrics, UI)."""

    HIT = 'HIT'  # served from cache
    FETCHING = 'FETCHING'  # miss; this request owns the upstream fetch
    COALESCED = 'COALESCED'  # miss; waiting on or tailing another request's fetch
    STALE = 'STALE'  # served from cache because the upstream failed
    REVALIDATED = 'REVALIDATED'  # metadata checked upstream, unchanged, served from cache
    ERROR = 'ERROR'


# Cache states whose response bytes came from the blob store (bandwidth saved).
FROM_CACHE = frozenset({CacheState.HIT, CacheState.STALE, CacheState.REVALIDATED})


class EventType(StrEnum):
    TRANSFER_STARTED = 'transfer.started'
    TRANSFER_PROGRESS = 'transfer.progress'
    TRANSFER_FINISHED = 'transfer.finished'
    TRANSFER_FAILED = 'transfer.failed'
    CACHE_HIT = 'cache.hit'
    CACHE_MISS = 'cache.miss'
    CACHE_STALE = 'cache.stale'
    UPSTREAM_STATE = 'upstream.state'
    ASSET_CREATED = 'asset.created'
    ASSET_DELETED = 'asset.deleted'
    JOB_STARTED = 'job.started'
    JOB_FINISHED = 'job.finished'
    JOB_FAILED = 'job.failed'
    WORKER_ONLINE = 'worker.online'
    WORKER_OFFLINE = 'worker.offline'


class Direction(StrEnum):
    DOWNLOAD = 'download'
    UPLOAD = 'upload'


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')


class Transfer(_Model):
    """Live state of one in-flight download/upload: the value in the ``transfers`` hash and the ``live`` payload."""

    id: str
    repo_id: str
    repo: str
    format: str
    path: str
    direction: Direction = Direction.DOWNLOAD
    cache: CacheState
    principal: str | None = None
    client_ip: str | None = None
    client_agent: str | None = None  # client product, e.g. 'docker/27.3'
    artifact: str | None = None  # display name (default: the last path segment)
    component: str | None = None  # component coordinates when known, e.g. 'nginx:1.27'
    error: str | None = None  # set on transfer.failed
    bytes_done: int = 0
    bytes_total: int | None = None
    rate_bps: float = 0.0
    upstream_latency_ms: float | None = None
    started_at: datetime
    updated_at: datetime


class ActivityEvent(_Model):
    """An entry of the ``activity`` stream (durable-ish history, SSE resume)."""

    type: EventType
    ts: datetime
    repo_id: str | None = None
    repo: str | None = None
    format: str | None = None
    path: str | None = None
    principal: str | None = None
    client_ip: str | None = None
    cache: CacheState | None = None
    status: int | None = None
    bytes: int | None = None
    duration_ms: float | None = None
    transfer_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class LiveMessage(_Model):
    """A message on the ``live`` pub/sub channel."""

    type: EventType
    transfer: Transfer
