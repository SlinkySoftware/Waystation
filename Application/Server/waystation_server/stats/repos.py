"""Per-repository status and 24 h summary figures for the repository API (OpenAPI ``RepositorySummaryStats``)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from waystation_server.repos.model import RepoConfig
from waystation_server.services import Services


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


def status_of(repo: RepoConfig) -> str:
    if not repo.online or (repo.proxy is not None and repo.proxy.offline):
        return 'offline'
    return 'ok'


async def statuses(services: Services, repos: Sequence[RepoConfig]) -> dict[str, str]:
    return {repo.id: status_of(repo) for repo in repos}


async def summaries(services: Services, repos: Sequence[RepoConfig]) -> dict[str, RepoSummary]:
    return {repo.id: RepoSummary() for repo in repos}
