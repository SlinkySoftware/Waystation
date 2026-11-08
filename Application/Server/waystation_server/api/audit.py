"""``/api/v1/audit`` (SPECIFICATION §4.11): search the audit log and export it (OpenAPI ``AuditRecord``, ``AuditPage``).

Needs ``audit-read`` (``admin`` implies it). Filters: ``from``/``to`` (time range), ``actor`` (name or id), ``action``
(repeatable; exact or ``<category>.*``), ``target`` (name or id), ``outcome``. ``GET /audit`` pages newest first with
an opaque keyset cursor; ``GET /audit/export`` streams the same selection as CSV or NDJSON (at most ``EXPORT_LIMIT``
records, read from MongoDB in batches, so memory stays flat), ending with a truncation marker when the limit cut it.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from waystation_common.audit import Outcome, dumps
from waystation_server.api.deps import ServicesDep
from waystation_server.db import audit as audit_db
from waystation_server.problems import FieldError, FieldErrors
from waystation_server.security import access
from waystation_server.security.access import Action

router = APIRouter(tags=['admin'])

PROBLEM = {'content': {'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}}}
EXPORT_LIMIT = 100_000
MAX_ACTIONS = 20
_ACTION = re.compile(r'^[a-z][a-z_]{0,31}\.(?:\*|[a-z][a-z_]{0,31})$')
CSV_COLUMNS = (
    'ts',
    'action',
    'outcome',
    'reason',
    'actor_kind',
    'actor_name',
    'actor_id',
    'actor_auth',
    'actor_provider',
    'actor_token_id',
    'target_kind',
    'target_name',
    'target_id',
    'client_ip',
    'request_id',
    'details',
    'changes',
    'id',
)


# ---------------------------------------------------------------------------- schemas
class AuditActor(BaseModel):
    kind: Literal['user', 'service_account', 'anonymous', 'system']
    id: str | None
    name: str
    auth: Literal['session', 'api_token', 'basic', 'registry_token', 'none', 'system']
    provider: str | None
    token_id: str | None


class AuditTarget(BaseModel):
    kind: str
    id: str | None
    name: str | None


class AuditChange(BaseModel):
    field: str
    before: Any
    after: Any
    redacted: bool


class AuditRecord(BaseModel):
    id: str
    ts: datetime
    action: str = Field(description='`<category>.<verb>`, e.g. `auth.login`, `repository.update`')
    outcome: Outcome
    actor: AuditActor
    target: AuditTarget | None
    client_ip: str | None
    request_id: str | None
    reason: str | None
    details: dict[str, Any]
    changes: list[AuditChange]


class AuditPage(BaseModel):
    items: list[AuditRecord]
    next_cursor: str | None


# ---------------------------------------------------------------------------- helpers
def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def to_record(doc: dict[str, Any]) -> AuditRecord:
    actor = doc.get('principal') or {}
    target = doc.get('target')
    return AuditRecord(
        id=doc['_id'],
        ts=_aware(doc['ts']),
        action=doc['action'],
        outcome=Outcome(doc.get('outcome', 'success')),
        actor=AuditActor(
            kind=actor.get('kind', 'anonymous'),
            id=actor.get('id'),
            name=actor.get('name', 'anonymous'),
            auth=actor.get('auth', 'none'),
            provider=actor.get('provider'),
            token_id=actor.get('token_id'),
        ),
        target=AuditTarget(kind=target.get('kind', ''), id=target.get('id'), name=target.get('name'))
        if target
        else None,
        client_ip=doc.get('ip'),
        request_id=doc.get('request_id'),
        reason=doc.get('reason'),
        details=doc.get('details') or {},
        changes=[AuditChange(**c) for c in doc.get('changes') or []],
    )


def encode_cursor(ts: datetime, record_id: str) -> str:
    millis = int(_aware(ts).timestamp() * 1000)
    return base64.urlsafe_b64encode(f'{millis}:{record_id}'.encode()).decode().rstrip('=')


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)).decode()
        millis, _, record_id = raw.partition(':')
        if not record_id or len(record_id) > 64:
            raise ValueError(cursor)
        return datetime.fromtimestamp(int(millis) / 1000, UTC), record_id
    except ValueError, binascii.Error, UnicodeDecodeError, OverflowError:
        raise FieldErrors([FieldError(field='cursor', message='invalid cursor', code='invalid')]) from None


def build_query(
    start: datetime | None,
    end: datetime | None,
    actor: str | None,
    actions: list[str] | None,
    target: str | None,
    outcome: Outcome | None,
) -> audit_db.AuditQuery:
    errors: list[FieldError] = []
    wanted = [a.strip() for a in actions or [] if a.strip()]
    if len(wanted) > MAX_ACTIONS:
        errors.append(FieldError(field='action', message=f'at most {MAX_ACTIONS} actions', code='too_many'))
    for i, action in enumerate(wanted):
        if not _ACTION.fullmatch(action):
            errors.append(
                FieldError(field=f'action.{i}', message='an action (`token.create`) or `<category>.*`', code='format')
            )
    if start is not None and end is not None and _aware(end) <= _aware(start):
        errors.append(FieldError(field='to', message='must be after `from`', code='range'))
    if errors:
        raise FieldErrors(errors)
    return audit_db.AuditQuery(
        start=_aware(start) if start else None,
        end=_aware(end) if end else None,
        actor=actor.strip() if actor and actor.strip() else None,
        actions=tuple(dict.fromkeys(wanted)),
        target=target.strip() if target and target.strip() else None,
        outcome=outcome.value if outcome else None,
    )


From = Annotated[datetime | None, Query(alias='from', description='Records at or after this time (RFC 3339)')]
To = Annotated[datetime | None, Query(description='Records before this time (RFC 3339)')]
Actor = Annotated[str | None, Query(max_length=256, description='Who acted, by name or id')]
Actions = Annotated[
    list[str] | None, Query(alias='action', description='An action (`token.create`) or `<category>.*`; repeatable')
]
Target = Annotated[str | None, Query(max_length=256, description='The object acted on, by name or id')]


# ---------------------------------------------------------------------------- routes
@router.get(
    '/audit',
    response_model=AuditPage,
    operation_id='listAudit',
    responses={403: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.AUDIT_READ)
async def list_audit(
    svc: ServicesDep,
    start: From = None,
    to: To = None,
    actor: Actor = None,
    action: Actions = None,
    target: Target = None,
    outcome: Outcome | None = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditPage:
    query = build_query(start, to, actor, action, target, outcome)
    docs = await audit_db.page(svc.db, query, cursor=decode_cursor(cursor) if cursor else None, limit=limit)
    items = [to_record(doc) for doc in docs[:limit]]
    next_cursor = encode_cursor(docs[limit - 1]['ts'], docs[limit - 1]['_id']) if len(docs) > limit else None
    return AuditPage(items=items, next_cursor=next_cursor)


def _cell(value: Any) -> str:
    """A CSV cell that spreadsheets will not evaluate (formula injection)."""
    text = '' if value is None else value if isinstance(value, str) else dumps(value)
    return "'" + text if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


def _csv_row(values: list[Any]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator='\r\n').writerow([_cell(v) for v in values])
    return buffer.getvalue()


def _row(record: AuditRecord) -> list[Any]:
    target = record.target
    return [
        record.ts.isoformat(),
        record.action,
        record.outcome.value,
        record.reason,
        record.actor.kind,
        record.actor.name,
        record.actor.id,
        record.actor.auth,
        record.actor.provider,
        record.actor.token_id,
        target.kind if target else None,
        target.name if target else None,
        target.id if target else None,
        record.client_ip,
        record.request_id,
        record.details or None,
        [c.model_dump(mode='json') for c in record.changes] or None,
        record.id,
    ]


async def _export(svc: ServicesDep, query: audit_db.AuditQuery, fmt: str) -> AsyncIterator[bytes]:
    count = 0
    chunk: list[str] = [_csv_row(list(CSV_COLUMNS))] if fmt == 'csv' else []
    async for doc in audit_db.stream(svc.db, query, limit=EXPORT_LIMIT + 1):
        count += 1
        if count > EXPORT_LIMIT:
            chunk.append(
                _csv_row([f'# truncated at {EXPORT_LIMIT} records']) if fmt == 'csv' else '{"truncated":true}\n'
            )
            break
        record = to_record(doc)
        chunk.append(_csv_row(_row(record)) if fmt == 'csv' else record.model_dump_json() + '\n')
        if len(chunk) >= 200:
            yield ''.join(chunk).encode()
            chunk = []
    if chunk:
        yield ''.join(chunk).encode()


@router.get(
    '/audit/export',
    operation_id='exportAudit',
    response_class=StreamingResponse,
    responses={
        200: {
            'description': 'The export file',
            'content': {
                'text/csv': {'schema': {'type': 'string'}},
                'application/x-ndjson': {'schema': {'type': 'string'}},
            },
        },
        403: PROBLEM,
        422: PROBLEM,
    },
)
@access.requires(Action.AUDIT_READ)
async def export_audit(
    svc: ServicesDep,
    start: From = None,
    to: To = None,
    actor: Actor = None,
    action: Actions = None,
    target: Target = None,
    outcome: Outcome | None = None,
    format: Literal['csv', 'ndjson'] = 'csv',
) -> StreamingResponse:
    query = build_query(start, to, actor, action, target, outcome)
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    media = 'text/csv; charset=utf-8' if format == 'csv' else 'application/x-ndjson'
    return StreamingResponse(
        _export(svc, query, format),
        media_type=media,
        headers={
            'Content-Disposition': f'attachment; filename="waystation-audit-{stamp}.{format}"',
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
        },
    )
