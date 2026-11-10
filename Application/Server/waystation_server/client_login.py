"""Package-client sign-in (SPECIFICATION §4.9; T-P5-NPMH): ``npm login --auth-type=legacy`` exchanges a username and
password for an API token, which the client then sends as its ``_authToken``; ``npm logout`` revokes it.

The router hands these requests to the format without authorising them (descriptor ``client_login``); the format
calls ``FormatRequest.sign_in`` / ``sign_out``, implemented here:

- **Sign in**: the password goes through ``security.login.verify_login`` (rate limit per address, lockout, LDAP; a
  one-time password or an account with two-factor sign-in is refused, as for ``docker login``). The user must be
  able to read the repository. A repository the caller could not see before signing in answers every failure (bad
  credentials, no access, a malformed request) like a missing repository (the router applies
  ``access.hidden_repo``: ``404``, or the ``401`` challenge for a request without credentials, ADR 0005).
  The token carries the repository scopes the user holds (``read``, ``write``, ``delete``; never more), no
  repository pattern (writes through a group reach its writable member) and no expiry; it is named after the client
  and repository, listed and revocable on the user's profile like any other token, and audited (``token.create``,
  plus ``auth.login`` with ``via`` = the client).
- **Sign out**: only the token the request was authenticated with can be revoked this way (``token.revoke``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from fastapi import Request

from waystation_common.errors import NotFound, Unauthorized, ValidationFailed
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.db import tokens as tokens_db
from waystation_server.repos.model import RepoConfig
from waystation_server.security import access
from waystation_server.security.actions import Action, TokenScope
from waystation_server.security.authn import client_address, principal_for
from waystation_server.security.identity import TokenOwner
from waystation_server.security.login import verify_login
from waystation_server.security.principal import AuthMethod, Principal, PrincipalKind
from waystation_server.services import Services

log = logging.getLogger('waystation.client_login')

MAX_ACTIVE_TOKENS = 100  # as for tokens created in the UI (``api.tokens``)
REPOSITORY_SCOPES = (TokenScope.READ, TokenScope.WRITE, TokenScope.DELETE)
MAX_PASSWORD = 1024


async def sign_in(
    services: Services,
    request: Request,
    repo: RepoConfig,
    caller: Principal,
    *,
    username: str,
    password: str,
    client: str,
) -> str:
    """A new API token's secret for the user these credentials belong to (see the module docstring)."""
    visible = access.can_see(caller, repo)
    try:
        if not username or not password or len(password) > MAX_PASSWORD:
            raise Unauthorized('invalid username or password')
        user = await verify_login(
            services, username=username, password=password, client_ip=client_address(request), via=client
        )
    except Unauthorized:
        if not visible:
            raise NotFound('Not Found') from None
        raise
    principal = await principal_for(services, user, AuthMethod.BASIC)
    if not access.check(principal, Action.READ, repo):
        raise NotFound('Not Found')
    held = set(access.token_scopes_for(principal))
    scopes = [scope.value for scope in REPOSITORY_SCOPES if scope in held]
    kind: Literal['user', 'service_account'] = (
        'service_account' if principal.kind is PrincipalKind.SERVICE_ACCOUNT else 'user'
    )
    owner = TokenOwner(kind=kind, id=principal.id)
    now = datetime.now(UTC)
    if await tokens_db.count_active(services.db, owner, now) >= MAX_ACTIVE_TOKENS:
        raise ValidationFailed(f'at most {MAX_ACTIVE_TOKENS} active API tokens: revoke one on your profile first')
    record, secret = await services.tokens.create(
        owner,
        name=f'{client} login ({repo.name}) {now:%Y-%m-%d %H:%M}',
        scopes=scopes,
        repo_globs=[],
        expires_at=None,
        created_by=principal.name,
    )
    audit.record(
        AuditAction.AUTH_LOGIN,
        actor=principal,
        target=audit.target('user', principal.id, principal.name),
        details={'username': principal.name, 'via': client, 'provider': principal.provider},
    )
    audit.record(
        AuditAction.TOKEN_CREATE,
        actor=principal,
        target=audit.target('api_token', record.id, record.name),
        details={
            'owner': owner.model_dump(),
            'scopes': record.scopes,
            'repositories': record.repo_globs,
            'expires_at': None,
            'prefix': record.prefix,
            'via': client,
        },
    )
    log.info('client signed in', extra={'user_id': principal.id, 'repo': repo.name, 'client': client})
    return secret


async def sign_out(services: Services, repo: RepoConfig, caller: Principal, secret: str) -> bool:
    """Revoke ``secret`` when it is the API token ``caller`` authenticated with; False otherwise."""
    if caller.token is None:
        return False
    target = await services.tokens.resolve(secret)
    if target is None or target.token is None or target.token.token_id != caller.token.token_id:
        return False
    record = await tokens_db.get(services.db, caller.token.token_id)
    await services.tokens.revoke(caller.token.token_id, by=caller.name)
    audit.record(
        AuditAction.TOKEN_REVOKE,
        actor=caller,
        target=audit.target('api_token', caller.token.token_id, record.name if record else None),
        details={
            'owner': record.principal.model_dump() if record else None,
            'prefix': record.prefix if record else None,
        },
    )
    return True
