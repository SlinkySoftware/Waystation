"""Prefetch jobs (T-O12, adr/0012; SPECIFICATION §4.1 "Prefetch"): ``prefetch.refresh`` and ``prefetch.warm``.

The worker decides **what** to prefetch and when; the server fetches it. Format protocol logic (a docker tag's
manifests and layers, an npm packument's tarball, validation, indexing, coalescing with clients) lives only in the
server's format plugins, and the worker may not import the server (ARCHITECTURE §3). So each job calls the server's
internal prefetch route (``POST {WAYSTATION_INTERNAL_URL}/_internal/prefetch/<repo id>``) with a short-lived system
grant for that repository (``waystation_common.system_grants``; header ``X-Ws-System-Grant``), and the server runs
the request through the format plugin as for a client, with the ``prefetch`` system principal. Its answer is NDJSON
(one line per file and per item, pings while large files stream); the job logs and counts it.

- ``prefetch.refresh`` (built-in, every 15 minutes): for each proxy repository with ``proxy.prefetch.refresh_popular``
  that is online, not offline and not auto-blocked, the metadata assets downloaded in the last ``window_days``, most
  downloaded first (at most ``top_n``; ``db.prefetch.popular_metadata``, index ``prefetch_popular``), whose copy is
  older than ``metadata_max_age_s - ahead_s`` (stale, or going stale before the next run) are sent in batches of
  ``REFRESH_BATCH``; the server revalidates each (conditional request; ``304`` moves only ``fetched_at``). Content is
  never refreshed. Prefetch requests are not downloads, so they never make an asset more popular.
- ``prefetch.warm`` (the repository's ``schedule``, ``job_definitions`` ``prefetch:<repo id>``, and "Run now"): the
  repository's ``proxy.prefetch.warm`` items, in one call; the server expands each item and fetches what is not
  cached. A run fails (and is retried once) only when every item failed.

Bounded: ``REPO_CONCURRENCY`` repositories at a time for refresh; the server bounds items per call and per process.
Without ``WAYSTATION_SECRET_KEYS`` no grant can be signed: the jobs skip with that reason.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis

from waystation_common.db import prefetch as prefetch_db
from waystation_common.db.client import Database, Document
from waystation_common.system_grants import HEADER, PREFETCH, SystemGrants
from waystation_worker.upstream import blocked

log = logging.getLogger('waystation.worker.prefetch')

REFRESH_BATCH = 50
REPO_CONCURRENCY = 2
MAX_LINE = 64 * 1024
MAX_ERRORS = 20
DEFAULT_MAX_AGE_S = 1800
# The server pings every 10 s while it works; anything quieter than this is a lost server.
READ_TIMEOUT_S = 120.0

LogLine = Callable[[str], Awaitable[None]]
Progress = Callable[[int, int | None, str | None], Awaitable[None]]


async def _no_log(line: str) -> None:
    return None


async def _no_progress(done: int, total: int | None, message: str | None) -> None:
    return None


class PrefetchUnavailableError(RuntimeError):
    """The worker cannot ask the server (no secret keys)."""


@dataclass
class Tally:
    items: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    files: int = 0
    fetched: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, line: dict[str, Any]) -> None:
        self.items += 1
        self.files += int(line.get('files') or 0)
        self.fetched += int(line.get('fetched') or 0)
        match line.get('outcome'):
            case 'ok':
                self.ok += 1
            case 'skipped':
                self.skipped += 1
            case _:
                self.failed += 1
                if len(self.errors) < MAX_ERRORS:
                    self.errors.append(f'{str(line.get("item"))[:200]}: {line.get("error") or line.get("outcome")}')

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            'items': self.items,
            'ok': self.ok,
            'failed': self.failed,
            'skipped': self.skipped,
            'files': self.files,
            'fetched': self.fetched,
        }
        if self.errors:
            out['errors'] = self.errors
        return out


class ServerClient:
    """Calls the server's internal prefetch route with a fresh system grant per call."""

    def __init__(
        self, base_url: str, grants: SystemGrants, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not grants.available:
            raise PrefetchUnavailableError('prefetch needs WAYSTATION_SECRET_KEYS (shared by the servers and workers)')
        self.base_url = base_url.rstrip('/')
        self.grants = grants
        timeout = httpx.Timeout(connect=10.0, read=READ_TIMEOUT_S, write=30.0, pool=10.0)
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(
        self, repo_id: str, op: str, items: Sequence[str], on_item: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """One call; ``on_item`` gets every ``item`` line. Raises ``RuntimeError`` when the server refuses the call or
        the answer ends without ``done``."""
        url = f'{self.base_url}/_internal/prefetch/{repo_id}'
        headers = {HEADER: self.grants.sign(PREFETCH, repo_id), 'Content-Type': 'application/json'}
        body = json.dumps({'op': op, 'items': list(items)}).encode()
        done = False
        async with self._client.stream('POST', url, content=body, headers=headers) as response:
            if response.status_code != 200:
                raise RuntimeError(f'the server refused the prefetch call ({response.status_code})')
            async for raw in response.aiter_lines():
                if not raw or len(raw) > MAX_LINE:
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(line, dict):
                    continue
                if line.get('event') == 'item':
                    await on_item(line)
                elif line.get('event') == 'done':
                    done = True
        if not done:
            raise RuntimeError('the prefetch answer ended early')


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _unavailable(valkey: Redis | None, repo: Document) -> str | None:
    """Why a repository is skipped (offline, blocked...), or None."""
    if repo.get('type') != 'proxy' or not isinstance(repo.get('proxy'), dict):
        return 'not a proxy repository'
    if not repo.get('online', True):
        return 'the repository is offline'
    if repo['proxy'].get('offline'):
        return 'offline mode'
    if await blocked(valkey, str(repo['_id'])):
        return 'the upstream is auto-blocked'
    return None


def due(docs: Sequence[Document], *, max_age_s: int, ahead_s: int, now: datetime) -> list[str]:
    """Paths of popular metadata whose copy is stale or goes stale within ``ahead_s``."""
    threshold = now - timedelta(seconds=max(0, max_age_s - ahead_s))
    out: list[str] = []
    for doc in docs:
        fetched = _aware((doc.get('upstream') or {}).get('fetched_at'))
        if fetched is None or fetched <= threshold:
            out.append(str(doc['path']))
    return out


async def refresh(
    db: Database,
    valkey: Redis | None,
    client: ServerClient,
    *,
    repo_id: str | None,
    window_days: int,
    top_n: int,
    ahead_s: int,
    now: datetime,
    log_line: LogLine = _no_log,
    progress: Progress = _no_progress,
) -> dict[str, Any]:
    repos = await prefetch_db.refresh_repositories(db, repo_id)
    since = now - timedelta(days=max(1, window_days))
    results: list[dict[str, Any]] = []
    total = Tally()
    done = 0
    limit = asyncio.Semaphore(REPO_CONCURRENCY)

    async def one(repo: Document) -> None:
        nonlocal done
        name, rid = str(repo.get('name') or ''), str(repo['_id'])
        async with limit:
            reason = await _unavailable(valkey, repo)
            if reason is not None:
                results.append({'repository': name, 'skipped': reason})
                await log_line(f'{name}: skipped ({reason})')
            else:
                popular = await prefetch_db.popular_metadata(db, rid, since=since, limit=top_n)
                max_age = int(repo['proxy'].get('metadata_max_age_s', DEFAULT_MAX_AGE_S) or 0)
                paths = due(popular, max_age_s=max_age, ahead_s=ahead_s, now=now)
                tally = Tally()

                async def item(line: dict[str, Any]) -> None:
                    tally.add(line)
                    total.add(line)

                for start in range(0, len(paths), REFRESH_BATCH):
                    await client.call(rid, 'refresh', paths[start : start + REFRESH_BATCH], item)
                outcome = {'repository': name, 'popular': len(popular), 'due': len(paths), **tally.as_dict()}
                results.append(outcome)
                await log_line(
                    f'{name}: {len(popular)} popular metadata files, {len(paths)} due, {tally.fetched} refreshed '
                    f'upstream, {tally.failed} failed' + (f' ({"; ".join(tally.errors[:3])})' if tally.errors else '')
                )
            done += 1
            await progress(done, len(repos), name)

    async with asyncio.TaskGroup() as group:
        for repo in repos:
            group.create_task(one(repo))
    return {'repositories': len(repos), 'results': sorted(results, key=lambda r: r['repository']), **total.as_dict()}


async def warm(
    db: Database,
    valkey: Redis | None,
    client: ServerClient,
    *,
    repo_id: str,
    log_line: LogLine = _no_log,
    progress: Progress = _no_progress,
) -> dict[str, Any]:
    repo = await prefetch_db.repository(db, repo_id)
    if repo is None:
        return {'repo_id': repo_id, 'skipped': 'the repository was deleted', **Tally().as_dict()}
    name = str(repo.get('name') or '')
    reason = await _unavailable(valkey, repo)
    if reason is not None:
        await log_line(f'{name}: skipped ({reason})')
        return {'repository': name, 'skipped': reason, **Tally().as_dict()}
    items = [str(i) for i in (repo['proxy'].get('prefetch') or {}).get('warm') or []]
    tally = Tally()
    await progress(0, len(items), name)

    async def item(line: dict[str, Any]) -> None:
        tally.add(line)
        text = str(line.get('item'))[:200]
        if line.get('outcome') == 'ok':
            await log_line(f'{text}: {line.get("files", 0)} files, {line.get("fetched", 0)} fetched upstream')
        else:
            await log_line(f'{text}: {line.get("outcome")}: {line.get("error") or ""}'.rstrip(': '))
        await progress(tally.items, len(items), text)

    if items:
        await client.call(repo_id, 'warm', items, item)
    result = {'repository': name, **tally.as_dict()}
    if items and tally.failed == tally.items:
        raise RuntimeError(f'every warm item failed: {"; ".join(tally.errors[:3])}')
    return result
