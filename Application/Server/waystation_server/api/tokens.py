"""API tokens and service accounts (SPECIFICATION §4.9; OpenAPI ``ApiToken*``, ``ServiceAccount*``).

- ``/auth/me/tokens``: the caller's own tokens. Listing and revoking work with any credential; creating one needs an
  interactive session (a token cannot mint tokens). A token can only carry scopes its owner holds and only narrows
  the owner's grants (``security.access``). The secret is in the creation response only.
- ``/service-accounts`` (``user-admin``): non-human principals holding roles; they authenticate only with API tokens,
  managed under ``/service-accounts/{id}/tokens``. Disabling or deleting an account (deleting revokes its tokens),
  or changing its roles, takes effect on every replica at once (``revocations`` channel).
- ``/tokens`` (``user-admin``): every token, for incident response; revoke any of them.

Tokens of other principals are 404 (never 403).
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from waystation_common.errors import Forbidden, NotFound
from waystation_common.ids import new_id
from waystation_server import audit
from waystation_server.api.deps import ServicesDep
from waystation_server.audit import AuditAction
from waystation_server.db import service_accounts as accounts_db
from waystation_server.db import tokens as tokens_db
from waystation_server.db import users as users_db
from waystation_server.problems import FieldError, FieldErrors
from waystation_server.security import access
from waystation_server.security.access import Action, TokenScope
from waystation_server.security.authn import authenticate
from waystation_server.security.identity import ApiTokenRecord, ServiceAccountRecord, TokenOwner
from waystation_server.security.principal import AuthMethod, Principal, PrincipalKind
from waystation_server.security.tokens import REVOKE_ALL
from waystation_server.services import Services

router = APIRouter(tags=['tokens'])

PROBLEM = {'content': {'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}}}
MAX_ACTIVE_TOKENS = 100
MAX_LIFETIME = timedelta(days=3653)
_ACCOUNT_NAME = re.compile(r'^[a-z0-9][a-z0-9._-]{1,62}$')
TokenId = Annotated[str, Path(min_length=1, max_length=64)]
AccountId = Annotated[str, Path(min_length=1, max_length=64)]


# ---------------------------------------------------------------------------- schemas
class TokenOwnerRef(BaseModel):
    kind: Literal['user', 'service_account']
    id: str
    name: str


class ApiToken(BaseModel):
    id: str
    name: str
    prefix: str = Field(description='The first characters of the token, for recognising it (`wst_` + 8)')
    owner: TokenOwnerRef
    scopes: list[TokenScope]
    repositories: list[str] = Field(description='Repository patterns the token is limited to; empty = all')
    status: Literal['active', 'expired', 'revoked']
    expires_at: datetime | None
    created_at: datetime
    last_used_at: datetime | None
    last_used_ip: str | None
    revoked_at: datetime | None


class ApiTokenList(BaseModel):
    items: list[ApiToken]
    available_scopes: list[TokenScope] = Field(description='Scopes the owner may put on a new token')


class ApiTokenCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1, max_length=64, description='What the token is for, e.g. `CI pipeline`')
    scopes: list[TokenScope] = Field(min_length=1, max_length=len(TokenScope))
    repositories: list[str] = Field(
        default_factory=list,
        max_length=20,
        description='Limit the token to these repositories: names, globs (`docker-*`), `format:<f>`, `type:<t>`',
    )
    expires_at: datetime | None = Field(None, description='null = never expires')

    @field_validator('name')
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('a name is required')
        return value


class ApiTokenCreated(BaseModel):
    token: ApiToken
    secret: str = Field(description='The token. Shown only in this response; store it now')


class ServiceAccount(BaseModel):
    id: str
    name: str
    description: str
    roles: list[str]
    disabled: bool
    active_tokens: int
    created_at: datetime


class ServiceAccountCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=2, max_length=63, description='Lower case letters, digits, `.`, `_`, `-`')
    description: str = Field('', max_length=500)
    roles: list[str] = Field(default_factory=list, max_length=20)


class ServiceAccountUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    description: str | None = Field(None, max_length=500)
    roles: list[str] | None = Field(None, max_length=20)
    disabled: bool | None = None


# ---------------------------------------------------------------------------- helpers
def _status(record: ApiTokenRecord, now: datetime) -> Literal['active', 'expired', 'revoked']:
    if record.revoked_at is not None:
        return 'revoked'
    if record.expires_at is not None and record.expires_at <= now:
        return 'expired'
    return 'active'


async def _bodies(svc: Services, records: list[ApiTokenRecord]) -> list[ApiToken]:
    now = datetime.now(UTC)
    users = await users_db.usernames(svc.db, sorted({r.principal.id for r in records if r.principal.kind == 'user'}))
    accounts = await accounts_db.names(
        svc.db, sorted({r.principal.id for r in records if r.principal.kind == 'service_account'})
    )
    pending = await svc.tokens.pending_use([r.id for r in records if r.revoked_at is None])
    out = []
    for record in records:
        names = users if record.principal.kind == 'user' else accounts
        last_used_at, last_used_ip = record.last_used_at, record.last_used_ip
        seen = pending.get(record.id)
        if seen is not None and (last_used_at is None or seen[0] > _aware(last_used_at)):
            last_used_at, last_used_ip = seen
        out.append(
            ApiToken(
                id=record.id,
                name=record.name,
                prefix=record.prefix,
                owner=TokenOwnerRef(
                    kind=record.principal.kind, id=record.principal.id, name=names.get(record.principal.id, '?')
                ),
                scopes=[TokenScope(s) for s in record.scopes if s in TokenScope],
                repositories=record.repo_globs,
                status=_status(record, now),
                expires_at=record.expires_at,
                created_at=record.created_at,
                last_used_at=last_used_at,
                last_used_ip=last_used_ip,
                revoked_at=record.revoked_at,
            )
        )
    return out


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _owner_of(principal: Principal) -> TokenOwner:
    kind: Literal['user', 'service_account'] = (
        'service_account' if principal.kind is PrincipalKind.SERVICE_ACCOUNT else 'user'
    )
    return TokenOwner(kind=kind, id=principal.id)


def _grants_only(principal: Principal) -> Principal:
    """The owner's own grants (without the narrowing of the credential used for this request)."""
    return dataclasses.replace(principal, token=None, token_access=None)


