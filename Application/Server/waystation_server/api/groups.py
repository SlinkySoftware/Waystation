"""``/api/v1/groups``: groups and their roles (SPECIFICATION §4.10); needs ``user-admin``.

- Local groups: create, rename, describe, give roles, delete; members are added and removed here (stored on the
  user, ``users.groups``).
- Groups mapped from an LDAP/OIDC provider (``source`` ``ldap``/``oidc``) are listed read-only except for their
  roles, which can be changed here or in the provider's group mapping (the same documents). They are created and
  deleted with the mapping, and their members are whoever the provider reported at the last sign-in.
- Delegation (``api.rbac``): roles added must be ones the caller may give away, and a group holding more than the
  caller (an administrator group, for a ``user-admin``) cannot be changed by it. Deleting a group or removing a member
  cannot leave the instance without an enabled administrator (409).
- Every change bumps ``grant_version`` (applied by every replica within moments) and is audited (``group.*``;
  membership changes as ``group.update`` with ``details.members_added|members_removed``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from waystation_common.errors import Conflict, NotFound
from waystation_common.ids import new_id
from waystation_server import audit
from waystation_server.api.deps import ServicesDep
from waystation_server.api.rbac import PROBLEM, check_roles, require_manageable
from waystation_server.api.repositories import managed
from waystation_server.api.users import (
    UserPage,
    decode_cursor,
    encode_cursor,
    ensure_admin_remains,
    user_summary,
)
from waystation_server.audit import AuditAction
from waystation_server.db import auth_providers as providers_db
from waystation_server.db import groups as groups_db
from waystation_server.db import users as users_db
from waystation_server.problems import FieldError, FieldErrors
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate
from waystation_server.security.groups import GroupRecord
from waystation_server.security.identity import UserRecord
from waystation_server.security.principal import Principal
from waystation_server.services import Services

router = APIRouter(tags=['admin'])

GroupId = Annotated[str, Path(min_length=1, max_length=64)]
UserId = Annotated[str, Path(min_length=1, max_length=64)]
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$')
MAX_MEMBERS_PER_CALL = 500


class ProviderRef(BaseModel):
    id: str
    name: str | None = Field(description='null when the provider no longer exists')


class Group(BaseModel):
    id: str
    name: str
    description: str
    source: Literal['local', 'ldap', 'oidc']
    provider: ProviderRef | None = Field(description='The provider of a mapped group')
    external_group: str | None = Field(description='The provider group DN or name a mapped group matches')
    roles: list[str]
    member_count: int | None = Field(description='Local groups; null for mapped groups (membership is per sign-in)')
    created_at: datetime
    updated_at: datetime
    managed_by: Literal['provisioning'] | None = Field(
        default=None, description='`provisioning`: declared in the provisioning file (changes here are overwritten)'
    )


class GroupCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1, max_length=64, description='Letters, digits, spaces, `.`, `_`, `-`')
    description: str = Field('', max_length=500)
    roles: list[str] = Field(default_factory=list, max_length=20)


class GroupUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str | None = Field(None, min_length=1, max_length=64, description='Local groups only')
    description: str | None = Field(None, max_length=500, description='Local groups only')
    roles: list[str] | None = Field(None, max_length=20)


class GroupMembersAdd(BaseModel):
    model_config = ConfigDict(extra='forbid')

    user_ids: list[str] = Field(min_length=1, max_length=MAX_MEMBERS_PER_CALL)


# ---------------------------------------------------------------------------- helpers
async def _bodies(svc: Services, groups: list[GroupRecord]) -> list[Group]:
    counts = await users_db.count_members(svc.db, [g.id for g in groups if g.source == 'local'])
    providers = {p.id: p.name for p in await providers_db.list_all(svc.db)}
    out = []
    for g in groups:
        local = g.source == 'local'
        source: Literal['local', 'ldap', 'oidc'] = g.source if g.source in ('ldap', 'oidc') else 'local'  # type: ignore[assignment]
        out.append(
            Group(
                id=g.id,
                name=g.name,
                description=g.description,
                source=source,
                provider=ProviderRef(id=g.provider_id, name=providers.get(g.provider_id)) if g.provider_id else None,
                external_group=None if local else (g.external_group or g.external_id),
                roles=g.roles,
                member_count=counts.get(g.id, 0) if local else None,
                created_at=g.created_at,
                updated_at=g.updated_at,
                managed_by=managed(g.managed_by),
            )
        )
    return out


async def _group(svc: Services, group_id: str) -> GroupRecord:
    group = await groups_db.get(svc.db, group_id)
    if group is None:
        raise NotFound('group not found')
    return group


def _local(group: GroupRecord) -> None:
    if group.source != 'local':
        raise Conflict("a mapped group is managed by its provider's group mapping (only its roles can change here)")


async def _manageable(svc: Services, caller: Principal, group: GroupRecord) -> None:
    require_manageable(caller, access.materialise(group.roles, await svc.rbac.table()), 'a group')


def _check_name(name: str) -> str:
    name = name.strip()
    if not NAME.fullmatch(name):
        raise FieldErrors(
            [FieldError(field='name', message='letters, digits, spaces, ".", "_" and "-" (up to 64)', code='pattern')]
        )
    return name


def _target(group: GroupRecord) -> audit.AuditTarget:
    return audit.target('group', group.id, group.name)


AUDIT_FIELDS = {'name', 'description', 'roles'}


def without_group(group_id: str, user_ids: set[str] | None = None) -> Any:
    def change(user: UserRecord) -> UserRecord | None:
        if group_id in user.groups and (user_ids is None or user.id in user_ids):
            return user.model_copy(update={'groups': [g for g in user.groups if g != group_id]})
        return user

    return change


# ---------------------------------------------------------------------------- routes
@router.get('/groups', response_model=list[Group], operation_id='listGroups')
@access.requires(Action.USER_ADMIN)
async def list_groups(
    svc: ServicesDep,
    source: Annotated[Literal['local', 'ldap', 'oidc'] | None, Query()] = None,
) -> list[Group]:
    """Local groups by name, then mapped groups by provider and mapping order."""
    groups = [g for g in await groups_db.list_all(svc.db) if source is None or g.source == source]
    groups.sort(key=lambda g: (g.source != 'local', g.provider_id or '', g.position, g.name.lower()))
    return await _bodies(svc, groups)


@router.post(
    '/groups',
    response_model=Group,
    status_code=201,
    operation_id='createGroup',
    responses={403: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def create_group(body: GroupCreate, request: Request, response: Response, svc: ServicesDep) -> Group:
    principal = await authenticate(request)
    name = _check_name(body.name)
    roles = await check_roles(svc, principal, body.roles, 'roles')
    now = datetime.now(UTC)
    group = GroupRecord(
        id=new_id(), name=name, description=body.description.strip(), roles=roles, created_at=now, updated_at=now
    )
    await groups_db.insert(svc.db, group)
    await svc.rbac.changed()
    audit.record(
        AuditAction.GROUP_CREATE,
        target=_target(group),
        changes=audit.diff(None, group.model_dump(include=AUDIT_FIELDS)),
    )
    response.headers['Location'] = f'/api/v1/groups/{group.id}'
    return (await _bodies(svc, [group]))[0]


@router.get('/groups/{group_id}', response_model=Group, operation_id='getGroup', responses={404: PROBLEM})
@access.requires(Action.USER_ADMIN)
async def get_group(group_id: GroupId, svc: ServicesDep) -> Group:
    return (await _bodies(svc, [await _group(svc, group_id)]))[0]


@router.patch(
    '/groups/{group_id}',
    response_model=Group,
    operation_id='updateGroup',
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def update_group(group_id: GroupId, body: GroupUpdate, request: Request, svc: ServicesDep) -> Group:
    principal = await authenticate(request)
    before = await _group(svc, group_id)
    await _manageable(svc, principal, before)
    changes: dict[str, Any] = {}
    if body.name is not None or body.description is not None:
        _local(before)
    if body.name is not None:
        changes['name'] = _check_name(body.name)
    if body.description is not None:
        changes['description'] = body.description.strip()
    if body.roles is not None:
        changes['roles'] = await check_roles(svc, principal, body.roles, 'roles', before.roles)
    if not changes:
        return (await _bodies(svc, [before]))[0]
    changes['updated_at'] = datetime.now(UTC)
    updated = await groups_db.update(svc.db, before.id, changes)
    if updated is None:
        raise NotFound('group not found')
    if 'roles' in changes:
        await svc.rbac.changed()
    audit.record(
        AuditAction.GROUP_UPDATE,
        target=_target(updated),
        changes=audit.diff(before.model_dump(include=AUDIT_FIELDS), updated.model_dump(include=AUDIT_FIELDS)),
    )
    return (await _bodies(svc, [updated]))[0]


@router.delete(
    '/groups/{group_id}',
    status_code=204,
    operation_id='deleteGroup',
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def delete_group(group_id: GroupId, request: Request, svc: ServicesDep) -> Response:
    principal = await authenticate(request)
    group = await _group(svc, group_id)
    _local(group)
    await _manageable(svc, principal, group)
    await ensure_admin_remains(svc, without_group(group.id))
    if not await groups_db.delete(svc.db, group.id):
        raise NotFound('group not found')
    removed = await users_db.remove_group(svc.db, group.id)
    await svc.rbac.changed()
    audit.record(
        AuditAction.GROUP_DELETE,
        target=_target(group),
        details={'members_removed': removed},
        changes=audit.diff(group.model_dump(include=AUDIT_FIELDS), None),
    )
    return Response(status_code=204)


@router.get(
    '/groups/{group_id}/members', response_model=UserPage, operation_id='listGroupMembers', responses={404: PROBLEM}
)
@access.requires(Action.USER_ADMIN)
async def list_members(
    group_id: GroupId,
    svc: ServicesDep,
    cursor: Annotated[str | None, Query(max_length=400)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> UserPage:
    """Members of a local group (by username). Mapped groups have no stored membership: empty."""
    group = await _group(svc, group_id)
    if group.source != 'local':
        return UserPage(items=[], next_cursor=None)
    users = await users_db.list_page(svc.db, group_id=group.id, after=decode_cursor(cursor), limit=limit + 1)
    now = datetime.now(UTC)
    page = users[:limit]
    return UserPage(
        items=[user_summary(u, now) for u in page],
        next_cursor=encode_cursor(page[-1].username) if len(users) > limit else None,
    )


@router.post(
    '/groups/{group_id}/members',
    status_code=204,
    operation_id='addGroupMembers',
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def add_members(group_id: GroupId, body: GroupMembersAdd, request: Request, svc: ServicesDep) -> Response:
    principal = await authenticate(request)
    group = await _group(svc, group_id)
    _local(group)
    await _manageable(svc, principal, group)
    ids = list(dict.fromkeys(body.user_ids))
    found = {u.id for u in await users_db.get_many(svc.db, ids)}
    missing = [
        FieldError(field=f'user_ids.{i}', message='no such user', code='user')
        for i, u in enumerate(ids)
        if u not in found
    ]
    if missing:
        raise FieldErrors(missing)
    added = await users_db.add_group(svc.db, ids, group.id)
    await svc.rbac.changed()
    audit.record(AuditAction.GROUP_UPDATE, target=_target(group), details={'members_added': ids, 'changed': added})
    return Response(status_code=204)


@router.delete(
    '/groups/{group_id}/members/{user_id}',
    status_code=204,
    operation_id='removeGroupMember',
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM},
)
@access.requires(Action.USER_ADMIN)
async def remove_member(group_id: GroupId, user_id: UserId, request: Request, svc: ServicesDep) -> Response:
    principal = await authenticate(request)
    group = await _group(svc, group_id)
    _local(group)
    await _manageable(svc, principal, group)
    await ensure_admin_remains(svc, without_group(group.id, {user_id}))
    if not await users_db.remove_group(svc.db, group.id, [user_id]):
        raise NotFound('not a member of this group')
    await svc.rbac.changed()
    audit.record(AuditAction.GROUP_UPDATE, target=_target(group), details={'members_removed': [user_id]})
    return Response(status_code=204)
