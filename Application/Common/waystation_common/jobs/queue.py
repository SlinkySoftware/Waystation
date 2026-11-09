"""The job queue on Valkey Streams (ARCHITECTURE §10): every state transition is one atomic Lua script.

Per queue: stream ``jobs:<queue>`` with consumer group ``workers`` (field ``job`` = ``JobEnvelope`` JSON) and the
sorted set ``jobs:<queue>:delayed`` for envelopes whose ``run_after`` is in the future. Per dedupe key: hash
``job:dedupe:<key>``:

* ``state``: ``pending`` (queued or delayed) or ``running``; ``job``: the envelope id it belongs to;
* ``outbox``: comma-separated ``job_outbox`` ids the pending/running job covers (marked done when it finishes);
* ``rerun``: envelope JSON of a request that arrived while running; ``rerun_outbox``: the outbox ids it covers;
* ``owner``: consumer running it.

Dirty-flag dedupe: enqueue onto a *pending* key merges (the queued job will see the change); onto a *running* key
sets ``rerun``, and the finish script queues that rerun exactly once, however many requests arrived. A key without
a hash (never seen, finished, or Valkey lost its data) starts a new job.

Delivery is at least once: a consumer that dies leaves its entry in the group's pending list; ``touch`` (the
running consumer's heartbeat) keeps it fresh, ``reclaim`` (XAUTOCLAIM) hands entries idle longer than the visibility
timeout to another consumer, with their delivery count. Retries go through the delayed set with backoff; the
attempt limit dead-letters the envelope to ``jobs:dead``.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from waystation_common.ids import new_id
from waystation_common.jobs import keys
from waystation_common.jobs.model import JobEnvelope

EnqueueStatus = Literal['enqueued', 'merged', 'rerun', 'running']
FinishMode = Literal['done', 'retry', 'dead']

DEDUPE_TTL_MS = 24 * 3600 * 1000  # safety net: a key stuck by a bug frees itself within a day

_ENQUEUE_LUA = """
local state = redis.call('HGET', KEYS[1], 'state')
local function append(name)
  if ARGV[3] == '' then return end
  local cur = redis.call('HGET', KEYS[1], name)
  if cur and cur ~= '' then cur = cur .. ',' .. ARGV[3] else cur = ARGV[3] end
  redis.call('HSET', KEYS[1], name, cur)
end
if state == 'pending' then
  append('outbox')
  redis.call('PEXPIRE', KEYS[1], ARGV[5])
  return {'merged', redis.call('HGET', KEYS[1], 'job')}
end
if state == 'running' then
  local job = redis.call('HGET', KEYS[1], 'job')
  if job == ARGV[2] then return {'running', job} end
  redis.call('HSET', KEYS[1], 'rerun', ARGV[1])
  append('rerun_outbox')
  redis.call('PEXPIRE', KEYS[1], ARGV[5])
  return {'rerun', job}
end
if ARGV[4] ~= '' then
  redis.call('ZADD', KEYS[3], ARGV[4], ARGV[1])
else
  redis.call('XADD', KEYS[2], '*', 'job', ARGV[1])
end
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[1], 'state', 'pending', 'job', ARGV[2], 'outbox', ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[5])
return {'enqueued', ARGV[2]}
"""

# Retry a dead letter: KEYS[1..3] and ARGV[1..5] as for enqueue, KEYS[4] = jobs:dead, KEYS[5] = jobs:dead:retried,
# ARGV[6] = the dead entry id, ARGV[7] = TTL (s) of the retried record. Enqueue + delete + record in one step.
_RETRY_DEAD_LUA = (
    """
local previous = redis.call('HGET', KEYS[5], ARGV[6])
if previous then return {'gone', previous} end
if #redis.call('XRANGE', KEYS[4], ARGV[6], ARGV[6]) == 0 then return {'gone', ''} end
local function enqueue()
"""
    + _ENQUEUE_LUA
    + """
end
local result = enqueue()
redis.call('XDEL', KEYS[4], ARGV[6])
redis.call('HSET', KEYS[5], ARGV[6], result[2])
redis.call('EXPIRE', KEYS[5], ARGV[7])
return result
"""
)

_START_LUA = """
local state = redis.call('HGET', KEYS[1], 'state')
if not state then
  redis.call('HSET', KEYS[1], 'state', 'running', 'job', ARGV[1], 'owner', ARGV[2], 'outbox', '')
  redis.call('PEXPIRE', KEYS[1], ARGV[3])
  return {'run', ''}
