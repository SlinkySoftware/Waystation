import asyncio
import os
import time
from pathlib import Path

import pytest

from waystation_common.settings import Settings
from waystation_worker import health, main


def test_heartbeat_file(tmp_path: Path) -> None:
    beat = tmp_path / 'hb'
    assert not health.is_healthy(beat)
    health.beat(beat)
    assert health.is_healthy(beat)
    old = time.time() - health.MAX_AGE_S - 5
    os.utime(beat, (old, old))
    assert not health.is_healthy(beat)


async def test_run_stops_on_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    beats: list[int] = []
    monkeypatch.setattr(health, 'beat', lambda: beats.append(1))
    monkeypatch.setattr(health, 'INTERVAL_S', 0.01)
    stop = asyncio.Event()

    async def stopper(settings: Settings, stop: asyncio.Event) -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.wait_for(main.run(Settings(), stop, [main.heartbeat_loop, stopper]), 2)
    assert len(beats) >= 2


async def test_run_propagates_loop_failure() -> None:
    async def boom(settings: Settings, stop: asyncio.Event) -> None:
        raise RuntimeError('loop failed')

    async def forever(settings: Settings, stop: asyncio.Event) -> None:
        await stop.wait()

    stop = asyncio.Event()
    with pytest.raises(RuntimeError, match='loop failed'):
        await asyncio.wait_for(main.run(Settings(), stop, [boom, forever]), 2)
    assert stop.is_set()
