"""``/api/v1/stats/*`` and ``/api/v1/repositories/{name}/stats/traffic`` (OpenAPI contract, SPECIFICATION §4.7).

Global figures need ``stats-read``; a repository's traffic needs ``browse`` on it as well. Series come from closed
minutes in MongoDB plus the live counters in Valkey (``stats.series``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from waystation_common.errors import NotFound
from waystation_common.events import keys
from waystation_server.api.deps import ServicesDep
from waystation_server.api.repositories import RateLimit
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.repos.schemas import Format, RepositoryStatus
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate
from waystation_server.services import Services
from waystation_server.stats import repos as repo_stats
from waystation_server.stats import series

router = APIRouter(tags=['stats'])

StatsRange = Literal['15m', '1h', '24h', '7d', '30d']
Metric = Literal['bytes', 'requests']
RangeQuery = Annotated[StatsRange, Query(alias='range')]


class StorageNow(BaseModel):
    used_bytes: int = Field(description='Bytes held by blobs')
    free_bytes: int = Field(description='Free space on the data directory filesystem')
    total_bytes: int


class Sparks(BaseModel):
    requests_per_s: list[float]
    hit_ratio: list[float | None]
    bytes_served: list[float]
    bytes_saved: list[float]
    storage_used: list[float]
    artifacts: list[float]
    active_transfers: list[float]


class StatsSummary(BaseModel):
    range: StatsRange
    step_s: int
    generated_at: datetime
    requests_per_s: float = Field(description='Current rate')
    requests: int
    hit_ratio: float | None
    bytes_served: int
    bytes_saved: int
    bytes_upstream: int
    storage: StorageNow
    artifacts_cached: int
    active_transfers: int
    sparks: Sparks


class Series(BaseModel):
    key: str = Field(description='`hit`/`upstream` (split=cache) or a format name (split=format)')
    label: str
    values: list[float]


class TimeSeries(BaseModel):
    range: StatsRange
    step_s: int = Field(description='Bucket width: 15m and 1h → 1 min, 24h → 10 min, 7d → 1 h, 30d → 6 h')
    unit: Metric
    timestamps: list[datetime]
    series: list[Series]


class FormatStorage(BaseModel):
    format: Format
    bytes: int
    artifacts: int


class RepositoryStorage(BaseModel):
    repository: str
    format: Format
    bytes: int
    artifacts: int


class StorageSummary(BaseModel):
    used_bytes: int
    free_bytes: int
    total_bytes: int
    blob_count: int
    dedup_saved_bytes: int
    by_format: list[FormatStorage]
    by_repository: list[RepositoryStorage]


class UpstreamHealth(BaseModel):
    repository: str
    format: Format
    upstream_url: str
    status: RepositoryStatus
    error_rate: float = Field(ge=0, le=1, description='Upstream errors / upstream requests, last hour')
    requests_1h: int = Field(description='Upstream requests in the last hour')
    p95_latency_ms: float | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    blocked_until: datetime | None = None
    rate_limit: RateLimit | None = None


# ---------------------------------------------------------------------------- helpers
async def _visible(svc: Services, request: Request) -> list[RepoConfig]:
    principal = await authenticate(request)
    return access.filter_repos(principal, Action.BROWSE, await svc.catalog.all())


async def _repo_filter(svc: Services, request: Request, name: str | None) -> list[RepoConfig]:
    repos = await _visible(svc, request)
    if name is None:
        return repos
    match = [r for r in repos if r.name == name or r.name.lower() == name.lower()]
    if not match:
        raise NotFound('repository not found')
    return match[:1]


async def _current_rate(svc: Services, repo_ids: list[str]) -> float:
    """Requests per second over roughly the last minute, from the live counters."""
    now = time.time()
    minute = int(now // 60)
    rows = await series.live_minutes(svc.valkey, since_minute=minute - 1, repo_ids=repo_ids)
    current = sum(int(c.get('requests', 0) or 0) for m, _, c in rows if m == minute)
    previous = sum(int(c.get('requests', 0) or 0) for m, _, c in rows if m == minute - 1)
    elapsed = now - minute * 60
    return round((previous * (60 - elapsed) / 60 + current) / 60, 2)


def _timestamps(win: series.Window) -> list[datetime]:
    return [datetime.fromtimestamp(b, UTC) for b in win.buckets]


# ---------------------------------------------------------------------------- routes
@router.get('/stats/summary', response_model=StatsSummary, operation_id='getStatsSummary')
@access.requires(Action.STATS_READ)
async def get_summary(request: Request, svc: ServicesDep, range_: RangeQuery = '24h') -> StatsSummary:
    repos = await _visible(svc, request)
    ids = [r.id for r in repos]
    win = series.window(range_)
    rows = await series.collect(svc.db, svc.valkey, win, repo_ids=ids)
    t = series.totals(rows)
    n = len(win.buckets)
    requests = series.series(rows, win, lambda r: r['requests'] / win.step_s).get('', [0.0] * n)
    served = series.series(rows, win, series.bytes_served).get('', [0.0] * n)
    saved = series.series(rows, win, lambda r: float(r['bytes_out_cache'])).get('', [0.0] * n)
    by_bucket: dict[int, dict[str, int]] = {}
    for row in rows:
        idx = win.index(int(row['bucket']) // 1000)
        if idx is not None:
            acc = by_bucket.setdefault(idx, {})
            for field in ('hits', 'stale', 'misses', 'coalesced'):
                acc[field] = acc.get(field, 0) + int(row.get(field) or 0)
    ratio = [series.hit_ratio(by_bucket[i]) if i in by_bucket else None for i in range(n)]
    storage = await svc.storage.get()
    active = int(await svc.valkey.hlen(keys.TRANSFERS))
    artifacts = sum(storage.by_repo[i].artifacts for i in ids if i in storage.by_repo)
    return StatsSummary(
        range=range_,
        step_s=win.step_s,
        generated_at=datetime.now(UTC),
        requests_per_s=await _current_rate(svc, ids),
        requests=t['requests'],
        hit_ratio=series.hit_ratio(t),
        bytes_served=t['bytes_out_cache'] + t['bytes_out_upstream'],
        bytes_saved=t['bytes_out_cache'],
        bytes_upstream=t['bytes_in_upstream'],
        storage=StorageNow(
            used_bytes=storage.used_bytes, free_bytes=storage.free_bytes, total_bytes=storage.total_bytes
        ),
        artifacts_cached=artifacts,
        active_transfers=active,
        sparks=Sparks(
            requests_per_s=[round(v, 3) for v in requests],
            hit_ratio=ratio,
            bytes_served=served,
            bytes_saved=saved,
            # No storage history yet (hourly storage_snapshots arrive with the full T-F09): flat current values.
            storage_used=[float(storage.used_bytes)] * n,
            artifacts=[float(artifacts)] * n,
            active_transfers=[0.0] * (n - 1) + [float(active)],
        ),
    )


async def _traffic(svc: Services, repos: list[RepoConfig], range_name: str, metric: str, split: str) -> TimeSeries:
    win = series.window(range_name)
    rows = await series.collect(svc.db, svc.valkey, win, repo_ids=[r.id for r in repos], group_repo=split == 'format')
    n = len(win.buckets)
    out: list[Series] = []
    if split == 'format':
        value = (lambda r: float(r['requests'])) if metric == 'requests' else series.bytes_served
        for fmt, values in sorted(series.series(rows, win, value, key=lambda r: str(r.get('format') or '')).items()):
            out.append(Series(key=fmt, label=fmt, values=values))
    else:
        if metric == 'requests':
            hit = series.series(rows, win, lambda r: float(r['hits'] + r['stale']))
            up = series.series(rows, win, lambda r: float(r['misses'] + r['coalesced']))
        else:
            hit = series.series(rows, win, lambda r: float(r['bytes_out_cache']))
            up = series.series(rows, win, lambda r: float(r['bytes_out_upstream']))
        out.append(Series(key='hit', label='From cache', values=hit.get('', [0.0] * n)))
        out.append(Series(key='upstream', label='From upstream', values=up.get('', [0.0] * n)))
    return TimeSeries(
        range=range_name,
        step_s=win.step_s,
        unit=metric,
        timestamps=_timestamps(win),
        series=out,
    )


@router.get('/stats/traffic', response_model=TimeSeries, operation_id='getTraffic')
@access.requires(Action.STATS_READ)
async def get_traffic(
    request: Request,
    svc: ServicesDep,
    range_: RangeQuery = '24h',
    metric: Metric = 'bytes',
    split: Literal['cache', 'format'] = 'cache',
    repo: str | None = None,
) -> TimeSeries:
    repos = await _repo_filter(svc, request, repo)
    return await _traffic(svc, repos, range_, metric, split)


@router.get('/repositories/{name}/stats/traffic', response_model=TimeSeries, operation_id='getRepositoryTraffic')
@access.requires_repo(Action.BROWSE)
async def get_repository_traffic(
    name: str, request: Request, svc: ServicesDep, range_: RangeQuery = '24h', metric: Metric = 'bytes'
) -> TimeSeries:
    access.require(await authenticate(request), Action.STATS_READ)
    return await _traffic(svc, [request.state.repo], range_, metric, 'cache')


@router.get('/stats/storage', response_model=StorageSummary, operation_id='getStorage')
@access.requires(Action.STATS_READ)
async def get_storage(request: Request, svc: ServicesDep) -> StorageSummary:
    repos = await _visible(svc, request)
    storage = await svc.storage.get()
    by_repo: list[RepositoryStorage] = []
    by_format: dict[str, FormatStorage] = {}
    for repo in repos:
        figures = storage.by_repo.get(repo.id)
        size, artifacts = (figures.bytes, figures.artifacts) if figures else (0, 0)
        by_repo.append(RepositoryStorage(repository=repo.name, format=repo.format, bytes=size, artifacts=artifacts))
        entry = by_format.setdefault(repo.format, FormatStorage(format=repo.format, bytes=0, artifacts=0))
        entry.bytes += size
        entry.artifacts += artifacts
    by_repo.sort(key=lambda r: r.bytes, reverse=True)
    return StorageSummary(
        used_bytes=storage.used_bytes,
        free_bytes=storage.free_bytes,
        total_bytes=storage.total_bytes,
        blob_count=storage.blob_count,
        dedup_saved_bytes=storage.dedup_saved_bytes,
        by_format=sorted(by_format.values(), key=lambda f: f.bytes, reverse=True),
        by_repository=by_repo,
    )


def _rate_limit(raw: dict[str, str]) -> RateLimit | None:
    if not raw.get('limit') or not raw.get('remaining'):
        return None
    reset = raw.get('reset_at') or None
    return RateLimit(
        limit=int(raw['limit']),
        remaining=int(raw['remaining']),
        window_s=int(raw['window_s']) if raw.get('window_s') else None,
        reset_at=datetime.fromisoformat(reset) if reset else None,
        source=raw.get('source') or None,
    )


@router.get('/stats/upstreams', response_model=list[UpstreamHealth], operation_id='listUpstreamHealth')
@access.requires(Action.STATS_READ)
async def list_upstreams(request: Request, svc: ServicesDep) -> list[UpstreamHealth]:
    repos = [r for r in await _visible(svc, request) if r.type is RepoType.PROXY and r.proxy is not None]
    states = await repo_stats.health(svc, repos)
    win = series.window('1h')
    rows = await series.collect(svc.db, svc.valkey, win, repo_ids=[r.id for r in repos], group_repo=True)
    out = []
    for repo in repos:
        assert repo.proxy is not None
        t = series.totals([r for r in rows if r.get('repo_id') == repo.id])
        h = states[repo.id]
        requests = t['upstream_requests']
        out.append(
            UpstreamHealth(
                repository=repo.name,
                format=repo.format,
                upstream_url=repo.proxy.upstream_url,
                status=repo_stats.status_of(repo, h),
                error_rate=round(min(1.0, t['upstream_errors'] / requests), 4) if requests else 0.0,
                requests_1h=requests,
                p95_latency_ms=None,
                last_error=h.last_error,
                last_error_at=h.last_error_at,
                blocked_until=None,
                rate_limit=_rate_limit(h.rate_limit),
            )
        )
    return out
