"""The event stream across server replicas (T-S11; ARCHITECTURE §8): every replica runs its own hub on the shared
Valkey, so an event emitted by one replica reaches the subscribers of all of them; when a replica goes away, its
clients reconnect to another one with ``Last-Event-ID`` and get the activity they missed, then live events again."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from ws_fake_upstream import FakeUpstream
from ws_testing import FilesPlugin, add_repo

from waystation_common.events import ActivityEvent, CacheState, EventType, Transfer
from waystation_common.settings import Settings
from waystation_server.security.principal import ANONYMOUS
from waystation_server.services import Services
from waystation_server.sse.hub import Subscriber
from waystation_server.sse.stream import event_stream

pytestmark = pytest.mark.integration

Frames = list[tuple[str | None, str, dict[str, Any]]]


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    async with FakeUpstream() as fake:
        yield fake


async def replica(settings: Settings) -> Services:
    svc = Services.create(settings)
    svc.formats.set_plugin(FilesPlugin(svc))  # type: ignore[arg-type]
    await svc.start()
    return svc


async def collect(gen: AsyncIterator[bytes], frames: Frames) -> None:
    buffer = ''
    async for chunk in gen:
        buffer += chunk.decode()
        while '\n\n' in buffer:
            block, buffer = buffer.split('\n\n', 1)
            fields = dict(
                line.split(': ', 1) for line in block.split('\n') if ': ' in line and not line.startswith(':')
            )
            if 'event' in fields:
                frames.append((fields.get('id'), fields['event'], json.loads(fields['data'])))


async def wait(predicate: Callable[[], bool], limit_s: float = 5.0) -> None:
    async with asyncio.timeout(limit_s):
        while not predicate():  # noqa: ASYNC110 - polling the collector is simplest here
            await asyncio.sleep(0.05)


def follow(svc: Services, frames: Frames, last_event_id: str | None = None) -> asyncio.Task[None]:
    sub = Subscriber(principal=ANONYMOUS, topics=frozenset({'transfers', 'activity'}))
    return asyncio.create_task(collect(event_stream(svc.hub, sub, last_event_id=last_event_id), frames))


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)


def upload(svc: Services, repo_id: str, path: str) -> None:
    svc.emitter.activity(
        ActivityEvent(
            type=EventType.ASSET_CREATED, ts=datetime.now(UTC), repo_id=repo_id, repo='files', format='raw', path=path
        )
    )


def paths(frames: Frames) -> list[str]:
    return [f[2]['event']['path'] for f in frames if f[1] == 'activity' and f[2]['event'].get('path')]


async def test_fan_out_across_replicas_and_resume_after_a_replica_stops(
    services: Services, settings: Settings, upstream: FakeUpstream
) -> None:
    services.formats.set_plugin(FilesPlugin(services))  # type: ignore[arg-type]
    repo_id = await add_repo(services, upstream.url)
    second = await replica(settings)
    on_first: Frames = []
    on_second: Frames = []
    tasks = [follow(services, on_first), follow(second, on_second)]
    try:
        await wait(lambda: any(f[1] == 'transfers.snapshot' for f in on_first + on_second) and len(on_second) > 0)
        await asyncio.sleep(0.3)  # both hubs' subscriptions settle
        # A transfer on the second replica and an upload on the first: every client sees both.
        now = datetime.now(UTC)
        second.emitter.transfer_started(
            Transfer(
                id='t-2',
                repo_id=repo_id,
                repo='files',
                format='raw',
                path='big.iso',
                cache=CacheState.FETCHING,
                started_at=now,
                updated_at=now,
            )
        )
        await second.emitter.flush()
        upload(services, repo_id, 'one.txt')
        await services.emitter.flush()
        for frames in (on_first, on_second):
            await wait(lambda frames=frames: 'one.txt' in paths(frames))  # type: ignore[misc]
            assert any(f[1] == 'transfer.started' and f[2]['transfer']['id'] == 't-2' for f in frames)
        last_seen = next(f[0] for f in reversed(on_first) if f[1] == 'activity')
        assert last_seen is not None
    finally:
        await stop(tasks[0])

    # The first replica goes away (its client is cut off); events keep coming through the second one.
    await services.hub.stop()
    upload(second, repo_id, 'two.txt')
    upload(second, repo_id, 'three.txt')
    await second.emitter.flush()
    await wait(lambda: 'three.txt' in paths(on_second))

    # The client reconnects to the surviving replica with Last-Event-ID: it gets what it missed, in order, once.
    resumed: Frames = []
    task = follow(second, resumed, last_event_id=last_seen)
    try:
        await wait(lambda: 'three.txt' in paths(resumed))
        assert paths(resumed)[:2] == ['two.txt', 'three.txt']
        assert 'one.txt' not in paths(resumed)
        await asyncio.sleep(0.3)
        upload(second, repo_id, 'four.txt')  # and live events again
        await second.emitter.flush()
        await wait(lambda: 'four.txt' in paths(resumed))
        assert paths(resumed).count('four.txt') == 1
    finally:
        await stop(task)
        await stop(tasks[1])
        await second.stop()
