"""Webhooks (SPECIFICATION §4.12, T-O05; OpenAPI tag ``webhooks``). Everything here needs ``admin``.

- ``/webhooks``: definitions. The secret (HMAC key) and custom header values are write-only: stored AES-GCM encrypted
  under ``WAYSTATION_SECRET_KEYS``, answered as ``secret_set`` and ``header_names``. A literal private address is
  refused unless the webhook allows private targets (the worker's SSRF guard checks every connection anyway).
- ``/webhooks/{id}/test`` records a ``webhook.ping`` delivery and asks a worker to attempt it now.
- ``/webhooks/{id}/deliveries``: the delivery log (newest first, cursor paging); one delivery with its request body
  and attempts; ``.../redeliver`` sends a delivery's document again as a new delivery (same event id).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from waystation_common import webhooks as wh
from waystation_common.db import webhooks as webhooks_db
from waystation_common.db.client import Document
from waystation_common.errors import NotFound, ValidationFailed
from waystation_common.ids import new_id
from waystation_common.jobs import WebhookDeliver
from waystation_server import audit
from waystation_server.api.deps import ServicesDep
from waystation_server.audit import AuditAction
from waystation_server.problems import FieldError, FieldErrors
from waystation_server.repos.schemas import NAME_PATTERN
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate
from waystation_server.security.principal import Principal
from waystation_server.services import Services

router = APIRouter(tags=['webhooks'])
log = logging.getLogger('waystation.webhooks')

PROBLEM = {'content': {'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}}}
WebhookId = Annotated[str, Path(min_length=1, max_length=64)]
DeliveryId = Annotated[str, Path(min_length=1, max_length=128)]
WebhookEventName = Literal[
    '*',
    'content.*',
    'repository.*',
    'content.upload',
    'content.delete',
    'content.update',
    'repository.create',
    'repository.update',
    'repository.delete',
]
DeliveryStatus = Literal['pending', 'succeeded', 'dead']
DeliveryTrigger = Literal['event', 'test', 'redeliver']


def secret_aad(webhook_id: str) -> bytes:
    return f'webhook:{webhook_id}'.encode()


def headers_aad(webhook_id: str) -> bytes:
    return f'webhook-headers:{webhook_id}'.encode()


# ---------------------------------------------------------------------------- schemas
class WebhookWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(pattern=NAME_PATTERN)
    description: str = Field(default='', max_length=500)
    url: str = Field(min_length=8, max_length=2048, description='Where events are POSTed (http or https)')
    events: list[WebhookEventName] = Field(min_length=1, max_length=20)
    repositories: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list,
        max_length=wh.MAX_REPO_GLOBS,
        description='Repository name globs (`*`, `?`, case-insensitive); empty = every repository',
    )
    enabled: bool = True
    secret: str | None = Field(
        default=None,
        min_length=wh.SECRET_MIN,
        max_length=wh.SECRET_MAX,
        description='HMAC-SHA256 signing key (write-only). Required when creating; null keeps the stored one',
        json_schema_extra={'writeOnly': True},
    )
    headers: dict[str, str] | None = Field(
        default=None,
        description='Extra request headers (write-only; values may be credentials). Null keeps the stored ones',
        json_schema_extra={'writeOnly': True},
    )
    allow_private: bool = Field(default=False, description='Allow targets on private or reserved addresses')
    timeout_s: int = Field(default=wh.DEFAULT_TIMEOUT_S, ge=1, le=30)
    max_attempts: int = Field(default=wh.DEFAULT_MAX_ATTEMPTS, ge=1, le=20)


class WebhookDeliverySummary(BaseModel):
    id: str
    event: str
    event_id: str
    trigger: DeliveryTrigger
    repository: str | None
    status: DeliveryStatus
    attempts: int
    created_at: datetime
    finished_at: datetime | None
    next_attempt_at: datetime | None
    status_code: int | None
    latency_ms: float | None
    error: str | None


class Webhook(BaseModel):
    id: str
    name: str
    description: str
    url: str
    events: list[WebhookEventName]
    repositories: list[str]
    enabled: bool
    secret_set: bool
    header_names: list[str]
    allow_private: bool
    timeout_s: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    last_delivery: WebhookDeliverySummary | None


class WebhookList(BaseModel):
    items: list[Webhook]
    events: list[str] = Field(description='Events a webhook can subscribe to')


class WebhookAttempt(BaseModel):
    n: int
    at: datetime
    outcome: Literal['succeeded', 'failed']
    status_code: int | None
    latency_ms: float | None
    error: str | None
    response: str | None = Field(description='The first KiB of the answer')


class WebhookDelivery(WebhookDeliverySummary):
    redelivery_of: str | None
    request_body: str
    request_headers: dict[str, str] = Field(description='Headers sent (custom header values masked)')
    log: list[WebhookAttempt]


class WebhookDeliveryList(BaseModel):
    items: list[WebhookDeliverySummary]
    next_cursor: str | None


class WebhookDeliveryAccepted(BaseModel):
    delivery_id: str


# ---------------------------------------------------------------------------- helpers
def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _summary(doc: Document) -> WebhookDeliverySummary:
    last = doc.get('last') or {}
    status = doc.get('status', 'pending')
    return WebhookDeliverySummary(
        id=str(doc['_id']),
        event=str(doc.get('event', '')),
        event_id=str(doc.get('event_id', '')),
        trigger=doc.get('trigger') or 'event',
        repository=doc.get('repository'),
        status=status,
        attempts=int(doc.get('attempts') or 0),
        created_at=doc['created_at'],
        finished_at=_aware(doc.get('finished_at')),
        next_attempt_at=_aware(doc.get('next_attempt_at')) if status == 'pending' else None,
        status_code=last.get('status_code'),
        latency_ms=last.get('latency_ms'),
        error=last.get('error'),
    )


def _view(doc: Document, last: Document | None) -> Webhook:
    return Webhook(
        id=str(doc['_id']),
        name=doc['name'],
        description=doc.get('description', ''),
        url=doc['url'],
        events=list(doc.get('events') or []),
        repositories=list(doc.get('repositories') or []),
        enabled=bool(doc.get('enabled', True)),
        secret_set=bool(doc.get('secret_enc')),
        header_names=list(doc.get('header_names') or []),
        allow_private=bool(doc.get('allow_private')),
        timeout_s=int(doc.get('timeout_s') or wh.DEFAULT_TIMEOUT_S),
        max_attempts=int(doc.get('max_attempts') or wh.DEFAULT_MAX_ATTEMPTS),
        created_at=doc['created_at'],
        updated_at=doc['updated_at'],
        last_delivery=_summary(last) if last else None,
    )


def _audit_view(doc: Document) -> dict[str, Any]:
    """What the audit trail compares: everything but the encrypted values (``secret`` is reported as redacted)."""
    return {
        'name': doc.get('name'),
        'description': doc.get('description', ''),
        'url': doc.get('url'),
        'events': list(doc.get('events') or []),
        'repositories': list(doc.get('repositories') or []),
        'enabled': bool(doc.get('enabled', True)),
        'secret': doc.get('secret_enc'),
        'header_names': list(doc.get('header_names') or []),
        'headers_enc': doc.get('headers_enc'),
        'allow_private': bool(doc.get('allow_private')),
        'timeout_s': doc.get('timeout_s'),
        'max_attempts': doc.get('max_attempts'),
    }


def _target(doc: Document) -> audit.AuditTarget:
    return audit.target('webhook', str(doc['_id']), doc.get('name'))


def _validate(svc: Services, body: WebhookWrite, *, creating: bool) -> None:
    errors: list[FieldError] = []
    problem = wh.check_url(body.url)
    if problem:
        errors.append(FieldError(field='url', message=problem, code='invalid'))
    else:
        ip = wh.literal_ip(body.url)
        if ip is not None and not svc.upstream.guard.allowed(ip, allow_private=body.allow_private):
            errors.append(
                FieldError(
                    field='url',
                    message='is a private or reserved address: allow private targets to use it',
                    code='private_address',
                )
            )
    if creating and not body.secret:
        errors.append(FieldError(field='secret', message='a signing secret is required', code='required'))
    if (body.secret or body.headers) and svc.keyring is None:
        errors.append(
            FieldError(
                field='secret' if body.secret else 'headers',
                message='WAYSTATION_SECRET_KEYS is not set: secrets cannot be stored',
                code='no_secret_keys',
            )
        )
    if body.headers is not None:
        if len(body.headers) > wh.MAX_HEADERS:
            errors.append(FieldError(field='headers', message=f'at most {wh.MAX_HEADERS} headers', code='too_many'))
        seen: set[str] = set()
        for name, value in body.headers.items():
            reason = wh.check_header(name, value)
            if reason is None and name.lower() in seen:
                reason = 'appears twice'
            seen.add(name.lower())
            if reason:
                errors.append(FieldError(field=f'headers.{name}', message=reason, code='header'))
    if errors:
        raise FieldErrors(errors)


def _fields(svc: Services, body: WebhookWrite, webhook_id: str) -> Document:
    fields: Document = body.model_dump(mode='python', exclude={'secret', 'headers'})
    fields['repositories'] = list(dict.fromkeys(body.repositories))
    fields['events'] = list(dict.fromkeys(body.events))
    if body.secret:
        assert svc.keyring is not None  # checked by _validate
        fields['secret_enc'] = svc.keyring.encrypt_str(body.secret, aad=secret_aad(webhook_id))
    if body.headers is not None:
        fields['header_names'] = sorted(body.headers, key=str.lower)
        if body.headers:
            assert svc.keyring is not None
            fields['headers_enc'] = svc.keyring.encrypt_str(json.dumps(body.headers), aad=headers_aad(webhook_id))
        else:
            fields['headers_enc'] = None
    return fields


async def _webhook_or_404(svc: Services, webhook_id: str) -> Document:
    doc = await webhooks_db.get_webhook(svc.db, webhook_id)
    if doc is None:
        raise NotFound('Not Found')
    return doc


def _encode_cursor(doc: WebhookDeliverySummary) -> str:
    raw = f'{doc.created_at.isoformat()}|{doc.id}'
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip('=')


def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)).decode()
        at, _, last_id = raw.partition('|')
        return datetime.fromisoformat(at), last_id
    except binascii.Error, UnicodeDecodeError, ValueError:
        raise ValidationFailed('invalid cursor') from None


async def _enqueue(svc: Services, delivery_id: str) -> None:
    try:
        await svc.jobs.enqueue(WebhookDeliver(delivery_id=delivery_id))
    except Exception:  # Valkey down: the worker's retry sweep finds the pending delivery anyway
        log.warning('webhook delivery job not queued', extra={'delivery_id': delivery_id})


# ---------------------------------------------------------------------------- definitions
@router.get('/webhooks', response_model=WebhookList, operation_id='listWebhooks')
@access.requires(Action.ADMIN)
async def list_webhooks(request: Request, svc: ServicesDep) -> WebhookList:
    docs = await webhooks_db.list_webhooks(svc.db)
    last = await webhooks_db.last_deliveries(svc.db, [str(d['_id']) for d in docs])
    return WebhookList(items=[_view(d, last.get(str(d['_id']))) for d in docs], events=list(wh.EVENTS))


@router.post(
    '/webhooks',
    status_code=201,
    response_model=Webhook,
    operation_id='createWebhook',
    responses={409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.ADMIN)
async def create_webhook(body: WebhookWrite, request: Request, response: Response, svc: ServicesDep) -> Webhook:
    principal = await authenticate(request)
    _validate(svc, body, creating=True)
    webhook_id = new_id()
    now = datetime.now(UTC)
    doc: Document = {
        '_id': webhook_id,
        'header_names': [],
        'headers_enc': None,
        **_fields(svc, body, webhook_id),
        'created_at': now,
        'created_by': principal.id,
        'updated_at': now,
        'updated_by': principal.id,
    }
    await webhooks_db.insert_webhook(svc.db, doc)
    svc.webhooks.invalidate()
    audit.record(AuditAction.WEBHOOK_CREATE, target=_target(doc), changes=audit.diff(None, _audit_view(doc)))
    response.headers['Location'] = f'/api/v1/webhooks/{webhook_id}'
    return _view(doc, None)


@router.get('/webhooks/{webhook_id}', response_model=Webhook, operation_id='getWebhook')
@access.requires(Action.ADMIN)
async def get_webhook(webhook_id: WebhookId, request: Request, svc: ServicesDep) -> Webhook:
    doc = await _webhook_or_404(svc, webhook_id)
    last = await webhooks_db.last_deliveries(svc.db, [webhook_id])
    return _view(doc, last.get(webhook_id))


@router.put(
    '/webhooks/{webhook_id}',
    response_model=Webhook,
    operation_id='updateWebhook',
    responses={409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.ADMIN)
async def update_webhook(webhook_id: WebhookId, body: WebhookWrite, request: Request, svc: ServicesDep) -> Webhook:
    principal = await authenticate(request)
    before = await _webhook_or_404(svc, webhook_id)
    _validate(svc, body, creating=False)
    fields = {**_fields(svc, body, webhook_id), 'updated_at': datetime.now(UTC), 'updated_by': principal.id}
    doc = await webhooks_db.update_webhook(svc.db, webhook_id, fields)
    if doc is None:
        raise NotFound('Not Found')
    svc.webhooks.invalidate()
    changes = audit.diff(_audit_view(before), _audit_view(doc))
    if changes:
        audit.record(AuditAction.WEBHOOK_UPDATE, target=_target(doc), changes=changes)
    last = await webhooks_db.last_deliveries(svc.db, [webhook_id])
    return _view(doc, last.get(webhook_id))


@router.delete('/webhooks/{webhook_id}', status_code=204, operation_id='deleteWebhook')
@access.requires(Action.ADMIN)
async def delete_webhook(webhook_id: WebhookId, request: Request, svc: ServicesDep) -> Response:
    doc = await _webhook_or_404(svc, webhook_id)
    if not await webhooks_db.delete_webhook(svc.db, webhook_id):
        raise NotFound('Not Found')
    svc.webhooks.invalidate()
    audit.record(AuditAction.WEBHOOK_DELETE, target=_target(doc), changes=audit.diff(_audit_view(doc), None))
    return Response(status_code=204)


# ---------------------------------------------------------------------------- test event and redelivery
def _ping(svc: Services, doc: Document, principal: Principal, event_id: str, now: datetime) -> str:
    document = wh.build_event(
        event_id=event_id,
        event=wh.PING,
        occurred_at=now,
        repository=None,
        actor={'kind': principal.kind.value if not principal.is_anonymous else 'anonymous', 'name': principal.name},
        target={'kind': 'webhook', 'id': str(doc['_id']), 'name': doc['name']},
        details={'message': 'Test event from Waystation', 'webhook': doc['name']},
        source=svc.settings.base_url or None,
    )
    return wh.encode(document).decode()


@router.post(
    '/webhooks/{webhook_id}/test',
    status_code=202,
    response_model=WebhookDeliveryAccepted,
    operation_id='testWebhook',
)
@access.requires(Action.ADMIN)
async def test_webhook(webhook_id: WebhookId, request: Request, svc: ServicesDep) -> WebhookDeliveryAccepted:
    principal = await authenticate(request)
    doc = await _webhook_or_404(svc, webhook_id)
    now = datetime.now(UTC)
    event_id = new_id()
    delivery = webhooks_db.new_delivery(
        delivery_id=new_id(),
        webhook_id=webhook_id,
        event_id=event_id,
        event=wh.PING,
        repository=None,
        body=_ping(svc, doc, principal, event_id, now),
        now=now,
        test=True,
    )
    await webhooks_db.insert_delivery(svc.db, delivery)
    await _enqueue(svc, delivery['_id'])
    audit.record(AuditAction.WEBHOOK_TEST, target=_target(doc), details={'delivery_id': delivery['_id']})
    return WebhookDeliveryAccepted(delivery_id=delivery['_id'])


@router.get(
    '/webhooks/{webhook_id}/deliveries', response_model=WebhookDeliveryList, operation_id='listWebhookDeliveries'
)
@access.requires(Action.ADMIN)
async def list_deliveries(
    webhook_id: WebhookId,
    request: Request,
    svc: ServicesDep,
    status: Annotated[DeliveryStatus | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> WebhookDeliveryList:
    await _webhook_or_404(svc, webhook_id)
    docs = await webhooks_db.list_deliveries(
        svc.db, webhook_id, status=status, before=_decode_cursor(cursor), limit=limit + 1
    )
    items = [_summary(d) for d in docs[:limit]]
    return WebhookDeliveryList(
        items=items, next_cursor=_encode_cursor(items[-1]) if len(docs) > limit and items else None
    )


def _request_headers(doc: Document, webhook: Document) -> dict[str, str]:
    """What a receiver gets, minus what changes per attempt's values (timestamp, signature) and secret values."""
    headers = dict.fromkeys(webhook.get('header_names') or [], '***')
    headers.update(
        {
            'Content-Type': 'application/json',
            'User-Agent': wh.USER_AGENT,
            wh.EVENT_HEADER: str(doc.get('event', '')),
            wh.EVENT_ID_HEADER: str(doc.get('event_id', '')),
            wh.DELIVERY_HEADER: str(doc['_id']),
            wh.IDEMPOTENCY_HEADER: str(doc['_id']),
            wh.ATTEMPT_HEADER: '<attempt>',
            wh.TIMESTAMP_HEADER: '<unix seconds>',
            wh.SIGNATURE_HEADER: 't=<unix seconds>,v1=<HMAC-SHA256 hex>',
        }
    )
    return headers


