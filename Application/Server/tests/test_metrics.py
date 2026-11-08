"""Prometheus metrics: process metrics (``metrics.Metrics``), the route latency middleware, the free-space guard
counter, multi-process mode and the cluster figures' failure handling (no MongoDB/Valkey here; the cluster figures
against real ones are in ``test_metrics_integration``)."""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.parser import text_string_to_metric_families
from starlette.routing import Route
from ws_testing import asgi_client

from waystation_common.errors import InsufficientStorage
from waystation_server import __main__ as entry
from waystation_server.metrics import MULTIPROC_ENV, Metrics, status_class
from waystation_server.middleware import UNMATCHED, route_template
from waystation_server.services import Services
from waystation_server.stats import prometheus


def samples(text: str | bytes) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    body = text.decode() if isinstance(text, bytes) else text
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(body)
        for s in family.samples
    }


def value(text: str | bytes, name: str, **labels: str) -> float | None:
    return samples(text).get((name, tuple(sorted(labels.items()))))


@pytest.fixture(autouse=True)
def fast_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prometheus, 'SOURCE_TIMEOUT_S', 0.2)


def test_status_class() -> None:
    assert [status_class(s) for s in (200, 304, 404, 503, 0, 600)] == ['2xx', '3xx', '4xx', '5xx', 'other', 'other']


def test_process_metrics_and_extra_families() -> None:
    live = [2.0]
    m = Metrics(inflight=lambda: live[0])
    m.track(m.sse_clients, lambda: 3.0)
    m.http_request(route='/api/v1/repositories/{name}', method='GET', status=200, seconds=0.02)
    m.http_request(route='/api/v1/repositories/{name}', method='BREW', status=404, seconds=0.02)
    m.storage_refused()
    extra = GaugeMetricFamily('waystation_test_extra', 'Extra.', labels=['x'])
    extra.add_metric(['y'], 7)
    text = m.render([extra])
    count = 'waystation_http_request_duration_seconds_count'
    assert value(text, count, route='/api/v1/repositories/{name}', method='GET', status='2xx') == 1
    assert value(text, count, route='/api/v1/repositories/{name}', method='OTHER', status='4xx') == 1
    assert value(text, 'waystation_storage_refusals_total') == 1
    assert value(text, 'waystation_inflight_transfers') == 2
    assert value(text, 'waystation_sse_clients') == 3
    assert value(text, 'waystation_test_extra', x='y') == 7
    live[0] = 5.0
    assert value(m.render(), 'waystation_inflight_transfers') == 5  # read on scrape with one process


async def test_route_latency_by_template(unit_services: Services) -> None:
    async with asgi_client(unit_services) as client:
        assert (await client.get('/healthz')).status_code == 200
        assert (await client.head('/healthz')).status_code == 200
        assert (await client.get('/no/such/path')).status_code == 404
        await client.get('/api/v1/repositories/c.txt')  # anonymous: refused, still timed by template
    text = unit_services.metrics.render()
    count = 'waystation_http_request_duration_seconds_count'
    assert value(text, count, route='/healthz', method='GET', status='2xx') == 1
    assert value(text, count, route='/healthz', method='HEAD', status='2xx') == 1  # template kept through HEAD
    assert value(text, count, route='unmatched', method='GET', status='4xx') == 1
    assert value(text, count, route='/api/v1/repositories/{name}', method='GET', status='4xx') == 1
    routes = {dict(labels)['route'] for (name, labels) in samples(text) if name == count}
    assert not any('c.txt' in r for r in routes)  # never the raw path


async def test_free_space_refusals_counted(unit_services: Services) -> None:
    blobs = unit_services.blobs
    blobs.min_free_bytes = 2**62
    with pytest.raises(InsufficientStorage):
        await blobs.open_writer()
    assert value(unit_services.metrics.render(), 'waystation_storage_refusals_total') == 1