end
if redis.call('HGET', KEYS[1], 'job') ~= ARGV[1] then
  if state == 'running' and not redis.call('HGET', KEYS[1], 'rerun') then
    redis.call('HSET', KEYS[1], 'rerun', ARGV[4])
  end
  return {'skip', ''}
end
redis.call('HSET', KEYS[1], 'state', 'running', 'owner', ARGV[2])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return {'run', redis.call('HGET', KEYS[1], 'outbox') or ''}
"""

_FINISH_LUA = """
redis.call('XACK', KEYS[2], ARGV[1], ARGV[2])
redis.call('XDEL', KEYS[2], ARGV[2])
if ARGV[4] == 'dead' then
  redis.call('XADD', KEYS[4], 'MAXLEN', '~', ARGV[8], '*', 'job', ARGV[5], 'error', ARGV[9], 'ts', ARGV[10])
end
local job = redis.call('HGET', KEYS[1], 'job')
if job and job ~= ARGV[3] then return {'stale', ''} end
if ARGV[4] == 'retry' then
  local outbox = redis.call('HGET', KEYS[1], 'outbox') or ''
  local extra = redis.call('HGET', KEYS[1], 'rerun_outbox')
  if extra and extra ~= '' then
    if outbox ~= '' then outbox = outbox .. ',' .. extra else outbox = extra end
  end
  redis.call('ZADD', KEYS[3], ARGV[6], ARGV[5])
  redis.call('DEL', KEYS[1])
  redis.call('HSET', KEYS[1], 'state', 'pending', 'job', ARGV[3], 'outbox', outbox)
  redis.call('PEXPIRE', KEYS[1], ARGV[7])
  return {'retry', ARGV[3]}
end
local rerun = redis.call('HGET', KEYS[1], 'rerun')
if rerun then
  local env = cjson.decode(rerun)
  local outbox = redis.call('HGET', KEYS[1], 'rerun_outbox') or ''
  redis.call('XADD', KEYS[2], '*', 'job', rerun)
  redis.call('DEL', KEYS[1])
  redis.call('HSET', KEYS[1], 'state', 'pending', 'job', env['id'], 'outbox', outbox)
  redis.call('PEXPIRE', KEYS[1], ARGV[7])
  return {'rerun', env['id']}
end
redis.call('DEL', KEYS[1])
return {'done', ''}
"""

_TOUCH_LUA = """
local p = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #p == 0 or p[1][2] ~= ARGV[3] then return 0 end
redis.call('XCLAIM', KEYS[1], ARGV[1], ARGV[3], 0, ARGV[2], 'JUSTID')
redis.call('PEXPIRE', KEYS[2], ARGV[4])
return 1
"""

_PROMOTE_LUA = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
for _, env in ipairs(due) do
  redis.call('XADD', KEYS[2], '*', 'job', env)
  redis.call('ZREM', KEYS[1], env)
end
return #due
"""


@dataclass(frozen=True)
class EnqueueResult:
    status: EnqueueStatus
    job_id: str  # the job the request is now part of (merged/rerun: the existing job)


@dataclass(frozen=True)
class Delivery:
    """A stream entry handed to this consumer. ``envelope`` is ``None`` when the entry is malformed."""

    queue: str
    entry_id: str
    raw: str
    envelope: JobEnvelope | None
    deliveries: int = 1  # times the group delivered it (1 = first delivery; > 1 = reclaimed from a lost worker)


@dataclass(frozen=True)
class StartResult:
    run: bool  # False: a stale duplicate of a key owned by another job; drop it
    outbox_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QueueStats:
    """One queue's figures (``JobQueue.stats``; Prometheus job metrics)."""

    waiting: int = 0  # in the stream, not yet delivered to a worker
    running: int = 0  # delivered, not acknowledged (running, or held by a worker that died until reclaimed)
    delayed: int = 0  # retry backoff and ``run_after`` jobs not due yet
    oldest_waiting_age_s: float = 0.0  # age of the oldest waiting entry (0 when none)


@dataclass(frozen=True)
class DeadLetter:
    """An entry of ``jobs:dead``. ``envelope`` is None for a malformed entry (``raw`` holds what was there)."""

    id: str
    raw: str
    envelope: JobEnvelope | None
    error: str
    dead_at: datetime

    @classmethod
    def from_entry(cls, entry_id: Any, fields: Any) -> DeadLetter:
        fields = fields or {}
        raw = _str(fields.get('job', ''))
        ts = fields.get('ts')
        dead_at: datetime | None
        try:
            dead_at = datetime.fromisoformat(_str(ts)) if ts else None
        except ValueError:
            dead_at = None
        if dead_at is None:
            dead_at = datetime.fromtimestamp(_stream_ms(entry_id) / 1000, UTC)
        return cls(
            id=_str(entry_id), raw=raw, envelope=_envelope(raw), error=_str(fields.get('error', '')), dead_at=dead_at
        )


