"""``/api/v1/transfers``, ``/api/v1/activity`` and the event stream ``/api/v1/events/stream`` (OpenAPI contract,
SPECIFICATION §7, ARCHITECTURE §8). All need ``stats-read`` and only show repositories the principal may see."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from waystation_common.errors import ValidationFailed
from waystation_server.api.deps import ServicesDep
from waystation_server.api.stats import UpstreamHealth, upstream_health
from waystation_server.repos.schemas import Format
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate
from waystation_server.services import Services
from waystation_server.sse.hub import Subscriber, UpstreamHealthSource
from waystation_server.sse.stream import event_stream, parse_topics, valid_event_id
from waystation_server.stats import export, live

router = APIRouter(tags=['live'])

EXPORT_MAX_ROWS = 100_000

CacheStateName = Literal['HIT', 'FETCHING', 'COALESCED', 'STALE', 'REVALIDATED', 'ERROR']
ActivityKind = Literal[
    'download', 'upload', 'delete', 'cache_miss', 'upstream_state', 'repository', 'cleanup', 'auth', 'error'
]


class TransferClient(BaseModel):
    ip: str
    user: str | None = None
    agent: str | None = Field(default=None, description='Client product, e.g. `docker/27.3`, `npm/11.6`')


class Transfer(BaseModel):
    id: str
    repository: str
    format: Format
    direction: Literal['download', 'upload']
    artifact: str
    path: str
    component: str | None = None
    client: TransferClient
    cache: CacheStateName
    followers: int = 0
    bytes_done: int
    bytes_total: int | None
    rate_bps: float
    upstream_latency_ms: float | None = None
    started_at: datetime
    updated_at: datetime
    state: Literal['active', 'finished', 'failed']
    error: str | None = None


class TransferList(BaseModel):
    items: list[Transfer]
    as_of: datetime


class ActivityEvent(BaseModel):
    id: str = Field(description='Stream id; also the SSE `id:`')
    ts: datetime
    kind: ActivityKind
    summary: str = Field(description='Plain-text one-liner (never rendered as HTML)')
    outcome: Literal['success', 'failure', 'info']
    repository: str | None = None
    format: Format | None = None
    artifact: str | None = None
    path: str | None = Field(default=None, description='Full path of the asset in the repository, when there is one')
    principal: str | None = None
    cache: CacheStateName | None = None
    bytes: int | None = None
    duration_ms: float | None = None


class ActivityPage(BaseModel):
    items: list[ActivityEvent]
    next_cursor: str | None


# ---------------------------------------------------------------------------- SSE payloads (documentation + tests)
class TransfersSnapshotEvent(BaseModel):
    type: Literal['transfers.snapshot']
    ts: datetime
    transfers: list[Transfer]


class TransferStartedEvent(BaseModel):
    type: Literal['transfer.started']
    ts: datetime
    transfer: Transfer


class TransferProgressEvent(BaseModel):
    type: Literal['transfer.progress']
    ts: datetime
    id: str
    bytes_done: int
    bytes_total: int | None = None
    rate_bps: float
    cache: CacheStateName | None = None
    followers: int | None = None
    upstream_latency_ms: float | None = None


class TransferFinishedEvent(BaseModel):
    type: Literal['transfer.finished']
    ts: datetime
    id: str
    bytes_done: int
    duration_ms: float
    cache: CacheStateName | None = None


class TransferFailedEvent(BaseModel):
    type: Literal['transfer.failed']
    ts: datetime
    id: str
    bytes_done: int | None = None
    error: str


class StatsTickEvent(BaseModel):
    type: Literal['stats.tick']
    ts: datetime
    requests_per_s: float
    bytes_out_per_s: float
    bytes_upstream_per_s: float
    bytes_saved_per_s: float | None = None
    hit_ratio_1m: float | None
    active_transfers: int
    storage_used_bytes: int | None = None
    artifacts_cached: int | None = None


class UpstreamStateEvent(BaseModel):
    type: Literal['upstream.state']
    ts: datetime
    health: UpstreamHealth


class ActivityStreamEvent(BaseModel):
    type: Literal['activity']
    ts: datetime
    event: ActivityEvent


LiveEvent = Annotated[
    TransfersSnapshotEvent
    | TransferStartedEvent
    | TransferProgressEvent
    | TransferFinishedEvent
    | TransferFailedEvent
    | StatsTickEvent
    | UpstreamStateEvent
    | ActivityStreamEvent,
    Field(discriminator='type'),
]
LIVE_EVENT_MODELS: tuple[type[BaseModel], ...] = (
    TransfersSnapshotEvent,
    TransferStartedEvent,
    TransferProgressEvent,
    TransferFinishedEvent,
    TransferFailedEvent,
    StatsTickEvent,
    UpstreamStateEvent,
    ActivityStreamEvent,
)


# ---------------------------------------------------------------------------- routes
async def _visible_names(svc: Services, request: Request) -> set[str]:
    principal = await authenticate(request)
    return {r.name for r in access.filter_repos(principal, Action.BROWSE, await svc.catalog.all())}


@router.get('/transfers', response_model=TransferList, operation_id='listTransfers')
@access.requires(Action.STATS_READ)
async def list_transfers(
    request: Request, svc: ServicesDep, repo: str | None = None, format: Format | None = None
) -> TransferList:
    visible = await _visible_names(svc, request)
    items = [
        live.transfer_view(t)
        for t in await live.current_transfers(svc.valkey)
        if t.repo in visible and (repo is None or t.repo == repo) and (format is None or t.format == format)
    ]
    return TransferList.model_validate({'items': items, 'as_of': datetime.now(UTC)})


async def activity_filter(
    request: Request,
    svc: ServicesDep,
    repo: str | None = None,
    kind: ActivityKind | None = None,
    format: Format | None = None,
    principal: Annotated[
        str | None, Query(max_length=128, description='User name or client address (case-insensitive substring)')
    ] = None,
    cache: CacheStateName | None = None,
    outcome: Literal['success', 'failure', 'info'] | None = None,
    since: Annotated[datetime | None, Query(description='Only events at or after this time')] = None,
    until: Annotated[datetime | None, Query(description='Only events at or before this time')] = None,
) -> live.ActivityFilter:
    """The Activity filters shared by the page and the CSV export."""
    since_ms = _epoch_ms(since) if since else None
    until_ms = _epoch_ms(until) if until else None
    if since_ms is not None and until_ms is not None and since_ms > until_ms:
        raise ValidationFailed('since must not be after until')
    return live.ActivityFilter(
        visible=frozenset(await _visible_names(svc, request)),
        repo=repo,
        kind=kind,
        format=format,
        principal=principal.strip() or None if principal else None,
        cache=cache,
        outcome=outcome,
        since_ms=since_ms,
        until_ms=until_ms,
    )


def _epoch_ms(value: datetime) -> int:
    return max(0, int((value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp() * 1000))


ActivityFilterDep = Annotated[live.ActivityFilter, Depends(activity_filter)]


@router.get('/activity', response_model=ActivityPage, operation_id='listActivity')
@access.requires(Action.STATS_READ)
async def list_activity(
    request: Request,
    flt: ActivityFilterDep,
    svc: ServicesDep,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> ActivityPage:
    if cursor is not None and valid_event_id(cursor) is None:
        raise ValidationFailed('invalid cursor')
    items, next_cursor = await live.activity_page(svc.valkey, flt, cursor=cursor, limit=limit)
    return ActivityPage.model_validate({'items': items, 'next_cursor': next_cursor})


@router.get(
    '/activity/export',
    operation_id='exportActivity',
    response_class=StreamingResponse,
    responses={200: {'description': 'CSV, newest first', 'content': {'text/csv': {'schema': {'type': 'string'}}}}},
)
@access.requires(Action.STATS_READ)
async def export_activity(request: Request, flt: ActivityFilterDep, svc: ServicesDep) -> StreamingResponse:
    """The filtered activity as CSV, streamed newest first, at most ``EXPORT_MAX_ROWS`` rows."""
    stamp = datetime.now(UTC).strftime('%Y%m%d-%H%M%S')
    return StreamingResponse(
        export.stream_csv(live.activity_scan(svc.valkey, flt, max_rows=EXPORT_MAX_ROWS)),
        media_type='text/csv; charset=utf-8',
        headers={
            'Content-Disposition': f'attachment; filename="waystation-activity-{stamp}.csv"',
            'Cache-Control': 'no-store',
            'X-Accel-Buffering': 'no',
        },
    )


def upstream_health_source(svc: Services) -> UpstreamHealthSource:
    """The hub's ``upstream.state`` payload: a repository's ``UpstreamHealth`` (the event-stream route installs it)."""

    async def health(name: str) -> dict[str, Any] | None:
        repo = await svc.catalog.get(name)
        rows = await upstream_health(svc, [repo]) if repo is not None else []
        return rows[0].model_dump(mode='json') if rows else None

    return health


@router.get('/events/stream', operation_id='streamEvents', response_class=StreamingResponse)
@access.requires(Action.STATS_READ)
async def stream_events(
    request: Request,
    svc: ServicesDep,
    topics: Annotated[str, Query(description='Comma-separated: transfers, stats, activity, repo:<name>')],
    last_event_id: Annotated[str | None, Query(max_length=64)] = None,
    last_event_id_header: Annotated[str | None, Header(alias='Last-Event-ID')] = None,
) -> StreamingResponse:
    wanted = parse_topics(topics)
    if wanted is None:
        raise ValidationFailed('topics must be a comma-separated list of transfers, stats, activity, repo:<name>')
    principal = await authenticate(request)
    if svc.hub.upstream_health is None:
        svc.hub.upstream_health = upstream_health_source(svc)
    sub = Subscriber(principal=principal, topics=wanted)
    resume = valid_event_id(last_event_id_header) or valid_event_id(last_event_id)
    return StreamingResponse(
        event_stream(svc.hub, sub, last_event_id=resume),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )
