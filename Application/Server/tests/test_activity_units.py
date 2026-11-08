"""Activity filters and the CSV export renderer (T-U07), without MongoDB/Valkey."""

import csv
import io
from datetime import UTC, datetime

from waystation_common.events import ActivityEvent, CacheState, EventType
from waystation_server.stats import export
from waystation_server.stats.live import ActivityFilter


def _fields(**kw: object) -> dict[str, str]:
    base: dict[str, object] = {
        'type': EventType.CACHE_HIT,
        'ts': datetime(2026, 11, 9, 10, 0, tzinfo=UTC),
        'repo': 'files',
        'format': 'raw',
        'path': 'dir/a.bin',
        'principal': 'alice',
        'cache': CacheState.HIT,
        'bytes': 10,
    }
    base.update(kw)
    return {'data': ActivityEvent.model_validate(base).model_dump_json()}


def test_filter_hides_invisible_repositories() -> None:
    flt = ActivityFilter(visible=frozenset({'files'}))
    assert flt.view('1-0', _fields()) is not None
    assert flt.view('1-0', _fields(repo='secret')) is None
    # events without a repository are not repository-scoped
    assert flt.view('1-0', _fields(repo=None, type=EventType.UPSTREAM_STATE)) is not None


def test_filter_fields_combine() -> None:
    visible = frozenset({'files', 'other'})
    hit = _fields()
    assert ActivityFilter(visible, repo='other').view('1-0', hit) is None
    assert ActivityFilter(visible, format='npm').view('1-0', hit) is None
    assert ActivityFilter(visible, format='raw', kind='download').view('1-0', hit) is not None
    assert ActivityFilter(visible, kind='error').view('1-0', hit) is None
    assert ActivityFilter(visible, cache='HIT', outcome='success').view('1-0', hit) is not None
    assert ActivityFilter(visible, cache='STALE').view('1-0', hit) is None
    assert ActivityFilter(visible, outcome='failure').view('1-0', hit) is None
    assert ActivityFilter(visible, principal='ALI').view('1-0', hit) is not None
    assert ActivityFilter(visible, principal='bob').view('1-0', hit) is None
    anonymous = _fields(principal='anonymous', client_ip='192.0.2.7')
    assert ActivityFilter(visible, principal='192.0.2.').view('1-0', anonymous) is not None


def test_view_carries_the_full_path() -> None:
    view = ActivityFilter(frozenset({'files'})).view('5-1', _fields())
    assert view is not None
    assert (view['artifact'], view['path'], view['id']) == ('a.bin', 'dir/a.bin', '5-1')


def test_csv_neutralises_formulas_and_quotes() -> None:
    rows = [
        {
            'ts': '2026-11-09T10:00:00Z',
            'kind': 'download',
            'summary': '=HYPERLINK("x")',
            'artifact': '@evil',
            'principal': '-1+1',
            'bytes': -5,
            'duration_ms': 1.5,
            'path': 'a,b\n"c"',
            'id': '1-0',
        },
    ]
    text = export.render(rows, header=True)
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == list(export.COLUMNS)
    row = dict(zip(export.COLUMNS, parsed[1], strict=True))
    assert row['time'] == '2026-11-09T10:00:00Z'
    assert row['summary'] == '\'=HYPERLINK("x")'
    assert row['artifact'] == "'@evil"
    assert row['principal'] == "'-1+1"
    assert row['bytes'] == '-5'
    assert row['path'] == 'a,b\n"c"'
    assert row['repository'] == ''
    assert text.endswith('\r\n')
