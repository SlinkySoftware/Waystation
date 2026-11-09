"""Read a Nexus Repository 3 instance's repository configuration through its REST API (read-only).

``GET <base>/service/rest/v1/repositories`` lists every repository (name, format, type, url, attributes); the detail of
each comes from ``GET <base>/service/rest/v1/repositories/<format>/<type>/<name>`` (``maven2`` is ``maven`` in that
path). A Nexus without a detail endpoint for a format (older versions, some formats) answers 404/400/405: the
repository is then mapped from its list entry alone.

Safety: the base URL is administrator-supplied, so every request goes through the shared upstream clients
(``proxy.http``): SSRF guard at connect time unless ``allow_private`` is set, optional extra CA bundle, no redirects
followed. The credentials are sent as HTTP basic auth to that origin only, are never stored, logged or echoed, and no
error message contains them. Every body is size-bounded and parsed off the event loop when large; the number of
repositories and the whole read are bounded too.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from waystation_common.errors import UpstreamError
from waystation_server.proxy.http import Network, UpstreamClients
from waystation_server.proxy.ssrf import UpstreamBlocked

log = logging.getLogger('waystation.migration.nexus')

API = 'service/rest/v1/repositories'
MAX_LIST_BYTES = 8 * 1024 * 1024
MAX_DETAIL_BYTES = 1024 * 1024
MAX_REPOSITORIES = 1000
DETAIL_CONCURRENCY = 8
REQUEST_TIMEOUT_S = 30.0
TOTAL_TIMEOUT_S = 120.0
PARSE_IN_THREAD_BYTES = 64 * 1024
# The detail endpoint's path segment when it differs from the format name the list reports.
DETAIL_FORMAT = {'maven2': 'maven'}
NO_DETAIL = frozenset({400, 404, 405})


class NexusError(UpstreamError):
    """Nexus could not be read (``detail`` is safe to show: never a credential)."""

    code = 'nexus'


@dataclass(frozen=True)
class NexusSource:
    url: str  # base URL, e.g. https://nexus.example.com or https://nexus.example.com/nexus
    username: str
    password: str = field(repr=False)
    network: Network = field(default_factory=Network)

    @property
    def base(self) -> str:
        return self.url.rstrip('/') + '/'


@dataclass
class NexusRepository:
    """One repository: the list entry and, when Nexus offers it, the format-specific detail."""

    entry: dict[str, Any]
    detail: dict[str, Any] | None = None
    detail_error: str | None = None  # why the detail could not be read (the list entry is used instead)

    @property
    def name(self) -> str:
        return str(self.entry.get('name') or '')

    @property
    def format(self) -> str:
        return str(self.entry.get('format') or '')

    @property
    def type(self) -> str:
        return str(self.entry.get('type') or '')

    @property
    def config(self) -> dict[str, Any]:
        """The detail when read, else the list entry with its ``attributes`` (e.g. ``proxy.remoteUrl``) inlined."""
        if self.detail is not None:
            return self.detail
        merged = {k: v for k, v in self.entry.items() if k != 'attributes'}
        attributes = self.entry.get('attributes')
        if isinstance(attributes, dict):
            merged.update({k: v for k, v in attributes.items() if isinstance(v, dict)})
        return merged


@dataclass
class NexusSnapshot:
    version: str | None
    repositories: list[NexusRepository]


def url_problem(url: str) -> str | None:
    """Why ``url`` is not a usable Nexus base URL (None when it is): http(s), a host, no credentials, query or
    fragment."""
    try:
        parts = urlsplit(url.strip())
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        return 'not a valid URL'
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return 'must be an http(s) URL with a host'
    if parts.username or parts.password:
        return 'put the credentials in username and password, not in the URL'
    if parts.query or parts.fragment:
        return 'must not have a query or fragment'
    return None


def _auth(source: NexusSource) -> dict[str, str]:
    raw = base64.b64encode(f'{source.username}:{source.password}'.encode()).decode('ascii')
    return {'Authorization': f'Basic {raw}', 'Accept': 'application/json'}


async def _parse(body: bytes) -> Any:
    if len(body) > PARSE_IN_THREAD_BYTES:
        return await asyncio.to_thread(json.loads, body)
    return json.loads(body)


class _Response:
    def __init__(self, status: int, headers: httpx.Headers, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body


async def _get(clients: UpstreamClients, source: NexusSource, url: str, limit: int) -> _Response:
    try:
        client = await clients.client(url, source.network)
        async with client.stream('GET', url, headers=_auth(source), timeout=REQUEST_TIMEOUT_S) as response:
            if response.status_code != 200:
                return _Response(response.status_code, response.headers, b'')
            declared = response.headers.get('content-length')
            if declared and declared.isdigit() and int(declared) > limit:
                raise NexusError(f'Nexus answered with more than {limit} bytes')
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise NexusError(f'Nexus answered with more than {limit} bytes')
                chunks.append(chunk)
            return _Response(200, response.headers, b''.join(chunks))
    except UpstreamBlocked:
        raise NexusError(
            'the Nexus host resolves to a private or reserved address: allow private addresses to import from it'
        ) from None
    except UpstreamError as exc:
        if isinstance(exc, NexusError):
            raise
        raise NexusError(exc.detail) from None
    except ssl.SSLError, ValueError:
        raise NexusError('cannot use the CA bundle or the URL') from None
    except httpx.TimeoutException:
        raise NexusError('Nexus did not answer in time') from None
    except httpx.HTTPError as exc:
        reason = 'TLS verification failed' if 'certificate' in str(exc).lower() else type(exc).__name__
        raise NexusError(f'cannot reach Nexus ({reason})') from None


def _status_error(status: int, what: str) -> NexusError:
    if status in (401, 403):
        return NexusError(
            f'Nexus refused the credentials for {what} (HTTP {status}); an administrator account is needed'
        )
    if 300 <= status < 400:
        return NexusError(f'Nexus answered {what} with a redirect (HTTP {status}): use the URL it redirects to')
    if status == 404:
        return NexusError(f'no Nexus 3 REST API at this URL ({what}: HTTP 404)')
    return NexusError(f'Nexus answered {what} with HTTP {status}')


def _version(headers: httpx.Headers) -> str | None:
    server = str(headers.get('server', ''))
    if server.lower().startswith('nexus/'):
        return server[:100]
    return None


async def _detail(
    clients: UpstreamClients, source: NexusSource, repo: NexusRepository, gate: asyncio.Semaphore
) -> None:
    fmt = DETAIL_FORMAT.get(repo.format, repo.format)
    url = f'{source.base}{API}/{quote(fmt, safe="")}/{quote(repo.type, safe="")}/{quote(repo.name, safe="")}'
    async with gate:
        try:
            response = await _get(clients, source, url, MAX_DETAIL_BYTES)
        except NexusError as exc:
            repo.detail_error = exc.detail
            return
    if response.status in NO_DETAIL:
        repo.detail_error = 'Nexus has no detail endpoint for this repository; mapped from the repository list'
        return
    if response.status != 200:
        repo.detail_error = _status_error(response.status, 'the repository detail').detail
        return
    try:
        detail = await _parse(response.body)
    except ValueError:
        repo.detail_error = 'the repository detail is not JSON; mapped from the repository list'
        return
    if not isinstance(detail, dict) or detail.get('name') != repo.name:
        repo.detail_error = 'the repository detail does not describe this repository; mapped from the repository list'
        return
    repo.detail = detail


async def read(clients: UpstreamClients, source: NexusSource) -> NexusSnapshot:
    """Every repository of the Nexus at ``source`` with its detail. Raises ``NexusError`` when the list cannot be
    read; a detail that cannot be read is recorded on its repository instead."""
    try:
        return await asyncio.wait_for(_read(clients, source), TOTAL_TIMEOUT_S)
    except TimeoutError:
        raise NexusError(f'reading Nexus took longer than {TOTAL_TIMEOUT_S:.0f} s') from None


async def _read(clients: UpstreamClients, source: NexusSource) -> NexusSnapshot:
    response = await _get(clients, source, source.base + API, MAX_LIST_BYTES)
    if response.status != 200:
        raise _status_error(response.status, 'the repository list')
    try:
        listing = await _parse(response.body)
    except ValueError:
        raise NexusError('the repository list is not JSON: is this a Nexus 3 URL?') from None
    if not isinstance(listing, list):
        raise NexusError('the repository list is not a JSON array: is this a Nexus 3 URL?')
    if len(listing) > MAX_REPOSITORIES:
        raise NexusError(f'Nexus lists more than {MAX_REPOSITORIES} repositories')
    repos = [
        NexusRepository(entry=e)
        for e in listing
        if isinstance(e, dict) and isinstance(e.get('name'), str) and isinstance(e.get('format'), str)
    ]
    gate = asyncio.Semaphore(DETAIL_CONCURRENCY)
    await asyncio.gather(*(_detail(clients, source, r, gate) for r in repos if r.type in ('proxy', 'hosted', 'group')))
    log.info('nexus repositories read', extra={'count': len(repos)})
    return NexusSnapshot(version=_version(response.headers), repositories=repos)
