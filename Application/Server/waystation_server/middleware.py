"""Pure ASGI middleware (no ``BaseHTTPMiddleware``: it buffers and breaks streaming).

``RequestContextMiddleware``: request id, client address, logging context and one access-log line per request
(ARCHITECTURE §12). Handlers add access-log fields through ``request.state.log`` (a dict: ``cache``, ``bytes``,
``repo``...). The request id is echoed in ``X-Request-Id``.

``HeadMiddleware``: answers ``HEAD`` for every non-format route that has a ``GET`` (FastAPI does not add HEAD): the
request runs as ``GET`` and the body is dropped, headers (including ``Content-Length``) kept. Format routes
(``/repository/``, ``/v2/``) declare HEAD themselves because HEAD there has protocol meaning.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from waystation_common.logs import bind_context, reset_context
from waystation_server.clientip import ClientResolver, request_id

log = logging.getLogger('waystation.access')

QUIET_PATHS = frozenset({'/healthz', '/readyz', '/metrics'})
FORMAT_PREFIXES = ('/repository/', '/v2/', '/v2')
STREAM_PATHS = frozenset({'/api/v1/events/stream'})


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, resolver: ClientResolver) -> None:
        self.app = app
        self.resolver = resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        peer = scope['client'][0] if scope.get('client') else None
        client_ip, internal = self.resolver.resolve(peer, headers)
        rid = request_id(headers, internal=internal)
        state: dict[str, Any] = scope.setdefault('state', {})
        state['request_id'] = rid
        state['client_ip'] = client_ip
        fields: dict[str, Any] = {}
        state['log'] = fields
        started = time.perf_counter()
        status = 0
        rid_header = (b'x-request-id', rid.encode('ascii'))

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                message['headers'] = [*message.get('headers', []), rid_header]
            await send(message)

        token = bind_context(request_id=rid)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope['path']
            level = logging.DEBUG if path in QUIET_PATHS else logging.INFO
            if log.isEnabledFor(level):
                log.log(
                    level,
                    'request',
                    extra={
                        'method': scope['method'],
                        'path': path,
                        'status': status or 500,
                        'duration_ms': round((time.perf_counter() - started) * 1000, 2),
                        'client_ip': client_ip,
                        **fields,
                    },
                )
            reset_context(token)


class HeadMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get('path', '')
        if (
            scope['type'] != 'http'
            or scope['method'] != 'HEAD'
            or path.startswith(FORMAT_PREFIXES)
            or path in STREAM_PATHS
        ):
            await self.app(scope, receive, send)
            return

        async def send_headers_only(message: Message) -> None:
            if message['type'] == 'http.response.body':
                if message.get('more_body', False):
                    return
                message = {'type': 'http.response.body', 'body': b'', 'more_body': False}
            await send(message)

        await self.app({**scope, 'method': 'GET'}, receive, send_headers_only)
