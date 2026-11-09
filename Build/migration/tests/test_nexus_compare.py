"""``nexus_compare`` against two fake servers (a "Nexus" and a "Waystation", stdlib HTTP servers on loopback): no
real Nexus. Covers matching answers (docker Bearer challenge, npm metadata with rewritten URLs, byte-identical files),
differences, failures, redirects, reports and exit codes."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import nexus_compare as nc
import pytest

INDEX = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[]}'
INDEX_DIGEST = 'sha256:' + hashlib.sha256(INDEX).hexdigest()
OCI_INDEX = 'application/vnd.oci.image.index.v1+json'
POOL = bytes(range(256)) * 4096  # 1 MiB: more than one read chunk
NEXUS_PASSWORD = 'nexus-secret-value'
WS_TOKEN = 'wst_' + 'a' * 43

Route = Callable[['Handler'], tuple[int, dict[str, str], bytes]]


@dataclass
class Fake:
    """A loopback HTTP server answering ``routes[path](handler)``; records each request's headers."""

    routes: dict[str, Route] = field(default_factory=dict)
    seen: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        host, port = self.server.server_address[:2]
        return f'http://{host!s}:{port}'


class Handler(BaseHTTPRequestHandler):
    fake: Fake

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _answer(self, body: bool) -> None:
        path = self.path.split('?', 1)[0]
        self.fake.seen.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
        route = self.fake.routes.get(path)
        status, headers, data = route(self) if route else (404, {'Content-Type': 'text/plain'}, b'not found')
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_GET(self) -> None:
        self._answer(True)

    def do_HEAD(self) -> None:
        self._answer(False)


@pytest.fixture
def serve() -> Iterator[Callable[[], Fake]]:
    started: list[ThreadingHTTPServer] = []

    def make() -> Fake:
        fake = Fake()
        handler = type('BoundHandler', (Handler,), {'fake': fake})
        fake.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=fake.server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True).start()
        started.append(fake.server)
        return fake

    yield make
    for server in started:
        server.shutdown()
        server.server_close()


def ok(body: bytes, content_type: str = 'application/octet-stream', **headers: str) -> Route:
    return lambda h: (200, {'Content-Type': content_type, **headers}, body)


def docker_registry(fake: Fake, *, authorised: Callable[[str], bool], token: str) -> None:
    """A registry that challenges anonymous requests and accepts the token its realm issues."""

    def manifest(h: Handler) -> tuple[int, dict[str, str], bytes]:
        if h.headers.get('Authorization') != f'Bearer {token}':
            challenge = f'Bearer realm="{fake.url}/token",service="registry",scope="repository:library/alpine:pull"'
            return 401, {'WWW-Authenticate': challenge, 'Content-Type': 'application/json'}, b'{"errors":[]}'
        return 200, {'Content-Type': OCI_INDEX, 'Docker-Content-Digest': INDEX_DIGEST}, INDEX

    def issue(h: Handler) -> tuple[int, dict[str, str], bytes]:
        if not authorised(h.headers.get('Authorization') or ''):
            return 401, {}, b''
        return 200, {'Content-Type': 'application/json'}, json.dumps({'token': token}).encode()

    fake.routes['/repository/docker-group/v2/library/alpine/manifests/3.20'] = manifest
    fake.routes['/token'] = issue


def packument(base: str, *, indent: int | None) -> bytes:
    doc = {
        'name': 'lodash',
        'dist-tags': {'latest': '4.17.21'},
        'versions': {
            '4.17.21': {
                'dist': {
                    'tarball': f'{base}/repository/npm/lodash/-/lodash-4.17.21.tgz',
                    'integrity': 'sha512-v2kDE',
                }
            }
        },
    }
    return json.dumps(doc, indent=indent).encode()


CHECKS = """
[[check]]
name = "alpine index (HEAD)"
format = "docker"
method = "HEAD"
path = "/repository/docker-group/v2/library/alpine/manifests/3.20"
headers = { Accept = "application/vnd.oci.image.index.v1+json" }
compare = ["status", "docker_digest", "content_type"]

[[check]]
name = "alpine index (GET)"
format = "docker"
path = "/repository/docker-group/v2/library/alpine/manifests/3.20"

[[check]]
name = "lodash packument"
format = "npm"
path = "/repository/npm/lodash"
compare = ["status", "digest", "content_type"]
normalise = ["base_url", "json"]
json = ["/dist-tags/latest", "/versions/4.17.21/dist/integrity", "/versions/4.17.21/dist/missing"]

[[check]]
name = "pool file"
format = "apt"
path = "/repository/debian/pool/main/h/hello/hello_2.10-5_amd64.deb"

[[check]]
name = "renamed raw repository"
format = "raw"
path = "/repository/old-files/a.txt"
waystation_path = "/repository/files/a.txt"
"""


