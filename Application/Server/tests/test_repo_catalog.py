"""``RepoCatalog``: snapshot freshness, change-stream invalidation and reloads after a missed lookup."""

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
from ws_testing import make_repo

from waystation_server.repos import catalog as catalog_mod
from waystation_server.repos.catalog import RepoCatalog
from waystation_server.repos.model import RepoConfig


class Store:
    def __init__(self, *names: str) -> None:
        self.repos = [make_repo(n) for n in names]
        self.loads = 0
        self.gate: asyncio.Event | None = None

    async def load(self) -> list[RepoConfig]:
        self.loads += 1
        snapshot = list(self.repos)
        if self.gate is not None:
            await self.gate.wait()
        return snapshot


async def test_snapshot_is_reused_within_the_ttl() -> None:
    store = Store('a')
    catalog = RepoCatalog(store.load, ttl_s=60)
    assert await catalog.get('a') is not None
    store.repos.append(make_repo('b'))
    assert await catalog.get('b') is None  # no reload without reload_on_miss
    assert store.loads == 1
    catalog.invalidate()
    assert await catalog.get('b') is not None
    assert store.loads == 2


async def test_a_miss_reloads_once_and_unknown_names_are_remembered() -> None:
    store = Store('a')
    catalog = RepoCatalog(store.load, ttl_s=60)
    assert await catalog.get('a') is not None
    store.repos.append(make_repo('new'))  # created by another process
    assert await catalog.get('new', reload_on_miss=True) is not None
    assert store.loads == 2
    for _ in range(20):
        assert await catalog.get('nothing', reload_on_miss=True) is None
    assert store.loads == 2  # within miss_reload_s: no reload; then remembered as missing
    catalog.invalidate()
    assert await catalog.get('nothing', reload_on_miss=True) is None
    assert store.loads == 3  # a new snapshot forgets the missing names, but the lookup does not reload again


async def test_miss_reloads_are_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(catalog_mod.time, 'monotonic', lambda: clock[0])
    store = Store('a')
    catalog = RepoCatalog(store.load, ttl_s=60, miss_reload_s=1.0)
    await catalog.all()
    clock[0] += 2
    assert await catalog.get('x', reload_on_miss=True) is None
    assert store.loads == 2
    clock[0] += 0.5
    assert await catalog.get('y', reload_on_miss=True) is None
    assert store.loads == 2
    clock[0] += 1
    store.repos.append(make_repo('z'))
    assert await catalog.get('z', reload_on_miss=True) is not None
    assert store.loads == 3


async def test_concurrent_misses_share_one_reload() -> None:
    store = Store('a')
    catalog = RepoCatalog(store.load, ttl_s=60, miss_reload_s=0)
    await catalog.all()
    store.repos.append(make_repo('new'))
    found = await asyncio.gather(*(catalog.get('new', reload_on_miss=True) for _ in range(10)))
    assert all(r is not None for r in found)
    assert store.loads <= 3


async def test_an_invalidation_during_a_load_forces_another_load() -> None:
    store = Store('a')
    catalog = RepoCatalog(store.load, ttl_s=60)
    store.gate = asyncio.Event()
    loading = asyncio.create_task(catalog.all())
    await asyncio.sleep(0)
    store.repos.append(make_repo('b'))  # the change lands after the load read the collection
    catalog.invalidate()
    store.gate.set()
    assert {r.name for r in await loading} == {'a'}
    store.gate = None
    assert await catalog.get('b') is not None
    assert store.loads == 2


async def test_change_stream_invalidates_and_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_mod, 'WATCH_RETRY_S', 0.0)
    store = Store('a')
    events: asyncio.Queue[str | Exception] = asyncio.Queue()
    opened_count = 0

    async def watcher(opened: Callable[[], None]) -> AsyncIterator[object]:
        nonlocal opened_count
        opened_count += 1
        opened()
        while True:
            item = await events.get()
            if isinstance(item, Exception):
                raise item
            yield item

    catalog = RepoCatalog(store.load, ttl_s=60, watcher=watcher)
    catalog.start()
    catalog.start()  # idempotent
    try:
        await asyncio.wait_for(catalog.watching.wait(), timeout=1)
        await catalog.all()
        loads = store.loads
        store.repos.append(make_repo('b'))
        await events.put('insert')
        for _ in range(100):
            if await catalog.get('b') is not None:
                break
            await asyncio.sleep(0.01)
        assert store.loads == loads + 1
        await events.put(RuntimeError('stream lost'))
        for _ in range(100):
            if opened_count == 2:
                break
            await asyncio.sleep(0.01)
        assert opened_count == 2  # reopened after the error
    finally:
        await catalog.stop()
        await catalog.stop()


async def test_without_a_watcher_start_and_stop_do_nothing() -> None:
    catalog = RepoCatalog(Store('a').load)
    catalog.start()
    await catalog.stop()
    assert not catalog.watching.is_set()
