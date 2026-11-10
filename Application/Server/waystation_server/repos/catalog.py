"""In-process repository catalog: every request resolves its repository here, never with a MongoDB query.

The full list (a few hundred small documents) is reloaded

- immediately after a change by any process: a MongoDB change stream on ``repositories`` (``watch``) invalidates the
  snapshot of every server process, and the process that made the change also invalidates it directly
  (``invalidate``);
- on a lookup by name that misses, when the caller asks for it (``get(name, reload_on_miss=True)``; Docker token
  issuance, request routing), at most once per ``miss_reload_s`` and once per name and snapshot: a repository created
  a moment ago on another process is found even before its change event arrives;
- at the latest every ``ttl_s`` seconds (the fallback while the change stream is down).

URL matching is case-sensitive (SPECIFICATION §3); uniqueness is case-insensitive.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from waystation_server.repos.model import RepoConfig

Loader = Callable[[], Awaitable[list[RepoConfig]]]
# Yields once per change to ``repositories``; calls its argument once the stream is open.
Watcher = Callable[[Callable[[], None]], AsyncIterator[object]]

log = logging.getLogger('waystation.repos.catalog')

WATCH_RETRY_S = 5.0
MISS_RELOAD_S = 1.0
MISSING_MAX = 10_000


class RepoCatalog:
    def __init__(
        self,
        loader: Loader,
        *,
        ttl_s: float = 5.0,
        watcher: Watcher | None = None,
        miss_reload_s: float = MISS_RELOAD_S,
    ) -> None:
        self._loader = loader
        self._ttl_s = ttl_s
        self._watcher = watcher
        self._miss_reload_s = miss_reload_s
        self._repos: list[RepoConfig] = []
        self._by_name: dict[str, RepoConfig] = {}
        self._loaded_at = -1.0
        self._generation = 0  # bumped by every invalidation
        self._miss_reload_at = -1e18
        self._missing: set[str] = set()  # names looked up again in the current snapshot and still unknown
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self.watching = asyncio.Event()  # the change stream is open (tests wait for it)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._watcher is not None and self._task is None:
            self._task = asyncio.create_task(self._watch(self._watcher), name='repo-catalog-watch')

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _watch(self, watcher: Watcher) -> None:
        while True:
            try:
                async for _ in watcher(self._opened):
                    self.invalidate()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # no replica set, MongoDB restarting: the TTL still bounds staleness
                log.warning('repository change stream lost; retrying', extra={'error': type(exc).__name__})
            self.watching.clear()
            await asyncio.sleep(WATCH_RETRY_S)

    def _opened(self) -> None:
        self.invalidate()  # changes made before the stream opened are not in the stream
        self.watching.set()

    # ------------------------------------------------------------------ snapshot
    def invalidate(self) -> None:
        self._loaded_at = -1.0
        self._generation += 1

    def _fresh(self) -> bool:
        return self._loaded_at >= 0 and time.monotonic() - self._loaded_at < self._ttl_s

    async def _ensure(self) -> None:
        if self._fresh():
            return
        async with self._lock:
            if self._fresh():
                return
            started, generation = time.monotonic(), self._generation
            repos = sorted(await self._loader(), key=lambda r: r.name.lower())
            self._repos = repos
            self._by_name = {r.name: r for r in repos}
            self._missing = set()
            # Invalidated while loading: the load may predate the change, so the next lookup loads again.
            self._loaded_at = started if generation == self._generation else -1.0

    async def _reload_after_miss(self) -> None:
        """A lookup missed: reload, at most once per ``miss_reload_s`` (names that do not exist cannot make every
        request scan ``repositories``); lookups that miss while a reload runs wait for it."""
        now = time.monotonic()
        if now - self._miss_reload_at >= self._miss_reload_s:
            self._miss_reload_at = now
            self.invalidate()
        await self._ensure()

    async def all(self) -> list[RepoConfig]:
        await self._ensure()
        return list(self._repos)

    async def get(self, name: str, *, reload_on_miss: bool = False) -> RepoConfig | None:
        """Exact (case-sensitive) name match, as used by URLs. ``reload_on_miss``: look again in a fresh snapshot
        when the name is unknown (a repository another process created a moment ago)."""
        await self._ensure()
        repo = self._by_name.get(name)
        if repo is None and reload_on_miss and name not in self._missing:
            await self._reload_after_miss()
            repo = self._by_name.get(name)
            if repo is None and len(self._missing) < MISSING_MAX:
                self._missing.add(name)  # until the next load: no reload per request for names that do not exist
        return repo

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
