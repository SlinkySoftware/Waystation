"""A fake Nexus Repository 3 REST API for the Nexus import tests (T-M01), serving the hand-written fixtures in
``tests/fixtures/nexus3`` (scrubbed: example.com hosts, public upstreams only) through ``FakeUpstream``."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from ws_fake_upstream import Respond, Seen

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'nexus3'
API = '/service/rest/v1/repositories'
USER, PASSWORD = 'admin', 'nexus-test-password'
SERVER = 'Nexus/3.70.1-02 (OSS)'
DETAIL_PATH = {'maven2': 'maven'}  # the detail endpoint's name for a format, when it differs


def listing() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / 'repositories.json').read_text())  # type: ignore[no-any-return]


def detail(name: str) -> dict[str, Any] | None:
    path = FIXTURES / 'details' / f'{name}.json'
    return json.loads(path.read_text()) if path.exists() else None


def nexus_routes(password: str = PASSWORD) -> dict[str, Any]:
    """Routes for ``FakeUpstream``: the repository list and every detail fixture, behind basic auth (401 like Nexus
    otherwise). A repository without a detail fixture answers 404, like a Nexus without that endpoint."""
    expected = 'Basic ' + base64.b64encode(f'{USER}:{password}'.encode()).decode()

    def guarded(body: bytes) -> Any:
        def respond(seen: Seen) -> Respond:
            if seen.headers.get('authorization') != expected:
                return Respond(401, {'www-authenticate': 'BASIC realm="Sonatype Nexus Repository Manager"'}, b'')
            return Respond(200, {'content-type': 'application/json', 'server': SERVER}, body)

        return respond

    routes: dict[str, Any] = {API: guarded((FIXTURES / 'repositories.json').read_bytes())}
    for entry in listing():
        path = FIXTURES / 'details' / f'{entry["name"]}.json'
        if path.exists():
            fmt = DETAIL_PATH.get(entry['format'], entry['format'])
            routes[f'{API}/{fmt}/{entry["type"]}/{entry["name"]}'] = guarded(path.read_bytes())
    return routes