@router.get(
    '/webhooks/{webhook_id}/deliveries/{delivery_id}', response_model=WebhookDelivery, operation_id='getWebhookDelivery'
)
@access.requires(Action.ADMIN)
async def get_delivery(
    webhook_id: WebhookId, delivery_id: DeliveryId, request: Request, svc: ServicesDep
) -> WebhookDelivery:
    webhook = await _webhook_or_404(svc, webhook_id)
    doc = await webhooks_db.get_delivery(svc.db, webhook_id, delivery_id)
    if doc is None:
        raise NotFound('Not Found')
    attempts = [
        WebhookAttempt(
            n=int(a.get('n') or 0),
            at=a['at'],
            outcome='succeeded' if a.get('outcome') == 'succeeded' else 'failed',
            status_code=a.get('status_code'),
            latency_ms=a.get('latency_ms'),
            error=a.get('error'),
            response=a.get('response'),
        )
        for a in reversed(doc.get('log') or [])
    ]
    return WebhookDelivery(
        **_summary(doc).model_dump(),
        redelivery_of=doc.get('redelivery_of'),
        request_body=str(doc.get('body', '')),
        request_headers=_request_headers(doc, webhook),
        log=attempts,
    )


@router.post(
    '/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver',
    status_code=202,
    response_model=WebhookDeliveryAccepted,
    operation_id='redeliverWebhookDelivery',
)
@access.requires(Action.ADMIN)
async def redeliver(
    webhook_id: WebhookId, delivery_id: DeliveryId, request: Request, svc: ServicesDep
) -> WebhookDeliveryAccepted:
    webhook = await _webhook_or_404(svc, webhook_id)
    original = await webhooks_db.get_delivery(svc.db, webhook_id, delivery_id)
    if original is None:
        raise NotFound('Not Found')
    delivery = webhooks_db.new_delivery(
        delivery_id=new_id(),
        webhook_id=webhook_id,
        event_id=str(original['event_id']),
        event=str(original['event']),
        repository=original.get('repository'),
        body=str(original['body']),
        now=datetime.now(UTC),
        redelivery_of=delivery_id,
        trigger='redeliver',
    )
    await webhooks_db.insert_delivery(svc.db, delivery)
    await _enqueue(svc, delivery['_id'])
    audit.record(
        AuditAction.WEBHOOK_REDELIVER,
        target=_target(webhook),
        details={'delivery_id': delivery['_id'], 'redelivery_of': delivery_id, 'event': original['event']},
    )
    return WebhookDeliveryAccepted(delivery_id=delivery['_id'])