def _stream_ms(entry_id: Any) -> int:
    return int(_str(entry_id).split('-', 1)[0])


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _str(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _split(text: Any) -> list[str]:
    return [part for part in _str(text).split(',') if part] if text else []


def _envelope(raw: str) -> JobEnvelope | None:
    try:
        return JobEnvelope.model_validate_json(raw)
    except ValidationError, ValueError:
        return None


class JobQueue:
    """Valkey side of the job system. The server uses ``enqueue`` (through ``JobClient``); the worker the rest."""

    def __init__(self, redis: Redis, *, dedupe_ttl_ms: int = DEDUPE_TTL_MS) -> None:
        self.redis = redis
        self._ttl = str(dedupe_ttl_ms)

    # ------------------------------------------------------------------ producer
    async def enqueue(self, envelope: JobEnvelope, *, outbox_id: str | None = None) -> EnqueueResult:
        run_after = ''
        if envelope.run_after is not None and envelope.run_after.timestamp() > time.time():
            run_after = str(_ms(envelope.run_after))
        status, job_id = await self.redis.eval(
            _ENQUEUE_LUA,
            3,
            keys.dedupe(envelope.dedupe_key),
            keys.stream(envelope.queue),
            keys.delayed(envelope.queue),
            envelope.to_json(),
            envelope.id,
            outbox_id or '',
            run_after,
            self._ttl,
        )
        return EnqueueResult(status=_str(status), job_id=_str(job_id))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ consumer
    async def ensure_groups(self, queues: list[str] | tuple[str, ...]) -> None:
        """Create the consumer groups (from the start of the stream: jobs queued before any worker ran count)."""
        for queue in queues:
            try:
                await self.redis.xgroup_create(keys.stream(queue), keys.GROUP, id='0', mkstream=True)
            except ResponseError as exc:
                if 'BUSYGROUP' not in str(exc):
                    raise

    async def read(self, consumer: str, queues: list[str], *, count: int, block_ms: int) -> list[Delivery]:
        """New entries for this consumer (blocks up to ``block_ms``). Recreates groups lost with Valkey's data."""
        if not queues or count < 1:
            return []
        streams = {keys.stream(q): '>' for q in queues}
        try:
            response = await self.redis.xreadgroup(keys.GROUP, consumer, streams, count=count, block=block_ms)  # type: ignore[arg-type]
        except ResponseError as exc:
            if 'NOGROUP' not in str(exc):
                raise
            await self.ensure_groups(queues)
            return []
        items: Any = response.items() if isinstance(response, dict) else (response or [])
        out: list[Delivery] = []
        for stream_name, entries in items:
            queue = _str(stream_name).removeprefix('jobs:')
            for entry_id, fields in entries:
                out.append(self._delivery(queue, entry_id, fields, 1))
        return out

    @staticmethod
    def _delivery(queue: str, entry_id: Any, fields: Any, deliveries: int) -> Delivery:
        raw = _str((fields or {}).get('job', ''))
        return Delivery(queue=queue, entry_id=_str(entry_id), raw=raw, envelope=_envelope(raw), deliveries=deliveries)

    async def reclaim(self, consumer: str, queue: str, *, min_idle_ms: int, count: int) -> list[Delivery]:
        """Take over entries whose consumer stopped heartbeating (idle > ``min_idle_ms``)."""
        if count < 1:
            return []
        stream = keys.stream(queue)
        try:
            result = await self.redis.xautoclaim(stream, keys.GROUP, consumer, min_idle_ms, '0-0', count=count)
        except ResponseError as exc:
            if 'NOGROUP' not in str(exc):
                raise
            return []
        entries = result[1] if len(result) > 1 else []
        out: list[Delivery] = []
        for entry_id, fields in entries:
            if fields is None:  # deleted while pending
                continue
            info = await self.redis.xpending_range(stream, keys.GROUP, min=entry_id, max=entry_id, count=1)
            deliveries = int(info[0]['times_delivered']) if info else 1
            out.append(self._delivery(queue, entry_id, fields, deliveries))
        return out

    async def touch(self, delivery: Delivery, consumer: str) -> bool:
        """Heartbeat of a running job: reset its idle time (without counting a delivery). False = ownership lost."""
        dedupe = keys.dedupe(delivery.envelope.dedupe_key) if delivery.envelope else 'job:dedupe:-'
        result = await self.redis.eval(
            _TOUCH_LUA, 2, keys.stream(delivery.queue), dedupe, keys.GROUP, delivery.entry_id, consumer, self._ttl
        )
        return bool(int(result))

    async def start(self, delivery: Delivery, consumer: str) -> StartResult:
        env = delivery.envelope
        assert env is not None
        action, outbox = await self.redis.eval(
            _START_LUA, 1, keys.dedupe(env.dedupe_key), env.id, consumer, self._ttl, delivery.raw
        )
        return StartResult(run=_str(action) == 'run', outbox_ids=_split(outbox))

    async def finish(
        self,
        delivery: Delivery,
        mode: FinishMode,
        *,
        envelope: JobEnvelope | None = None,
        retry_at: datetime | None = None,
        error: str = '',
    ) -> tuple[str, str]:
        """Ack the entry and move the key on: ``done`` (or queue the rerun), ``retry`` (``envelope`` with the next
        attempt, due at ``retry_at``) or ``dead`` (``envelope`` to ``jobs:dead``). Returns (outcome, job id)."""
        env = delivery.envelope
        assert env is not None
        payload = (envelope or env).to_json()
        outcome, job_id = await self.redis.eval(
            _FINISH_LUA,
            4,
            keys.dedupe(env.dedupe_key),
            keys.stream(delivery.queue),
            keys.delayed(delivery.queue),
            keys.DEAD,
            keys.GROUP,
            delivery.entry_id,
            env.id,
            mode,
            payload,
            str(_ms(retry_at)) if retry_at else '0',
            self._ttl,
            str(keys.DEAD_MAXLEN),
            error[:2000],
            datetime.now(UTC).isoformat(),
        )
        return _str(outcome), _str(job_id)

    async def drop(self, delivery: Delivery, *, dead_reason: str | None = None) -> None:
        """Ack and delete an entry without touching any dedupe key (stale duplicate, or malformed: dead-lettered)."""
        stream = keys.stream(delivery.queue)
        async with self.redis.pipeline(transaction=True) as pipe:
            if dead_reason is not None:
                pipe.xadd(
                    keys.DEAD,
                    {'job': delivery.raw, 'error': dead_reason, 'ts': datetime.now(UTC).isoformat()},
                    maxlen=keys.DEAD_MAXLEN,
                )
            pipe.xack(stream, keys.GROUP, delivery.entry_id)
            pipe.xdel(stream, delivery.entry_id)
            await pipe.execute()

    async def promote_due(self, queue: str, *, now: float, limit: int = 500) -> int:
        """Move delayed envelopes that are due into the stream."""
        moved = await self.redis.eval(
            _PROMOTE_LUA, 2, keys.delayed(queue), keys.stream(queue), str(int(now * 1000)), str(limit)
        )
        return int(moved)

    # ------------------------------------------------------------------ maintenance
    async def epoch(self) -> str:
        """The data epoch of this Valkey: created on first use; a new value means Valkey lost its data."""
        await self.redis.set(keys.EPOCH, new_id(), nx=True)
        return _str(await self.redis.get(keys.EPOCH))

    async def prune_consumers(self, queue: str, *, idle_ms: int, keep: str) -> int:
        """Delete consumers with no pending entries idle longer than ``idle_ms`` (workers that went away)."""
        try:
            consumers = await self.redis.xinfo_consumers(keys.stream(queue), keys.GROUP)
        except ResponseError:
            return 0
        removed = 0
        for info in consumers:
            name = _str(info['name'])
            if name != keep and int(info['pending']) == 0 and int(info['idle']) > idle_ms:
                await self.redis.xgroup_delconsumer(keys.stream(queue), keys.GROUP, name)
                removed += 1
        return removed

    async def stats(self, queues: tuple[str, ...] | list[str], *, now: float | None = None) -> dict[str, QueueStats]:
        """Waiting, running and delayed jobs per queue and the oldest waiting age (two pipelined round trips)."""
        now_ms = (time.time() if now is None else now) * 1000
        async with self.redis.pipeline(transaction=False) as pipe:
            for queue in queues:
                pipe.xlen(keys.stream(queue))
                pipe.xinfo_groups(keys.stream(queue))
                pipe.zcard(keys.delayed(queue))
            raw = await pipe.execute(raise_on_error=False)
        figures: dict[str, tuple[int, int, int, str]] = {}
        for i, queue in enumerate(queues):
            length, groups, delayed = raw[3 * i : 3 * i + 3]
            length = length if isinstance(length, int) else 0
            delayed = delayed if isinstance(delayed, int) else 0
            group = (
                next((g for g in groups if _str(g.get('name')) == keys.GROUP), None)
                if isinstance(groups, list)
                else None
            )
            running = int(group.get('pending') or 0) if group else 0
            last = _str(group.get('last-delivered-id') or '0-0') if group else '0-0'
            figures[queue] = (max(0, length - running), running, delayed, last)
        oldest: dict[str, int] = {}
        waiting = [q for q, f in figures.items() if f[0] > 0]
        if waiting:
            async with self.redis.pipeline(transaction=False) as pipe:
                for queue in waiting:
                    pipe.xrange(keys.stream(queue), min=f'({figures[queue][3]}', count=1)
                entries = await pipe.execute(raise_on_error=False)
            for queue, found in zip(waiting, entries, strict=True):
                if isinstance(found, list) and found:
                    oldest[queue] = _stream_ms(found[0][0])
        return {
            queue: QueueStats(
                waiting=f[0],
                running=f[1],
                delayed=f[2],
                oldest_waiting_age_s=max(0.0, (now_ms - oldest[queue]) / 1000) if queue in oldest else 0.0,
            )
            for queue, f in figures.items()
        }

    async def dead_count(self) -> int:
        return int(await self.redis.xlen(keys.DEAD))

    async def dead_page(self, *, before: str | None, count: int) -> list[DeadLetter]:
        """Dead letters newest first, starting after (older than) the entry ``before``; up to ``count``."""
        entries: Any = await self.redis.xrevrange(keys.DEAD, max=f'({before}' if before else '+', min='-', count=count)
        return [DeadLetter.from_entry(entry_id, fields) for entry_id, fields in entries or []]

    async def dead_get(self, entry_id: str) -> DeadLetter | None:
        entries: Any = await self.redis.xrange(keys.DEAD, min=entry_id, max=entry_id, count=1)
        return DeadLetter.from_entry(*entries[0]) if entries else None

    async def retry_dead(self, entry_id: str, envelope: JobEnvelope) -> EnqueueResult | None:
        """Queue ``envelope`` (a fresh request for the dead letter's work) and remove the dead letter, atomically.

        The usual dedupe applies (a pending job with the same key absorbs it: ``merged``). Idempotent: a dead letter
        is retried once: None when the entry is gone (retried before: ``retried_job`` names that job; or discarded or
        trimmed)."""
        status, job_id = await self.redis.eval(
            _RETRY_DEAD_LUA,
            5,
            keys.dedupe(envelope.dedupe_key),
            keys.stream(envelope.queue),
            keys.delayed(envelope.queue),
            keys.DEAD,
            keys.DEAD_RETRIED,
            envelope.to_json(),
            envelope.id,
            '',
            '',
            self._ttl,
            entry_id,
            str(keys.DEAD_RETRIED_TTL_S),
        )
        if _str(status) == 'gone':  # retried before (see retried_job), discarded or trimmed
            return None
        return EnqueueResult(status=_str(status), job_id=_str(job_id))  # type: ignore[arg-type]

    async def retried_job(self, entry_id: str) -> str | None:
        """The job a retry of this dead letter joined (remembered for ``DEAD_RETRIED_TTL_S``)."""
        value = await self.redis.hget(keys.DEAD_RETRIED, entry_id)
        return _str(value) if value else None

    async def discard_dead(self, entry_id: str) -> bool:
        """Delete a dead letter; False when it was already gone (discarding twice is harmless)."""
        return bool(int(await self.redis.xdel(keys.DEAD, entry_id)))

    async def dead_letters(self, count: int = 100) -> list[dict[str, Any]]:
        """Most recent dead-lettered jobs, newest first: ``{'id', 'job', 'error', 'ts'}``."""
        out: list[dict[str, Any]] = []
        entries: Any = await self.redis.xrevrange(keys.DEAD, count=count)
        for entry_id, fields in entries or []:
            job: Any = fields.get('job', '')
            with contextlib.suppress(ValueError):
                job = json.loads(job)
            out.append({'id': _str(entry_id), 'job': job, 'error': fields.get('error', ''), 'ts': fields.get('ts')})
        return out
