"""Performance budget report (``make budgets``; SPECIFICATION §5.1, ARCHITECTURE §9).

Reads ``Build/ci/budgets.json`` and the measured values each check leaves behind (``source``: a JSON object of
``{budget id: value}``), prints a table (Markdown with ``--markdown``, e.g. for ``$GITHUB_STEP_SUMMARY``) and exits 1
only when a budget whose ``enforce`` is ``fail`` is over its limit. ``warn`` budgets are reported, never fatal
(until milestone M2, CLAUDE.md). Budgets without a source, or whose source has not run, are "not measured".
Stdlib only; runs in the dev image.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_BUDGETS = Path('Build/ci/budgets.json')


def evaluate(budgets: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    rows = []
    for budget in budgets['budgets']:
        value = None
        source = budget.get('source')
        if source and (root / source).is_file():
            value = json.loads((root / source).read_text()).get(budget['id'])
        if value is None:
            status = 'not measured'
        elif value <= budget['limit']:
            status = 'ok'
        else:
            status = 'FAIL' if budget['enforce'] == 'fail' else 'WARN'
        rows.append({**budget, 'value': value, 'status': status})
    return rows


def render(rows: list[dict[str, Any]], markdown: bool) -> str:
    def fmt(value: object, unit: str) -> str:
        return '-' if value is None else f'{value} {unit}'

    if markdown:
        lines = [
            '### Performance budgets',
            '',
            '| Budget | Limit | Measured | Enforce | Status |',
            '|---|---|---|---|---|',
        ]
        for r in rows:
            cells = [
                r['description'],
                fmt(r['limit'], r['unit']),
                fmt(r['value'], r['unit']),
                r['enforce'],
                r['status'],
            ]
            lines.append('| ' + ' | '.join(str(c) for c in cells) + ' |')
        return '\n'.join(lines) + '\n'
    width = max(len(r['description']) for r in rows)
    lines = []
    for r in rows:
        limit, value = fmt(r['limit'], r['unit']), fmt(r['value'], r['unit'])
        lines.append(
            f'{r["description"]:<{width}}  limit {limit:>10}  measured {value:>10}  {r["enforce"]:<4}  {r["status"]}'
        )
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--budgets', type=Path, default=DEFAULT_BUDGETS)
    parser.add_argument('--root', type=Path, default=Path())
    parser.add_argument('--markdown', action='store_true', help='Markdown table (CI step summary)')
    args = parser.parse_args(argv)
    rows = evaluate(json.loads(args.budgets.read_text()), args.root)
    sys.stdout.write(render(rows, args.markdown))
    failed = [r['id'] for r in rows if r['status'] == 'FAIL']
    warned = [r['id'] for r in rows if r['status'] == 'WARN']
    for budget_id in warned:
        print(f'::warning title=Performance budget::{budget_id} is over budget (warning until M2)', file=sys.stderr)
    if failed:
        print(f'budgets: over budget: {", ".join(failed)}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
