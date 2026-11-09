"""``audit.record(...)``: the one way the server writes audit records (ARCHITECTURE §7, SPECIFICATION §4.11).

``RequestContextMiddleware`` binds every HTTP request to an audit context (its state: request id, client address after
the trusted-proxy rules, the principal once authenticated; and the app's ``AuditWriter``), so a call site anywhere
below a request — a route, ``security.login``, ``security.external`` — is a single line::

    audit.record(AuditAction.REPOSITORY_UPDATE, target=audit.target('repository', repo.id, repo.name),
                 changes=audit.diff(before, after))

The actor defaults to the request's principal; pass ``actor=`` when the request is not (yet) authenticated as the one
acting (sign-in). ``record`` never raises and never blocks (the writer queues; see ``waystation_common.audit``);
outside a request (or without a writer) it is a no-op unless ``writer=`` is given.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from waystation_common.audit import (
    ANONYMOUS,
    ActorKind,
    AuditAction,
    AuditActor,
    AuditChange,
    AuditEvent,
    AuditTarget,
    AuditWriter,
    Outcome,
    diff,
    sanitize,
)
from waystation_server.security.principal import Principal, PrincipalKind
from waystation_server.webhooks import WebhookNotifier

__all__ = ['AuditAction', 'AuditTarget', 'Outcome', 'actor_of', 'bind', 'diff', 'record', 'reset', 'target']

log = logging.getLogger('waystation.audit')

_KINDS = {
    PrincipalKind.USER: ActorKind.USER,
    PrincipalKind.SERVICE_ACCOUNT: ActorKind.SERVICE_ACCOUNT,
    PrincipalKind.ANONYMOUS: ActorKind.ANONYMOUS,
}


@dataclass
class _Context:
    state: Mapping[str, Any]  # the ASGI scope's ``state`` (``request.state``)
    app: Any


_current: ContextVar[_Context | None] = ContextVar('waystation_audit', default=None)


def bind(state: Mapping[str, Any], app: Any) -> Token[_Context | None]:
    """Called by the request middleware for every HTTP request."""
    return _current.set(_Context(state, app))


def reset(token: Token[_Context | None]) -> None:
    _current.reset(token)


def actor_of(principal: Principal) -> AuditActor:
    if principal.is_anonymous:
        return ANONYMOUS.model_copy(update={'auth': principal.method.value})
    return AuditActor(
        kind=_KINDS.get(principal.kind, ActorKind.USER),
        id=principal.id,
        name=principal.name,
        auth=principal.method.value,
        provider=principal.provider,
        token_id=principal.token.token_id if principal.token is not None else None,
    )


def target(kind: str, id: str | None = None, name: str | None = None) -> AuditTarget:
    return AuditTarget(kind=kind, id=id, name=name)


def _writer(ctx: _Context | None) -> AuditWriter | None:
    if ctx is None:
        return None
    services = getattr(getattr(ctx.app, 'state', None), 'services', None)
    writer = getattr(services, 'audit', None)
    return writer if isinstance(writer, AuditWriter) else None


def _notifier(ctx: _Context | None) -> WebhookNotifier | None:
    services = getattr(getattr(getattr(ctx, 'app', None), 'state', None), 'services', None)
    notifier = getattr(services, 'webhooks', None)
    return notifier if isinstance(notifier, WebhookNotifier) else None


def record(
    action: AuditAction,
    *,
    outcome: Outcome = Outcome.SUCCESS,
    target: AuditTarget | None = None,
    actor: Principal | AuditActor | None = None,
    reason: str | None = None,
    details: Mapping[str, Any] | None = None,
    changes: list[AuditChange] | None = None,
    writer: AuditWriter | None = None,
) -> None:
    """Queue one audit record for ``action`` (never raises, never blocks)."""
    try:
        ctx = _current.get()
        sink = writer or _writer(ctx)
        if sink is None:
            log.debug('audit record outside a request', extra={'action': action.value})
            return
        state: Mapping[str, Any] = ctx.state if ctx is not None else {}
        who = actor if actor is not None else state.get('principal')
        if isinstance(who, Principal):
            who = actor_of(who)
        event = AuditEvent(
            action=action.value,
            outcome=outcome,
            actor=who if isinstance(who, AuditActor) else ANONYMOUS,
            target=target,
            ip=state.get('client_ip'),
            request_id=state.get('request_id'),
            reason=reason,
            details=sanitize(details),
            changes=changes or [],
        )
        sink.record(event)
        notifier = _notifier(ctx)
        if notifier is not None:  # content and repository changes are webhook events too (SPEC §4.12)
            notifier.offer(event)
    except Exception:  # never fail the request because of the audit trail
        log.exception('audit record failed', extra={'action': getattr(action, 'value', str(action))})
