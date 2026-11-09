"""Webhooks (SPECIFICATION §4.12, T-O05): the event catalog, filters, payloads, signatures and retry schedule.

Pure functions shared by the server (which decides whether an event concerns any webhook and enqueues it through
the durable outbox) and the worker (which builds, signs and delivers the requests). Persistence is in
``waystation_common.db.webhooks``; delivery in ``waystation_worker.webhooks``.

Signature (``Waystation-Signature: t=<unix seconds>,v1=<hex>``): HMAC-SHA256 under the webhook's secret over
``<t>.`` followed by the exact request body bytes. Each attempt is signed afresh with the current time, so a receiver
can refuse anything older than a few minutes (``verify``'s ``tolerance_s``, default 300 s) and so refuse replays.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import ipaddress
import json
import random
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------- catalog
# Events a webhook can subscribe to: the audit actions of content and repository changes (same names, same records).
EVENTS: Final[tuple[str, ...]] = (
    'content.upload',
    'content.delete',
    'content.update',
    'repository.create',
    'repository.update',
    'repository.delete',
)
PING: Final = 'webhook.ping'  # the "send test event" request; always delivered, whatever the filter
EVENT_PATTERNS: Final[tuple[str, ...]] = ('*', 'content.*', 'repository.*', *EVENTS)

SIGNATURE_HEADER: Final = 'Waystation-Signature'
TIMESTAMP_HEADER: Final = 'Waystation-Timestamp'
EVENT_HEADER: Final = 'Waystation-Event'
EVENT_ID_HEADER: Final = 'Waystation-Event-Id'
DELIVERY_HEADER: Final = 'Waystation-Delivery'
ATTEMPT_HEADER: Final = 'Waystation-Attempt'
IDEMPOTENCY_HEADER: Final = 'Idempotency-Key'
USER_AGENT: Final = 'Waystation-Webhooks (+https://github.com/SlinkySoftware/Waystation)'

DEFAULT_TOLERANCE_S: Final = 300
MAX_BODY_BYTES: Final = 256 * 1024  # a request body larger than this drops the event's details
RESPONSE_SNIPPET_BYTES: Final = 1024  # what is kept of a receiver's answer, per attempt
MAX_HEADERS: Final = 20
MAX_HEADER_VALUE: Final = 1024
MAX_REPO_GLOBS: Final = 50
SECRET_MIN: Final = 16
SECRET_MAX: Final = 256
DEFAULT_TIMEOUT_S: Final = 10
DEFAULT_MAX_ATTEMPTS: Final = 8
RETRY_BASE_S: Final = 30
RETRY_CAP_S: Final = 3600

# Header names an administrator may not set: the transport's own and ours.
RESERVED_HEADERS: Final = frozenset(
    {
        'host',
        'content-length',
        'content-type',
        'content-encoding',
        'transfer-encoding',
        'connection',
        'keep-alive',
        'upgrade',
        'te',
        'trailer',
        'expect',
        'proxy-authorization',
        'user-agent',
        IDEMPOTENCY_HEADER.lower(),
    }
)
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")  # RFC 9110 token
_BAD_VALUE = re.compile(r'[\x00-\x08\x0a-\x1f\x7f]')


# ---------------------------------------------------------------------------- filters
def event_matches(patterns: Iterable[str], event: str) -> bool:
    """Does a webhook subscribed to ``patterns`` (exact names, ``<category>.*`` or ``*``) receive ``event``?"""
    if event == PING:
        return True
    category = event.partition('.')[0]
    return any(p == '*' or p == event or p == f'{category}.*' for p in patterns)


def repo_matches(globs: Sequence[str], repository: str | None) -> bool:
    """Repository scope: no globs = every repository; otherwise a case-insensitive shell-style match on the name.
    Events without a repository go only to webhooks without a scope."""
    if not globs:
        return True
    if not repository:
        return False
    name = repository.lower()
    return any(fnmatch.fnmatchcase(name, g.lower()) for g in globs)


def wants(webhook: Mapping[str, Any], event: str, repository: str | None) -> bool:
    return event_matches(webhook.get('events') or (), event) and repo_matches(
        webhook.get('repositories') or (), repository
    )


def event_repository(action: str, target: Mapping[str, Any] | None, details: Mapping[str, Any] | None) -> str | None:
    """The repository an audit record of a webhook event is about (content: ``details.repository``)."""
    repo = (details or {}).get('repository')
    if isinstance(repo, str) and repo:
        return repo
    if action.startswith('repository.') and target and target.get('kind') == 'repository':
        name = target.get('name')
        return name if isinstance(name, str) else None
    return None


# ---------------------------------------------------------------------------- validation
def check_url(url: str) -> str | None:
    """Why ``url`` cannot be a webhook target (None = fine). Literal private addresses are checked by the caller."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return 'not a valid URL'
    if parts.scheme not in ('http', 'https'):
        return 'must be an http:// or https:// URL'
    if not parts.hostname:
        return 'must name a host'
    if parts.username is not None or parts.password is not None:
        return 'credentials in the URL are not allowed: use a custom header'
    if parts.fragment:
        return 'must not have a fragment'
    if port == 0:
        return 'not a valid port'
    return None


