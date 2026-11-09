""" "Test upstream" (SPECIFICATION §4.12): reachability, SSRF, TLS, HTTP and authentication of an upstream before (or
after) saving a proxy repository. A failing upstream is a normal result (``reachable=False``), never an error."""

from __future__ import annotations

import base64
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urljoin

import httpx

from waystation_common.errors import UpstreamError
from waystation_server.proxy.http import Network, UpstreamClients
from waystation_server.proxy.ssrf import UpstreamBlocked

AuthResult = Literal['anonymous', 'authenticated', 'failed', 'not_required']
PROBE_TIMEOUT_S = 10.0


@dataclass
class Check:
    name: str
    ok: bool
    message: str


@dataclass
class ProbeResult:
    reachable: bool = False
    http_status: int | None = None
    latency_ms: float | None = None
    auth: AuthResult | None = None
    tls: str | None = None
    checks: list[Check] = field(default_factory=list)


def auth_headers(kind: str, username: str | None, secret: str | None) -> dict[str, str]:
    if kind == 'basic' and username is not None and secret is not None:
        raw = base64.b64encode(f'{username}:{secret}'.encode()).decode('ascii')
        return {'Authorization': f'Basic {raw}'}
    if kind == 'bearer' and secret is not None:
        return {'Authorization': f'Bearer {secret}'}
    return {}


def _tls_info(response: httpx.Response) -> str | None:
    stream: Any = response.extensions.get('network_stream')
    if stream is None:
        return None
    ssl_object = stream.get_extra_info('ssl_object')
    if ssl_object is None:
        return None
    parts = [str(ssl_object.version())]
    cert = ssl_object.getpeercert() or {}
    for rdn in cert.get('subject', ()):
        for key, value in rdn:
            if key == 'commonName':
                parts.append(f'CN={value}')
    return ', '.join(parts)


async def probe(
    clients: UpstreamClients,
    *,
    upstream_url: str,
    probe_path: str,
    network: Network,
    headers: dict[str, str],
    timeout: float = PROBE_TIMEOUT_S,  # noqa: ASYNC109 - an httpx request timeout, not a deadline
) -> ProbeResult:
    result = ProbeResult()
    url = urljoin(upstream_url if upstream_url.endswith('/') else upstream_url + '/', probe_path)
    try:
        await clients.check_target(url, network)
    except UpstreamBlocked as exc:
        result.checks.append(Check('ssrf', ok=False, message=exc.detail))
        return result
    except (UpstreamError, ValueError) as exc:
        result.checks.append(Check('dns', ok=False, message=str(exc)))
        return result
    result.checks.append(Check('dns', ok=True, message='resolved'))
    result.checks.append(Check('ssrf', ok=True, message='allowed address'))
    via = clients.proxy_for(url, network)
    if via:
        result.checks.append(Check('proxy', ok=True, message='through the outbound HTTP proxy'))
    started = time.perf_counter()
    try:
        client = await clients.client(url, network)
        async with client.stream('GET', url, headers=headers, timeout=timeout) as response:
            result.latency_ms = round((time.perf_counter() - started) * 1000, 1)
            result.reachable = True
            result.http_status = response.status_code
            result.tls = _tls_info(response) if url.startswith('https') else None
            challenge = response.headers.get('www-authenticate', '')
    except UpstreamBlocked as exc:
        result.checks.append(Check('ssrf', ok=False, message=exc.detail))
        return result
    except UpstreamError as exc:
        result.checks.append(Check('dns', ok=False, message=exc.detail))
        return result
    except (ssl.SSLError, ValueError) as exc:
        result.checks.append(Check('tls', ok=False, message=f'invalid CA bundle ({type(exc).__name__})'))
        return result
    except httpx.ProxyError as exc:
        result.checks.append(Check('proxy', ok=False, message=f'outbound proxy failed ({type(exc).__name__})'))
        return result
    except httpx.HTTPError as exc:
        name = 'tls' if 'SSL' in repr(exc) or 'certificate' in str(exc).lower() else 'http'
        result.checks.append(Check(name, ok=False, message=type(exc).__name__))
        return result
    if url.startswith('https'):
        result.checks.append(Check('tls', ok=True, message=result.tls or 'verified'))
    status = response.status_code
    has_creds = 'Authorization' in headers
    if status in (401, 403):
        if not has_creds and challenge.lower().startswith('bearer'):
            result.auth = 'anonymous'  # token service: anonymous tokens available
            result.checks.append(Check('auth', ok=True, message='anonymous token authentication offered'))
        else:
            result.auth = 'failed'
            result.checks.append(Check('auth', ok=False, message=f'upstream answered {status}'))
    else:
        result.auth = 'authenticated' if has_creds and status < 400 else 'not_required'
        result.checks.append(Check('auth', ok=status < 400, message=f'upstream answered {status}'))
    ok_status = status < 400 or result.auth == 'anonymous'
    result.checks.append(Check('http', ok=ok_status, message=f'HTTP {status}'))
    return result
