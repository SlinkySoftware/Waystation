"""Stand-in for the Waystation web tier, used by check.sh to exercise the reverse-proxy examples (stdlib only).

Endpoints (all under the alias waystation-web:8080):
  GET  /healthz                  200 {"status":"ok"}
  GET  /api/v1/events/stream     text/event-stream, one event per second for 10 s (shows whether a proxy buffers)
  GET  /big?mb=N                 N MiB of zeros, streamed in 256 KiB chunks
  PUT  /upload                   reads the body (Content-Length or chunked), answers {"bytes": n}
  GET  /headers                  the X-Forwarded-* headers it received
  GET  /slow?s=N                 waits N seconds before answering (proxy idle timeouts)
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CHUNK = 256 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass

    def _json(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path == '/healthz':
            self._json(200, {'status': 'ok'})
        elif url.path == '/api/v1/events/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Connection', 'close')
            self.end_headers()
            for i in range(10):
                self.wfile.write(f'id: {i}\nevent: tick\ndata: {{"n": {i}}}\n\n'.encode())
                self.wfile.flush()
                time.sleep(1)
            self.close_connection = True
        elif url.path == '/big':
            size = int(query.get('mb', ['16'])[0]) * 1024 * 1024
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(size))
            self.end_headers()
            block = bytes(CHUNK)
            left = size
            while left > 0:
                n = min(CHUNK, left)
                self.wfile.write(block[:n])
                left -= n
        elif url.path == '/headers':
            self._json(200, {k: v for k, v in self.headers.items() if k.lower().startswith('x-')})
        elif url.path == '/slow':
            time.sleep(float(query.get('s', ['1'])[0]))
            self._json(200, {'status': 'ok'})
        else:
            self._json(404, {'error': 'not found'})

    def do_PUT(self) -> None:  # noqa: N802 - stdlib name
        total = 0
        if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
            while True:
                size = int(self.rfile.readline().split(b';')[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline() not in (b'\r\n', b'\n', b''):
                        pass
                    break
                left = size
                while left > 0:
                    left -= len(self.rfile.read(min(CHUNK, left)))
                total += size
                self.rfile.readline()
        else:
            left = int(self.headers.get('Content-Length', '0'))
            while left > 0:
                got = len(self.rfile.read(min(CHUNK, left)))
                if got == 0:
                    break
                total += got
                left -= got
        self._json(200, {'bytes': total})

    do_POST = do_PUT  # noqa: N815


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()  # noqa: S104 - test container
