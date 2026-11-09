"""The audit log (SPECIFICATION §4.11, ARCHITECTURE §7, DATA_MODEL ``audit``): event schema, catalog, redaction and
the non-blocking writer shared by the server and the workers.

Catalog
-------
``AuditAction`` names every action as ``<category>.<verb>``. ``SPEC_ACTIONS`` maps each phrase of SPEC §4.11 to the
actions that cover it; ``PENDING`` lists the actions whose feature does not exist yet, with the task that adds it
(the acceptance test enumerates the catalog and fails when an emitted action is not exercised or a pending one has
no owner).

Records
-------
``AuditEvent``: who (``AuditActor``: principal kind, id, name, how it authenticated, its sign-in provider, the API
token used), what (``action``, ``outcome``, ``reason`` for failures), on what (``AuditTarget``), from where (client
address after the trusted-proxy rules, request id), plus ``details`` (plain facts) and ``changes`` (field-level
before/after). **Secret values never reach a record**: ``sanitize`` replaces anything under a secret-looking key and
``diff`` reports a changed secret as ``redacted`` without its values. Every record carries ``expires_at`` = ``ts`` +
``WAYSTATION_AUDIT_RETENTION_DAYS``; MongoDB's TTL index removes it then.

Writer
------
``AuditWriter.record(event)`` never blocks and never raises: it appends to a bounded in-memory queue that one
background task drains into MongoDB with batched ``insert_many``. A failed batch is retried with backoff; when the
queue is full (MongoDB down or far too slow) the record is dropped, counted (``dropped``) and logged at ERROR (the
first drop at once, then at most every 10 s with the running total). ``aclose()`` drains what is queued.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from waystation_common.ids import new_id

log = logging.getLogger('waystation.audit')


# ---------------------------------------------------------------------------- catalog
class AuditAction(StrEnum):
    AUTH_LOGIN = 'auth.login'  # success (UI; docker login at most hourly per user and address) or failure
    AUTH_LOGOUT = 'auth.logout'
    AUTH_LOCKOUT = 'auth.lockout'
    AUTH_PASSWORD_CHANGE = 'auth.password_change'  # noqa: S105 - an action name
    SESSION_REVOKE = 'session.revoke'
    TOKEN_CREATE = 'token.create'  # noqa: S105 - an action name
    TOKEN_REVOKE = 'token.revoke'  # noqa: S105 - an action name
    TOKEN_USE_REFUSED = 'token.use_refused'  # noqa: S105 - an action name
    SERVICE_ACCOUNT_CREATE = 'service_account.create'
    SERVICE_ACCOUNT_UPDATE = 'service_account.update'
    SERVICE_ACCOUNT_DELETE = 'service_account.delete'
    USER_CREATE = 'user.create'
    USER_LINK = 'user.link'
    USER_GROUPS_SYNC = 'user.groups_sync'
    USER_UPDATE = 'user.update'
    USER_DELETE = 'user.delete'
    GROUP_CREATE = 'group.create'
    GROUP_UPDATE = 'group.update'
    GROUP_DELETE = 'group.delete'
    ROLE_CREATE = 'role.create'
    ROLE_UPDATE = 'role.update'
    ROLE_DELETE = 'role.delete'
    AUTH_PROVIDER_CREATE = 'auth_provider.create'
    AUTH_PROVIDER_UPDATE = 'auth_provider.update'
    AUTH_PROVIDER_DELETE = 'auth_provider.delete'
    AUTH_PROVIDER_TEST = 'auth_provider.test'
    REPOSITORY_CREATE = 'repository.create'
    REPOSITORY_UPDATE = 'repository.update'
    REPOSITORY_DELETE = 'repository.delete'
    REPOSITORY_TEST_UPSTREAM = 'repository.test_upstream'
    CONTENT_UPLOAD = 'content.upload'
    CONTENT_DELETE = 'content.delete'
    CLEANUP_RUN = 'cleanup.run'
    TASK_UPDATE = 'task.update'  # a scheduled task's schedule or enabled flag (Tasks page)
    TASK_RUN = 'task.run'  # a scheduled task run now
    TASK_RETRY = 'task.retry'  # a dead-lettered job queued again
    TASK_DISCARD = 'task.discard'  # a dead-lettered job dropped
    SETTINGS_UPDATE = 'settings.update'

    @property
    def category(self) -> str:
        return self.value.partition('.')[0]


A = AuditAction

# SPECIFICATION §4.11, phrase by phrase → the actions that record it.
SPEC_ACTIONS: dict[str, tuple[AuditAction, ...]] = {
    'sign-in': (A.AUTH_LOGIN,),
    'sign-out': (A.AUTH_LOGOUT, A.SESSION_REVOKE),
    'sign-in failure': (A.AUTH_LOGIN, A.AUTH_LOCKOUT),
    'token create': (A.TOKEN_CREATE,),
    'token revoke': (A.TOKEN_REVOKE,),
    'token use-refused': (A.TOKEN_USE_REFUSED,),
    'user changes': (
        A.USER_CREATE,
        A.USER_LINK,
        A.USER_GROUPS_SYNC,
        A.USER_UPDATE,
        A.USER_DELETE,
        A.AUTH_PASSWORD_CHANGE,
        A.SERVICE_ACCOUNT_CREATE,
        A.SERVICE_ACCOUNT_UPDATE,
        A.SERVICE_ACCOUNT_DELETE,
    ),
    'group changes': (A.GROUP_CREATE, A.GROUP_UPDATE, A.GROUP_DELETE),
    'role changes': (A.ROLE_CREATE, A.ROLE_UPDATE, A.ROLE_DELETE),
    'provider changes': (A.AUTH_PROVIDER_CREATE, A.AUTH_PROVIDER_UPDATE, A.AUTH_PROVIDER_DELETE, A.AUTH_PROVIDER_TEST),
    'repo create': (A.REPOSITORY_CREATE,),
    'repo update': (A.REPOSITORY_UPDATE, A.REPOSITORY_TEST_UPSTREAM),
    'repo delete': (A.REPOSITORY_DELETE,),
    'uploads on hosted repos': (A.CONTENT_UPLOAD,),
    'deletes on hosted repos': (A.CONTENT_DELETE,),
    'cleanup runs': (A.CLEANUP_RUN,),
    'task changes': (A.TASK_UPDATE, A.TASK_RUN, A.TASK_RETRY, A.TASK_DISCARD),
    'settings changes': (A.SETTINGS_UPDATE,),
}

# Actions whose feature does not exist yet → the task that adds the feature (and emits the action).
PENDING: dict[AuditAction, str] = {
    A.CLEANUP_RUN: 'T-O01',
}


class Outcome(StrEnum):
    SUCCESS = 'success'
    FAILURE = 'failure'


class ActorKind(StrEnum):
    USER = 'user'
    SERVICE_ACCOUNT = 'service_account'
    ANONYMOUS = 'anonymous'
    SYSTEM = 'system'


# ---------------------------------------------------------------------------- records
class _Model(BaseModel):
    model_config = ConfigDict(extra='ignore', frozen=True)


class AuditActor(_Model):
    kind: ActorKind
    id: str | None = None
    name: str
    auth: str = 'none'  # session, api_token, basic, registry_token, none, system
    provider: str | None = None  # local, ldap:<id>, oidc:<id>
    token_id: str | None = None


SYSTEM = AuditActor(kind=ActorKind.SYSTEM, id=None, name='system', auth='system')
ANONYMOUS = AuditActor(kind=ActorKind.ANONYMOUS, id=None, name='anonymous', auth='none')


class AuditTarget(_Model):
    kind: str
    id: str | None = None
    name: str | None = None


class AuditChange(_Model):
    field: str
    before: Any = None
    after: Any = None
    redacted: bool = False


class AuditEvent(_Model):
    id: str = Field(default_factory=new_id)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    action: str
    outcome: Outcome = Outcome.SUCCESS
    actor: AuditActor
    target: AuditTarget | None = None
    ip: str | None = None
    request_id: str | None = None
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    changes: list[AuditChange] = Field(default_factory=list)

    def to_doc(self, retention_days: int) -> dict[str, Any]:
        """The stored document (DATA_MODEL ``audit``: the actor is stored as ``principal``)."""
        return {
            '_id': self.id,
            'ts': self.ts,
            'action': self.action,
            'outcome': self.outcome.value,
            'principal': self.actor.model_dump(mode='json'),
            'target': self.target.model_dump(mode='json') if self.target else None,
            'ip': self.ip,
            'request_id': self.request_id,
            'reason': self.reason,
            'details': self.details,
            'changes': [c.model_dump(mode='json') for c in self.changes],
            'expires_at': self.ts + timedelta(days=retention_days),
        }


# ---------------------------------------------------------------------------- redaction and diffs
REDACTED = '***'
MAX_DEPTH = 6
MAX_ITEMS = 100
MAX_STRING = 1000
MAX_CHANGES = 200
# Keys whose values are secrets (or secret-derived): never recorded. ``*_id``/``*_set``/``*_count`` are not secrets.
_SECRET_KEY = re.compile(
    r'(passw|secret|token$|_enc$|hash|private|credential|api_?key|csrf|cookie|authorization)', re.I
)
_NOT_SECRET = re.compile(r'(_id|_ids|_set|_count|_prefix|^prefix)$', re.I)
# Credentials inside a URL (``https://user:pass@host/``).
_USERINFO = re.compile(r'\b([a-z][a-z0-9+.-]*://)[^/@\s]+@', re.I)
# Bookkeeping fields that change on every save and say nothing about what changed.
IGNORED_FIELDS = frozenset({'_id', 'id', 'created_at', 'created_by', 'updated_at', 'updated_by', 'config_version'})


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key)) and not _NOT_SECRET.search(key)


def _plain(value: Any, depth: int) -> Any:
    """A bounded, JSON-safe copy of ``value`` with secrets under secret-looking keys replaced."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, str):
        value = _USERINFO.sub(r'\1***@', value)
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + '…'
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='json')
    if depth >= MAX_DEPTH:
        return '…'
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for i, (key, item) in enumerate(value.items()):
            if i >= MAX_ITEMS:
                out['…'] = f'{len(value) - MAX_ITEMS} more'
                break
            name = str(key)
            out[name] = (
                (REDACTED if item not in (None, '') else None) if is_secret_key(name) else _plain(item, depth + 1)
            )
        return out
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        out_list = [_plain(item, depth + 1) for item in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            out_list.append(f'… {len(items) - MAX_ITEMS} more')
        return out_list
    return _plain(str(value), depth)


def sanitize(details: Mapping[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    result = _plain(dict(details), 0)
    assert isinstance(result, dict)
    return result


def _flatten(value: Any, prefix: str, out: dict[str, Any], secret: set[str]) -> None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='python')
    if isinstance(value, Mapping) and (value or not prefix):
        for key, item in value.items():
            name = str(key)
            path = f'{prefix}.{name}' if prefix else name
            if not prefix and name in IGNORED_FIELDS:
                continue
            if is_secret_key(name):
                secret.add(path)
                out[path] = item not in (None, '')  # only whether it is set, compared by identity of value below
                out[path + '\0value'] = item
                continue
            _flatten(item, path, out, secret)
        return
    out[prefix] = value


def diff(
    before: Mapping[str, Any] | BaseModel | None, after: Mapping[str, Any] | BaseModel | None
) -> list[AuditChange]:
    """Field-level changes between two documents (dotted paths; lists compare as a whole). A changed secret is
    reported as ``redacted`` with ``***`` for a value that is set and null for one that is not."""
    old: dict[str, Any] = {}
    new: dict[str, Any] = {}
    secrets: set[str] = set()
    _flatten(before or {}, '', old, secrets)
    _flatten(after or {}, '', new, secrets)
    changes: list[AuditChange] = []
    for path in sorted(k for k in set(old) | set(new) if '\0' not in k):
        if path in secrets:
            was, now = old.get(path + '\0value'), new.get(path + '\0value')
            if was != now:
                changes.append(
                    AuditChange(
                        field=path,
                        before=REDACTED if old.get(path) else None,
                        after=REDACTED if new.get(path) else None,
                        redacted=True,
                    )
                )
            continue
        was, now = _plain(old.get(path), 1), _plain(new.get(path), 1)
        if was != now:
            changes.append(AuditChange(field=path, before=was, after=now))
        if len(changes) >= MAX_CHANGES:
            break
    return changes


# ---------------------------------------------------------------------------- writer
Insert = Callable[[list[dict[str, Any]]], Awaitable[None]]


class AuditWriter:
    """Bounded queue + one task writing batches (see the module docstring)."""

    def __init__(
        self,
        insert: Insert,
        *,
        retention_days: int = 400,
        max_queue: int = 10_000,
        batch_size: int = 500,
        retries: tuple[float, ...] = (0.5, 2.0, 5.0),
    ) -> None:
        self._insert = insert
        self.retention_days = retention_days
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self._batch_size = batch_size
        self._retries = retries
        self._task: asyncio.Task[None] | None = None
        self.written = 0
        self.dropped = 0
        self._reported_drops = 0
        self._last_drop_log = 0.0

    def record(self, event: AuditEvent) -> None:
        """Queue ``event``; never blocks, never raises."""
        try:
            self._queue.put_nowait(event.to_doc(self.retention_days))
        except asyncio.QueueFull:
            self._drop(1, 'queue full')
        except Exception:  # pragma: no cover - a bug in the event; never fail the caller
            log.exception('audit record could not be queued', extra={'action': event.action})
            self._drop(1, 'invalid record')

    def _drop(self, count: int, why: str) -> None:
        self.dropped += count
        now = time.monotonic()
        if self._reported_drops == 0 or now - self._last_drop_log >= 10.0:
            log.error(
                'AUDIT RECORDS DROPPED',
                extra={'reason': why, 'dropped_total': self.dropped, 'queued': self._queue.qsize()},
            )
            self._reported_drops = self.dropped
            self._last_drop_log = now

    @property
    def queued(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name='audit-writer')

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            batch = [first]
            while len(batch) < self._batch_size and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await self._write(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _write(self, batch: list[dict[str, Any]]) -> None:
        for attempt, pause in enumerate((*self._retries, None)):
            try:
                await self._insert(batch)
                self.written += len(batch)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if pause is None:
                    log.error('audit batch lost', extra={'records': len(batch), 'error': type(exc).__name__})
                    self._drop(len(batch), 'write failed')
                    return
                log.warning(
                    'audit write failed; retrying',
                    extra={'records': len(batch), 'attempt': attempt + 1, 'error': type(exc).__name__},
                )
                await asyncio.sleep(pause)

    async def flush(self, wait_s: float = 10.0) -> None:
        """Wait until everything queued so far is written (or given up), at most ``wait_s``."""
        if self._task is None:
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(wait_s):
                await self._queue.join()

    async def aclose(self, wait_s: float = 5.0) -> None:
        await self.flush(wait_s)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if not self._queue.empty():
            self._drop(self._queue.qsize(), 'shutdown')


def dumps(value: Any) -> str:
    """Compact JSON for exports."""
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False, default=str)
