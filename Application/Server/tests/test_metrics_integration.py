"""Cluster figures on ``/metrics`` (``stats.prometheus``) against real MongoDB/Valkey: upstream state and rate
limits, job queues and dead letters, workers, blob store, cleanup, GC and verify totals, assets missing their blob."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families
from ws_testing import make_repo, static_catalog

from waystation_common.db import jobs as jobs_db
from waystation_common.db.collections import ASSETS
from waystation_common.events import keys as event_keys
from waystation_common.jobs import JobQueue, StatsRollup, keys
from waystation_server.services import Services

pytestmark = pytest.mark.integration


def scrape(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
    }


async def test_cluster_figures(services: Services, client: httpx.AsyncClient) -> None:
    hub = make_repo('hub', 'docker')
    down = make_repo('down')
    off = make_repo('off', proxy={'upstream_url': 'https://upstream.example.com/', 'offline': True})
    hosted = make_repo('mine', type='hosted', proxy=None)
    services.catalog = static_catalog([hub, down, off, hosted])

    services.emitter.upstream(repo_id=hub.id, format='docker', ok=True, latency_ms=12.0)
    for _ in range(3):
        services.emitter.upstream(repo_id=down.id, format='raw', ok=False, error='connect failed')
    services.proxy.record_rate_limit(hub, limit=100, remaining=40, window_s=21600)
    await services.emitter.flush()

    queue = JobQueue(services.valkey)
    await queue.ensure_groups(['stats'])
    await services.jobs.enqueue(StatsRollup())
    await services.valkey.xadd(keys.DEAD, {'job': '{}', 'error': 'boom', 'ts': '0'})
    now = datetime.now(UTC)
    await jobs_db.worker_heartbeat(services.db, 'w1', {'leader': True}, now=now)
    await jobs_db.worker_heartbeat(services.db, 'w2', {'leader': False}, now=now)
    await jobs_db.worker_heartbeat(services.db, 'gone', {'leader': False}, now=now - timedelta(minutes=4))
    await services.valkey.hset(
        event_keys.MAINTENANCE_TOTALS, mapping={'gc_runs': '2', 'gc_freed_bytes': '4096', 'verify_hosted_lost': '3'}
    )
    await services.db[ASSETS].insert_many(
        [
            {'_id': 'a1', 'repo_id': hosted.id, 'path': 'x', 'blob_missing': {'reason': 'missing', 'hosted': True}},
            {'_id': 'a2', 'repo_id': down.id, 'path': 'y', 'blob_missing': {'reason': 'damaged', 'hosted': False}},
            {'_id': 'a3', 'repo_id': down.id, 'path': 'z', 'blob_missing': {'reason': 'missing', 'hosted': False}},
            {'_id': 'a4', 'repo_id': down.id, 'path': 'w'},
        ]
    )

    response = await client.get('/metrics')
    assert response.status_code == 200
    m = scrape(response.text)

    def get(name: str, **labels: str) -> float | None:
        return m.get((name, tuple(sorted(labels.items()))))

    for source in ('valkey', 'mongodb', 'filesystem'):
        assert get('waystation_metrics_source_up', source=source) == 1
    assert get('waystation_upstream_state', repo='hub') == 0
    assert get('waystation_upstream_state', repo='down') == 1  # degraded
    assert get('waystation_upstream_state', repo='off') == 3  # offline
    assert get('waystation_upstream_state', repo='mine') is None  # hosted: no upstream
    assert get('waystation_upstream_consecutive_failures', repo='down') == 3
    assert get('waystation_upstream_ratelimit_limit', repo='hub') == 100
    assert get('waystation_upstream_ratelimit_remaining', repo='hub') == 40
    assert get('waystation_upstream_ratelimit_window_seconds', repo='hub') == 21600
    assert (get('waystation_upstream_ratelimit_updated_timestamp_seconds', repo='hub') or 0) > now.timestamp() - 60
    assert get('waystation_upstream_ratelimit_limit', repo='down') is None
    assert get('waystation_jobs_waiting', queue='stats') == 1
    assert get('waystation_jobs_running', queue='stats') == 0
    assert get('waystation_jobs_delayed', queue='stats') == 0
    assert (get('waystation_jobs_oldest_waiting_seconds', queue='stats') or 0) >= 0
    assert get('waystation_jobs_waiting', queue='index') == 0
    assert get('waystation_jobs_dead_letters') == 1
    assert get('waystation_workers') == 2
    assert get('waystation_scheduler_leaders') == 1
    assert get('waystation_blobstore_used_bytes') == 0
    assert get('waystation_blobstore_blobs') == 0
    assert (get('waystation_blobstore_free_bytes') or 0) > 0
    assert get('waystation_gc_runs_total') == 2
    assert get('waystation_gc_freed_bytes_total') == 4096
    assert get('waystation_cleanup_removed_items_total') == 0
    assert get('waystation_verify_hosted_lost_total') == 3
    assert get('waystation_verify_runs_total') == 0
    assert get('waystation_assets_blob_missing', repository_type='hosted') == 1
    assert get('waystation_assets_blob_missing', repository_type='proxy') == 2
