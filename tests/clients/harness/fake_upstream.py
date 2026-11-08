"""Record/replay fake upstream for the real-client harness (tests/clients/README.md).

Listens on :80 (HTTP) and :443 (HTTPS, leaf certificate from ``gen_certs.sh`` covering every impersonated host).
Containers that should reach the "upstream" resolve the public upstream host names to this container
(``extra_hosts``), so URLs, redirects and token realms stay exactly as the real upstream sends them.

Modes (``HARNESS_MODE``):
- ``replay``: serve only from fixtures; a miss is answered ``502`` and appended to ``$HARNESS_STATE/misses.txt``.
- ``record``: forward to the live upstream, scrub, store the fixture, return the live response.
- ``live``:   forward to the live upstream, store nothing (nightly smoke).

Other settings: ``HARNESS_HOSTS`` (space separated impersonated hosts), ``HARNESS_ORIGINS`` (optional
``host=scheme://addr:port`` overrides for where record/live mode really connects), ``HARNESS_FIXTURES`` (fixture
root), ``HARNESS_CA_DIR`` (certificates), ``HARNESS_MAX_BODY_MB`` (default 64).

This is test infrastructure: a small threaded stdlib server, not production code.
"""

from __future__ import annotations

import http.client
import os
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from upstream_store import Exchange, FixtureStore, RequestKey, scrub_response

HOP_BY_HOP = frozenset(
    {
        'connection',
        'keep-alive',
        'proxy-authenticate',
        'proxy-authorization',
        'proxy-connection',
        'te',
        'trailer',
        'transfer-encoding',
        'upgrade',
        'host',
        'accept-encoding',
        'content-length',
    }
)


