"""Per-repository status and 24 h summary figures (OpenAPI ``Repository.status``, ``RepositorySummaryStats``) and
upstream health (``UpstreamHealth``)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from waystation_common.events import keys
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.services import Services
from waystation_server.stats import series

DEGRADED_WINDOW = timedelta(minutes=10)


@dataclass
class RepoSummary:
    size_bytes: int = 0
    artifact_count: int = 0
    hit_ratio_24h: float | None = None
    requests_24h: int = 0
    bytes_served_24h: int = 0
    bytes_saved_24h: int = 0
    requests_spark_24h: list[int] = field(default_factory=lambda: [0] * 24)
    last_activity_at: datetime | None = None


@dataclass
class Health:
    last_ok_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0
    last_latency_ms: float | None = None
    rate_limit: dict[str, str] = field(default_factory=dict)


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def health(services: Services, repos: Sequence[RepoConfig]) -> dict[str, Health]:
    proxies = [r for r in repos if r.type is RepoType.PROXY]
    out: dict[str, Health] = {r.id: Health() for r in repos}
    if not proxies:
        return out
    try:
        async with services.valkey.pipeline(transaction=False) as pipe:
            for repo in proxies:
                pipe.hgetall(keys.upstream_health_key(repo.id))
                pipe.hgetall(keys.upstream_ratelimit_key(repo.id))
            results = await pipe.execute()
    except Exception:  # Valkey down: health unknown, report ok
        return out
    for i, repo in enumerate(proxies):
        out[repo.id] = parse_health(results[2 * i], results[2 * i + 1])
    return out


def parse_health(raw: dict[str, str], rate: dict[str, str]) -> Health:
    """``Health`` from the ``upstream:<id>:health`` and ``upstream:<id>:ratelimit`` hashes."""
    latency = raw.get('last_latency_ms')
    return Health(
        last_ok_at=_when(raw.get('last_ok_at')),
        last_error=raw.get('last_error'),
        last_error_at=_when(raw.get('last_error_at')),
        consecutive_failures=int(raw.get('consecutive_failures') or 0),
        last_latency_ms=float(latency) if latency else None,
        rate_limit=dict(rate),
    )


def status_of(repo: RepoConfig, h: Health | None, *, now: datetime | None = None) -> str:
    if not repo.online or (repo.proxy is not None and repo.proxy.offline):
        return 'offline'
    now = now or datetime.now(UTC)
    if h is not None and h.consecutive_failures > 0 and h.last_error_at and now - h.last_error_at < DEGRADED_WINDOW:
        return 'degraded'
    return 'ok'


async def statuses(services: Services, repos: Sequence[RepoConfig]) -> dict[str, str]:
    states = await health(services, repos)
    return {repo.id: status_of(repo, states.get(repo.id)) for repo in repos}


async def summaries(services: Services, repos: Sequence[RepoConfig]) -> dict[str, RepoSummary]:
    out = {repo.id: RepoSummary() for repo in repos}
    if not repos:
        return out
    win = series.Window('24h', 3600, *_last_24h())
    rows = await series.collect(services.db, services.valkey, win, repo_ids=list(out), group_repo=True)
    storage = await services.storage.get()
    for repo_id, summary in out.items():
        mine = [r for r in rows if r.get('repo_id') == repo_id]
        t = series.totals(mine)
        summary.requests_24h = t['requests']
        summary.bytes_served_24h = t['bytes_out_cache'] + t['bytes_out_upstream']
        summary.bytes_saved_24h = t['bytes_out_cache']
        summary.hit_ratio_24h = series.hit_ratio(t)
        spark = series.series(mine, win, lambda r: float(r['requests'])).get('', [0.0] * 24)
        summary.requests_spark_24h = [int(v) for v in spark]
        stamps = [r['last_ts'] for r in mine if r.get('last_ts')]
        summary.last_activity_at = max(stamps) if stamps else None
        stored = storage.by_repo.get(repo_id)
        if stored is not None:
            summary.size_bytes = stored.bytes
            summary.artifact_count = stored.artifacts
    return out


def _last_24h() -> tuple[int, int]:
    end = (int(datetime.now(UTC).timestamp()) // 3600) * 3600 + 3600
    return end - 86400, end
