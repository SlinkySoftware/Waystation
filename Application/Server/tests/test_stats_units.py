"""Pure parts of the stats API (T-S11): latency percentiles from histograms, top repositories, storage sparklines and
the range windows."""

from waystation_server.stats import history, series, top


def row(repo: str, requests: int, hits: int, misses: int, cache_bytes: int, lat: dict[str, int]) -> series.Row:
    out: series.Row = dict.fromkeys(series.keys.COUNTERS, 0)
    out.update(
        repo_id=repo,
        requests=requests,
        hits=hits,
        misses=misses,
        bytes_out_cache=cache_bytes,
        bytes_out_upstream=10,
        lat_hist=lat,
        bucket=0,
    )
    return out


def test_percentiles_sum_histograms() -> None:
    rows = [row('a', 10, 10, 0, 0, {'le_5': 9, 'le_100': 1}), row('a', 10, 0, 10, 0, {'le_5': 10})]
    assert series.histogram(rows) == {'le_5': 19, 'le_100': 1}
    assert series.percentiles(rows) == (5.0, 5.0)
    assert series.percentiles([row('a', 1, 1, 0, 0, {'le_inf': 1})]) == (60000.0, 60000.0)
    assert series.percentiles([row('a', 0, 0, 0, 0, {})]) == (None, None)
    assert series.percentiles(rows, 'ulat_hist') == (None, None)


def test_top_repositories() -> None:
    rows = [
        row('a', 5, 5, 0, 500, {'le_10': 5}),
        row('b', 9, 0, 9, 0, {'le_1000': 9}),
        row('b', 1, 1, 0, 50, {'le_1000': 1}),
        row('c', 0, 0, 0, 0, {}),
    ]
    by_requests = top.repositories(rows, by='requests', limit=10)
    assert [r.repo_id for r in by_requests] == ['b', 'a']  # no requests: left out
    assert by_requests[0].requests == 10 and by_requests[0].hit_ratio == 0.1 and by_requests[0].latency_p95_ms == 1000
    assert [r.repo_id for r in top.repositories(rows, by='saved', limit=1)] == ['a']
    assert [(r.repo_id, r.bytes_served) for r in top.repositories(rows, by='bytes', limit=10)] == [
        ('a', 510),
        ('b', 70),
    ]


def test_storage_sparkline_carries_snapshots_forward() -> None:
    assert history.spark([None, 5, None, 7, None], 9.0) == [5.0, 5.0, 5.0, 7.0, 9.0]
    assert history.spark([None, None], 3.0) == [3.0, 3.0]
    assert history.spark([], 3.0) == []


def test_windows() -> None:
    now = 1_800_000_123.0
    win = series.window('1y', now=now)
    assert win.step_s == 86400 and len(win.buckets) == 365 and win.end > now
    assert len(series.window('90d', now=now).buckets) == 90
    hist = history.window('30d', now=now)
    assert hist.step_s == 21600 and len(hist.buckets) == 120
    assert history.window('7d', now=now).step_s == 3600
