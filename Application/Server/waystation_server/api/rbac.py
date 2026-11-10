"""Helpers shared by the identity administration routes (users, groups, roles, service accounts, tokens).

Delegation is bounded by the caller's own powers (``access.may_assign`` / ``may_manage``): roles a caller adds must be
ones it may give away, and an account holding more than the caller (an administrator, for a ``user-admin``) cannot be
changed by it. Those refusals are 403: the object is visible to every ``user-admin``, so it is not hidden.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Literal

from pydantic import BaseModel, Field

from waystation_common.errors import Forbidden
from waystation_server.db import groups as groups_db
from waystation_server.problems import FieldError, FieldErrors
from waystation_server.security import access
from waystation_server.security.actions import GLOBAL_ACTIONS, Action, Grants
from waystation_server.security.groups import role_sources
from waystation_server.security.identity import ServiceAccountRecord, UserRecord
from waystation_server.security.principal import ANONYMOUS_ROLE, AuthMethod, Principal, PrincipalKind
from waystation_server.services import Services

PROBLEM = {'content': {'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}}}


async def check_roles(
    svc: Services, caller: Principal, roles: list[str], field: str, keep: Collection[str] = ()
) -> list[str]:
    """``roles`` exist and are grantable (built-in or custom); roles not already held (``keep``) must be ones the
    caller may give away. Returns them de-duplicated, in order."""
    custom = await svc.rbac.table()
    errors = []
    for i, role in enumerate(roles):
        if not access.is_role(role, custom):
            errors.append(FieldError(field=f'{field}.{i}', message=f'unknown role {role!r}', code='role'))
        elif role not in keep and not access.may_assign_role(caller, role, custom):
            errors.append(FieldError(field=f'{field}.{i}', message=f'you cannot grant {role!r}', code='role'))
    if errors:
        raise FieldErrors(errors)
    return list(dict.fromkeys(roles))


async def user_grants(svc: Services, user: UserRecord) -> tuple[tuple[str, ...], Grants]:
    """A user's effective roles (own plus groups) and their materialised grants."""
    roles = await svc.rbac.roles_for(user)
    return roles, await svc.rbac.grants(user.id, roles)


async def account_principal(svc: Services, account: ServiceAccountRecord) -> Principal:
    """A service account as a principal holding its own (materialised) grants and the anonymous grants every
    authenticated principal also receives."""
    roles = tuple(account.roles)
    principal = Principal(
        PrincipalKind.SERVICE_ACCOUNT,
        account.id,
        account.name,
        roles,
        AuthMethod.NONE,
        grants=await svc.rbac.grants(account.id, roles),
    )
    return await svc.rbac.with_anonymous(principal)


def require_manageable(caller: Principal, target: Grants, what: str) -> None:
    if not access.may_manage(caller, target):
        raise Forbidden(f'you cannot change {what} that holds more permissions than you')


# ---------------------------------------------------------------------------- effective permissions
REPO_ACTIONS = tuple(a for a in Action if a not in GLOBAL_ACTIONS)


class PrincipalRef(BaseModel):
    kind: Literal['user', 'service_account', 'anonymous']
    id: str
    name: str


class EffectiveRole(BaseModel):
    id: str
    name: str
    via: list[str] = Field(
        description='`direct`, `anonymous` (anonymous access, which every signed-in principal also receives), or the '
        'names of the groups the role comes from'
    )


class GrantedPermission(BaseModel):
    action: Action
    scope: str


class RepositoryAccess(BaseModel):
    name: str
    format: str
    type: str
    actions: list[Action]


class EffectivePermissions(BaseModel):
    principal: PrincipalRef
    roles: list[EffectiveRole]
    permissions: list[GrantedPermission] = Field(
        description='Every (action, scope) held, implied ones and the anonymous-access ones included'
    )
    global_actions: list[Action]
    repositories: list[RepositoryAccess] = Field(description='Repositories with at least one allowed action')
    narrowed_by_token: bool = Field(description='The request used an API token, which narrows the grants shown')


VIA_ANONYMOUS = 'anonymous'  # a role every signed-in principal receives from anonymous access


async def _role_names(svc: Services) -> dict[str, str]:
    names = {rid: name for rid, name, _ in access.roles()}
    names[ANONYMOUS_ROLE] = 'Anonymous'
    names.update({rid: r.name for rid, r in (await svc.rbac.custom_roles()).items()})
    return names


async def effective_permissions(
    svc: Services, principal: Principal, *, user: UserRecord | None = None
) -> EffectivePermissions:
    """What ``principal`` may do (its materialised grants plus the anonymous ones, and per repository after any
    token narrowing)."""
    names = await _role_names(svc)
    via: dict[str, list[str]] = {}
    if user is not None:
        for role, source in role_sources(user, await groups_db.list_all(svc.db)):
            via.setdefault(role, []).append(source)
    else:
        for role in principal.roles:
            via.setdefault(role, []).append('direct')
    if access.anonymous_part(principal) is not None:
        for role in await svc.rbac.anonymous_roles():
            via.setdefault(role, []).append(VIA_ANONYMOUS)
    grants = access.effective_grants(principal)
    repos = []
    for repo in await svc.catalog.all():
        actions = [a for a in REPO_ACTIONS if access.check(principal, a, repo)]
        if actions:
            repos.append(RepositoryAccess(name=repo.name, format=repo.format, type=repo.type.value, actions=actions))
    kind: Literal['user', 'service_account', 'anonymous'] = (
        'anonymous'
        if principal.is_anonymous
        else 'service_account'
        if principal.kind is PrincipalKind.SERVICE_ACCOUNT
        else 'user'
    )
    return EffectivePermissions(
        principal=PrincipalRef(kind=kind, id=principal.id, name=principal.name),
        roles=[EffectiveRole(id=role, name=names.get(role, role), via=sources) for role, sources in via.items()],
        permissions=[GrantedPermission(action=p.action, scope=p.scope) for p in grants.permissions],
        global_actions=[a for a in Action if a in GLOBAL_ACTIONS and access.check(principal, a)],
        repositories=repos,
        narrowed_by_token=principal.token is not None or principal.token_access is not None,
    )
