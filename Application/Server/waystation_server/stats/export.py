"""CSV rendering of the activity feed (OpenAPI ``exportActivity``).

Cells come from clients and upstreams (artifact names, error texts), so anything a spreadsheet would read as a
formula (leading ``=``, ``+``, ``-``, ``@``, tab or carriage return) is prefixed with ``'`` (OWASP CSV injection).
"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Iterable
from typing import Any

COLUMNS = (
    'time',
    'kind',
    'outcome',
    'repository',
    'format',
    'artifact',
    'path',
    'principal',
    'cache',
    'bytes',
    'duration_ms',
    'summary',
    'id',
)
_SOURCE = {'time': 'ts'}
_FORMULA = ('=', '+', '-', '@', '\t', '\r')


def safe_cell(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int | float):
        return str(value)  # numbers are written as numbers (negative values are not text formulas)
    text = str(value)
    return f"'{text}" if text.startswith(_FORMULA) else text


def render(rows: Iterable[dict[str, Any]], *, header: bool = False) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\r\n')
    if header:
        writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([safe_cell(row.get(_SOURCE.get(c, c))) for c in COLUMNS])
    return buffer.getvalue()


async def stream_csv(batches: AsyncIterator[list[dict[str, Any]]]) -> AsyncIterator[bytes]:
    """Header first (so an empty result is still a valid file), then one chunk per batch."""
    yield render([], header=True).encode()
    async for batch in batches:
        yield render(batch).encode()