@pytest.fixture
def pair(serve: Callable[[], Fake]) -> tuple[Fake, Fake]:
    nexus, ws = serve(), serve()
    basic = 'Basic ' + base64.b64encode(f'admin:{NEXUS_PASSWORD}'.encode()).decode()
    docker_registry(nexus, authorised=lambda auth: auth == basic, token='nexus-registry-token')
    docker_registry(ws, authorised=lambda auth: auth.startswith('Basic '), token='ws-registry-token')
    nexus.routes['/repository/npm/lodash'] = ok(packument(nexus.url, indent=2), 'application/json')
    ws.routes['/repository/npm/lodash'] = ok(packument(ws.url, indent=None), 'application/json; charset=utf-8')
    for fake in (nexus, ws):
        fake.routes['/repository/debian/pool/main/h/hello/hello_2.10-5_amd64.deb'] = ok(POOL)
    nexus.routes['/repository/old-files/a.txt'] = ok(b'same\n', 'text/plain')
    ws.routes['/repository/files/a.txt'] = ok(b'same\n', 'text/plain')
    return nexus, ws


ENV = {'NEXUS_USERNAME': 'admin', 'NEXUS_PASSWORD': NEXUS_PASSWORD, 'WAYSTATION_TOKEN': WS_TOKEN}


def compare(
    tmp_path: Path, nexus: Fake, ws: Fake, checks: str = CHECKS, *args: str, env: dict[str, str] | None = None
) -> tuple[int, str]:
    path = tmp_path / 'checks.toml'
    path.write_text(checks)
    out = io.StringIO()
    argv = ['--nexus', nexus.url, '--waystation', ws.url + '/', '--checks', str(path), *args]
    code = nc.main(argv, env=ENV if env is None else env, out=out)
    return code, out.getvalue()


def test_everything_matches(tmp_path: Path, pair: tuple[Fake, Fake]) -> None:
    nexus, ws = pair
    code, report = compare(tmp_path, nexus, ws)
    assert code == nc.EXIT_OK, report
    assert report.splitlines()[0].split() == ['RESULT', 'FORMAT', 'CHECK', 'NEXUS', 'WAYSTATION', 'DETAIL']
    assert '5 checks: 5 match, 0 differ, 0 failed' in report
    assert INDEX_DIGEST[:19] in report and f'{len(POOL)} B' in report
    assert NEXUS_PASSWORD not in report and WS_TOKEN not in report
    # the Bearer challenge was answered with each side's credentials, then the manifest asked again with the token
    token_auth = [h.get('authorization', '') for m, p, h in ws.seen if p.startswith('/token')]
    assert token_auth and all(base64.b64decode(a.split()[1]).decode() == f'validate:{WS_TOKEN}' for a in token_auth)
    assert ('GET', '/repository/files/a.txt') in {(m, p) for m, p, _ in ws.seen}


def test_differences_fail_the_run(tmp_path: Path, pair: tuple[Fake, Fake]) -> None:
    nexus, ws = pair
    ws.routes['/repository/debian/pool/main/h/hello/hello_2.10-5_amd64.deb'] = ok(POOL[:-1] + b'x')
    ws.routes['/repository/files/a.txt'] = lambda h: (404, {'Content-Type': 'text/plain'}, b'Not Found\n')
    tampered = json.loads(packument(ws.url, indent=None))
    tampered['dist-tags']['latest'] = '4.17.20'
    ws.routes['/repository/npm/lodash'] = ok(json.dumps(tampered).encode(), 'text/html')
    code, report = compare(tmp_path, nexus, ws, CHECKS, '--report', 'json')
    assert code == nc.EXIT_DIFFERENT
    doc = json.loads(report)
    assert doc['summary'] == {'total': 5, 'ok': 2, 'diff': 3, 'error': 0}
    by_name = {r['name']: r for r in doc['results']}
    assert by_name['pool file']['differences'] == [
        f'digest: nexus {hashlib.sha256(POOL).hexdigest()[:12]} != waystation '
        f'{hashlib.sha256(POOL[:-1] + b"x").hexdigest()[:12]}'
    ]
    assert by_name['renamed raw repository']['differences'] == ['status: nexus 200 != waystation 404']
    npm = by_name['lodash packument']['differences']
    assert 'content_type: nexus application/json != waystation text/html' in npm
    assert 'json /dist-tags/latest: nexus 4.17.21 != waystation 4.17.20' in npm
    assert any(d.startswith('digest:') for d in npm)
    assert by_name['lodash packument']['nexus']['json']['/versions/4.17.21/dist/missing'] == '<missing>'


