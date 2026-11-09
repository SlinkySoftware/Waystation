"""Turn a perf run's raw results into budget values and a Markdown report (tests/perf/README.md).

Reads ``--results`` (``k6-<scenario>.json`` from the k6 script, ``sse.json`` from ``sse_probe.py``,
``stats-<scenario>.csv`` resource samples from ``run.sh``) and writes:

- ``measured.json``: ``{budget id: value}``, the ``source`` of the k6-measured budgets in ``Build/ci/budgets.json``
  (compared by ``Build/ci/budgets.py``);
- ``details.md``: per-scenario figures and container CPU/memory, for the run report.

How each budget is derived is documented in ``BUDGET_RULES`` and tests/perf/README.md. Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

Metrics = dict[str, dict[str, float]]

BUDGET_RULES = {
    'cache_hit_content_ttfb_p95_ms': 'highest p95 TTFB of the small-file, range and large-file hit scenarios',
    'cache_hit_metadata_p95_ms': 'p95 duration over npm packuments (both forms), PyPI pages (both), maven-metadata.xml, '
    'apt Release and Packages',
    'docker_manifest_cached_p95_ms': 'p95 duration of cached manifest HEAD and GET (by tag and by digest)',
    'cache_miss_added_ttfb_ms': 'median TTFB of small never-cached files through Waystation minus straight from the '
    'origin',
    'cache_miss_throughput_loss_pct': '1 - median MB/s of large never-cached files through Waystation / from the origin',
    'api_list_search_p95_ms': 'p95 over search (prefix, per repository, page 2, everything), browse (root, folder) and '
    'the repository list',
    'live_event_latency_ms': 'p95 of transfer events (arrival - event ts) or transfer.started (arrival - request), '
    'whichever is higher',
    'concurrent_download_errors': 'failed downloads (status, timeout, reset) in the concurrency scenario',
}


def load_k6(results: Path) -> dict[str, Metrics]:
    out: dict[str, Metrics] = {}
    for file in sorted(results.glob('k6-*.json')):
        data = json.loads(file.read_text())
        out[str(data['scenario'])] = data['metrics']
    return out


def _value(k6: dict[str, Metrics], scenario: str, metric: str, stat: str) -> float | None:
    value = k6.get(scenario, {}).get(metric, {}).get(stat)
    return None if value is None else float(value)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def measured(k6: dict[str, Metrics], sse: dict[str, Any] | None) -> dict[str, float | int | None]:
    p95 = 'p(95)'
    content = [
        v
        for s, m in (('hit_small', 'ws_hit_small_ttfb'), ('hit_range', 'ws_hit_range_ttfb'), ('hit_large', 'ws_hit_large_ttfb'))
        if (v := _value(k6, s, m, p95)) is not None
    ]
    added = None
    proxied, direct = _value(k6, 'miss_small', 'ws_miss_ttfb_proxied', 'med'), _value(k6, 'miss_small', 'ws_miss_ttfb_direct', 'med')
    if proxied is not None and direct is not None:
        added = max(0.0, proxied - direct)
    loss = None
    proxied, direct = _value(k6, 'miss_large', 'ws_miss_mbps_proxied', 'med'), _value(k6, 'miss_large', 'ws_miss_mbps_direct', 'med')
    if proxied is not None and direct:
        loss = max(0.0, (1 - proxied / direct) * 100)
    live = None
    if sse:
        live = max(float(sse['event_latency_ms'][p95]), float(sse['started_latency_ms'][p95]))
    errors: int | None = None
    if 'concurrent' in k6:
        errors = int(_value(k6, 'concurrent', 'ws_errors', 'count') or 0)
    return {
        'cache_hit_content_ttfb_p95_ms': _round(max(content)) if content else None,
        'cache_hit_metadata_p95_ms': _round(_value(k6, 'metadata', 'ws_hit_metadata', p95)),
        'docker_manifest_cached_p95_ms': _round(_value(k6, 'docker', 'ws_docker_manifest', p95)),
        'cache_miss_added_ttfb_ms': _round(added),
        'cache_miss_throughput_loss_pct': _round(loss),
        'api_list_search_p95_ms': _round(_value(k6, 'api', 'ws_api', p95)),
        'live_event_latency_ms': _round(live),
        'concurrent_download_errors': errors,
    }


def _fmt(value: float | None, unit: str = '') -> str:
    if value is None:
        return '-'
    text = f'{value:.0f}' if abs(value) >= 100 else f'{value:.2f}'
    return f'{text}{unit}'


def scenario_table(k6: dict[str, Metrics]) -> list[str]:
    lines = [
        '| Scenario | Metric | n | median | p95 | p99 | max |',
        '|---|---|---|---|---|---|---|',
    ]
    for scenario, metrics in k6.items():
        for name, v in sorted(metrics.items()):
            if not name.startswith('ws_') or 'p(95)' not in v or not v.get('count'):
                continue
            rate = '_mbps' in name
            unit = '' if rate else ' ms'
            label = name.removeprefix('ws_') + (' (MiB/s)' if rate else '')
            cells = [scenario, label, f'{int(v["count"])}', _fmt(v.get('med'), unit), _fmt(v.get('p(95)'), unit)]
            cells += [_fmt(v.get('p(99)'), unit), _fmt(v.get('max'), unit)]
            lines.append('| ' + ' | '.join(cells) + ' |')
    counters = []
    for scenario, metrics in k6.items():
        for name in ('ws_errors', 'ws_not_hit'):
            if metrics.get(name, {}).get('count'):
                counters.append(f'- {scenario}: {name.removeprefix("ws_")} = {int(metrics[name]["count"])}')
        reqs = metrics.get('http_reqs', {})
        if reqs.get('rate'):
            counters.append(f'- {scenario}: {int(reqs["count"])} requests, {reqs["rate"]:.0f}/s')
        dropped = metrics.get('dropped_iterations', {}).get('count')
        if dropped:
            counters.append(f'- {scenario}: {int(dropped)} iterations dropped (the stack did not keep up with the rate)')
    return lines + ([''] + counters if counters else [])


_CPU = re.compile(r'([\d.]+)%')
_MEM = re.compile(r'([\d.]+)\s*([KMG]i?B)')
_UNITS = {'KiB': 1 / 1024, 'KB': 1 / 1024, 'MiB': 1.0, 'MB': 1.0, 'GiB': 1024.0, 'GB': 1024.0}


def resources(results: Path, project: str) -> list[str]:
    """Average and peak CPU (% of one core) and peak memory per container and scenario, from ``docker stats``."""
    lines = ['| Scenario | Container | CPU avg | CPU max | Memory max |', '|---|---|---|---|---|']
    for file in sorted(results.glob('stats-*.csv')):
        scenario = file.stem.removeprefix('stats-')
        rows: dict[str, list[tuple[float, float]]] = {}
        with file.open() as f:
            for row in csv.reader(f):
                if len(row) < 3:
                    continue
                cpu, mem = _CPU.search(row[1]), _MEM.search(row[2])
                if not cpu or not mem:
                    continue
                name = row[0].removeprefix(f'{project}-')
                rows.setdefault(name, []).append((float(cpu[1]), float(mem[1]) * _UNITS.get(mem[2], 1.0)))
        for name, samples in sorted(rows.items()):
            cpus = [c for c, _ in samples]
            cells = [scenario, name, f'{sum(cpus) / len(cpus):.0f}%', f'{max(cpus):.0f}%', f'{max(m for _, m in samples):.0f} MiB']
            lines.append('| ' + ' | '.join(cells) + ' |')
    return lines if len(lines) > 2 else []


def details(k6: dict[str, Metrics], sse: dict[str, Any] | None, results: Path, project: str) -> str:
    out = ['### Scenarios', '', *scenario_table(k6), '']
    if sse:
        out += [
            '### Live events (SSE)',
            '',
            f'{sse["transfers"]} slow transfers, events received: '
            + ', '.join(f'{k} {v}' for k, v in sorted(sse['events'].items()))
            + f', download errors: {sse["errors"]}.',
            '',
            '| Sample | n | median | p95 | max |',
            '|---|---|---|---|---|',
        ]
        for key, label in (('event_latency_ms', 'event ts to arrival'), ('started_latency_ms', 'request to transfer.started')):
            s = sse[key]
            out.append(f'| {label} | {s["count"]} | {_fmt(s["med"], " ms")} | {_fmt(s["p(95)"], " ms")} | {_fmt(s["max"], " ms")} |')
        out.append('')
    res = resources(results, project)
    if res:
        out += ['### Resources (docker stats, % of one core)', '', *res, '']
    out += ['### How budgets are derived', '']
    out += [f'- `{k}`: {v}' for k, v in BUDGET_RULES.items()]
    return '\n'.join(out) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--project', default='')
    args = parser.parse_args(argv)
    k6 = load_k6(args.results)
    sse_file = args.results / 'sse.json'
    sse = json.loads(sse_file.read_text()) if sse_file.is_file() else None
    values = measured(k6, sse)
    (args.results / 'measured.json').write_text(json.dumps(values, indent=1) + '\n')
    (args.results / 'details.md').write_text(details(k6, sse, args.results, args.project))
    print(json.dumps(values))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
