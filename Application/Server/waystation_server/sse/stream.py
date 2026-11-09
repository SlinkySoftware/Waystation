"""One client's Server-Sent Events stream (OpenAPI ``streamEvents``).

On connect: ``retry: 3000``; a ``transfers.snapshot`` (current in-flight transfers) when the client follows
transfers; when it sends ``Last-Event-ID`` (header, or ``last_event_id`` query parameter for clients that reopen
the stream themselves) and follows activity, the missed ``activity`` entries (up to ``REPLAY_MAX``) with their ids.
Then live events as they come, and a ``: hb`` comment every 15 s. The client is subscribed to the hub before the
snapshot and replay are read, and replayed ids are not sent twice, so nothing falls between replay and live.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from waystation_server.sse.hub import Frame, Hub, Subscriber
from waystation_server.stats import live

HEARTBEAT_S = 15.0
REPLAY_MAX = 1000
STREAM_ID = re.compile(r'^\d{1,20}-\d{1,20}$')
TOPIC = re.compile(r'^(transfers|stats|activity|tasks|repo:[A-Za-z0-9][A-Za-z0-9._-]{1,62})$')


def parse_topics(raw: str) -> frozenset[str] | None:
    topics = frozenset(t.strip() for t in raw.split(',') if t.strip())
    if not topics or any(not TOPIC.fullmatch(t) for t in topics):
        return None
    return topics


def valid_event_id(value: str | None) -> str | None:
    return value if value and STREAM_ID.fullmatch(value) else None


def _order(stream_id: str) -> tuple[int, int]:
    ms, _, seq = stream_id.partition('-')
    return int(ms), int(seq or 0)


def encode(frame: Frame) -> bytes:
    stream_id, event, data = frame
    head = f'id: {stream_id}\n' if stream_id else ''
    return f'{head}event: {event}\ndata: {data}\n\n'.encode()


async def event_stream(hub: Hub, sub: Subscriber, *, last_event_id: str | None) -> AsyncIterator[bytes]:
    await hub.subscribe(sub)
    try:
        yield b'retry: 3000\n\n'
        wants_transfers = 'transfers' in sub.topics or bool(sub.repo_topics)
        wants_activity = 'activity' in sub.topics or bool(sub.repo_topics)
        if wants_transfers:
            transfers = [
                live.transfer_view(t)
                for t in await live.current_transfers(hub.valkey)
                if sub.wants('transfers', t.repo)
            ]
            snapshot = {
                'type': 'transfers.snapshot',
                'ts': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
                'transfers': transfers,
            }
            yield encode((None, 'transfers.snapshot', json.dumps(snapshot, separators=(',', ':'), default=str)))
        replayed: tuple[int, int] | None = None
        if last_event_id and wants_activity:
            for stream_id, fields in await live.xrange(hub.valkey, after=last_event_id, count=REPLAY_MAX):
                replayed = _order(stream_id)
                parsed = live.parse_activity(fields)
                if parsed is None or not sub.wants('activity', parsed.repo):
                    continue
                view = live.activity_view(stream_id, parsed)
                if view is not None:
                    event = {'type': 'activity', 'ts': view['ts'], 'event': view}
                    yield encode((stream_id, 'activity', json.dumps(event, separators=(',', ':'), default=str)))
        while True:
            try:
                frame = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_S)
            except TimeoutError:
                yield b': hb\n\n'
                continue
            if frame is None:
                return
            if frame[0] and replayed is not None and _order(frame[0]) <= replayed:
                continue
            yield encode(frame)
    finally:
        hub.unsubscribe(sub)
