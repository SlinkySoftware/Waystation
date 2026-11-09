"""Webhook delivery (SPECIFICATION §4.12, T-O05).

* ``fan_out``: an event job (``webhook.event``, from the server's outbox) becomes one delivery per enabled webhook that
  wants it. The delivery id is derived from the event and the webhook, so a job that runs again creates nothing new;
  new deliveries are claimed by the job itself and attempted at once.
* ``attempt``: one HTTP POST of the stored body, signed afresh (``Waystation-Signature``), through the SSRF guard
  (the checked address is the connected address; private targets only for webhooks that allow them), no redirects,
  bounded by the webhook's timeout; the first KiB of the answer is kept. A 2xx is success; anything else is retried
  with exponential backoff until the webhook's ``max_attempts``, then the delivery is dead. A target the SSRF guard
  refuses is dead at once (retrying cannot help).
* ``sweep``: the scheduled job that attempts every delivery whose retry is due (and any whose job was lost).

Secrets are decrypted only to sign and to add custom headers; they are never logged or stored on deliveries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx
from redis.asyncio import Redis

from waystation_common import webhooks as wh
from waystation_common.crypto import Keyring
from waystation_common.db import webhooks as webhooks_db
from waystation_common.db.client import Database, Document
from waystation_common.errors import UpstreamError
from waystation_common.events import keys
from waystation_common.settings import Settings
from waystation_common.ssrf import GuardedBackend, SsrfGuard, UpstreamBlocked

log = logging.getLogger('waystation.worker.webhooks')

SWEEP_BATCH = 100
CONCURRENCY = 8


def secret_aad(webhook_id: str) -> bytes:
    return f'webhook:{webhook_id}'.encode()


def headers_aad(webhook_id: str) -> bytes:
    return f'webhook-headers:{webhook_id}'.encode()


@dataclass(frozen=True)
class Outcome:
    status_code: int | None
    latency_ms: float
    response: str | None
    error: str | None
    retry: bool  # a failure worth another attempt (False for SSRF refusals)

    @property
    def ok(self) -> bool:
        return wh.success(self.status_code)


class Sender:
    """The outbound HTTP side: clients per SSRF mode, the server-wide outbound proxy when one is configured."""

    def __init__(self, settings: Settings | None) -> None:
        self.guard = SsrfGuard(settings.ssrf_allow_cidrs if settings else ())
        self.proxy = (settings.http_proxy or None) if settings else None
        self.no_proxy = {h.strip().lower() for h in ((settings.no_proxy or '') if settings else '').split(',') if h}
        self.extra_ca = str(settings.extra_ca_file) if settings and settings.extra_ca_file else None
        self._ssl: ssl.SSLContext | None = None
        self._clients: dict[tuple[bool, bool], httpx.AsyncClient] = {}

    async def __aenter__(self) -> Sender:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.aclose()

    def _proxied(self, host: str) -> bool:
        return bool(self.proxy) and not any(host == h or host.endswith('.' + h.lstrip('.')) for h in self.no_proxy)

    async def _client(self, *, allow_private: bool, proxied: bool) -> httpx.AsyncClient:
        key = (allow_private, proxied)
        client = self._clients.get(key)
        if client is not None:
            return client
        if self._ssl is None:
            self._ssl = await asyncio.to_thread(_ssl_context, self.extra_ca)
        common: dict[str, Any] = {'follow_redirects': False, 'trust_env': False}
        if proxied:
            transport = httpx.AsyncHTTPTransport(verify=self._ssl, proxy=self.proxy)
        else:
            transport = httpx.AsyncHTTPTransport(verify=self._ssl)
            # The SSRF guard as the network backend (as the proxy engine does, ARCH §6.1).
            transport._pool = httpcore.AsyncConnectionPool(
                ssl_context=self._ssl,
                max_connections=16,
                http1=True,
                http2=False,
                network_backend=GuardedBackend(self.guard, allow_private=allow_private),
            )
        client = httpx.AsyncClient(transport=transport, **common)
        self._clients[key] = client
        return client

    async def post(
        self, url: str, body: bytes, headers: Mapping[str, str], *, allow_private: bool, timeout_s: float
    ) -> Outcome:
        started = time.perf_counter()

        def elapsed() -> float:
            return round((time.perf_counter() - started) * 1000, 1)

        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        proxied = self._proxied(host)
        try:
            if proxied:  # the proxy connects: check the target before the request (ARCH §6.1)
                port = parts.port or (443 if parts.scheme == 'https' else 80)
                await self.guard.resolve(host, port, allow_private=allow_private)
            client = await self._client(allow_private=allow_private, proxied=proxied)
            timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 10.0))
            async with asyncio.timeout(timeout_s + 5):
                async with client.stream('POST', url, content=body, headers=dict(headers), timeout=timeout) as resp:
                    raw = b''
                    async for chunk in resp.aiter_bytes():
                        raw += chunk
                        if len(raw) >= wh.RESPONSE_SNIPPET_BYTES:
                            break
                    return Outcome(resp.status_code, elapsed(), wh.snippet(raw) or None, None, True)
        except UpstreamBlocked:
            return Outcome(
                None, elapsed(), None, 'refused: the target resolves to a private or reserved address', False
            )
        except UpstreamError as exc:
            return Outcome(None, elapsed(), None, str(exc)[:300], True)
        except TimeoutError, httpx.TimeoutException:
            return Outcome(None, elapsed(), None, f'timed out after {timeout_s:g} s', True)
        except httpx.HTTPError as exc:
            return Outcome(None, elapsed(), None, f'{type(exc).__name__}: {str(exc)[:300]}'.rstrip(': '), True)


def _ssl_context(extra_ca: str | None) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if extra_ca:
        ctx.load_verify_locations(cafile=extra_ca)
    return ctx


# ---------------------------------------------------------------------------- deliveries
@dataclass
class Deliverer:
    db: Database
    valkey: Redis
    keyring: Keyring | None
    sender: Sender
    now: Callable[[], datetime]

    def _secrets(self, webhook: Document) -> tuple[str, dict[str, str]]:
        if self.keyring is None:
            raise RuntimeError('WAYSTATION_SECRET_KEYS is not set: webhook secrets cannot be read')
        webhook_id = str(webhook['_id'])
        secret = self.keyring.decrypt_str(webhook['secret_enc'], aad=secret_aad(webhook_id))
        headers: dict[str, str] = {}
        if webhook.get('headers_enc'):
            headers = json.loads(self.keyring.decrypt_str(webhook['headers_enc'], aad=headers_aad(webhook_id)))
        return secret, headers

    async def attempt(self, delivery_id: str, *, claimed: Document | None = None) -> str | None:
        """Attempt one delivery if it is due and nobody else holds it. Returns the resulting status or None."""
        now = self.now()
        webhook: Document | None = None
        if claimed is None:
            # The lease must outlast the webhook's timeout: read the webhook first (its timeout), then claim.
            owner = await webhooks_db.delivery_webhook_id(self.db, delivery_id)
            if owner is None:
                return None
            webhook = await webhooks_db.get_webhook(self.db, owner)
            timeout_s = float((webhook or {}).get('timeout_s') or wh.DEFAULT_TIMEOUT_S)
            claimed = await webhooks_db.claim(self.db, delivery_id, now=now, lease_s=timeout_s + 5)
            if claimed is None:
                return None
        else:
            webhook = await webhooks_db.get_webhook(self.db, str(claimed['webhook_id']))
        if webhook is None:
            await webhooks_db.give_up(self.db, delivery_id, reason='the webhook was deleted', now=now)
            return 'dead'
        if not webhook.get('enabled', True) and claimed.get('trigger') == 'event':
            await webhooks_db.give_up(self.db, delivery_id, reason='the webhook is turned off', now=now)
            await _count(self.valkey, 'dead')
            return 'dead'
        attempt_no = int(claimed.get('attempts') or 0) + 1
        body = str(claimed['body']).encode()
        try:
            secret, custom = self._secrets(webhook)
        except Exception as exc:
            log.error('webhook secret unreadable', extra={'webhook_id': webhook['_id'], 'error': type(exc).__name__})
            outcome = Outcome(None, 0.0, None, 'the webhook secret cannot be decrypted (WAYSTATION_SECRET_KEYS)', False)
        else:
            headers = wh.headers_for(
                body=body,
                secret=secret,
                event=str(claimed['event']),
                event_id=str(claimed['event_id']),
                delivery_id=delivery_id,
                attempt=attempt_no,
                timestamp=int(now.timestamp()),
                custom=custom,
            )
            outcome = await self.sender.post(
                str(webhook['url']),
                body,
                headers,
                allow_private=bool(webhook.get('allow_private')),
                timeout_s=float(webhook.get('timeout_s') or wh.DEFAULT_TIMEOUT_S),
            )
        finished = self.now()
        max_attempts = int(webhook.get('max_attempts') or wh.DEFAULT_MAX_ATTEMPTS)
        if claimed.get('trigger') == 'test':
            max_attempts = 1  # a test shows the answer at once; send another to try again
        if outcome.ok:
            status: webhooks_db.DeliveryStatus = 'succeeded'
            next_at = None
        elif outcome.retry and attempt_no < max_attempts:
            status = 'pending'
            next_at = finished + timedelta(seconds=wh.retry_delay_s(attempt_no))
        else:
            status = 'dead'
            next_at = None
        entry = {
            'n': attempt_no,
            'at': now,
            'status_code': outcome.status_code,
            'latency_ms': outcome.latency_ms,
            'error': outcome.error,
            'response': outcome.response,
            'outcome': 'succeeded' if outcome.ok else 'failed',
        }
        await webhooks_db.record_attempt(
            self.db, delivery_id, attempt=entry, status=status, next_attempt_at=next_at, now=finished
        )
        await _count(self.valkey, 'succeeded' if status == 'succeeded' else 'failed' if status == 'pending' else 'dead')
        level = logging.INFO if outcome.ok else logging.WARNING
        log.log(
            level,
            'webhook delivery %s',
            status,
            extra={
                'webhook_id': webhook['_id'],
                'delivery_id': delivery_id,
                'event': claimed['event'],
                'attempt': attempt_no,
                'status_code': outcome.status_code,
                'latency_ms': outcome.latency_ms,
                'error': outcome.error,
            },
        )
        return status

    async def fan_out(
        self, *, event_id: str, event: str, repository: str | None, document: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Record and attempt the deliveries of one event."""
        hooks = [h for h in await webhooks_db.list_enabled(self.db) if wh.wants(h, event, repository)]
        if not hooks:
            return {'event': event, 'deliveries': 0}
        body = wh.encode(document).decode()
        now = self.now()
        full = {str(h['_id']): h for h in await webhooks_db.get_webhooks(self.db, [str(h['_id']) for h in hooks])}
        created: list[Document] = []
        for webhook_id, webhook in full.items():
            timeout_s = float(webhook.get('timeout_s') or wh.DEFAULT_TIMEOUT_S)
            doc = webhooks_db.new_delivery(
                delivery_id=f'{event_id}-{webhook_id}',
                webhook_id=webhook_id,
                event_id=event_id,
                event=event,
                repository=repository,
                body=body,
                now=now,
                lease_until=now + timedelta(seconds=timeout_s + 5 + webhooks_db.LEASE_MARGIN_S),
            )
            if await webhooks_db.insert_delivery(self.db, doc):
                created.append(doc)
        # Deliveries this run created are leased to it; ones that already existed are left to the sweep.
        statuses = await _bounded([self.attempt(str(d['_id']), claimed=d) for d in created])
        return {
            'event': event,
            'deliveries': len(created),
            'succeeded': sum(1 for s in statuses if s == 'succeeded'),
        }

    async def sweep(self) -> dict[str, Any]:
        ids = await webhooks_db.due(self.db, now=self.now(), limit=SWEEP_BATCH)
        statuses = await _bounded([self.attempt(i) for i in ids])
        return {'attempted': sum(1 for s in statuses if s is not None), 'succeeded': statuses.count('succeeded')}


async def _bounded(coros: list[Any]) -> list[str | None]:
    if not coros:
        return []
    gate = asyncio.Semaphore(CONCURRENCY)

    async def run(coro: Any) -> str | None:
        async with gate:
            result: str | None = await coro
            return result

    return list(await asyncio.gather(*(run(c) for c in coros)))


async def _count(valkey: Redis, outcome: str) -> None:
    try:
        await valkey.hincrby(keys.WEBHOOK_TOTALS, outcome, 1)
    except Exception as exc:  # metrics only
        log.warning('could not count a webhook delivery', extra={'error': repr(exc)})


def deliverer(
    db: Database, valkey: Redis, settings: Settings | None, sender: Sender, now: Callable[[], datetime]
) -> Deliverer:
    keyring = Keyring.from_settings(settings.secret_keys) if settings and settings.secret_keys else None
    return Deliverer(db=db, valkey=valkey, keyring=keyring, sender=sender, now=now)
