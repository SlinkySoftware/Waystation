import asyncio
import os
import time
from pathlib import Path

import pytest

from waystation_common.jobs import StatsRollup
from waystation_common.settings import Settings
from waystation_worker import health, main
from waystation_worker.jobs import HANDLERS, JobConfig, JobSystem, handles


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


async def test_heartbeat_withheld_while_watchdog_reports_a_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    beats: list[int] = []
    monkeypatch.setattr(health, 'beat', lambda: beats.append(1))
    monkeypatch.setattr(health, 'INTERVAL_S', 0.01)
    healthy = False
    health.set_watchdog(lambda: healthy)
    try:
        stop = asyncio.Event()
        task = asyncio.create_task(main.heartbeat_loop(Settings(), stop))
        await asyncio.sleep(0.05)
        assert beats == []
        healthy = True
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 2)
        assert beats
    finally:
        health.set_watchdog(None)
    assert health.watchdog_ok()


def test_job_config_backoff() -> None:
    config = JobConfig(retry_base_s=5, retry_max_s=60, visibility_s=30)
    assert [config.backoff_s(a) for a in (1, 2, 3, 4, 5)] == [5, 10, 20, 40, 60]
    assert (config.heartbeat_s, config.reclaim_s) == (10, 15)


def test_unknown_queue_rejected() -> None:
    with pytest.raises(ValueError, match='unknown job queues'):
        JobSystem(None, None, handlers={}, queues=['nope'])  # type: ignore[arg-type]


def test_builtin_handlers_and_registry() -> None:
    assert StatsRollup.kind in HANDLERS
    with pytest.raises(ValueError, match='already registered'):
        handles(StatsRollup)(lambda job, ctx: None)  # type: ignore[arg-type,return-value]