def _account_principal(account: ServiceAccountRecord) -> Principal:
    return Principal(PrincipalKind.SERVICE_ACCOUNT, account.id, account.name, tuple(account.roles))


async def _create(
    svc: Services, owner_grants: Principal, owner: TokenOwner, body: ApiTokenCreate, created_by: str
) -> ApiTokenCreated:
    errors: list[FieldError] = []
    allowed = set(access.token_scopes_for(owner_grants))
    for i, scope in enumerate(body.scopes):
        if scope not in allowed:
            errors.append(
                FieldError(field=f'scopes.{i}', message=f'the owner does not hold {scope.value!r}', code='scope')
            )
    for i, pattern in enumerate(body.repositories):
        if not access.valid_repository_pattern(pattern):
            errors.append(
                FieldError(
                    field=f'repositories.{i}',
                    message='use a repository name, a glob (docker-*), format:<format> or type:<type>',
                    code='pattern',
                )
            )
    now = datetime.now(UTC)
    if body.expires_at is not None:
        expires = _aware(body.expires_at)
        if expires <= now + timedelta(minutes=1):
            errors.append(FieldError(field='expires_at', message='must be in the future', code='past'))
        elif expires > now + MAX_LIFETIME:
            errors.append(FieldError(field='expires_at', message='at most 10 years ahead', code='too_far'))
    if errors:
        raise FieldErrors(errors)
    if await tokens_db.count_active(svc.db, owner, now) >= MAX_ACTIVE_TOKENS:
        raise FieldErrors(
            [FieldError(field='name', message=f'at most {MAX_ACTIVE_TOKENS} active tokens; revoke one first')]
        )
    record, secret = await svc.tokens.create(
        owner,
        name=body.name,
        scopes=list(dict.fromkeys(s.value for s in body.scopes)),
        repo_globs=list(dict.fromkeys(body.repositories)),
        expires_at=_aware(body.expires_at) if body.expires_at else None,
        created_by=created_by,
    )
    body_out = (await _bodies(svc, [record]))[0]
    audit.record(
        AuditAction.TOKEN_CREATE,
        target=_token_target(record),
        details={
            'owner': body_out.owner.model_dump(),
            'scopes': record.scopes,
            'repositories': record.repo_globs,
            'expires_at': record.expires_at,
            'prefix': record.prefix,
        },
    )
    return ApiTokenCreated(token=body_out, secret=secret)


def _token_target(record: ApiTokenRecord) -> audit.AuditTarget:
    return audit.target('api_token', record.id, record.name)


