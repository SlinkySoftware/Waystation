"""Events, live transfers and per-minute stats (SPECIFICATION §4.7, ARCHITECTURE §8, DATA_MODEL §2).

Producer API (server; one ``EventEmitter`` per process, started in the app lifespan)::

    emitter = EventEmitter(create_valkey(settings))
    await emitter.start()                                   # background flush every 250 ms
    emitter.transfer_started(Transfer(id=new_id(), repo_id=..., repo='npm-proxy', format='npm', path=...,
                                      cache=CacheState.FETCHING, started_at=now, updated_at=now))
    emitter.transfer_progress(tid, bytes_done, bytes_total=n)   # call as often as you like
    emitter.transfer_finished(tid, status=200)                  # or error='upstream reset'
    emitter.request(repo_id=..., format='npm', cache=CacheState.HIT, duration_ms=3.2, bytes_out=1234)
    emitter.activity(ActivityEvent(type=EventType.ASSET_DELETED, ts=now, ...))
    await emitter.aclose()                                  # final flush

Every producer call is synchronous, O(1) and never raises or waits on Valkey: it appends to a bounded in-memory
queue (overflow is counted in ``emitter.dropped``) and the flush task sends everything in one pipeline. Progress is
kept in memory and only the latest state per transfer is sent each flush, so ``live`` sees ≤ 4 Hz per transfer.
If Valkey is down the batch is dropped (``failed_flushes``): live stats pause, serving is unaffected.

What lands in Valkey (consumers: SSE and stats APIs, T-S11; rollups, worker):

- ``transfers`` hash: transfer id -> ``Transfer`` JSON (current state; removed on finish; stale entries swept by
  the worker rollup). Reconnecting clients read this, never a replay.
- ``live`` pub/sub: ``LiveMessage`` JSON ``{"type": "transfer.started|progress|finished|failed", "transfer": {...}}``.
- ``activity`` stream (``MAXLEN ~ 200000``): fields ``type`` and ``data`` (``ActivityEvent`` JSON); stream ids are
  the SSE ``Last-Event-ID``.
- ``cnt:<minute>:<repo_id>`` hash: counters ``keys.COUNTERS`` + ``format``; ``lat:<minute>:<repo_id>`` hash:
  latency bucket (``keys.LATENCY_FIELDS``) -> count; ``<minute>`` = epoch minute; ``stats:pending`` set lists
  ``<minute>:<repo_id>`` pairs awaiting rollup. Counting: HIT and REVALIDATED -> ``hits``, STALE -> ``stale``
  (all three add ``bytes_out_cache``: bandwidth saved); FETCHING -> ``misses``, COALESCED -> ``coalesced`` (both
  add ``bytes_out_upstream``; FETCHING also ``bytes_in_upstream``); ERROR -> ``errors``; uploads add
  ``bytes_in_upload``. Every request adds ``requests`` and one latency sample.

The worker rolls closed minutes (end + 5 s grace) into the ``stats_minute`` time-series collection exactly once
(``waystation_worker.rollup``). Percentiles are computed at query time from summed histograms
(``keys.percentile``).
"""

from waystation_common.events.emitter import EventEmitter
from waystation_common.events.schema import (
    FROM_CACHE,
    ActivityEvent,
    CacheState,
    Direction,
    EventType,
    LiveMessage,
    Transfer,
)

__all__ = [
    'FROM_CACHE',
    'ActivityEvent',
    'CacheState',
    'Direction',
    'EventEmitter',
    'EventType',
    'LiveMessage',
    'Transfer',
]
