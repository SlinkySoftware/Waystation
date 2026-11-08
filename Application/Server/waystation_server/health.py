"""``/healthz``, ``/readyz`` and ``/metrics`` (SPECIFICATION §4.4, §5.5).

- ``/healthz``: liveness; the process answers. Reveals nothing else.
- ``/readyz``: MongoDB and Valkey answer a ping within 2 s; 503 otherwise. Reveals only ``ok``/``unavailable``.
- ``/metrics``: Prometheus text format, only for clients in ``WAYSTATION_METRICS_ALLOW_CIDRS`` (default loopback);
  or with an API token carrying the ``metrics`` scope whose owner holds ``stats-read`` (``Authorization: Bearer``);
  everyone else gets 404.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from waystation_common.errors import WaystationError
from waystation_server.clientip import in_networks, parse_ip, parse_networks
from waystation_server.metrics import CONTENT_TYPE
from waystation_server.problems import native_error
from waystation_server.security import access
from waystation_server.security.authn import authenticate
from waystation_server.services import Services

log = logging.getLogger('waystation.health')

READY_TIMEOUT_S = 2.0

router = APIRouter(include_in_schema=False)


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


@router.get('/healthz')
@access.public
async def healthz() -> dict[str, str]:
    return {'status': 'ok'}


async def _ready(services: Services) -> bool:
    async def mongo() -> None:
        await services.db.command('ping')

    async def valkey() -> None:
        await services.valkey.ping()

    try:
        async with asyncio.timeout(READY_TIMEOUT_S):
            await asyncio.gather(mongo(), valkey())
    except Exception as exc:
        log.warning('not ready', extra={'error': type(exc).__name__})
        return False
    return True


@router.get('/readyz')
@access.public
async def readyz(request: Request) -> JSONResponse:
    ok = await _ready(_services(request))
    return JSONResponse({'status': 'ok' if ok else 'unavailable'}, status_code=200 if ok else 503)


@router.get('/metrics')
@access.public  # restricted by client address in the handler
async def metrics(request: Request) -> Response:
    services = _services(request)
    allowed = parse_networks(services.settings.metrics_allow_cidrs)
    ip = parse_ip(getattr(request.state, 'client_ip', None))
    if ip is None or not in_networks(ip, allowed):
        try:
            principal = await authenticate(request)
        except WaystationError:
            return native_error(404, 'Not Found')
        if not access.may_scrape_metrics(principal):
            return native_error(404, 'Not Found')
    return Response(services.metrics.render(), media_type=CONTENT_TYPE)
