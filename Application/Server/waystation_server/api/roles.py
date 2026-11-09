"""``/api/v1/roles`` and ``/api/v1/anonymous-access``: built-in and custom roles (SPECIFICATION §4.10).

- ``GET /roles`` (any signed-in principal): the grantable roles with their permissions (built-in first, then custom
  by id). ``anonymous`` is the anonymous principal's own role and is not listed.
- ``POST /roles``, ``PUT /roles/{id}``, ``DELETE /roles/{id}`` (``user-admin``): custom roles. Built-in roles are
  read-only (409). Permissions are ``(action, scope)`` pairs checked by ``access.permission_error`` (global actions
  only on ``*``). Permissions a caller adds must be ones it may give away (``access.may_assign``: ``admin`` only by
  administrators, other global actions only by holders). A role still held by a user, group or service account
  cannot be deleted (409; remove it from them first).
- ``GET/PUT /anonymous-access`` (``user-admin``): the roles of requests without credentials (default: the built-in
  ``anonymous`` role, browse and read on proxy repositories); repository permissions only, never a global action.
- Every change bumps ``grant_version`` (``RbacState.changed``): every replica applies it within moments.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from waystation_common.errors import Conflict, Forbidden, NotFound
from waystation_server import audit
from waystation_server.api.deps import ServicesDep
from waystation_server.api.rbac import PROBLEM
from waystation_server.audit import AuditAction
from waystation_server.db import roles as roles_db
from waystation_server.problems import FieldError, FieldErrors
from waystation_server.security import access
from waystation_server.security.actions import Action, Permission
from waystation_server.security.authn import authenticate
from waystation_server.security.identity import RolePermission, RoleRecord
from waystation_server.security.principal import ANONYMOUS_ROLE, Principal
from waystation_server.services import Services

router = APIRouter(tags=['admin'])

_ROLE_ID = re.compile(r'^[a-z][a-z0-9-]{1,47}$')
MAX_PERMISSIONS = 100
RoleId = Annotated[str, Path(min_length=1, max_length=64)]


class RolePermissionBody(BaseModel):
    model_config = ConfigDict(extra='forbid')

    action: Action
    scope: str = Field(
        min_length=1,
        max_length=140,
        description='`*`, a repository name, a glob (`docker-*`), `format:<format>` or `type:<type>`; '
        'global actions only `*`',
    )


class RoleUsage(BaseModel):
    users: int
    groups: int
    service_accounts: int


class Role(BaseModel):
    id: str
    name: str
    description: str
    builtin: bool
    permissions: list[RolePermissionBody]


class RoleDetail(Role):
    usage: RoleUsage = Field(description='Who holds the role directly (custom roles; zeros for built-in roles)')


class RoleWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1, max_length=64)
    description: str = Field('', max_length=500)
    permissions: list[RolePermissionBody] = Field(min_length=1, max_length=MAX_PERMISSIONS)

    @field_validator('name')
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('a name is required')
        return value


class RoleCreate(RoleWrite):
    id: str = Field(
        min_length=2, max_length=48, description='Lower-case letters, digits and `-`; what users and groups store'
    )


# ---------------------------------------------------------------------------- helpers
def _builtin_body(role_id: str, name: str, description: str) -> Role:
    perms = access.BUILTIN_ROLES[role_id]
    return Role(
        id=role_id,
        name=name,
        description=description,
        builtin=True,
        permissions=[RolePermissionBody(action=p.action, scope=p.scope) for p in perms],
    )


def _custom_body(record: RoleRecord) -> Role:
    perms = [
        RolePermissionBody(action=Action(p.action), scope=p.scope) for p in record.permissions if p.action in Action
    ]
    return Role(id=record.id, name=record.name, description=record.description, builtin=False, permissions=perms)


def _check_permissions(body: RoleWrite) -> list[Permission]:
    errors: list[FieldError] = []
    perms: list[Permission] = []
    for i, perm in enumerate(body.permissions):
        problem = access.permission_error(perm.action.value, perm.scope)
        if problem:
            errors.append(FieldError(field=f'permissions.{i}.scope', message=problem, code='scope'))
        perms.append(Permission(perm.action, perm.scope))
    if errors:
        raise FieldErrors(errors)
    return list(dict.fromkeys(perms))


def _check_delegation(request_principal: Principal, added: list[Permission]) -> None:
    if added and not access.may_assign(request_principal, access.materialise(('_',), {'_': added})):
        raise FieldErrors(
            [
                FieldError(
                    field='permissions',
                    message='you can only add global permissions you hold yourself (admin: administrators only)',
                    code='delegation',
                )
            ]
        )


def _stored(perms: list[Permission]) -> list[dict[str, str]]:
    return [{'action': p.action.value, 'scope': p.scope} for p in perms]


def _target(role_id: str, name: str) -> audit.AuditTarget:
    return audit.target('role', role_id, name)


async def _custom(svc: Services, role_id: str) -> RoleRecord:
    if role_id in access.BUILTIN_ROLES:
        raise Conflict('built-in roles are read-only')
    record = await roles_db.get(svc.db, role_id)
    if record is None:
        raise NotFound('role not found')
    return record


# ---------------------------------------------------------------------------- routes
@router.get('/roles', response_model=list[Role], operation_id='listRoles')
@access.authenticated
async def list_roles(svc: ServicesDep) -> list[Role]:
    """Grantable roles: the built-in ones, then the custom ones by id."""
    builtin = [_builtin_body(rid, name, desc) for rid, name, desc in access.roles()]
    custom = await svc.rbac.custom_roles()
    return builtin + [_custom_body(custom[rid]) for rid in sorted(custom)]


@router.get('/roles/{role_id}', response_model=RoleDetail, operation_id='getRole', responses={404: PROBLEM})
@access.requires(Action.USER_ADMIN)
async def get_role(role_id: RoleId, svc: ServicesDep) -> RoleDetail:
    builtin = {rid: (name, desc) for rid, name, desc in access.roles()}
    if role_id in builtin:
        body = _builtin_body(role_id, *builtin[role_id])
    else:
        record = await roles_db.get(svc.db, role_id)
        if record is None:
            raise NotFound('role not found')
        body = _custom_body(record)
    usage = await roles_db.usage(svc.db, role_id)
    return RoleDetail(**body.model_dump(), usage=RoleUsage(**usage))


@router.post(
    '/roles',
    response_model=Role,
    status_code=201,
    operation_id='createRole',
    responses={403: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def create_role(body: RoleCreate, request: Request, response: Response, svc: ServicesDep) -> Role:
    principal = await authenticate(request)
    role_id = body.id.strip().lower()
    if not _ROLE_ID.fullmatch(role_id):
        raise FieldErrors([FieldError(field='id', message='lower-case letters, digits and "-"', code='pattern')])
    if role_id in access.BUILTIN_ROLES:
        raise Conflict('a built-in role has this id', field='id')
    perms = _check_permissions(body)
    _check_delegation(principal, perms)
    record = RoleRecord(
        id=role_id,
        name=body.name,
        description=body.description.strip(),
        permissions=[RolePermission(**p) for p in _stored(perms)],
        created_at=datetime.now(UTC),
        created_by=principal.id,
    )
    await roles_db.insert(svc.db, record)
    await svc.rbac.changed()
    audit.record(
        AuditAction.ROLE_CREATE,
        target=_target(record.id, record.name),
        changes=audit.diff(None, record.model_dump(include={'name', 'description', 'permissions'})),
    )
    response.headers['Location'] = f'/api/v1/roles/{record.id}'
    return _custom_body(record)


@router.put(
    '/roles/{role_id}',
    response_model=Role,
    operation_id='updateRole',
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def update_role(role_id: RoleId, body: RoleWrite, request: Request, svc: ServicesDep) -> Role:
    principal = await authenticate(request)
    before = await _custom(svc, role_id)
    perms = _check_permissions(body)
    held = {Permission(Action(p.action), p.scope) for p in before.permissions if p.action in Action}
    _check_delegation(principal, [p for p in perms if p not in held])
    updated = await roles_db.update(
        svc.db,
        role_id,
        {
            'name': body.name,
            'description': body.description.strip(),
            'permissions': _stored(perms),
            'updated_at': datetime.now(UTC),
            'updated_by': principal.id,
        },
    )
    if updated is None:
        raise NotFound('role not found')
    await svc.rbac.changed()
    fields = {'name', 'description', 'permissions'}
    audit.record(
        AuditAction.ROLE_UPDATE,
        target=_target(updated.id, updated.name),
        changes=audit.diff(before.model_dump(include=fields), updated.model_dump(include=fields)),
    )
    return _custom_body(updated)


@router.delete(
    '/roles/{role_id}', status_code=204, operation_id='deleteRole', responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM}
)
@access.requires(Action.USER_ADMIN)
async def delete_role(role_id: RoleId, request: Request, svc: ServicesDep) -> Response:
    principal = await authenticate(request)
    record = await _custom(svc, role_id)
    held = [Permission(Action(p.action), p.scope) for p in record.permissions if p.action in Action]
    if not access.may_assign(principal, access.materialise(('_',), {'_': held})):
        raise Forbidden('you cannot delete a role holding more permissions than you')
    usage = await roles_db.usage(svc.db, role_id)
    if any(usage.values()):
        held_by = ', '.join(f'{n} {kind.replace("_", " ")}' for kind, n in usage.items() if n)
        raise Conflict(f'the role is still held by {held_by}; remove it from them first')
    if not await roles_db.delete(svc.db, role_id):
        raise NotFound('role not found')
    await svc.rbac.changed()
    audit.record(
        AuditAction.ROLE_DELETE,
        target=_target(record.id, record.name),
        changes=audit.diff(record.model_dump(include={'name', 'description', 'permissions'}), None),
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------- anonymous access
class AnonymousAccess(BaseModel):
    model_config = ConfigDict(extra='forbid')

    roles: list[str] = Field(
        max_length=20,
        description='Roles of requests without credentials: `anonymous` (browse and read proxy repositories, the '
        'default) and/or roles holding repository permissions only; empty = no anonymous access',
    )


ANONYMOUS_TARGET = ('anonymous_access', 'anonymous', 'Anonymous access')


@router.get('/anonymous-access', response_model=AnonymousAccess, operation_id='getAnonymousAccess')
@access.requires(Action.USER_ADMIN)
async def get_anonymous_access(svc: ServicesDep) -> AnonymousAccess:
    return AnonymousAccess(roles=list(await svc.rbac.anonymous_roles()))


@router.put(
    '/anonymous-access',
    response_model=AnonymousAccess,
    operation_id='updateAnonymousAccess',
    responses={403: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def update_anonymous_access(body: AnonymousAccess, request: Request, svc: ServicesDep) -> AnonymousAccess:
    """What requests without credentials may do (SPECIFICATION §4.9). Only repository permissions can be given
    (no global action), and only ones the caller may give away; applied by every replica within moments."""
    principal = await authenticate(request)
    custom = await svc.rbac.table()
    roles = list(dict.fromkeys(body.roles))
    errors: list[FieldError] = []
    for i, role in enumerate(roles):
        if role != ANONYMOUS_ROLE and not access.is_role(role, custom):
            errors.append(FieldError(field=f'roles.{i}', message=f'unknown role {role!r}', code='role'))
            continue
        grants = access.materialise((role,), custom)
        if grants.is_admin or access.global_actions(grants):
            errors.append(
                FieldError(
                    field=f'roles.{i}', message='anonymous access can only hold repository permissions', code='global'
                )
            )
        elif not access.may_assign(principal, grants):
            errors.append(FieldError(field=f'roles.{i}', message=f'you cannot grant {role!r}', code='role'))
    if errors:
        raise FieldErrors(errors)
    before = list(await svc.rbac.anonymous_roles())
    await roles_db.set_anonymous_roles(svc.db, roles)
    await svc.rbac.changed()
    audit.record(
        AuditAction.ROLE_UPDATE,
        target=audit.target(*ANONYMOUS_TARGET),
        changes=audit.diff({'roles': before}, {'roles': roles}),
    )
    return AnonymousAccess(roles=roles)
