"""``/api/v1/users``: user administration (SPECIFICATION §4.9, §4.10, §4.12); needs ``user-admin``.

- List (filters: username prefix, source, disabled, local group, direct role; keyset paging by username), get (source,
  linked identities, local and provider groups, effective roles), create a local user (with or without a password:
  a password-less local account is what an LDAP/OIDC sign-in of the same username links to), update (display name,
  email, disabled, roles, local groups), delete (its sessions end and its API tokens are revoked), reset a local
  password (``must_change_password`` by default; every session of the user ends), unlock (clears a lockout), and the
  effective permissions.
- Delegation (``api.rbac``): roles and groups the caller adds must be ones it may give away, and a user holding more
  than the caller (an administrator, for a ``user-admin``) cannot be changed by it (403).
- Nobody can disable or delete their own account, and the last enabled administrator (direct role or local group)
  cannot be disabled, deleted or demoted (409).
- Role, group and status changes bump ``grant_version`` and drop cached API-token principals on every replica.
- Every change is audited (``user.create|update|delete``, ``auth.password_change``); password values never are.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from waystation_common.errors import Conflict, NotFound, ValidationFailed
from waystation_common.ids import new_id
from waystation_server import audit
from waystation_server.api.deps import ServicesDep
from waystation_server.api.rbac import (
    PROBLEM,
    EffectivePermissions,
    check_roles,
    effective_permissions,
    require_manageable,
    user_grants,
)
from waystation_server.audit import AuditAction
from waystation_server.db import groups as groups_db
from waystation_server.db import sessions as sessions_db
from waystation_server.db import users as users_db
from waystation_server.problems import FieldError, FieldErrors
from waystation_server.security import access, passwords
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate, principal_for
from waystation_server.security.groups import role_sources
from waystation_server.security.identity import TokenOwner, UserRecord
from waystation_server.security.principal import AuthMethod, Principal
from waystation_server.security.tokens import REVOKE_ALL
from waystation_server.services import Services

router = APIRouter(tags=['admin'])

UserId = Annotated[str, Path(min_length=1, max_length=64)]
_EMAIL = re.compile(r'^[^@\s]{1,128}@[^@\s]{1,127}$')


def _email(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not _EMAIL.fullmatch(value):
        raise ValueError('an e-mail address (name@example.com)')
    return value


Email = Annotated[str | None, AfterValidator(_email)]
_USERNAME = re.compile(r'^[a-z0-9][a-z0-9._@-]{0,127}$')
MAX_GROUPS = 100
AUDIT_FIELDS = {'username', 'display_name', 'email', 'source', 'disabled', 'roles', 'groups'}


# ---------------------------------------------------------------------------- schemas
class GroupRef(BaseModel):
    id: str
    name: str
    source: str = Field(description='`local`, `ldap` or `oidc` (mapped from a provider)')


class ExternalGroupBody(BaseModel):
    provider: str = Field(description='Provider source, `ldap:<id>` or `oidc:<id>`')
    id: str
    name: str


class LinkedIdentityBody(BaseModel):
    provider: str
    subject: str
    linked_at: datetime
    last_login: datetime | None


class UserSummary(BaseModel):
    id: str
    username: str
    display_name: str
    email: str | None
    source: str = Field(description='`local`, or the provider that created or linked the user (`ldap:<id>`)')
    disabled: bool
    locked: bool = Field(description='Locked out after failed sign-ins (local accounts)')
    totp_enabled: bool = Field(description='Two-factor sign-in (TOTP) is on (local accounts)')
    roles: list[str] = Field(description='Roles held directly')
    groups: list[str] = Field(description='Local group ids')
    last_login: datetime | None
    created_at: datetime


class UserPage(BaseModel):
    items: list[UserSummary]
    next_cursor: str | None


class UserDetail(UserSummary):
    locked_until: datetime | None
    must_change_password: bool
    has_password: bool = Field(description='A local password is set (local accounts)')
    group_refs: list[GroupRef] = Field(description='Local groups and mapped provider groups the user is in')
    external_groups: list[ExternalGroupBody] = Field(description='Groups providers reported at the last sign-in')
    identities: list[LinkedIdentityBody]
    effective_roles: list[str] = Field(description='Own roles plus those of its groups')


class UserCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    username: str = Field(min_length=1, max_length=128, description='Lower case; letters, digits, `.`, `_`, `@`, `-`')
    display_name: str = Field('', max_length=256)
    email: Email = Field(None, max_length=256)
    password: str | None = Field(
        None,
        max_length=passwords.MAX_LENGTH,
        json_schema_extra={'writeOnly': True},
        description='Omit for an account that signs in through LDAP/OIDC (linked by username)',
    )
    must_change_password: bool = Field(True, description='The user must change the password at the next sign-in')
    roles: list[str] = Field(default_factory=list, max_length=20)
    groups: list[str] = Field(default_factory=list, max_length=MAX_GROUPS, description='Local group ids')


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    display_name: str | None = Field(None, min_length=1, max_length=256)
    email: Email = Field(None, max_length=256, description='null clears it')
    disabled: bool | None = None
    roles: list[str] | None = Field(None, max_length=20)
    groups: list[str] | None = Field(None, max_length=MAX_GROUPS, description='Local group ids')


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra='forbid')

    password: str = Field(min_length=1, max_length=passwords.MAX_LENGTH, json_schema_extra={'writeOnly': True})
    must_change_password: bool = True


# ---------------------------------------------------------------------------- helpers
def _locked(user: UserRecord, now: datetime) -> bool:
    until = user.locked_until
    if until is None:
        return False
    return (until if until.tzinfo else until.replace(tzinfo=UTC)) > now


def user_summary(user: UserRecord, now: datetime) -> UserSummary:
    return UserSummary(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        source=user.source,
        disabled=user.disabled,
        locked=_locked(user, now),
        totp_enabled=bool(user.totp_enc),
        roles=user.roles,
        groups=user.groups,
        last_login=user.last_login,
        created_at=user.created_at,
    )


async def _detail(svc: Services, user: UserRecord) -> UserDetail:
    now = datetime.now(UTC)
    roles = await svc.rbac.roles_for(user)
    all_groups = await groups_db.list_all(svc.db)
    member_of = {via for _, via in role_sources(user, all_groups) if via != 'direct'}
    refs = [
        GroupRef(id=g.id, name=g.name, source=g.source)
        for g in all_groups
        if g.id in user.groups or g.name in member_of
    ]
    return UserDetail(
        **user_summary(user, now).model_dump(),
        locked_until=user.locked_until if _locked(user, now) else None,
        must_change_password=user.must_change_password,
        has_password=bool(user.password_hash),
        group_refs=refs,
        external_groups=[ExternalGroupBody(**g.model_dump()) for g in user.external_groups],
        identities=[LinkedIdentityBody(**i.model_dump()) for i in user.identities],
        effective_roles=list(roles),
    )


def _target(user: UserRecord) -> audit.AuditTarget:
    return audit.target('user', user.id, user.username)


def _audited(user: UserRecord) -> dict[str, Any]:
    return user.model_dump(include=AUDIT_FIELDS)


async def _user(svc: Services, user_id: str) -> UserRecord:
    user = await users_db.get(svc.db, user_id)
    if user is None:
        raise NotFound('user not found')
    return user


async def _manageable(svc: Services, caller: Principal, user: UserRecord) -> None:
    _, grants = await user_grants(svc, user)
    require_manageable(caller, grants, 'a user')


async def _check_groups(svc: Services, caller: Principal, ids: list[str], keep: list[str]) -> list[str]:
    """``ids`` are local groups; groups newly added must only hold roles the caller may give away."""
    ids = list(dict.fromkeys(ids))
    found = {g.id: g for g in await groups_db.get_many(svc.db, ids)}
    custom = await svc.rbac.table()
    errors: list[FieldError] = []
    for i, gid in enumerate(ids):
        group = found.get(gid)
        if group is None or group.source != 'local':
            errors.append(FieldError(field=f'groups.{i}', message='not a local group', code='group'))
        elif gid not in keep and not access.may_assign(caller, access.materialise(group.roles, custom)):
            errors.append(FieldError(field=f'groups.{i}', message=f'you cannot grant {group.name!r}', code='role'))
    if errors:
        raise FieldErrors(errors)
    return ids


async def is_admin(svc: Services, user: UserRecord) -> bool:
    _, grants = await user_grants(svc, user)
    return grants.is_admin


async def ensure_admin_remains(svc: Services, change: Callable[[UserRecord], UserRecord | None]) -> None:
    """Refuse a change that leaves no enabled administrator. ``change`` maps a user to what it would be afterwards
    (None: deleted). Administrators by direct role or local group are counted (mapped provider groups are not: a
    directory outage must not lock everyone out)."""
    custom = await svc.rbac.table()
    admin_roles = ['admin', *(rid for rid in custom if access.materialise((rid,), custom).is_admin)]
    groups = [g.id for g in await groups_db.list_all(svc.db) if g.source == 'local' and set(g.roles) & set(admin_roles)]
    candidates = {
        u.id: u for u in [*await users_db.with_roles(svc.db, admin_roles), *await users_db.in_groups(svc.db, groups)]
    }
    admins = [u for u in candidates.values() if await is_admin(svc, u)]
    if not admins:
        return  # no local administrator to begin with (provider-mapped only): nothing this change can take away
    for user in admins:
        after = change(user)
        if after is not None and not after.disabled and await is_admin(svc, after):
            return
    raise Conflict('this would leave no enabled administrator')


def _without_user(user_id: str) -> Callable[[UserRecord], UserRecord | None]:
    return lambda u: None if u.id == user_id else u


async def _rbac_changed(svc: Services) -> None:
    await svc.rbac.changed()
    await svc.tokens.announce(REVOKE_ALL)  # cached API-token principals carry the owner's grants and status


def encode_cursor(username: str) -> str:
    return base64.urlsafe_b64encode(username.encode()).decode().rstrip('=')


def decode_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    try:
        return base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True).decode()
    except binascii.Error, UnicodeDecodeError:
        raise ValidationFailed('invalid cursor') from None


# ---------------------------------------------------------------------------- routes
@router.get('/users', response_model=UserPage, operation_id='listUsers')
@access.requires(Action.USER_ADMIN)
async def list_users(
    svc: ServicesDep,
    q: Annotated[str | None, Query(max_length=128, description='Username prefix')] = None,
    source: Annotated[str | None, Query(max_length=80, description='`local`, `ldap`, `oidc` or `ldap:<id>`')] = None,
    disabled: bool | None = None,
    group: Annotated[str | None, Query(max_length=64, description='Local group id')] = None,
    role: Annotated[str | None, Query(max_length=64, description='Role held directly')] = None,
    cursor: Annotated[str | None, Query(max_length=400)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> UserPage:
    users = await users_db.list_page(
        svc.db,
        prefix=q.strip() if q else None,
        source=source,
        disabled=disabled,
        group_id=group,
        role=role,
        after=decode_cursor(cursor),
        limit=limit + 1,
    )
    now = datetime.now(UTC)
    page = users[:limit]
    return UserPage(
        items=[user_summary(u, now) for u in page],
        next_cursor=encode_cursor(page[-1].username) if len(users) > limit else None,
    )


@router.post(
    '/users',
    response_model=UserDetail,
    status_code=201,
    operation_id='createUser',
    responses={403: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def create_user(body: UserCreate, request: Request, response: Response, svc: ServicesDep) -> UserDetail:
    principal = await authenticate(request)
    username = body.username.strip().lower()
    if not _USERNAME.fullmatch(username):
        raise FieldErrors(
            [FieldError(field='username', message='lower case letters, digits, ".", "_", "@" and "-"', code='pattern')]
        )
    if body.password is not None:
        problem = passwords.password_problem(body.password, username=username)
        if problem:
            raise FieldErrors([FieldError(field='password', message=problem, code='password')])
    roles = await check_roles(svc, principal, body.roles, 'roles')
    groups = await _check_groups(svc, principal, body.groups, [])
    user = UserRecord(
        id=new_id(),
        username=username,
        display_name=body.display_name.strip() or username,
        email=body.email,
        source='local',
        password_hash=await passwords.hash_password(body.password) if body.password is not None else None,
        roles=roles,
        groups=groups,
        must_change_password=body.must_change_password and body.password is not None,
        created_at=datetime.now(UTC),
    )
    try:
        await users_db.insert(svc.db, user)
    except Conflict:
        raise Conflict('a user with this username already exists', field='username') from None
    if roles or groups:
        await svc.rbac.changed()
    audit.record(
        AuditAction.USER_CREATE,
        target=_target(user),
        details={'password_set': body.password is not None, 'change_at_sign_in': user.must_change_password},
        changes=audit.diff(None, _audited(user)),
    )
    response.headers['Location'] = f'/api/v1/users/{user.id}'
    return await _detail(svc, user)


@router.get('/users/{user_id}', response_model=UserDetail, operation_id='getUser', responses={404: PROBLEM})
@access.requires(Action.USER_ADMIN)
async def get_user(user_id: UserId, svc: ServicesDep) -> UserDetail:
    return await _detail(svc, await _user(svc, user_id))


@router.patch(
    '/users/{user_id}',
    response_model=UserDetail,
    operation_id='updateUser',
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def update_user(user_id: UserId, body: UserUpdate, request: Request, svc: ServicesDep) -> UserDetail:
    principal = await authenticate(request)
    before = await _user(svc, user_id)
    await _manageable(svc, principal, before)
    changes: dict[str, Any] = {}
    if body.display_name is not None:
        changes['display_name'] = body.display_name.strip() or before.username
    if 'email' in body.model_fields_set:
        changes['email'] = body.email
    if body.roles is not None:
        changes['roles'] = await check_roles(svc, principal, body.roles, 'roles', before.roles)
    if body.groups is not None:
        changes['groups'] = await _check_groups(svc, principal, body.groups, before.groups)
    if body.disabled is not None:
        if body.disabled and before.id == principal.id:
            raise Conflict('you cannot disable your own account', field='disabled')
        changes['disabled'] = body.disabled
    rbac = {'roles', 'groups', 'disabled'} & changes.keys()
    if rbac:
        proposed = before.model_copy(update={k: changes[k] for k in rbac})
        await ensure_admin_remains(svc, lambda u: proposed if u.id == before.id else u)
    if not changes:
        return await _detail(svc, before)
    updated = await users_db.update(svc.db, before.id, changes)
    if updated is None:
        raise NotFound('user not found')
    if updated.disabled and not before.disabled:
        await sessions_db.delete_for_user(svc.db, updated.id)
    if rbac:
        await _rbac_changed(svc)
    audit.record(
        AuditAction.USER_UPDATE, target=_target(updated), changes=audit.diff(_audited(before), _audited(updated))
    )
    return await _detail(svc, updated)


@router.delete(
    '/users/{user_id}', status_code=204, operation_id='deleteUser', responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM}
)
@access.requires(Action.USER_ADMIN)
async def delete_user(user_id: UserId, request: Request, svc: ServicesDep) -> Response:
    principal = await authenticate(request)
    user = await _user(svc, user_id)
    if user.id == principal.id:
        raise Conflict('you cannot delete your own account')
    await _manageable(svc, principal, user)
    await ensure_admin_remains(svc, _without_user(user.id))
    if not await users_db.delete(svc.db, user.id):
        raise NotFound('user not found')
    sessions = await sessions_db.delete_for_user(svc.db, user.id)
    tokens = await svc.tokens.revoke_all(TokenOwner(kind='user', id=user.id), by=principal.id)
    await _rbac_changed(svc)
    audit.record(
        AuditAction.USER_DELETE,
        target=_target(user),
        details={'sessions_ended': sessions, 'tokens_revoked': tokens},
        changes=audit.diff(_audited(user), None),
    )
    return Response(status_code=204)


@router.post(
    '/users/{user_id}/password',
    status_code=204,
    operation_id='resetUserPassword',
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def reset_password(user_id: UserId, body: PasswordReset, request: Request, svc: ServicesDep) -> Response:
    """Set a local user's password (the user must change it at the next sign-in unless told otherwise); every
    session of the user ends."""
    principal = await authenticate(request)
    user = await _user(svc, user_id)
    await _manageable(svc, principal, user)
    if user.source != 'local':
        raise Conflict(f'the account signs in through {user.source}; its password is managed there')
    problem = passwords.password_problem(body.password, username=user.username)
    if problem:
        raise FieldErrors([FieldError(field='password', message=problem, code='password')])
    updated = await users_db.set_password(
        svc.db, user.id, await passwords.hash_password(body.password), must_change=body.must_change_password
    )
    if updated is None:
        raise NotFound('user not found')
    ended = await sessions_db.delete_for_user(svc.db, user.id)
    audit.record(
        AuditAction.AUTH_PASSWORD_CHANGE,
        target=_target(user),
        details={'by_admin': True, 'change_at_sign_in': body.must_change_password, 'sessions_ended': ended},
    )
    return Response(status_code=204)


@router.post(
    '/users/{user_id}/unlock',
    response_model=UserDetail,
    operation_id='unlockUser',
    responses={403: PROBLEM, 404: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def unlock_user(user_id: UserId, request: Request, svc: ServicesDep) -> UserDetail:
    principal = await authenticate(request)
    user = await _user(svc, user_id)
    await _manageable(svc, principal, user)
    updated = await users_db.unlock(svc.db, user.id)
    if updated is None:
        raise NotFound('user not found')
    audit.record(
        AuditAction.USER_UPDATE,
        target=_target(updated),
        details={'unlocked': True},
        changes=audit.diff(
            {'locked_until': user.locked_until, 'failed_logins': user.failed_logins},
            {'locked_until': None, 'failed_logins': 0},
        ),
    )
    return await _detail(svc, updated)


@router.get(
    '/users/{user_id}/permissions',
    response_model=EffectivePermissions,
    operation_id='getUserPermissions',
    responses={404: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def user_permissions(user_id: UserId, svc: ServicesDep) -> EffectivePermissions:
    user = await _user(svc, user_id)
    return await effective_permissions(svc, await principal_for(svc, user, AuthMethod.NONE), user=user)
