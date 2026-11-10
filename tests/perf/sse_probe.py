"""Live event latency probe (SPECIFICATION §5.1 "transfer progress to UI < 1 s"; tests/perf/README.md).

k6 cannot read a response while it streams, so this stdlib script measures the event stream the UI uses: it
subscribes to ``GET /api/v1/events/stream?topics=transfers`` as the admin, then downloads rate-limited, never-cached
files through Waystation one at a time (each takes a few seconds, so the server emits ``transfer.progress`` events at
up to 4 Hz). Two latencies are sampled:

- every transfer event: the time it arrives here minus its ``ts`` (when the server produced it; the containers
  share the host clock);
- ``transfer.started``: the time it arrives minus the time the download request was sent.

Writes ``--out`` (JSON: the samples' percentiles). Runs in the dev image on the perf stack's network.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

from prepare import Client


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[k]


def summary(values: list[float]) -> dict[str, float]:
    return {
        'count': len(values),
        'med': statistics.median(values) if values else 0.0,
        'p(95)': percentile(values, 95),
        'max': max(values, default=0.0),
    }


class Listener(threading.Thread):
    """Reads the SSE stream and keeps ``(arrival epoch seconds, event)`` for every transfer event."""

    def __init__(self, base: str, cookie: str) -> None:
        super().__init__(daemon=True)
        parts = urllib.parse.urlsplit(base)
        self.conn = http.client.HTTPConnection(parts.hostname or 'localhost', parts.port or 80, timeout=300)
        self.cookie = cookie
        self.events: list[tuple[float, dict[str, Any]]] = []
        self.ready = threading.Event()
        self.error: str | None = None

    def run(self) -> None:
        try:
            self.conn.request(
                'GET',
                '/api/v1/events/stream?topics=transfers',
                headers={'Cookie': self.cookie, 'Accept': 'text/event-stream'},
            )
            resp = self.conn.getresponse()
            if resp.status != 200:
                self.error = f'event stream: {resp.status}'
                self.ready.set()
                return
            self.ready.set()
            data: list[str] = []
            while True:
                raw = resp.fp.readline()
                if not raw:
                    return
                line = raw.decode().rstrip('\r\n')
                if line.startswith('data:'):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    arrived = time.time()
                    try:
                        event = json.loads('\n'.join(data))
                    except ValueError:
                        event = {}
                    data = []
                    if str(event.get('type', '')).startswith('transfer.'):
                        self.events.append((arrived, event))
        except OSError as exc:
            if not self.ready.is_set():
                self.error = f'event stream: {exc}'
                self.ready.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument('--base', default='http://waystation-web:8080')
    parser.add_argument('--targets', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    targets = json.loads(args.targets.read_text())
    client = Client(args.base)
    client.login(os.environ['PERF_ADMIN_PASSWORD'])
    listener = Listener(args.base, client.cookie)
    listener.start()
    listener.ready.wait(30)
    if listener.error:
        print(f'sse_probe: {listener.error}', file=sys.stderr)
        return 1
    time.sleep(1.0)  # let the hub register the subscription
    sent: dict[str, float] = {}
    errors = 0
    for url in targets['slow']:
        sent[url.split('/repository/raw-perf/', 1)[1]] = time.time()
        status, headers, _ = client.request('GET', url)
        if status != 200 or headers.get('x-ws-cache') not in ('FETCHING', 'COALESCED', None):
            errors += 1
    time.sleep(2.0)  # the last events
    event_ms: list[float] = []
    started_ms: list[float] = []
    for arrived, event in listener.events:
        ts = event.get('ts')
        if ts:
            event_ms.append((arrived - datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()) * 1000)
        if event.get('type') == 'transfer.started':
            path = str(event.get('transfer', {}).get('path', ''))
            if path in sent:
                started_ms.append((arrived - sent[path]) * 1000)
    kinds: dict[str, int] = {}
    for _, event in listener.events:
        kinds[event['type']] = kinds.get(event['type'], 0) + 1
    result = {
        'transfers': len(targets['slow']),
        'errors': errors,
        'events': kinds,
        'event_latency_ms': summary(event_ms),
        'started_latency_ms': summary(started_ms),
    }
    args.out.write_text(json.dumps(result, indent=1))
    print(f'sse_probe: {json.dumps(result)}')
    if not event_ms or not started_ms:
        print('sse_probe: no transfer events received', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