async def _revoke(svc: Services, record: ApiTokenRecord, by: str) -> None:
    await svc.tokens.revoke(record.id, by=by)
    audit.record(
        AuditAction.TOKEN_REVOKE,
        target=_token_target(record),
        details={'owner': record.principal.model_dump(), 'prefix': record.prefix},
    )


async def _revoke_owned(svc: Services, owner: TokenOwner, token_id: str, by: str) -> None:
    record = await tokens_db.get(svc.db, token_id)
    if record is None or record.principal != owner:
        raise NotFound('token not found')
    await _revoke(svc, record, by)


# ---------------------------------------------------------------------------- own tokens
@router.get('/auth/me/tokens', response_model=ApiTokenList, operation_id='listMyTokens')
@access.authenticated
async def list_my_tokens(request: Request, svc: ServicesDep) -> ApiTokenList:
    principal = await authenticate(request)
    records = await tokens_db.list_for(svc.db, _owner_of(principal))
    return ApiTokenList(
        items=await _bodies(svc, records), available_scopes=access.token_scopes_for(_grants_only(principal))
    )


@router.post(
    '/auth/me/tokens',
    response_model=ApiTokenCreated,
    status_code=201,
    operation_id='createMyToken',
    responses={403: PROBLEM, 422: PROBLEM},
)
@access.authenticated
async def create_my_token(body: ApiTokenCreate, request: Request, svc: ServicesDep) -> ApiTokenCreated:
    principal = await authenticate(request)
    if principal.method is not AuthMethod.SESSION:
        raise Forbidden('sign in to create API tokens')
    return await _create(svc, principal, _owner_of(principal), body, created_by=principal.id)