async def test_sse_clients_gauge(unit_services: Services) -> None:
    unit_services.hub.subscribers.add(object())  # type: ignore[arg-type]
    assert value(unit_services.metrics.render(), 'waystation_sse_clients') == 1


async def test_cluster_sources_down_still_scrape(unit_client: httpx.AsyncClient) -> None:
    response = await unit_client.get('/metrics')
    assert response.status_code == 200
    text = response.text
    assert value(text, 'waystation_metrics_source_up', source='valkey') == 0
    assert value(text, 'waystation_metrics_source_up', source='mongodb') == 0
    assert value(text, 'waystation_metrics_source_up', source='filesystem') == 1
    assert (value(text, 'waystation_blobstore_free_bytes') or 0) > 0
    assert (value(text, 'waystation_blobstore_size_bytes') or 0) > 0
    assert value(text, 'waystation_blobstore_min_free_bytes') == 0
    assert 'waystation_jobs_waiting' not in text  # left out, not reported as zero


async def test_cluster_figures_cached(unit_services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = prometheus.ClusterMetrics(unit_services)
    calls: list[int] = []

    async def collect() -> list[object]:
        calls.append(1)
        return []

    monkeypatch.setattr(cluster, '_collect', collect)
    await cluster.families()
    await cluster.families()
    assert calls == [1]


def test_prepare_multiprocess_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / 'mp'
    directory.mkdir()
    (directory / 'counter_1.db').write_bytes(b'old')
    monkeypatch.delenv(MULTIPROC_ENV, raising=False)
    entry.prepare_multiprocess_metrics(1, directory)
    assert MULTIPROC_ENV not in os.environ
    entry.prepare_multiprocess_metrics(2, directory)
    assert os.environ[MULTIPROC_ENV] == str(directory)
    assert list(directory.iterdir()) == []  # a previous run's values are not merged in
    entry.prepare_multiprocess_metrics(1, directory)
    assert MULTIPROC_ENV not in os.environ


MULTIPROC_CHILD = """
import asyncio, sys
from waystation_server.metrics import Metrics
m = Metrics(inflight=lambda: 2.0)
m.request(repo='r', format='raw', status=200, cache='HIT')
m.http_request(route='/healthz', method='GET', status=200, seconds=0.01)
if sys.argv[1] == 'stop':
    asyncio.run(m.stop())
sys.stdout.write(m.render().decode())
"""


def test_multiprocess_mode_merges_processes(tmp_path: Path) -> None:
    env = {**os.environ, MULTIPROC_ENV: str(tmp_path)}

    def run(mode: str) -> str:
        args = [sys.executable, '-c', MULTIPROC_CHILD, mode]
        return subprocess.run(args, env=env, capture_output=True, text=True, check=True).stdout

    run('stop')  # a process that exited: its counters stay, its live gauges go
    text = run('keep')
    assert value(text, 'waystation_requests_total', repo='r', format='raw', status='200', cache='HIT') == 2
    count = 'waystation_http_request_duration_seconds_count'
    assert value(text, count, route='/healthz', method='GET', status='2xx') == 2
    assert value(text, 'waystation_inflight_transfers') == 2  # only the live process


def test_route_template() -> None:
    def endpoint() -> None: ...

    route = Route('/repository/{repo_name}/{path:path}', endpoint)
    params = {'repo_name': 'files', 'path': 'a/b/c.txt'}
    scope = {'route': route, 'path': '/repository/files/a/b/c.txt', 'path_params': params}
    assert route_template(scope) == '/repository/{repo_name}/{path}'
    prefixed = {**scope, 'path': '/prefix/repository/files/a/b/c.txt'}
    assert route_template(prefixed) == '/prefix/repository/{repo_name}/{path}'
    assert route_template({**scope, 'path': '/elsewhere'}) == '/repository/{repo_name}/{path}'
    assert route_template({**scope, 'path_params': {'unknown': 'x'}}) == '/repository/{repo_name}/{path}'
    assert route_template({'path': '/x'}) == UNMATCHED