class Config:
    def __init__(self) -> None:
        self.mode = os.environ.get('HARNESS_MODE', 'replay')
        if self.mode not in {'replay', 'record', 'live'}:
            raise SystemExit(f'HARNESS_MODE must be replay, record or live, not {self.mode!r}')
        self.hosts = frozenset(os.environ.get('HARNESS_HOSTS', '').split())
        self.origins: dict[str, str] = {}
        for item in os.environ.get('HARNESS_ORIGINS', '').split():
            host, _, origin = item.partition('=')
            self.origins[host] = origin.rstrip('/')
        self.store = FixtureStore(Path(os.environ.get('HARNESS_FIXTURES', '/fixtures')))
        self.state = Path(os.environ.get('HARNESS_STATE', '/state'))
        self.ca_dir = Path(os.environ.get('HARNESS_CA_DIR', '/harness-ca'))
        self.max_body = int(os.environ.get('HARNESS_MAX_BODY_MB', '64')) * 1024 * 1024
        self.lock = threading.Lock()

    def note(self, kind: str, text: str) -> None:
        """Append a miss/error line; the runner fails the run when this file is non-empty."""
        with self.lock, (self.state / 'misses.txt').open('a') as fh:
            fh.write(f'{kind} {text}\n')
        print(f'{kind} {text}', file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'waystation-harness-upstream'
    sys_version = ''
    scheme = 'http'
    config: Config  # set once by main()

    def log_message(self, format: str, *args: object) -> None:
        pass  # one line per exchange is printed by _respond instead

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self._handle()

    def _read_body(self) -> bytes:
        if self.headers.get('transfer-encoding', '').lower() == 'chunked':
            chunks: list[bytes] = []
            while True:
                size = int(self.rfile.readline().split(b';', 1)[0].strip() or b'0', 16)
                if size == 0:
                    while self.rfile.readline() not in {b'\r\n', b'\n', b''}:
                        pass
                    return b''.join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get('content-length') or 0)
        if length > self.config.max_body:
            raise ValueError('request body too large')
        return self.rfile.read(length) if length else b''

    def _handle(self) -> None:
        host = (self.headers.get('host') or '').rsplit(':', 1)[0].strip('[]').lower()
        if self.path.startswith('/__harness/health'):
            self._respond(Exchange(200, [('content-type', 'text/plain')], b'ok\n'), 'health')
            return
        body = self._read_body()
        if host not in self.config.hosts:
            self.config.note('UNKNOWN-HOST', f'{self.command} {self.scheme}://{host}{self.path}')
            self._fail(f'host {host!r} is not impersonated by this harness run (format.env HOSTS)')
            return
        url = f'{self.scheme}://{host}{self.path}'
        headers = dict(self.headers.items())
        key = RequestKey.build(self.command, url, headers, body)
        if self.config.mode == 'replay':
            exchange = self.config.store.load(key)
            if exchange is None:
                self.config.note('MISS', key.describe())
                self._fail(f'fixture miss (replay mode): {key.describe()}')
                return
            self._respond(exchange, 'REPLAY ' + key.describe())
            return
        try:
            live = self._forward(host, body)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self.config.note('UPSTREAM-ERROR', f'{key.describe()}: {exc}')
            self._fail(f'upstream error: {exc}')
            return
        if self.config.mode == 'record':
            self.config.store.save(key, scrub_response(live.status, live.headers, live.body))
        self._respond(live, f'{self.config.mode.upper()} ' + key.describe())

    def _forward(self, host: str, body: bytes) -> Exchange:
        origin = self.config.origins.get(host, f'{self.scheme}://{host}')
        parts = urlsplit(origin)
        conn: http.client.HTTPConnection
        if parts.scheme == 'https':
            conn = http.client.HTTPSConnection(parts.netloc, timeout=60, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(parts.netloc, timeout=60)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers['Host'] = host
        headers['Accept-Encoding'] = 'identity'  # fixtures hold identity bytes; replay never re-encodes
        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
            data = b'' if self.command == 'HEAD' else resp.read(self.config.max_body + 1)
            if len(data) > self.config.max_body:
                raise ValueError(f'upstream body exceeds HARNESS_MAX_BODY_MB ({self.config.max_body} bytes)')
            return Exchange(resp.status, [(k, v) for k, v in resp.getheaders()], data)
        finally:
            conn.close()

    def _respond(self, exchange: Exchange, label: str) -> None:
        self.send_response(exchange.status)
        sent_length = False
        for name, value in exchange.headers:
            lname = name.lower()
            if lname in {'transfer-encoding', 'connection', 'keep-alive', 'content-encoding'}:
                continue
            if lname == 'content-length':
                if self.command != 'HEAD':
                    continue  # recomputed below from the bytes actually served
                sent_length = True
            self.send_header(name, value)
        if not sent_length and self.command != 'HEAD' and exchange.status >= 200 and exchange.status not in {204, 304}:
            self.send_header('Content-Length', str(len(exchange.body)))
        self.end_headers()
        if self.command != 'HEAD' and exchange.body:
            self.wfile.write(exchange.body)
        if label != 'health':
            print(f'{exchange.status} {label}', flush=True)

    def _fail(self, message: str) -> None:
        self._respond(
            Exchange(502, [('content-type', 'text/plain'), ('x-harness-error', 'true')], (message + '\n').encode()),
            'FAIL ' + message,
        )


class TLSHandler(Handler):
    scheme = 'https'


def serve(server: ThreadingHTTPServer) -> None:
    server.serve_forever()


def main() -> None:
    config = Config()
    Handler.config = config
    config.state.mkdir(parents=True, exist_ok=True)
    (config.state / 'misses.txt').touch()
    plain = ThreadingHTTPServer(('0.0.0.0', 80), Handler)
    tls = ThreadingHTTPServer(('0.0.0.0', 443), TLSHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(config.ca_dir / 'leaf.crt', config.ca_dir / 'leaf.key')
    # Handshake lazily in the handler thread so a stalled client never blocks accept().
    tls.socket = ctx.wrap_socket(tls.socket, server_side=True, do_handshake_on_connect=False)
    plain.daemon_threads = tls.daemon_threads = True
    print(f'fake upstream: mode={config.mode} hosts={" ".join(sorted(config.hosts))}', flush=True)
    threading.Thread(target=serve, args=(plain,), daemon=True).start()
    serve(tls)


if __name__ == '__main__':
    main()