@router.delete('/auth/me/tokens/{token_id}', status_code=204, operation_id='revokeMyToken', responses={404: PROBLEM})
@access.authenticated
async def revoke_my_token(token_id: TokenId, request: Request, svc: ServicesDep) -> Response:
    principal = await authenticate(request)
    await _revoke_owned(svc, _owner_of(principal), token_id, principal.id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------- all tokens (administrators)
@router.get('/tokens', response_model=list[ApiToken], operation_id='listAllTokens')
@access.requires(Action.USER_ADMIN)
async def list_all_tokens(svc: ServicesDep) -> list[ApiToken]:
    return await _bodies(svc, await tokens_db.list_all(svc.db))


@router.delete('/tokens/{token_id}', status_code=204, operation_id='revokeToken', responses={404: PROBLEM})
@access.requires(Action.USER_ADMIN)
async def revoke_token(token_id: TokenId, request: Request, svc: ServicesDep) -> Response:
    principal = await authenticate(request)
    record = await tokens_db.get(svc.db, token_id)
    if record is None:
        raise NotFound('token not found')
    await _revoke(svc, record, principal.id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------- service accounts
def _check_roles(caller: Principal, roles: list[str], field: str) -> list[str]:
    errors = []
    for i, role in enumerate(roles):
        if not access.is_role(role):
            errors.append(FieldError(field=f'{field}.{i}', message=f'unknown role {role!r}', code='role'))
        elif not access.may_assign_role(caller, role):
            errors.append(FieldError(field=f'{field}.{i}', message=f'you cannot grant {role!r}', code='role'))
    if errors:
        raise FieldErrors(errors)
    return list(dict.fromkeys(roles))


async def _account_body(svc: Services, account: ServiceAccountRecord) -> ServiceAccount:
    owner = TokenOwner(kind='service_account', id=account.id)
    return ServiceAccount(
        id=account.id,
        name=account.name,
        description=account.description,
        roles=account.roles,
        disabled=account.disabled,
        active_tokens=await tokens_db.count_active(svc.db, owner, datetime.now(UTC)),
        created_at=account.created_at,
    )


def _account_target(account: ServiceAccountRecord) -> audit.AuditTarget:
    return audit.target('service_account', account.id, account.name)


async def _account(svc: Services, account_id: str) -> ServiceAccountRecord:
    account = await accounts_db.get(svc.db, account_id)
    if account is None:
        raise NotFound('service account not found')
    return account


@router.get('/service-accounts', response_model=list[ServiceAccount], operation_id='listServiceAccounts')
@access.requires(Action.USER_ADMIN)
async def list_service_accounts(svc: ServicesDep) -> list[ServiceAccount]:
    return [await _account_body(svc, account) for account in await accounts_db.list_all(svc.db)]


@router.post(
    '/service-accounts',
    response_model=ServiceAccount,
    status_code=201,
    operation_id='createServiceAccount',
    responses={409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def create_service_account(body: ServiceAccountCreate, request: Request, svc: ServicesDep) -> ServiceAccount:
    principal = await authenticate(request)
    name = body.name.strip().lower()
    if not _ACCOUNT_NAME.fullmatch(name):
        raise FieldErrors([FieldError(field='name', message='lower case letters, digits, ".", "_" and "-"')])
    record = ServiceAccountRecord(
        id=new_id(),
        name=name,
        description=body.description.strip(),
        roles=_check_roles(principal, body.roles, 'roles'),
        owner_user_id=principal.id if principal.kind is PrincipalKind.USER else None,
        created_at=datetime.now(UTC),
    )
    await accounts_db.insert(svc.db, record)
    audit.record(AuditAction.SERVICE_ACCOUNT_CREATE, target=_account_target(record), changes=audit.diff(None, record))
    return await _account_body(svc, record)


@router.patch(
    '/service-accounts/{account_id}',
    response_model=ServiceAccount,
    operation_id='updateServiceAccount',
    responses={404: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def update_service_account(
    account_id: AccountId, body: ServiceAccountUpdate, request: Request, svc: ServicesDep
) -> ServiceAccount:
    principal = await authenticate(request)
    before = await _account(svc, account_id)
    changes: dict[str, object] = {'updated_at': datetime.now(UTC)}
    if body.description is not None:
        changes['description'] = body.description.strip()
    if body.roles is not None:
        changes['roles'] = _check_roles(principal, body.roles, 'roles')
    if body.disabled is not None:
        changes['disabled'] = body.disabled
    updated = await accounts_db.update(svc.db, account_id, changes)
    if updated is None:
        raise NotFound('service account not found')
    await svc.tokens.announce(REVOKE_ALL)  # cached token principals carry the account's roles and status
    audit.record(
        AuditAction.SERVICE_ACCOUNT_UPDATE, target=_account_target(updated), changes=audit.diff(before, updated)
    )
    return await _account_body(svc, updated)


@router.delete(
    '/service-accounts/{account_id}', status_code=204, operation_id='deleteServiceAccount', responses={404: PROBLEM}
)
@access.requires(Action.USER_ADMIN)
async def delete_service_account(account_id: AccountId, request: Request, svc: ServicesDep) -> Response:
    principal = await authenticate(request)
    account = await _account(svc, account_id)
    revoked = await svc.tokens.revoke_all(TokenOwner(kind='service_account', id=account.id), by=principal.id)
    await accounts_db.delete(svc.db, account.id)
    audit.record(
        AuditAction.SERVICE_ACCOUNT_DELETE,
        target=_account_target(account),
        details={'tokens_revoked': revoked},
        changes=audit.diff(account, None),
    )
    return Response(status_code=204)


@router.get(
    '/service-accounts/{account_id}/tokens',
    response_model=ApiTokenList,
    operation_id='listServiceAccountTokens',
    responses={404: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def list_service_account_tokens(account_id: AccountId, svc: ServicesDep) -> ApiTokenList:
    account = await _account(svc, account_id)
    records = await tokens_db.list_for(svc.db, TokenOwner(kind='service_account', id=account.id))
    return ApiTokenList(
        items=await _bodies(svc, records), available_scopes=access.token_scopes_for(_account_principal(account))
    )


@router.post(
    '/service-accounts/{account_id}/tokens',
    response_model=ApiTokenCreated,
    status_code=201,
    operation_id='createServiceAccountToken',
    responses={404: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def create_service_account_token(
    account_id: AccountId, body: ApiTokenCreate, request: Request, svc: ServicesDep
) -> ApiTokenCreated:
    principal = await authenticate(request)
    account = await _account(svc, account_id)
    owner = TokenOwner(kind='service_account', id=account.id)
    return await _create(svc, _account_principal(account), owner, body, created_by=principal.id)


@router.delete(
    '/service-accounts/{account_id}/tokens/{token_id}',
    status_code=204,
    operation_id='revokeServiceAccountToken',
    responses={404: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def revoke_service_account_token(
    account_id: AccountId, token_id: TokenId, request: Request, svc: ServicesDep
) -> Response:
    principal = await authenticate(request)
    await _revoke_owned(svc, TokenOwner(kind='service_account', id=account_id), token_id, principal.id)
    return Response(status_code=204)
