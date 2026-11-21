"""Real-server wiring of the UI (T-U03): every API call the SPA makes exists in the server app with that method,
every live topic the UI subscribes to is accepted by the event stream, and every live event type the UI handles is
one the server documents (``api/live.py`` SSE payloads). The UI is built against the contract and its mocks first;
this keeps it from depending on anything the real server does not serve."""

from __future__ import annotations

import re
import typing
from pathlib import Path

from ws_testing import iter_routes

from waystation_server.api import live
from waystation_server.app import create_app
from waystation_server.sse.stream import parse_topics

WEB = Path(__file__).resolve().parents[2] / 'Web' / 'src'
CALL = re.compile(r"api\.(GET|POST|PUT|PATCH|DELETE)\(\s*'([^']+)'")
TOPICS = re.compile(r'useLiveTopic\(\s*(?:\(\)\s*=>\s*)?\[([^\]]*)\]')
CASE = re.compile(r"case '([a-z_]+\.[a-z_.]+|activity)'")


def _sources() -> list[tuple[Path, str]]:
    files = [*WEB.rglob('*.ts'), *WEB.rglob('*.vue')]
    skip = ('mocks', 'test')
    return [
        (f, f.read_text(encoding='utf-8'))
        for f in files
        if not f.name.endswith('.spec.ts') and not any(part in skip for part in f.relative_to(WEB).parts[:1])
    ]


def test_every_ui_api_call_is_served() -> None:
    routes = {
        (path.removeprefix('/api/v1'), method)
        for path, methods, _ in iter_routes(create_app())
        if path.startswith('/api/v1/')
        for method in methods
    }
    calls = {(path, method) for _, text in _sources() for method, path in CALL.findall(text)}
    assert len(calls) > 100  # the scan works
    missing = sorted(f'{method} {path}' for path, method in calls if (path, method) not in routes)
    assert missing == []


def test_live_topics_are_accepted() -> None:
    topics: set[str] = set()
    for _, text in _sources():
        for group in TOPICS.findall(text):
            for literal in re.findall(r"['`]([^'`]+)['`]", group):
                topics.add(literal.replace('${name.value}', 'dockerhub-proxy').replace('${props.repository}', 'npm'))
    assert {'transfers', 'stats', 'activity'} <= topics
    for topic in topics:
        assert parse_topics(topic) is not None, topic


def test_live_event_types_are_documented() -> None:
    documented = set()
    for name in dir(live):
        model = getattr(live, name)
        field = getattr(model, 'model_fields', {}).get('type')
        if field is not None and typing.get_origin(field.annotation) is typing.Literal:
            documented.update(typing.get_args(field.annotation))
    handled = set(CASE.findall((WEB / 'stores' / 'live.ts').read_text(encoding='utf-8')))
    assert {'transfer.started', 'stats.tick', 'activity'} <= handled
    assert handled <= documented, handled - documented
