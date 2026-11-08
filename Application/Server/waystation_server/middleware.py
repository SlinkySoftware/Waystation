"""Pure ASGI middleware (no ``BaseHTTPMiddleware``: it buffers and breaks streaming).

``RequestContextMiddleware``: request id, client address, logging context and one access-log line per request
(ARCHITECTURE §12), and the ``waystation_http_request_duration_seconds`` observation (route template, method,
status class; not for the long-lived event stream). Handlers add access-log fields through ``request.state.log`` (a
dict: ``cache``, ``bytes``, ``repo``...). The request id is echoed in ``X-Request-Id``.

``HeadMiddleware``: answers ``HEAD`` for every non-format route that has a ``GET`` (FastAPI does not add HEAD): the
request runs as ``GET`` and the body is dropped, headers (including ``Content-Length``) kept. Format routes
(``/repository/``, ``/v2/``) declare HEAD themselves because HEAD there has protocol meaning.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from starlette.datastructures import Headers
from starlette.routing import replace_params
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from waystation_common.logs import bind_context, reset_context
from waystation_server.clientip import ClientResolver, request_id

log = logging.getLogger('waystation.access')

QUIET_PATHS = frozenset({'/healthz', '/readyz', '/metrics'})
FORMAT_PREFIXES = ('/repository/', '/v2/', '/v2')
STREAM_PATHS = frozenset({'/api/v1/events/stream'})
UNMATCHED = 'unmatched'  # route label of requests no route matched (404s of unknown paths)


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
            if path not in STREAM_PATHS:
                _observe(scope, status or 500, time.perf_counter() - started)
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


def _observe(scope: Scope, status: int, seconds: float) -> None:
    """Route latency metric: labelled by the matched route's template (bounded), never the raw path."""
    services = getattr(getattr(scope.get('app'), 'state', None), 'services', None)
    if services is None:
        return
    services.metrics.http_request(route=route_template(scope), method=scope['method'], status=status, seconds=seconds)


def route_template(scope: Scope) -> str:
    """The matched route's full template (``/api/v1/repositories/{name}``). FastAPI puts the route of an included
    router in ``scope['route']`` with its own path (no prefix): the prefix is what precedes the rendered route."""
    route = scope.get('route')
    template = getattr(route, 'path_format', None)
    if not isinstance(template, str):
        return UNMATCHED
    try:
        rendered, _ = replace_params(
            template, getattr(route, 'param_convertors', {}), dict(scope.get('path_params') or {})
        )
    except Exception:
        return template
    path: str = scope.get('path', '')
    if rendered and path.endswith(rendered):
        return path[: len(path) - len(rendered)] + template
    return template


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

        inner = {**scope, 'method': 'GET'}
        try:
            await self.app(inner, receive, send_headers_only)
        finally:
            for key in ('route', 'path_params'):  # for the route latency metric
                if key in inner:
                    scope[key] = inner[key]
