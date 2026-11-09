"""The Grafana dashboard and the Zabbix template in ``Build/monitoring/`` (MONITORING.md) stay consistent.

Static checks only (no Grafana or Zabbix here): both files parse, every metric they query is one the server exports,
dashboard panels have unique ids and do not overlap, and the Zabbix template's uuids, item keys, trigger item
references, trigger dependencies, value maps and user macros all resolve.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
DASHBOARD = ROOT / 'Build/monitoring/grafana/waystation-dashboard.json'
TEMPLATE = ROOT / 'Build/monitoring/zabbix/waystation_by_http.yaml'
SOURCES = [
    ROOT / 'Application/Server/waystation_server/metrics.py',
    ROOT / 'Application/Server/waystation_server/stats/prometheus.py',
]
METRIC = re.compile(r'\bwaystation_[a-z0-9_]+')
SUFFIXES = ('_total', '_bucket', '_sum', '_count', '_created')
TEMPLATE_NAME = 'Waystation by HTTP'


def exported() -> set[str]:
    names: set[str] = set()
    for src in SOURCES:
        names.update(re.findall(r"'(waystation_[a-z0-9_]+)'", src.read_text()))
    return names


def unknown_metrics(text: str, known: set[str]) -> set[str]:
    out = set()
    for name in METRIC.findall(text):
        base = next((name.removesuffix(s) for s in SUFFIXES if name.endswith(s)), name)
        if name not in known and base not in known:
            out.add(name)
    return out


def walk(node: Any) -> list[dict[str, Any]]:
    """Every mapping in a parsed YAML/JSON tree."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(walk(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(walk(value))
    return found


def test_dashboard_parses_and_queries_exported_metrics() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    exprs = [t['expr'] for p in dashboard['panels'] for t in p.get('targets', [])]
    exprs += [v['definition'] for v in dashboard['templating']['list'] if 'definition' in v]
    assert exprs
    assert unknown_metrics('\n'.join(exprs), exported()) == set()


def test_dashboard_panels_have_unique_ids_and_do_not_overlap() -> None:
    panels = json.loads(DASHBOARD.read_text())['panels']
    ids = [p['id'] for p in panels]
    assert len(ids) == len(set(ids))
    cells: dict[tuple[int, int], str] = {}
    for p in panels:
        g = p['gridPos']
        assert g['x'] + g['w'] <= 24, p['title']
        for x in range(g['x'], g['x'] + g['w']):
            for y in range(g['y'], g['y'] + g['h']):
                assert (x, y) not in cells, f'{p["title"]} overlaps {cells[(x, y)]}'
                cells[(x, y)] = p['title']


def test_dashboard_has_quota_row() -> None:
    panels = json.loads(DASHBOARD.read_text())['panels']
    rows = [p['title'] for p in panels if p['type'] == 'row']
    assert 'Quotas and disk' in rows
    text = json.dumps(panels)
    for metric in (
        'waystation_repository_quota_used_ratio',
        'waystation_repository_quota_bytes',
        'waystation_repository_stored_bytes',
        'waystation_fills_not_stored_total',
        'waystation_quota_refusals_total',
        'waystation_storage_low',
    ):
        assert metric in text, metric


def load_template() -> tuple[dict[str, Any], str]:
    text = TEMPLATE.read_text()
    export = yaml.safe_load(text)['zabbix_export']
    (template,) = export['templates']
    assert template['template'] == TEMPLATE_NAME
    return template, text


def test_template_queries_exported_metrics() -> None:
    template, _ = load_template()
    patterns = [
        str(param)
        for node in walk(template)
        if node.get('type') in {'PROMETHEUS_PATTERN', 'PROMETHEUS_TO_JSON'}
        for param in node['parameters'][:1]
    ]
    assert patterns
    assert unknown_metrics('\n'.join(patterns), exported()) == set()


def test_template_references_resolve() -> None:
    template, text = load_template()
    nodes = walk(template)
    uuids = [n['uuid'] for n in nodes if 'uuid' in n]
    assert all(re.fullmatch(r'[0-9a-f]{32}', u) for u in uuids)
    assert len(uuids) == len(set(uuids))

    items = template['items'] + [p for r in template['discovery_rules'] for p in r.get('item_prototypes', [])]
    keys = [i['key'] for i in items] + [r['key'] for r in template['discovery_rules']]
    assert len(keys) == len(set(keys))

    triggers = [n for n in nodes if 'expression' in n and 'priority' in n]
    by_name = {t['name']: t['expression'] for t in triggers}
    assert len(by_name) == len(triggers), 'trigger names are unique'
    for trigger in triggers:
        refs = re.findall(rf'/{re.escape(TEMPLATE_NAME)}/([^,)]+)', trigger['expression'])
        assert refs, trigger['name']
        assert set(refs) <= set(keys), trigger['name']
        for dep in trigger.get('dependencies', []):
            assert by_name.get(dep['name']) == dep['expression'], f'{trigger["name"]} -> {dep["name"]}'

    valuemaps = {v['name'] for v in template['valuemaps']}
    assert {i['valuemap']['name'] for i in items if 'valuemap' in i} <= valuemaps
    macros = {m['macro'] for m in template['macros']}
    assert set(re.findall(r'\{\$[A-Z0-9_.]+\}', text)) <= macros


def test_template_has_quota_monitoring() -> None:
    template, _ = load_template()
    rules = {r['key']: r for r in template['discovery_rules']}
    quota = rules['waystation.quota.discovery']
    names = {t['name'] for p in quota['item_prototypes'] for t in p.get('trigger_prototypes', [])}
    assert {
        'Waystation: {#REPO} reached its hard quota',
        'Waystation: {#REPO} is over its soft quota',
        'Waystation: {#REPO} is near its quota (over {$WAYSTATION.QUOTA.WARN}%)',
    } <= names
    keys = {i['key'] for i in template['items']}
    assert {'waystation.storage.low', 'waystation.blobstore.free.pct', 'waystation.quota.refusals.upload.rate'} <= keys
