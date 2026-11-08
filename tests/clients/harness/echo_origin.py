"""Echo origin: the "live upstream" of the harness self-test format ``echo`` (tests/clients/echo/).

It deliberately behaves like a real registry in the ways that matter to record/replay, so recording it exercises
the scrubbing rules: it sets cookies, request ids and client-IP headers (must be dropped), issues bearer tokens in
JSON (must be redacted), redirects to signed, expiring URLs (query values must be scrubbed), answers 401 without
credentials and 200 with them (auth scheme is part of the fixture key), and supports ETag revalidation (304).
Only started in record/live mode; replay never needs it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOST = 'echo.example.com'


def blob(size: int) -> bytes:
    """Deterministic pseudo-random bytes: sha256 counter stream."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(counter.to_bytes(8, 'big')).digest()
        counter += 1
    return bytes(out[:size])


class Echo(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'echo-origin'
    sys_version = ''

    def do_GET(self) -> None:
        self._route()

    def do_HEAD(self) -> None:
        self._route()

    def _send(self, status: int, body: bytes, ctype: str = 'text/plain', extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        # Volatile / environment-specific headers a recording must never keep.
        self.send_header('Set-Cookie', f'session={secrets.token_hex(8)}; HttpOnly')
        self.send_header('X-Request-Id', secrets.token_hex(8))
        self.send_header('X-Client-Ip', self.client_address[0])
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _route(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        if path == '/hello':
            self._send(200, b'hello from the echo origin\n', extra={'Cache-Control': 'max-age=60'})
        elif path == '/token':
            body = {'token': secrets.token_urlsafe(24), 'access_token': secrets.token_urlsafe(24), 'expires_in': 300}
            self._send(200, json.dumps(body).encode(), 'application/json')
        elif path == '/private':
            if self.headers.get('Authorization', '').startswith('Bearer '):
                self._send(200, b'private content\n')
            else:
                self._send(401, b'unauthorized\n', extra={'WWW-Authenticate': f'Bearer realm="https://{HOST}/token"'})
        elif path == '/redirect':
            expires = int(time.time()) + 300
            location = f'https://{HOST}/blob/4096?X-Amz-Expires={expires}&X-Amz-Signature={secrets.token_hex(16)}'
            self._send(307, b'', extra={'Location': location})
        elif path.startswith('/blob/'):
            size = min(int(path.rsplit('/', 1)[1] or 0), 1 << 20)
            data = blob(size)
            etag = '"' + hashlib.sha256(data).hexdigest()[:16] + '"'
            if self.headers.get('If-None-Match') == etag:
                self._send(304, b'', 'application/octet-stream', {'ETag': etag})
            else:
                self._send(200, data, 'application/octet-stream', {'ETag': etag})
        else:
            self._send(404, b'not found\n')

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    server = ThreadingHTTPServer(('0.0.0.0', 8080), Echo)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == '__main__':
    main()
