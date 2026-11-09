"""Live transfers and the activity feed in the API's shapes (OpenAPI ``Transfer``, ``ActivityEvent`` and the SSE
``LiveEvent`` variants), built from the emitter's Valkey state (``transfers`` hash, ``live`` channel, ``activity``
stream; ``waystation_common.events``)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from redis.asyncio import Redis

from waystation_common.events import ActivityEvent, CacheState, EventType, LiveMessage, Transfer, keys

log = logging.getLogger('waystation.live')

ACTIVITY_KINDS = (
    'download',
    'upload',
    'delete',
    'cache_miss',
    'upstream_state',
    'repository',
    'cleanup',
    'auth',
    'error',
)


Entry = tuple[str, dict[str, str]]


def _s(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _entries(raw: Any) -> list[Entry]:
    return [(_s(stream_id), {_s(k): _s(v) for k, v in (fields or {}).items()}) for stream_id, fields in raw or []]


async def xrange(valkey: Redis, *, after: str, count: int) -> list[Entry]:
    """``activity`` entries strictly after ``after``, oldest first."""
    return _entries(await valkey.xrange(keys.ACTIVITY, min=f'({after}', max='+', count=count))


async def xrevrange(
    valkey: Redis, *, before: str | None, count: int, until_ms: int | None = None, since_ms: int | None = None
) -> list[Entry]:
    """``activity`` entries strictly before ``before`` (or the newest), newest first; ``until_ms`` / ``since_ms``
    bound them by time (stream ids start with the entry's epoch milliseconds, so this is a pure range read)."""
    upper = f'({before}' if before else '+'
    if until_ms is not None and (before is None or _ms(before) > until_ms):
        upper = str(until_ms)  # an id prefix as the max: every entry of that millisecond
    lower = str(since_ms) if since_ms is not None else '-'
    return _entries(await valkey.xrevrange(keys.ACTIVITY, max=upper, min=lower, count=count))


def _ms(stream_id: str) -> int:
    return int(stream_id.partition('-')[0])


async def xread(valkey: Redis, *, after: str, block_ms: int, count: int) -> list[Entry]:
    raw: Any = await valkey.xread({keys.ACTIVITY: after}, block=block_ms, count=count)
    out: list[Entry] = []
    for _stream, entries in raw or []:
        out.extend(_entries(entries))
    return out


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def artifact_of(transfer: Transfer) -> str:
    return transfer.artifact or transfer.path.rsplit('/', 1)[-1] or transfer.path


def transfer_view(t: Transfer, *, state: str = 'active') -> dict[str, Any]:
    return {
        'id': t.id,
        'repository': t.repo,
        'format': t.format,
        'direction': t.direction.value,
        'artifact': artifact_of(t),
        'path': t.path,
        'component': t.component,
        'client': {'ip': t.client_ip or 'unknown', 'user': t.principal, 'agent': t.client_agent},
        'cache': t.cache.value,
        'followers': t.followers,
        'bytes_done': t.bytes_done,
        'bytes_total': t.bytes_total,
        'rate_bps': round(t.rate_bps, 1),
        'upstream_latency_ms': t.upstream_latency_ms,
        'started_at': _iso(t.started_at),
        'updated_at': _iso(t.updated_at),
        'state': state,
        'error': t.error,
    }


async def current_transfers(valkey: Redis) -> list[Transfer]:
    raw = await valkey.hgetall(keys.TRANSFERS)
    out = []
    for value in raw.values():
        try:
            out.append(Transfer.model_validate_json(value))
        except ValidationError:
            continue
    out.sort(key=lambda t: t.started_at)
    return out


def live_event(message: LiveMessage) -> dict[str, Any] | None:
    """A ``live`` pub/sub message as the SSE event the contract defines (None: not forwarded)."""
    t = message.transfer
    ts = _iso(t.updated_at)
    if message.type is EventType.TRANSFER_STARTED:
        return {'type': 'transfer.started', 'ts': ts, 'transfer': transfer_view(t)}
    if message.type is EventType.TRANSFER_PROGRESS:
        return {
            'type': 'transfer.progress',
            'ts': ts,
            'id': t.id,
            'bytes_done': t.bytes_done,
            'bytes_total': t.bytes_total,
            'rate_bps': round(t.rate_bps, 1),
            'cache': t.cache.value,
            'followers': t.followers,
            'upstream_latency_ms': t.upstream_latency_ms,
        }
    if message.type is EventType.TRANSFER_FINISHED:
        duration = (t.updated_at - t.started_at).total_seconds() * 1000
        return {
            'type': 'transfer.finished',
            'ts': ts,
            'id': t.id,
            'bytes_done': t.bytes_done,
            'duration_ms': round(duration, 1),
            'cache': t.cache.value,
        }
    if message.type is EventType.TRANSFER_FAILED:
        return {
            'type': 'transfer.failed',
            'ts': ts,
            'id': t.id,
            'bytes_done': t.bytes_done,
            'error': t.error or 'failed',
        }
    return None


def _who(event: ActivityEvent) -> str | None:
    if event.principal and event.principal != 'anonymous':
        return event.principal
    return event.client_ip or event.principal


def activity_view(stream_id: str, event: ActivityEvent) -> dict[str, Any] | None:
    """An ``activity`` stream entry in the API shape; None for entries the feed does not show (transfer starts)."""
    artifact = event.artifact or (event.path or '').rsplit('/', 1)[-1] or event.path
    who = _who(event) or 'someone'
    repo = event.repo or '?'
    kind: str
    outcome = 'success'
    match event.type:
        case EventType.TRANSFER_FINISHED if event.detail.get('direction') == 'upload':
            kind = 'upload'
            summary = f'{who} uploaded {artifact} to {repo}'
        case EventType.TRANSFER_FINISHED:
            kind = 'download'
            verb = 'fetched' if event.cache is CacheState.FETCHING else 'downloaded'
            summary = f'{who} {verb} {artifact} from {repo}'
        case EventType.CACHE_HIT | EventType.CACHE_STALE:
            kind = 'download'
            stale = ' (stale copy)' if event.type is EventType.CACHE_STALE else ''
            summary = f'{who} downloaded {artifact} from {repo}{stale}'
        case EventType.TRANSFER_FAILED:
            kind, outcome = 'error', 'failure'
            reason = event.detail.get('error') or 'failed'
            if event.detail.get('direction') == 'upload':
                summary = f'Uploading {artifact} to {repo} failed: {reason}'
            else:
                summary = f'Fetching {artifact} into {repo} failed: {reason}'
        case EventType.CACHE_MISS:
            kind, outcome = 'cache_miss', 'info'
            summary = f'{artifact} was not cached in {repo}'
        case EventType.UPSTREAM_STATE:
            kind, outcome = 'upstream_state', 'info'
            summary = f'Upstream of {repo} is {event.detail.get("state", "changed")}'
        case EventType.ASSET_CREATED:
            kind = 'upload'
            summary = f'{who} uploaded {artifact} to {repo}'
        case EventType.ASSET_DELETED:
            kind = 'delete'
            summary = f'{artifact} was deleted from {repo}'
        case EventType.JOB_STARTED | EventType.JOB_FINISHED | EventType.JOB_FAILED:
            kind = 'cleanup'
            outcome = 'failure' if event.type is EventType.JOB_FAILED else 'info'
            summary = str(event.detail.get('summary') or event.type.value)
        case _:
            return None
    return {
        'id': stream_id,
        'ts': _iso(event.ts),
        'kind': kind,
        'summary': summary,
        'outcome': outcome,
        'repository': event.repo,
        'format': event.format,
        'artifact': artifact,
        'path': event.path,
        'principal': _who(event),
        'cache': event.cache.value if event.cache else None,
        'bytes': event.bytes,
        'duration_ms': event.duration_ms,
    }


def parse_activity(fields: dict[str, str]) -> ActivityEvent | None:
    try:
        return ActivityEvent.model_validate_json(fields.get('data', ''))
    except ValidationError, json.JSONDecodeError:
        return None


@dataclass(frozen=True)
class ActivityFilter:
    """What the Activity page (and its CSV export) asks for. ``visible`` = repository names the principal may see;
    events of other repositories are never returned. ``principal`` matches case-insensitively as a substring (a user
    name or a client address prefix); every other field is an exact match. Times are epoch milliseconds."""

    visible: frozenset[str]
    repo: str | None = None
    kind: str | None = None
    format: str | None = None
    principal: str | None = None
    cache: str | None = None
    outcome: str | None = None
    since_ms: int | None = None
    until_ms: int | None = None

    def view(self, stream_id: str, fields: dict[str, str]) -> dict[str, Any] | None:
        """The entry in the API shape when it passes the filter, else None."""
        event = parse_activity(fields)
        if event is None or (event.repo and event.repo not in self.visible):
            return None
        if self.repo and event.repo != self.repo:
            return None
        if self.format and event.format != self.format:
            return None
        view = activity_view(stream_id, event)
        if view is None:
            return None
        if self.kind and view['kind'] != self.kind:
            return None
        if self.cache and view['cache'] != self.cache:
            return None
        if self.outcome and view['outcome'] != self.outcome:
            return None
        if self.principal and self.principal.lower() not in (view['principal'] or '').lower():
            return None
        return view


async def activity_page(
    valkey: Redis,
    flt: ActivityFilter,
    *,
    cursor: str | None,
    limit: int,
    scan_cap: int = 5000,
) -> tuple[list[dict[str, Any]], str | None]:
    """Newest first. Reads at most ``scan_cap`` entries per call; when that runs out before ``limit`` matches, the
    page is short and ``next_cursor`` continues the scan (sparse filters page through history without a long call)."""
    items: list[dict[str, Any]] = []
    before = cursor
    scanned = 0
    last_id: str | None = None
    while len(items) < limit and scanned < scan_cap:
        batch = await xrevrange(
            valkey,
            before=before,
            count=min(500, max(limit * 2, 50), scan_cap - scanned),
            until_ms=flt.until_ms,
            since_ms=flt.since_ms,
        )
        if not batch:
            return items, None
        for stream_id, fields in batch:
            scanned += 1
            last_id = stream_id
            view = flt.view(stream_id, fields)
            if view is None:
                continue
            items.append(view)
            if len(items) >= limit:
                return items, stream_id
        before = last_id
    return items, last_id if scanned >= scan_cap else None


async def activity_scan(valkey: Redis, flt: ActivityFilter, *, max_rows: int) -> AsyncIterator[list[dict[str, Any]]]:
    """Every matching entry, newest first, in batches (CSV export); stops after ``max_rows`` matches."""
    before: str | None = None
    sent = 0
    while sent < max_rows:
        batch = await xrevrange(valkey, before=before, count=500, until_ms=flt.until_ms, since_ms=flt.since_ms)
        if not batch:
            return
        out: list[dict[str, Any]] = []
        for stream_id, fields in batch:
            view = flt.view(stream_id, fields)
            if view is not None:
                out.append(view)
                if sent + len(out) >= max_rows:
                    break
        before = batch[-1][0]
        if out:
            sent += len(out)
            yield out
