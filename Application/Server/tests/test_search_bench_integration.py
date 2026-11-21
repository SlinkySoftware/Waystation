"""Search benchmark (T-U06, SPECIFICATION §4.6 / §5.1: p95 < 150 ms at 1M assets): seeds cached-file records in bulk
(the perf suite's layout, ``tests/perf/seed_assets.py``: ``group-<g>/pkg-<p>/<version>/artifact-<n>.tar.gz``) and
times ``search.engine.search`` for prefix, substring, paging, list-everything and digest queries, sequentially,
straight against MongoDB (no HTTP, no load: the query cost alone; ``make perf PROFILE=full`` measures the whole stack
under load).

Default size 20 000 (seconds, part of every integration run). The 1M-asset measurement:

    WAYSTATION_SEARCH_BENCH_ASSETS=1000000 make test-integration \\
        ARGS=Application/Server/tests/test_search_bench_integration.py

(a few minutes to seed; writes the index sizes and per-query p50/p95/max to the terminal).
"""

import hashlib
import os
import random
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from waystation_common.db.client import Database
from waystation_common.db.collections import ASSETS
from waystation_common.db.schema import ensure_schema
from waystation_common.ids import new_id
from waystation_common.searchkeys import file_keys
from waystation_server.repos.model import RepoConfig
from waystation_server.search.engine import SearchQuery, search

pytestmark = pytest.mark.integration

ASSETS_N = int(os.environ.get('WAYSTATION_SEARCH_BENCH_ASSETS') or 20_000)
BATCH = 10_000
RUNS = 60
WARMUP = 5
BUDGET_MS = 150.0
NOW = datetime(2026, 11, 21, tzinfo=UTC)


def asset_path(n: int) -> str:
    return f'group-{n // 10_000:03d}/pkg-{n // 100 % 100:02d}/{n % 100}.0.0/artifact-{n:07d}.tar.gz'


def document(repo_id: str, n: int) -> dict[str, Any]:
    path = asset_path(n)
    digest = hashlib.sha256(path.encode()).hexdigest()
    return {
        '_id': new_id(),
        'repo_id': repo_id,
        'path': path,
        'kind': 'content',
        'blob_sha256': digest,
        'size': 65_536,
        'content_type': 'application/gzip',
        'checksums': {'sha256': digest},
        'upstream': {'url': f'https://upstream.example.com/{path}', 'fetched_at': NOW},
        'created_at': NOW,
        'updated_at': NOW,
        **file_keys(path),
    }


async def seed(db: Database, repo_id: str, count: int) -> float:
    started = time.monotonic()
    batch: list[dict[str, Any]] = []
    for n in range(count):
        batch.append(document(repo_id, n))
        if len(batch) == BATCH:
            await db[ASSETS].insert_many(batch, ordered=False)
            batch = []
    if batch:
        await db[ASSETS].insert_many(batch, ordered=False)
    return time.monotonic() - started


async def timed(fn: Callable[[], Awaitable[Any]]) -> list[float]:
    for _ in range(WARMUP):
        await fn()
    out = []
    for _ in range(RUNS):
        started = time.perf_counter()
        await fn()
        out.append((time.perf_counter() - started) * 1000)
    return out


def p95(values: list[float]) -> float:
    return statistics.quantiles(values, n=100, method='inclusive')[94]


async def test_search_p95_at_scale(mongo_db: Database, capsys: pytest.CaptureFixture[str]) -> None:
    db = mongo_db
    await ensure_schema(db)
    repo = RepoConfig.model_validate(
        {
            '_id': new_id(),
            'name': 'perf-bulk',
            'format': 'raw',
            'type': 'proxy',
            'proxy': {'upstream_url': 'https://upstream.example.com/'},
            'created_at': NOW,
            'updated_at': NOW,
        }
    )
    seconds = await seed(db, repo.id, ASSETS_N)
    stats = await (await db[ASSETS].aggregate([{'$collStats': {'storageStats': {}}}])).to_list()
    sizes = stats[0]['storageStats']['indexSizes']
    rnd = random.Random(7)

    def number() -> str:
        return f'{rnd.randrange(ASSETS_N):07d}'

    async def run(**kwargs: Any) -> Any:
        return await search(db, SearchQuery(**kwargs), [repo])

    async def page2() -> None:
        first = await run(q='artifact', facets=False)
        assert first.next_cursor is not None
        await run(q='artifact', facets=False, cursor=first.next_cursor)

    sample = asset_path(rnd.randrange(ASSETS_N)).rsplit('/', 1)[-1]
    rare = (await run(q=sample[10:15])).facets
    assert rare is not None
    assert rare.total >= 1  # a substring of a seeded name finds it
    queries: dict[str, Callable[[], Awaitable[Any]]] = {
        'prefix + facets': lambda: run(q=f'artifact-{number()[:5]}'),
        'prefix page 2': page2,
        'everything': lambda: run(),
        'substring rare + facets': lambda: run(q=number()[1:6]),
        'substring common + facets': lambda: run(q='tar.gz'),
        'digest prefix': lambda: run(
            q='sha256:' + hashlib.sha256(asset_path(rnd.randrange(ASSETS_N)).encode()).hexdigest()[:12]
        ),
    }
    results = {name: await timed(fn) for name, fn in queries.items()}

    lines = [
        f'search benchmark: {ASSETS_N} assets seeded in {seconds:.1f} s; {RUNS} runs per query, sequential',
        'index sizes (MiB): ' + ', '.join(f'{k} {v / 2**20:.0f}' for k, v in sorted(sizes.items())),
    ]
    for name, values in results.items():
        median, top = statistics.median(values), max(values)
        lines.append(f'  {name:28s} p50 {median:7.1f} ms  p95 {p95(values):7.1f} ms  max {top:7.1f} ms')
    with capsys.disabled():
        sys.stdout.write('\n' + '\n'.join(lines) + '\n')
    over = {name: round(p95(values), 1) for name, values in results.items() if p95(values) >= BUDGET_MS}
    assert not over, f'search p95 over {BUDGET_MS} ms at {ASSETS_N} assets: {over}'