def literal_ip(url: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        host = urlsplit(url).hostname or ''
        return ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        return None


def check_header(name: str, value: str) -> str | None:
    """Why a custom header cannot be sent (None = fine)."""
    if not _TOKEN.match(name):
        return 'not a valid header name'
    lowered = name.lower()
    if lowered in RESERVED_HEADERS or lowered.startswith('waystation-'):
        return 'is set by Waystation'
    if len(value) > MAX_HEADER_VALUE:
        return f'at most {MAX_HEADER_VALUE} characters'
    if _BAD_VALUE.search(value):
        return 'must not contain control characters'
    return None


# ---------------------------------------------------------------------------- payloads
def _iso(ts: datetime) -> str:
    return ts.isoformat().replace('+00:00', 'Z')


def build_event(
    *,
    event_id: str,
    event: str,
    occurred_at: datetime,
    repository: str | None,
    actor: Mapping[str, Any] | None,
    target: Mapping[str, Any] | None,
    details: Mapping[str, Any] | None,
    changes: Sequence[Mapping[str, Any]] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """The JSON document a receiver gets (also kept on the delivery, so a redelivery sends the same document)."""
    body: dict[str, Any] = {
        'id': event_id,
        'type': event,
        'occurred_at': _iso(occurred_at),
        'repository': repository,
        'actor': {'kind': (actor or {}).get('kind', 'system'), 'name': (actor or {}).get('name', 'system')},
        'target': dict(target) if target else None,
        'details': dict(details or {}),
    }
    if changes:
        body['changes'] = [dict(c) for c in changes]
    if source:
        body['source'] = source
    return body


def encode(document: Mapping[str, Any]) -> bytes:
    """Compact, stable JSON; a document over ``MAX_BODY_BYTES`` loses its details and changes (marked truncated)."""
    raw = json.dumps(document, separators=(',', ':'), ensure_ascii=False, default=str).encode()
    if len(raw) <= MAX_BODY_BYTES:
        return raw
    slim = {k: v for k, v in document.items() if k not in ('details', 'changes')}
    slim['details'] = {'truncated': True}
    return json.dumps(slim, separators=(',', ':'), ensure_ascii=False, default=str).encode()


# ---------------------------------------------------------------------------- signatures
def sign(secret: str, body: bytes, timestamp: int) -> str:
    """The ``Waystation-Signature`` header value for ``body`` sent at ``timestamp`` (unix seconds)."""
    mac = hmac.new(secret.encode(), f'{timestamp}.'.encode() + body, hashlib.sha256).hexdigest()
    return f't={timestamp},v1={mac}'


def verify(
    secret: str, body: bytes, header: str, *, tolerance_s: int = DEFAULT_TOLERANCE_S, now: float | None = None
) -> bool:
    """A receiver's check: the header parses, its timestamp is within ``tolerance_s`` of now, and one ``v1``
    signature matches (constant-time). Documented in ``Documents/Operations/WEBHOOKS.md``."""
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(','):
        key, _, value = part.strip().partition('=')
        if key == 't' and value.isdigit():
            timestamp = int(value)
        elif key == 'v1':
            signatures.append(value)
    if timestamp is None or not signatures:
        return False
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_s:
        return False
    expected = sign(secret, body, timestamp).rpartition('v1=')[2]
    return any(hmac.compare_digest(expected, s) for s in signatures)


def headers_for(
    *,
    body: bytes,
    secret: str,
    event: str,
    event_id: str,
    delivery_id: str,
    attempt: int,
    timestamp: int,
    custom: Mapping[str, str] | None = None,
) -> dict[str, str]:
    headers = {k: v for k, v in (custom or {}).items() if check_header(k, v) is None}
    headers.update(
        {
            'Content-Type': 'application/json',
            'User-Agent': USER_AGENT,
            EVENT_HEADER: event,
            EVENT_ID_HEADER: event_id,
            DELIVERY_HEADER: delivery_id,
            IDEMPOTENCY_HEADER: delivery_id,
            ATTEMPT_HEADER: str(attempt),
            TIMESTAMP_HEADER: str(timestamp),
            SIGNATURE_HEADER: sign(secret, body, timestamp),
        }
    )
    return headers


# ---------------------------------------------------------------------------- retries
def retry_delay_s(attempts: int, *, jitter: float | None = None) -> float:
    """Seconds before attempt ``attempts + 1``: 30 s doubling per failed attempt, at most an hour, ±10 % jitter."""
    base = float(min(RETRY_CAP_S, RETRY_BASE_S << max(0, min(attempts - 1, 20))))
    spread = random.uniform(-0.1, 0.1) if jitter is None else jitter  # noqa: S311 - not security relevant
    return base * (1 + spread)


def success(status_code: int | None) -> bool:
    return status_code is not None and 200 <= status_code < 300


def snippet(raw: bytes) -> str:
    """A receiver's answer as kept on the delivery log: the first ``RESPONSE_SNIPPET_BYTES``, printable text only."""
    text = raw[:RESPONSE_SNIPPET_BYTES].decode('utf-8', errors='replace')
    return ''.join(c if c.isprintable() or c in '\n\t' else '�' for c in text)