def test_unreachable_side_is_an_error(tmp_path: Path, serve: Callable[[], Fake], pair: tuple[Fake, Fake]) -> None:
    nexus, _ = pair
    gone = serve()
    assert gone.server is not None
    gone.server.shutdown()
    gone.server.server_close()
    code, report = compare(tmp_path, nexus, gone, CHECKS, '--report', 'markdown', '--only', 'apt', '--timeout', '5')
    assert code == nc.EXIT_DIFFERENT
    assert report.startswith('| RESULT | FORMAT | CHECK | NEXUS | WAYSTATION | DETAIL |')
    assert '| ERROR | apt | pool file | 200 | error | waystation: connection failed' in report
    assert '1 checks: 0 match, 0 differ, 1 failed' in report


def test_redirects_never_carry_credentials_to_another_host(
    tmp_path: Path, serve: Callable[[], Fake], pair: tuple[Fake, Fake]
) -> None:
    nexus, ws = pair
    cdn = serve()
    cdn.routes['/blob'] = ok(POOL)
    nexus.routes['/repository/debian/pool/main/h/hello/hello_2.10-5_amd64.deb'] = lambda h: (
        307,
        {'Location': f'{cdn.url}/blob'},
        b'',
    )
    out = tmp_path / 'report.txt'
    code, report = compare(tmp_path, nexus, ws, CHECKS, '--only', 'apt', '--output', str(out))
    assert code == nc.EXIT_OK, report
    assert out.read_text() == report
    assert [h.get('authorization') for _, _, h in cdn.seen] == [None]
    assert nexus.seen[-1][2]['authorization'].startswith('Basic ')


@pytest.mark.parametrize(
    ('checks', 'message'),
    [
        ('', 'at least one [[check]]'),
        ('[[check]]\npath = "relative"', 'path must start with /'),
        ('[[check]]\npath = "/x"\nmethod = "PUT"', 'GET or HEAD'),
        ('[[check]]\npath = "/x"\nheaders = { Authorization = "Basic x" }', 'from the environment'),
        ('[[check]]\npath = "/x"\ncompare = ["sha1"]', "unknown comparison 'sha1'"),
        ('[[check]]\npath = "/x"\nnormalise = ["xml"]', "unknown normaliser 'xml'"),
        ('[[check]]\npath = "/x"\njson = ["a/b"]', 'must start with /'),
        ('[[check]]\npath = "/x"\nmethod = "HEAD"\njson = ["/a"]', 'need a GET'),
        ('[[check]]\npath = "/x"\nextra = 1', 'unknown keys extra'),
        ('[[check]\n', 'not valid TOML'),
    ],
)
def test_bad_checks_files(
    tmp_path: Path, pair: tuple[Fake, Fake], capsys: pytest.CaptureFixture[str], checks: str, message: str
) -> None:
    nexus, ws = pair
    code, report = compare(tmp_path, nexus, ws, checks)
    assert code == nc.EXIT_USAGE
    assert report == ''
    assert message in capsys.readouterr().err


def test_bad_urls_and_filters(tmp_path: Path, pair: tuple[Fake, Fake], capsys: pytest.CaptureFixture[str]) -> None:
    nexus, ws = pair
    path = tmp_path / 'checks.toml'
    path.write_text(CHECKS)
    out = io.StringIO()
    argv = ['--nexus', 'https://admin:pw@nexus.example.com', '--waystation', ws.url, '--checks', str(path)]
    assert nc.main(argv, env={}, out=out) == nc.EXIT_USAGE
    assert 'without credentials' in capsys.readouterr().err
    assert compare(tmp_path, nexus, ws, CHECKS, '--only', 'maven')[0] == nc.EXIT_USAGE
    assert nc.main(['--nexus', nexus.url, '--waystation', ws.url, '--checks', str(tmp_path / 'none')], env={}) == 2


def test_example_checks_file_parses() -> None:
    checks = nc.load_checks(str(Path(nc.__file__).with_name('checks.example.toml')))
    formats = {c.format for c in checks}
    assert {'docker', 'npm', 'apt', 'yum', 'raw', 'pypi'} <= formats
    assert any(c.method == 'HEAD' for c in checks)


def test_pointer_and_normalise_helpers() -> None:
    doc = {'a': [{'b/c': 1, 'd~e': 2}]}
    assert nc.resolve_pointer(doc, '/a/0/b~1c') == 1
    assert nc.resolve_pointer(doc, '/a/0/d~0e') == 2
    assert nc.resolve_pointer(doc, '/a/5') == '<missing>'
    assert nc.resolve_pointer(doc, '') == doc
    body = b'{"u": "https://nexus.example.com/repository/x", "k": 1}'
    assert nc.normalise(body, ['base_url', 'json'], 'https://nexus.example.com') == b'{"k":1,"u":"{BASE}/repository/x"}'
    assert nc.normalise(b'not json', ['json'], 'https://x.example.com') == b'not json'
    assert nc.media_type('Application/JSON; charset=utf-8') == 'application/json'
    assert nc.media_type(None) is None
