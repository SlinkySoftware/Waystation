"""In-process repository catalog: every request resolves its repository here, never with a MongoDB query.

The full list is loaded at most every ``ttl_s`` seconds (a few hundred small documents) and immediately after a
local change (``invalidate``). Other replicas pick a change up within ``ttl_s`` (change-stream invalidation is the
DATA_MODEL target, later). URL matching is case-sensitive (SPECIFICATION §3); uniqueness is case-insensitive.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from waystation_server.repos.model import RepoConfig

Loader = Callable[[], Awaitable[list[RepoConfig]]]


class RepoCatalog:
    def __init__(self, loader: Loader, *, ttl_s: float = 5.0) -> None:
        self._loader = loader
        self._ttl_s = ttl_s
        self._repos: list[RepoConfig] = []
        self._by_name: dict[str, RepoConfig] = {}
        self._loaded_at = -1.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._loaded_at = -1.0

    def _fresh(self) -> bool:
        return self._loaded_at >= 0 and time.monotonic() - self._loaded_at < self._ttl_s

    async def _ensure(self) -> None:
        if self._fresh():
            return
        async with self._lock:
            if self._fresh():
                return
            repos = sorted(await self._loader(), key=lambda r: r.name.lower())
            self._repos = repos
            self._by_name = {r.name: r for r in repos}
            self._loaded_at = time.monotonic()

    async def all(self) -> list[RepoConfig]:
        await self._ensure()
        return list(self._repos)

    async def get(self, name: str) -> RepoConfig | None:
        """Exact (case-sensitive) name match, as used by URLs."""
        await self._ensure()
        return self._by_name.get(name)

    async def get_ci(self, name: str) -> RepoConfig | None:
        await self._ensure()
        lowered = name.lower()
        return next((r for r in self._repos if r.name.lower() == lowered), None)

    async def by_id(self, repo_id: str) -> RepoConfig | None:
        await self._ensure()
        return next((r for r in self._repos if r.id == repo_id), None)

    async def docker_alias(self, alias: str) -> RepoConfig | None:
        """The docker repository declaring ``alias`` as an upstream alias (SPECIFICATION §4.4), case-insensitive."""
        await self._ensure()
        lowered = alias.lower()
        for repo in self._repos:
            if repo.format == 'docker':
                aliases = repo.format_options.get('aliases') or []
                if any(str(a).lower() == lowered for a in aliases):
                    return repo
        return None

    async def default_docker(self) -> RepoConfig | None:
        await self._ensure()
        return next((r for r in self._repos if r.format == 'docker' and r.format_options.get('default_repo')), None)
