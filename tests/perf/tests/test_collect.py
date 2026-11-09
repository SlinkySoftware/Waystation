"""Unit tests for the perf suite's result collection (tests/perf/collect.py) and the budget report's strict mode
(Build/ci/budgets.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import budgets
import collect
import pytest


def trend(med: float, p95: float, count: int = 100) -> dict[str, float]:
    return {
        'avg': med,
        'min': 0.1,
        'med': med,
        'max': p95 * 2,
        'p(90)': p95,
        'p(95)': p95,
        'p(99)': p95,
        'count': count,
    }


K6: dict[str, collect.Metrics] = {
    'hit_small': {'ws_hit_small_ttfb': trend(3, 9)},
    'hit_range': {'ws_hit_range_ttfb': trend(3, 12.345)},
    'hit_large': {'ws_hit_large_ttfb': trend(2, 4), 'ws_hit_large_mbps': trend(3000, 3500, 3)},
    'metadata': {'ws_hit_metadata': trend(2, 5)},
    'docker': {'ws_docker_manifest': trend(3, 6)},
    'miss_small': {'ws_miss_ttfb_proxied': trend(5, 8), 'ws_miss_ttfb_direct': trend(0.5, 1)},
    'miss_large': {'ws_miss_mbps_proxied': trend(90, 91, 3), 'ws_miss_mbps_direct': trend(100, 100, 3)},
    'api': {'ws_api': trend(10, 40), 'ws_api_search': trend(8, 20)},
    'concurrent': {'ws_concurrent_duration': trend(50, 120), 'ws_errors': {'count': 2, 'rate': 0.1}},
}
SSE: dict[str, Any] = {
    'transfers': 3,
    'errors': 0,
    'events': {'transfer.started': 3, 'transfer.progress': 12},
    'event_latency_ms': {'count': 15, 'med': 20.0, 'p(95)': 200.0, 'max': 250.0},
    'started_latency_ms': {'count': 3, 'med': 30.0, 'p(95)': 300.0, 'max': 300.0},
}


def test_measured_maps_every_k6_budget() -> None:
    values = collect.measured(K6, SSE)
    assert values == {
        'cache_hit_content_ttfb_p95_ms': 12.35,  # the worst of small, range and large
        'cache_hit_metadata_p95_ms': 5.0,
        'docker_manifest_cached_p95_ms': 6.0,
        'cache_miss_added_ttfb_ms': 4.5,  # medians: 5 - 0.5
        'cache_miss_throughput_loss_pct': 10.0,  # 1 - 90/100
        'api_list_search_p95_ms': 40.0,
        'live_event_latency_ms': 300.0,  # the higher of the two p95s
        'concurrent_download_errors': 2,
    }
    budget_ids = {b['id'] for b in json.loads(Path('Build/ci/budgets.json').read_text())['budgets']}
    assert set(values) <= budget_ids
    assert set(values) == set(collect.BUDGET_RULES)


def test_missing_scenarios_are_not_measured() -> None:
    values = collect.measured({'hit_small': K6['hit_small']}, None)
    assert values['cache_hit_content_ttfb_p95_ms'] == 9.0
    assert all(v is None for k, v in values.items() if k != 'cache_hit_content_ttfb_p95_ms')


def test_faster_than_direct_is_no_loss() -> None:
    k6 = {
        'miss_small': {'ws_miss_ttfb_proxied': trend(0.4, 1), 'ws_miss_ttfb_direct': trend(0.5, 1)},
        'miss_large': {'ws_miss_mbps_proxied': trend(101, 101), 'ws_miss_mbps_direct': trend(100, 100)},
    }
    values = collect.measured(k6, None)
    assert values['cache_miss_added_ttfb_ms'] == 0.0
    assert values['cache_miss_throughput_loss_pct'] == 0.0


def test_report_files(tmp_path: Path) -> None:
    for scenario, metrics in K6.items():
        (tmp_path / f'k6-{scenario}.json').write_text(json.dumps({'scenario': scenario, 'metrics': metrics}))
    (tmp_path / 'sse.json').write_text(json.dumps(SSE))
    (tmp_path / 'stats-hit_small.csv').write_text(
        'p-server,150.5%,300MiB / 2GiB\np-server,49.5%,1.5GiB / 2GiB\np-web,5%,20MiB / 512MiB\nbroken line\n'
    )
    assert collect.main(['--results', str(tmp_path), '--project', 'p']) == 0
    assert json.loads((tmp_path / 'measured.json').read_text())['concurrent_download_errors'] == 2
    report = (tmp_path / 'details.md').read_text()
    assert '| miss_large | miss_mbps_proxied (MiB/s) | 3 | 90.00 | 91.00 |' in report
    assert '| hit_small | server | 100% | 150% | 1536 MiB |' in report
    assert '- concurrent: errors = 2' in report
    assert 'request to transfer.started' in report


def _budgets(tmp_path: Path, value: float, enforce: str) -> dict[str, Any]:
    (tmp_path / 'measured.json').write_text(json.dumps({'x': value}))
    return {
        'budgets': [
            {'id': 'x', 'description': 'X', 'limit': 10, 'unit': 'ms', 'enforce': enforce, 'source': 'measured.json'}
        ]
    }


@pytest.mark.parametrize(
    ('value', 'enforce', 'strict', 'status'),
    [
        (5, 'warn', False, 'ok'),
        (15, 'warn', False, 'WARN'),
        (15, 'warn', True, 'FAIL'),
        (15, 'fail', False, 'FAIL'),
    ],
)
def test_budget_status(tmp_path: Path, value: float, enforce: str, strict: bool, status: str) -> None:
    rows = budgets.evaluate(_budgets(tmp_path, value, enforce), tmp_path, strict=strict)
    assert rows[0]['status'] == status


def test_strict_flag_fails_warn_budgets(tmp_path: Path) -> None:
    file = tmp_path / 'budgets.json'
    file.write_text(json.dumps(_budgets(tmp_path, 15, 'warn')))
    assert budgets.main(['--budgets', str(file), '--root', str(tmp_path)]) == 0
    assert budgets.main(['--budgets', str(file), '--root', str(tmp_path), '--strict']) == 1
