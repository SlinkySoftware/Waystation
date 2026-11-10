"""``StorageStats``: per-repository figures from ``repo_usage``, an expired snapshot answered at once and refreshed in
the background (T-P2-PERF: no request waits for an aggregation once the first figures exist)."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from waystation_server.stats import storage as storage_mod
from waystation_server.stats.storage import StorageStats


class Figures:
    def __init__(self) -> None:
        self.repos = {'r1': (100, 2), 'r2': (50, 1)}
        self.loads = 0
        self.fail = False
        self.gate: asyncio.Event | None = None

    async def repo_totals(self, db: Any) -> dict[str, tuple[int, int]]:
        self.loads += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError('mongodb unavailable')
        return dict(self.repos)

    async def blob_totals(self, db: Any) -> tuple[int, int]:
        return 2, 120


@pytest.fixture
def figures(monkeypatch: pytest.MonkeyPatch) -> Figures:
    f = Figures()
    monkeypatch.setattr(storage_mod.stats_db, 'repo_totals', f.repo_totals)
    monkeypatch.setattr(storage_mod.stats_db, 'blob_totals', f.blob_totals)
    return f


async def test_figures_and_stale_while_refreshing(
    figures: Figures, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(storage_mod.time, 'monotonic', lambda: clock[0])
    stats = StorageStats(None, tmp_path)  # type: ignore[arg-type]
    first = await stats.get()
    assert (first.blob_count, first.used_bytes, first.asset_bytes, first.dedup_saved_bytes) == (2, 120, 150, 30)
    assert first.artifacts == 3 and first.by_repo['r1'].bytes == 100
    assert await stats.get() is first and figures.loads == 1  # within the TTL

    clock[0] += storage_mod.TTL_S
    figures.repos['r3'] = (7, 7)
    figures.gate = asyncio.Event()
    assert await stats.get() is first  # expired: answered at once, refreshed in the background
    assert await stats.get() is first
    await asyncio.sleep(0)
    assert figures.loads == 2  # one refresh at a time
    figures.gate.set()
    assert stats._refresh is not None
    await stats._refresh  # (time.monotonic is frozen here: no asyncio.sleep with a delay)
    assert 'r3' in (await stats.get()).by_repo

    clock[0] += storage_mod.TTL_S
    figures.fail = True
    figures.gate = None
    current = await stats.get()
    assert stats._refresh is not None
    await stats._refresh
    assert await stats.get() is current  # a failed refresh keeps the previous figures
    await stats.aclose()


async def test_aclose_cancels_a_running_refresh(
    figures: Figures, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(storage_mod.time, 'monotonic', lambda: clock[0])
    stats = StorageStats(None, tmp_path)  # type: ignore[arg-type]
    await stats.get()
    clock[0] += storage_mod.TTL_S
    figures.gate = asyncio.Event()
    await stats.get()
    await asyncio.sleep(0)
    await stats.aclose()
    await stats.aclose()
    stats.invalidate()
    figures.gate = None
    assert (await stats.get()).blob_count == 2
