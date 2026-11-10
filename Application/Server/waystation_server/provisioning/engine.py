"""Plan and apply a provisioning file (``Documents/Operations/PROVISIONING.md``).

``plan`` compares what the file declares with what is stored and lists, per object, ``create``, ``update`` (with
field-level changes), ``unchanged`` or ``prune``; nothing is written. ``apply`` makes the same plan and then the
changes, in dependency order:

roles → cleanup policies → repositories (proxy and hosted, then groups; cleanup attachments) → local groups →
service accounts → sign-in providers (with their group → role mapping) → webhooks → settings → prunes (reverse).

Rules
-----
- **Invalid means nothing is applied**: schema errors, unresolvable secret references and validation errors (the same
  checks as the REST API, run as a dry build) are all reported first and stop the apply.
- **Idempotent**: applying an unchanged file changes nothing (secrets are compared with the stored values, which are
  decrypted for the comparison only; a re-encryption is never a change).
- **Ownership**: everything the file creates or updates is tagged ``managed_by: provisioning``. An object with the
  same name that the file did not create is adopted (tagged) on the first apply. Without ``prune`` for its section a
  managed object the file no longer declares is released (the tag is removed), never deleted; with ``prune`` it is
  deleted (roles still held and repositories still in a group are refused, like in the API).
- **Secrets** are references (``{file: ...}`` / ``{env: ...}``), resolved while planning; plans, audit records and
  logs show a changed secret only as ``redacted``.
- **Audit**: every change is recorded with the usual action (``repository.create``, ``role.update``, ...) and
  ``details.source = provisioning`` (plus the file's sha256); the actor is the administrator who applied it, or
  ``system`` when ``init`` did.

The file is always the configured one (``WAYSTATION_PROVISION_FILE``): the API never accepts an uploaded document,
so secret references can only be what the operator wrote.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any

from pydantic import BaseModel, ValidationError

from waystation_common.audit import AuditActor, AuditChange, AuditWriter, Outcome
from waystation_common.cleanup import Criteria
from waystation_common.db import cleanup as cleanup_db
from waystation_common.db import storage_guard
from waystation_common.db import webhooks as webhooks_db
from waystation_common.db.client import Document
from waystation_common.errors import Conflict, NotFound, WaystationError
from waystation_common.ids import new_id
from waystation_server import audit
from waystation_server.api import auth_providers as providers_api
from waystation_server.api import cleanup as cleanup_api
from waystation_server.api import groups as groups_api
from waystation_server.api import repositories as repos_api
from waystation_server.api import webhooks as webhooks_api
from waystation_server.api.roles import ANONYMOUS_TARGET
from waystation_server.api.users import ensure_admin_remains
from waystation_server.audit import AuditAction, AuditTarget
from waystation_server.db import auth_providers as providers_db
from waystation_server.db import groups as groups_db
from waystation_server.db import repositories as repos_db
from waystation_server.db import roles as roles_db
from waystation_server.db import service_accounts as accounts_db
from waystation_server.db import settings as settings_db
from waystation_server.db import users as users_db
from waystation_server.problems import FieldErrors
from waystation_server.provisioning import loader
from waystation_server.provisioning.loader import Loaded, Problem, SecretError
from waystation_server.provisioning.model import (
    MANAGED_BY,
    AuthProviderSpec,
    CleanupPolicySpec,
    GroupSpec,
    ProvisioningFile,
    RepositorySpec,
    RoleSpec,
    SecretRef,
    ServiceAccountSpec,
    SettingsSpec,
    WebhookSpec,
)
from waystation_server.provisioning.report import (
    ObjectKind,
    PlanAction,
    ProvisioningChange,
    ProvisioningItem,
    ProvisioningLastApply,
    ProvisioningPlan,
    ProvisioningProblem,
    ProvisioningSummary,
)
from waystation_server.repos import service as repos_service
from waystation_server.repos.model import AuthKind, RepoConfig, RepoType
from waystation_server.repos.schemas import RepositoryWrite, StorageConfig
from waystation_server.security import access
from waystation_server.security.actions import Action, Permission
from waystation_server.security.groups import GroupRecord
from waystation_server.security.identity import RolePermission, RoleRecord, ServiceAccountRecord, TokenOwner
from waystation_server.security.principal import ANONYMOUS_ROLE, Principal
from waystation_server.security.providers import (
    AuthProviderWrite,
    OidcClientAuth,
    ProviderRecord,
    decrypt_bind_password,
    decrypt_client_secret,
)
from waystation_server.security.tokens import REVOKE_ALL
from waystation_server.services import Services
from waystation_server.webhooks import WebhookNotifier

log = logging.getLogger('waystation.provisioning')

STATUS_SECTION = 'provisioning'  # ``settings`` document recording the last apply
UNREADABLE = '\0unreadable'  # a stored secret that cannot be decrypted (never equal to a resolved one)
_lock = asyncio.Lock()

Run = Callable[[], Awaitable[AuditTarget | None]]


@dataclass
class Context:
    """Who applies the file and where audit records go."""

    svc: Services
    principal: Principal  # stored as created_by / updated_by
    by: str  # ``init`` or the administrator's name (last-apply record)
    actor: Principal | AuditActor | None = None  # audit actor (default: the request's principal)
    writer: AuditWriter | None = None  # outside a request (init)
    notifier: WebhookNotifier | None = None


@dataclass
class Step:
    item: ProvisioningItem
    run: Run | None = None
    action: AuditAction | None = None
    target: AuditTarget | None = None
    changes: list[AuditChange] = field(default_factory=list)


@dataclass
class _Planner:
    ctx: Context
    doc: ProvisioningFile
    errors: list[Problem] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    prunes: list[Step] = field(default_factory=list)
    secrets: dict[str, str] = field(default_factory=dict)  # file path of the reference -> resolved value
    table: dict[str, tuple[Permission, ...]] = field(default_factory=dict)  # custom roles after the apply

    @property
    def svc(self) -> Services:
        return self.ctx.svc


# ---------------------------------------------------------------------------- helpers
def _digest(value: str | None) -> str | None:
    """Compared instead of the secret itself (``audit.diff`` reports any difference as redacted)."""
    if value is None:
        return None
    return hashlib.sha256(b'waystation-provisioning\0' + value.encode()).hexdigest()


def _decrypted(read: Callable[[], str | None]) -> str | None:
    try:
        return read()
    except Exception:  # a key no longer in WAYSTATION_SECRET_KEYS, a corrupt value: treat as different
        return UNREADABLE


def _problems(exc: FieldErrors, prefix: str) -> list[Problem]:
    return [Problem(f'{prefix}.{e.field}' if e.field else prefix, e.message) for e in exc.errors]


def _attempt[T](build: Callable[[], T], prefix: str, errors: list[Problem]) -> T | None:
    """``build()``, or None with its validation errors (paths under ``prefix``; values never echoed)."""
    try:
        return build()
    except ValidationError as exc:
        for err in exc.errors(include_input=False, include_url=False):
            loc = '.'.join(str(p) for p in err['loc'])
            errors.append(Problem(f'{prefix}.{loc}' if loc else prefix, str(err['msg'])))
        return None


def _validated[M: BaseModel](model: type[M], data: Any, prefix: str, errors: list[Problem]) -> M | None:
    return _attempt(lambda: model.model_validate(data), prefix, errors)


def _item(kind: ObjectKind, name: str, action: PlanAction, changes: list[AuditChange], note: str | None = None) -> Step:
    item = ProvisioningItem(
        kind=kind,
        name=name,
        action=action,
        changes=[ProvisioningChange(**c.model_dump()) for c in changes],
        note=note,
    )
    return Step(item=item, changes=changes)


def _change_step(
    kind: ObjectKind,
    name: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    run: Run,
    action: AuditAction,
    target: AuditTarget | None,
    note: str | None = None,
) -> Step:
    changes = audit.diff(before, after)
    if before is not None and not changes:
        return _item(kind, name, 'unchanged', [])
    step = _item(kind, name, 'create' if before is None else 'update', changes, note)
    step.run, step.action, step.target = run, action, target
    return step


def _release_step(kind: ObjectKind, name: str, run: Run, action: AuditAction, target: AuditTarget) -> Step:
    """A managed object no longer in the file, without prune: drop the tag."""
    changes = audit.diff({'managed_by': MANAGED_BY}, {'managed_by': None})
    step = _item(
        kind, name, 'update', changes, f'no longer in the file: released (set prune.{_SECTIONS[kind]} to delete)'
    )
    step.run, step.action, step.target = run, action, target
    return step


def _prune_step(
    kind: ObjectKind, name: str, before: dict[str, Any], run: Run, action: AuditAction, target: AuditTarget
) -> Step:
    changes = audit.diff(before, None)
    step = _item(kind, name, 'prune', changes, 'managed by the file and no longer declared')
    step.run, step.action, step.target = run, action, target
    return step


_SECTIONS: dict[str, str] = {
    'role': 'roles',
    'cleanup_policy': 'cleanup_policies',
    'repository': 'repositories',
    'group': 'groups',
    'service_account': 'service_accounts',
    'auth_provider': 'auth_providers',
    'webhook': 'webhooks',
}


def _perms(record: RoleRecord) -> tuple[Permission, ...]:
    return tuple(Permission(Action(p.action), p.scope) for p in record.permissions if p.action in Action)


# ---------------------------------------------------------------------------- secrets
async def _resolve_all(planner: _Planner) -> None:
    """Resolve every secret reference of the file (errors name the reference, never a value)."""
    refs: list[tuple[str, SecretRef]] = []
    doc = planner.doc
    for i, repo in enumerate(doc.repositories or []):
        if repo.proxy is not None and repo.proxy.auth.secret is not None:
            refs.append((f'repositories.{i}.proxy.auth.secret', repo.proxy.auth.secret))
    for i, provider in enumerate(doc.auth_providers or []):
        if provider.ldap is not None and provider.ldap.connection.bind_password is not None:
            refs.append((f'auth_providers.{i}.ldap.connection.bind_password', provider.ldap.connection.bind_password))
        if provider.oidc is not None and provider.oidc.client_secret is not None:
            refs.append((f'auth_providers.{i}.oidc.client_secret', provider.oidc.client_secret))
    for i, hook in enumerate(doc.webhooks or []):
        refs.append((f'webhooks.{i}.secret', hook.secret))
        for name, ref in hook.headers.items():
            refs.append((f'webhooks.{i}.headers.{name}', ref))
    for path, ref in refs:
        try:
            planner.secrets[path] = await loader.resolve(ref)
        except SecretError as exc:
            planner.errors.append(Problem(path, str(exc)))
    if refs and planner.svc.keyring is None:
        planner.errors.append(Problem('file', 'WAYSTATION_SECRET_KEYS is not set: secrets cannot be stored'))


# ---------------------------------------------------------------------------- roles
async def _plan_roles(p: _Planner, specs: list[RoleSpec], prune: bool) -> None:
    db = p.svc.db
    current = {r.id: r for r in await roles_db.list_all(db)}
    declared = {s.id for s in specs}
    for i, spec in enumerate(specs):
        prefix = f'roles.{i}'
        if spec.id in access.BUILTIN_ROLES:
            p.errors.append(Problem(f'{prefix}.id', 'a built-in role has this id'))
            continue
        perms: list[Permission] = []
        for j, perm in enumerate(spec.permissions):
            if problem := access.permission_error(perm.action.value, perm.scope):
                p.errors.append(Problem(f'{prefix}.permissions.{j}.scope', problem))
            perms.append(Permission(perm.action, perm.scope))
        perms = list(dict.fromkeys(perms))
        stored = [{'action': x.action.value, 'scope': x.scope} for x in perms]
        after = {'name': spec.name.strip(), 'description': spec.description.strip(), 'permissions': stored}
        existing = current.get(spec.id)
        before = None
        if existing is not None:
            before = {
                'name': existing.name,
                'description': existing.description,
                'permissions': [{'action': x.action, 'scope': x.scope} for x in existing.permissions],
                'managed_by': existing.managed_by,
            }

        async def run(
            spec: RoleSpec = spec, stored: list[dict[str, str]] = stored, new: bool = existing is None
        ) -> AuditTarget:
            now = datetime.now(UTC)
            if new:
                record = RoleRecord(
                    id=spec.id,
                    name=spec.name.strip(),
                    description=spec.description.strip(),
                    permissions=[RolePermission(**x) for x in stored],
                    managed_by=MANAGED_BY,
                    created_at=now,
                    created_by=p.ctx.principal.id,
                )
                await roles_db.insert(db, record)
            else:
                fields = {
                    'name': spec.name.strip(),
                    'description': spec.description.strip(),
                    'permissions': stored,
                    'managed_by': MANAGED_BY,
                    'updated_at': now,
                    'updated_by': p.ctx.principal.id,
                }
                if await roles_db.update(db, spec.id, fields) is None:
                    raise NotFound('the role was deleted meanwhile')
            await p.svc.rbac.changed()
            return audit.target('role', spec.id, spec.name.strip())

        p.steps.append(
            _change_step(
                'role',
                spec.id,
                before,
                {**after, 'managed_by': MANAGED_BY},
                run,
                AuditAction.ROLE_CREATE if existing is None else AuditAction.ROLE_UPDATE,
                audit.target('role', spec.id, spec.name.strip()),
            )
        )
    p.table = {rid: _perms(r) for rid, r in current.items()}
    for spec in specs:
        p.table[spec.id] = tuple(Permission(x.action, x.scope) for x in spec.permissions)
    for record in current.values():
        if record.managed_by != MANAGED_BY or record.id in declared:
            continue
        target = audit.target('role', record.id, record.name)
        if prune:
            p.table.pop(record.id, None)

            async def delete(record: RoleRecord = record) -> AuditTarget:
                usage = await roles_db.usage(db, record.id)
                if any(usage.values()):
                    held = ', '.join(f'{n} {kind.replace("_", " ")}' for kind, n in usage.items() if n)
                    raise Conflict(f'the role is still held by {held}')
                await roles_db.delete(db, record.id)
                await p.svc.rbac.changed()
                return audit.target('role', record.id, record.name)

            before = record.model_dump(include={'name', 'description', 'permissions'})
            p.prunes.append(_prune_step('role', record.id, before, delete, AuditAction.ROLE_DELETE, target))
        else:

            async def release(record: RoleRecord = record) -> AuditTarget:
                await roles_db.update(db, record.id, {'managed_by': None})
                return audit.target('role', record.id, record.name)

            p.steps.append(_release_step('role', record.id, release, AuditAction.ROLE_UPDATE, target))


async def _role_table(p: _Planner) -> None:
    """Custom roles when the file does not manage them (references are checked against these)."""
    p.table = {r.id: _perms(r) for r in await roles_db.list_all(p.svc.db)}


def _check_roles(p: _Planner, roles: Sequence[str], prefix: str) -> list[str]:
    for i, role in enumerate(roles):
        if not access.is_role(role, p.table):
            p.errors.append(Problem(f'{prefix}.{i}', f'unknown role {role!r}'))
    return list(dict.fromkeys(roles))


# ---------------------------------------------------------------------------- cleanup policies
def _policy_view(doc: Document) -> dict[str, Any]:
    return {
        'description': doc.get('description', ''),
        'format': doc.get('format'),
        'criteria': Criteria.model_validate(doc.get('criteria') or {}).model_dump(mode='json', exclude_defaults=True),
        'schedule': doc.get('schedule'),
        'enabled': bool(doc.get('enabled', True)),
        'managed_by': doc.get('managed_by'),
    }


async def _plan_policies(p: _Planner, specs: list[CleanupPolicySpec], prune: bool) -> None:
    db = p.svc.db
    docs = await cleanup_db.list_policies(db)
    by_name = {str(d['name']).lower(): d for d in docs}
    declared = {s.name.lower() for s in specs}
    for i, spec in enumerate(specs):
        try:
            cleanup_api.check_criteria(spec.format, spec.criteria)
        except FieldErrors as exc:
            p.errors.extend(_problems(exc, f'cleanup_policies.{i}'))
            continue
        fields: Document = {
            **spec.model_dump(mode='python', exclude={'criteria'}),
            'criteria': spec.criteria.model_dump(mode='json', exclude_defaults=True),
            'managed_by': MANAGED_BY,
        }
        existing = by_name.get(spec.name.lower())

        async def run(fields: Document = fields, existing: Document | None = existing) -> AuditTarget:
            now = datetime.now(UTC)
            if existing is None:
                doc: Document = {
                    '_id': new_id(),
                    **fields,
                    'created_at': now,
                    'created_by': p.ctx.principal.id,
                    'updated_at': now,
                    'updated_by': p.ctx.principal.id,
                }
                await cleanup_db.insert_policy(db, doc)
            else:
                stored = await cleanup_db.update_policy(
                    db, str(existing['_id']), {**fields, 'updated_at': now, 'updated_by': p.ctx.principal.id}
                )
                if stored is None:
                    raise NotFound('the cleanup policy was deleted meanwhile')
                doc = stored
            await cleanup_db.sync_schedule(db, doc)
            return cleanup_api.policy_target(doc)

        before = _policy_view(existing) if existing is not None else None
        after = _policy_view(fields)
        p.steps.append(
            _change_step(
                'cleanup_policy',
                spec.name,
                before,
                after,
                run,
                AuditAction.CLEANUP_POLICY_CREATE if existing is None else AuditAction.CLEANUP_POLICY_UPDATE,
                cleanup_api.policy_target(existing) if existing is not None else None,
            )
        )
    for doc in docs:
        if doc.get('managed_by') != MANAGED_BY or str(doc['name']).lower() in declared:
            continue
        target = cleanup_api.policy_target(doc)
        if prune:

            async def delete(doc: Document = doc) -> AuditTarget:
                if not await cleanup_db.delete_policy(db, str(doc['_id'])):
                    raise NotFound('the cleanup policy was deleted meanwhile')
                p.svc.catalog.invalidate()
                return cleanup_api.policy_target(doc)

            p.prunes.append(
                _prune_step(
                    'cleanup_policy',
                    doc['name'],
                    cleanup_api.audit_view(doc),
                    delete,
                    AuditAction.CLEANUP_POLICY_DELETE,
                    target,
                )
            )
        else:

            async def release(doc: Document = doc) -> AuditTarget:
                await cleanup_db.update_policy(db, str(doc['_id']), {'managed_by': None})
                return cleanup_api.policy_target(doc)

            p.steps.append(
                _release_step('cleanup_policy', doc['name'], release, AuditAction.CLEANUP_POLICY_UPDATE, target)
            )


# ---------------------------------------------------------------------------- repositories
def _repo_view(repo: RepoConfig, secret: str | None) -> dict[str, Any]:
    proxy = repos_api.proxy_view(repo)
    hosted = repos_api.hosted_view(repo)
    group = repos_api.group_view(repo)
    proxy_doc: dict[str, Any] | None = None
    if proxy is not None:
        proxy_doc = proxy.model_dump(mode='json', exclude={'auth'})
        proxy_doc['auth'] = {
            'kind': proxy.auth.kind.value,
            'username': proxy.auth.username,
            'secret': _digest(secret) if proxy.auth.kind is not AuthKind.NONE else None,
        }
    return {
        'online': repo.online,
        'description': repo.description,
        'proxy': proxy_doc,
        'hosted': hosted.model_dump(mode='json') if hosted else None,
        'group': group.model_dump(mode='json') if group else None,
        'storage': {'quota_bytes': repo.storage.quota_bytes, 'quota_mode': repo.storage.quota_mode.value},
        'format_options': repo.format_options,
    }


def _stored_secret(svc: Services, repo: RepoConfig) -> str | None:
    if repo.proxy is None or repo.proxy.auth.kind is AuthKind.NONE or not repo.proxy.auth.secret_enc:
        return None
    return _decrypted(lambda: repos_service.decrypt_secret(svc, repo.id, repo.proxy.auth))  # type: ignore[union-attr]


async def _plan_repositories(
    p: _Planner, specs: list[RepositorySpec], prune: bool, policies: dict[str, str]
) -> list[RepositorySpec]:
    """``policies``: cleanup policy name (lower case) -> format, after the apply. Returns the specs as planned."""
    svc, db = p.svc, p.svc.db
    current = await repos_db.list_all(db)
    by_name = {r.name.lower(): r for r in current}
    declared = {s.name.lower(): s for s in specs}
    pending = {name for name in declared if name not in by_name}
    policy_names = {r.id: [] for r in current}  # type: dict[str, list[str]]
    id_to_name = {str(d['_id']): str(d['name']) for d in await cleanup_db.list_policies(db)}
    for repo in current:
        policy_names[repo.id] = [id_to_name.get(i, i) for i in repo.cleanup_policy_ids]
    indexed = list(enumerate(specs))
    ordered = [x for x in indexed if x[1].type is not RepoType.GROUP] + [
        x for x in indexed if x[1].type is RepoType.GROUP
    ]
    for i, spec in ordered:
        prefix = f'repositories.{i}'
        existing = by_name.get(spec.name.lower())
        for j, name in enumerate(spec.cleanup_policies):
            fmt = policies.get(name.lower())
            if fmt is None:
                p.errors.append(Problem(f'{prefix}.cleanup_policies.{j}', f'no cleanup policy {name!r}'))
            elif fmt not in ('*', spec.format):
                p.errors.append(Problem(f'{prefix}.cleanup_policies.{j}', f'is a {fmt} policy'))
        resolved = p.secrets.get(f'{prefix}.proxy.auth.secret')
        stored_secret = _stored_secret(svc, existing) if existing is not None else None
        body = _attempt(partial(spec.write, resolved), prefix, p.errors)
        if body is None:
            continue
        group_pending = [m for m in (spec.group.members if spec.group else []) if m.lower() in pending]
        after_view: dict[str, Any] | None = None
        if group_pending:
            after_view = _pending_group_view(p, spec, prefix, declared)
        else:
            try:
                built = await repos_service.preview(svc, body, p.ctx.principal, existing)
            except FieldErrors as exc:
                p.errors.extend(_problems(exc, prefix))
                continue
            kind_set = built.proxy is not None and built.proxy.auth.kind is not AuthKind.NONE
            wanted_secret = (resolved if resolved is not None else stored_secret) if kind_set else None
            after_view = _repo_view(built, wanted_secret)
        if after_view is None:
            continue
        cleanup_names = list(spec.cleanup_policies)
        after = {**after_view, 'cleanup_policies': cleanup_names, 'managed_by': MANAGED_BY}
        before = None
        if existing is not None:
            before = {
                **_repo_view(existing, stored_secret),
                'cleanup_policies': policy_names.get(existing.id, []),
                'managed_by': existing.managed_by,
            }
        secret_changed = resolved is not None and resolved != stored_secret
        config_changed = before is None or {k: v for k, v in before.items() if k != 'cleanup_policies'} != {
            k: v for k, v in after.items() if k != 'cleanup_policies'
        }

        async def run(
            spec: RepositorySpec = spec,
            resolved: str | None = resolved,
            secret_changed: bool = secret_changed,
            config_changed: bool = config_changed,
            new: bool = existing is None,
        ) -> AuditTarget:
            if new:
                repo = await repos_service.create(svc, spec.write(resolved), p.ctx.principal, managed_by=MANAGED_BY)
            else:
                fresh = await repos_db.get_by_name_ci(db, spec.name)
                if fresh is None:
                    raise NotFound('the repository was deleted meanwhile')
                repo = fresh
                if config_changed:
                    write = spec.write(resolved if secret_changed else None)
                    repo = await repos_service.update(svc, fresh, write, p.ctx.principal, managed_by=MANAGED_BY)
            await _attach(p, repo, spec.cleanup_policies)
            return audit.target('repository', repo.id, repo.name)

        p.steps.append(
            _change_step(
                'repository',
                spec.name,
                before,
                after,
                run,
                AuditAction.REPOSITORY_CREATE if existing is None else AuditAction.REPOSITORY_UPDATE,
                audit.target('repository', existing.id, existing.name) if existing is not None else None,
            )
        )
    _plan_repo_prunes(p, current, set(declared), prune)
    return specs


def _pending_group_view(
    p: _Planner, spec: RepositorySpec, prefix: str, declared: dict[str, RepositorySpec]
) -> dict[str, Any] | None:
    """A group whose members this file creates: checked against the file (the full check runs when it is applied)."""
    assert spec.group is not None
    ok = True
    for j, name in enumerate(spec.group.members):
        member = declared.get(name.lower())
        if member is None:
            continue  # an existing repository: checked by the API's rules at apply time
        if member.format != spec.format:
            p.errors.append(Problem(f'{prefix}.group.members.{j}', f'{member.name} is a {member.format} repository'))
            ok = False
        elif member.type is RepoType.GROUP:
            p.errors.append(Problem(f'{prefix}.group.members.{j}', 'groups cannot contain groups'))
            ok = False
    descriptor = p.svc.formats.descriptor(spec.format)
    if descriptor is None:
        p.errors.append(Problem(f'{prefix}.format', f'format {spec.format} is not available yet'))
        return None
    try:
        options = descriptor.parse_options(spec.format_options).model_dump(mode='json')
    except ValidationError as exc:
        for err in exc.errors(include_input=False, include_url=False):
            loc = '.'.join(str(x) for x in err['loc'])
            p.errors.append(Problem(f'{prefix}.format_options.{loc}', str(err['msg'])))
        return None
    if not ok:
        return None
    members = [declared[m.lower()].name if m.lower() in declared else m for m in spec.group.members]
    return {
        'online': spec.online,
        'description': spec.description,
        'proxy': None,
        'hosted': None,
        'group': {'members': members, 'writable_member': spec.group.writable_member},
        'storage': {'quota_bytes': None, 'quota_mode': 'soft'},
        'format_options': options,
    }


async def _attach(p: _Planner, repo: RepoConfig, names: list[str]) -> None:
    by_name = {str(d['name']).lower(): str(d['_id']) for d in await cleanup_db.list_policies(p.svc.db)}
    ids = [by_name[n.lower()] for n in names if n.lower() in by_name]
    if list(repo.cleanup_policy_ids) != ids:
        await cleanup_db.set_repository_policies(p.svc.db, repo.id, ids)
        p.svc.catalog.invalidate()


def _plan_repo_prunes(p: _Planner, current: list[RepoConfig], declared: set[str], prune: bool) -> None:
    managed = [r for r in current if r.managed_by == MANAGED_BY and r.name.lower() not in declared]
    managed.sort(key=lambda r: r.type is not RepoType.GROUP)  # groups first: members cannot go before them
    for repo in managed:
        target = audit.target('repository', repo.id, repo.name)
        if prune:

            async def delete(repo: RepoConfig = repo) -> AuditTarget:
                await repos_service.delete(p.svc, repo)
                return audit.target('repository', repo.id, repo.name)

            before = _repo_view(repo, None)
            p.prunes.append(_prune_step('repository', repo.name, before, delete, AuditAction.REPOSITORY_DELETE, target))
        else:

            async def release(repo: RepoConfig = repo) -> AuditTarget:
                fresh = await repos_db.get_by_name_ci(p.svc.db, repo.name)
                if fresh is not None:
                    await repos_db.replace(
                        p.svc.db,
                        fresh.model_copy(update={'managed_by': None, 'config_version': fresh.config_version + 1}),
                        expected_version=fresh.config_version,
                    )
                    p.svc.catalog.invalidate()
                return audit.target('repository', repo.id, repo.name)

            p.steps.append(_release_step('repository', repo.name, release, AuditAction.REPOSITORY_UPDATE, target))


# ---------------------------------------------------------------------------- local groups
async def _plan_groups(p: _Planner, specs: list[GroupSpec], prune: bool) -> None:
    db = p.svc.db
    local = [g for g in await groups_db.list_all(db) if g.source == 'local']
    by_name = {g.name.lower(): g for g in local}
    declared = {s.name.strip().lower() for s in specs}
    for i, spec in enumerate(specs):
        prefix = f'groups.{i}'
        name = spec.name.strip()
        if not groups_api.NAME.fullmatch(name):
            p.errors.append(Problem(f'{prefix}.name', 'letters, digits, spaces, ".", "_" and "-" (up to 64)'))
            continue
        roles = _check_roles(p, spec.roles, f'{prefix}.roles')
        existing = by_name.get(name.lower())
        after = {'name': name, 'description': spec.description.strip(), 'roles': roles, 'managed_by': MANAGED_BY}
        before = None
        if existing is not None:
            before = {
                'name': existing.name,
                'description': existing.description,
                'roles': existing.roles,
                'managed_by': existing.managed_by,
            }

        async def run(after: dict[str, Any] = after, existing: GroupRecord | None = existing) -> AuditTarget:
            now = datetime.now(UTC)
            if existing is None:
                group = GroupRecord(id=new_id(), created_at=now, updated_at=now, **after)
                await groups_db.insert(db, group)
            else:
                updated = await groups_db.update(db, existing.id, {**after, 'updated_at': now})
                if updated is None:
                    raise NotFound('the group was deleted meanwhile')
                group = updated
            await p.svc.rbac.changed()
            return audit.target('group', group.id, group.name)

        p.steps.append(
            _change_step(
                'group',
                name,
                before,
                after,
                run,
                AuditAction.GROUP_CREATE if existing is None else AuditAction.GROUP_UPDATE,
                audit.target('group', existing.id, existing.name) if existing is not None else None,
            )
        )
    for group in local:
        if group.managed_by != MANAGED_BY or group.name.lower() in declared:
            continue
        target = audit.target('group', group.id, group.name)
        if prune:

            async def delete(group: GroupRecord = group) -> AuditTarget:
                await ensure_admin_remains(p.svc, groups_api.without_group(group.id))
                if not await groups_db.delete(db, group.id):
                    raise NotFound('the group was deleted meanwhile')
                await users_db.remove_group(db, group.id)
                await p.svc.rbac.changed()
                return audit.target('group', group.id, group.name)

            before = group.model_dump(include=groups_api.AUDIT_FIELDS)
            p.prunes.append(_prune_step('group', group.name, before, delete, AuditAction.GROUP_DELETE, target))
        else:

            async def release(group: GroupRecord = group) -> AuditTarget:
                await groups_db.update(db, group.id, {'managed_by': None})
                return audit.target('group', group.id, group.name)

            p.steps.append(_release_step('group', group.name, release, AuditAction.GROUP_UPDATE, target))


# ---------------------------------------------------------------------------- service accounts
async def _plan_accounts(p: _Planner, specs: list[ServiceAccountSpec], prune: bool) -> None:
    db = p.svc.db
    accounts = await accounts_db.list_all(db)
    by_name = {a.name: a for a in accounts}
    declared = {s.name for s in specs}
    for i, spec in enumerate(specs):
        roles = _check_roles(p, spec.roles, f'service_accounts.{i}.roles')
        existing = by_name.get(spec.name)
        after = {
            'description': spec.description.strip(),
            'roles': roles,
            'disabled': spec.disabled,
            'managed_by': MANAGED_BY,
        }
        before = None
        if existing is not None:
            before = {
                'description': existing.description,
                'roles': existing.roles,
                'disabled': existing.disabled,
                'managed_by': existing.managed_by,
            }

        async def run(
            spec: ServiceAccountSpec = spec,
            after: dict[str, Any] = after,
            existing: ServiceAccountRecord | None = existing,
        ) -> AuditTarget:
            now = datetime.now(UTC)
            if existing is None:
                record = ServiceAccountRecord(id=new_id(), name=spec.name, created_at=now, **after)
                await accounts_db.insert(db, record)
            else:
                updated = await accounts_db.update(db, existing.id, {**after, 'updated_at': now})
                if updated is None:
                    raise NotFound('the service account was deleted meanwhile')
                record = updated
                await p.svc.rbac.changed()
                await p.svc.tokens.announce(REVOKE_ALL)  # cached token principals carry the account's roles
            return audit.target('service_account', record.id, record.name)

        p.steps.append(
            _change_step(
                'service_account',
                spec.name,
                before,
                after,
                run,
                AuditAction.SERVICE_ACCOUNT_CREATE if existing is None else AuditAction.SERVICE_ACCOUNT_UPDATE,
                audit.target('service_account', existing.id, existing.name) if existing is not None else None,
            )
        )
    for account in accounts:
        if account.managed_by != MANAGED_BY or account.name in declared:
            continue
        target = audit.target('service_account', account.id, account.name)
        if prune:

            async def delete(account: ServiceAccountRecord = account) -> AuditTarget:
                owner = TokenOwner(kind='service_account', id=account.id)
                await p.svc.tokens.revoke_all(owner, by=p.ctx.principal.id)
                await accounts_db.delete(db, account.id)
                await p.svc.rbac.changed()
                return audit.target('service_account', account.id, account.name)

            before = account.model_dump(include={'name', 'description', 'roles', 'disabled'})
            p.prunes.append(
                _prune_step('service_account', account.name, before, delete, AuditAction.SERVICE_ACCOUNT_DELETE, target)
            )
        else:

            async def release(account: ServiceAccountRecord = account) -> AuditTarget:
                await accounts_db.update(db, account.id, {'managed_by': None})
                return audit.target('service_account', account.id, account.name)

            p.steps.append(
                _release_step('service_account', account.name, release, AuditAction.SERVICE_ACCOUNT_UPDATE, target)
            )


# ---------------------------------------------------------------------------- sign-in providers
def _provider_write(spec: AuthProviderSpec, bind_password: str | None, client_secret: str | None) -> dict[str, Any]:
    data = spec.model_dump(mode='python', exclude={'ldap', 'oidc'})
    data['ldap'] = data['oidc'] = None
    if spec.ldap is not None:
        ldap = spec.ldap.model_dump(mode='python')
        ldap['connection']['bind_password'] = bind_password
        data['ldap'] = ldap
    if spec.oidc is not None:
        oidc = spec.oidc.model_dump(mode='python')
        oidc['client_secret'] = client_secret
        data['oidc'] = oidc
    return data


def _provider_current(svc: Services, record: ProviderRecord, groups: list[GroupRecord]) -> dict[str, Any]:
    view = providers_api.provider_view(svc, record, groups).model_dump(
        mode='json', exclude={'id', 'created_at', 'updated_at'}
    )
    if view.get('ldap'):
        view['ldap']['connection'].pop('bind_password_set', None)
        bind = _decrypted(lambda: decrypt_bind_password(svc.keyring, record))
        view['ldap']['connection']['bind_password'] = _digest(bind)
    if view.get('oidc'):
        for key in ('client_secret_set', 'redirect_uri', 'post_logout_redirect_uri'):
            view['oidc'].pop(key, None)
        view['oidc']['client_secret'] = _digest(_decrypted(lambda: decrypt_client_secret(svc.keyring, record)))
    view['managed_by'] = record.managed_by
    return view


def _provider_wanted(body: AuthProviderWrite, current: dict[str, Any] | None) -> dict[str, Any]:
    view = body.model_dump(mode='json')
    view['group_mapping'] = [
        {'external_group': row['external_group'], 'roles': list(dict.fromkeys(row['roles']))}
        for row in view['group_mapping']
    ]
    if view.get('ldap'):
        conn = view['ldap']['connection']
        given = conn.pop('bind_password', None)
        kept = ((current or {}).get('ldap') or {}).get('connection', {}).get('bind_password')
        conn['bind_password'] = (_digest(given) if given else kept) if conn.get('bind_dn') else None
    if view.get('oidc'):
        given = view['oidc'].pop('client_secret', None)
        kept = ((current or {}).get('oidc') or {}).get('client_secret')
        public = view['oidc'].get('client_auth') == OidcClientAuth.NONE.value
        view['oidc']['client_secret'] = None if public else (_digest(given) if given else kept)
    view['managed_by'] = MANAGED_BY
    return view


async def _plan_providers(p: _Planner, specs: list[AuthProviderSpec], prune: bool) -> None:
    svc, db = p.svc, p.svc.db
    records = await providers_db.list_all(db)
    by_name = {r.name.strip().lower(): r for r in records}
    declared = {s.name.strip().lower() for s in specs}
    for i, spec in enumerate(specs):
        prefix = f'auth_providers.{i}'
        bind = p.secrets.get(f'{prefix}.ldap.connection.bind_password')
        client = p.secrets.get(f'{prefix}.oidc.client_secret')
        body = _validated(AuthProviderWrite, _provider_write(spec, bind, client), prefix, p.errors)
        if body is None:
            continue
        existing = by_name.get(spec.name.strip().lower())
        if existing is not None and existing.kind is not body.kind:
            p.errors.append(Problem(f'{prefix}.kind', 'the kind of an existing provider cannot change'))
            continue
        groups = await groups_db.for_provider(db, existing.id) if existing is not None else []
        problems = providers_api.check_config(body, existing, keyring_set=svc.keyring is not None)
        for i_row, row in enumerate(body.group_mapping):
            _check_roles(p, row.roles, f'{prefix}.group_mapping.{i_row}.roles')
        problems += [
            e for e in providers_api.check_mapping(body.group_mapping, p.table) if not e.field.endswith('.roles')
        ]
        p.errors.extend(Problem(f'{prefix}.{e.field}', e.message) for e in problems)
        if problems:
            continue
        before = _provider_current(svc, existing, groups) if existing is not None else None
        after = _provider_wanted(body, before)
        bind_changed = bind is not None and (
            before is None or _digest(bind) != _get(before, 'ldap', 'connection', 'bind_password')
        )
        client_changed = client is not None and (
            before is None or _digest(client) != _get(before, 'oidc', 'client_secret')
        )

        async def run(
            spec: AuthProviderSpec = spec,
            bind: str | None = bind if bind_changed else None,
            client: str | None = client if client_changed else None,
            existing: ProviderRecord | None = existing,
        ) -> AuditTarget:
            write = AuthProviderWrite.model_validate(_provider_write(spec, bind, client))
            record = await providers_api.save(svc, write, existing, by=p.ctx.principal.display, managed_by=MANAGED_BY)
            return audit.target('auth_provider', record.id, record.name)

        p.steps.append(
            _change_step(
                'auth_provider',
                spec.name.strip(),
                before,
                after,
                run,
                AuditAction.AUTH_PROVIDER_CREATE if existing is None else AuditAction.AUTH_PROVIDER_UPDATE,
                audit.target('auth_provider', existing.id, existing.name) if existing is not None else None,
            )
        )
    for record in records:
        if record.managed_by != MANAGED_BY or record.name.strip().lower() in declared:
            continue
        target = audit.target('auth_provider', record.id, record.name)
        if prune:

            async def delete(record: ProviderRecord = record) -> AuditTarget:
                if not await providers_db.delete(db, record.id):
                    raise NotFound('the provider was deleted meanwhile')
                await groups_db.delete_for_provider(db, record.id)
                await providers_api.providers_changed(svc)
                return audit.target('auth_provider', record.id, record.name)

            before = _provider_current(svc, record, await groups_db.for_provider(db, record.id))
            p.prunes.append(
                _prune_step('auth_provider', record.name, before, delete, AuditAction.AUTH_PROVIDER_DELETE, target)
            )
        else:

            async def release(record: ProviderRecord = record) -> AuditTarget:
                await providers_db.set_fields(db, record.id, {'managed_by': None})
                svc.auth_providers.invalidate()
                return audit.target('auth_provider', record.id, record.name)

            p.steps.append(
                _release_step('auth_provider', record.name, release, AuditAction.AUTH_PROVIDER_UPDATE, target)
            )


def _get(doc: dict[str, Any] | None, *path: str) -> Any:
    value: Any = doc
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


# ---------------------------------------------------------------------------- webhooks
def _headers_digest(headers: dict[str, str] | None) -> str | None:
    return _digest(json.dumps(headers, sort_keys=True)) if headers else None


def _webhook_current(svc: Services, doc: Document) -> dict[str, Any]:
    hook_id = str(doc['_id'])
    secret = None
    if doc.get('secret_enc') and svc.keyring is not None:
        enc = str(doc['secret_enc'])
        secret = _decrypted(lambda: svc.keyring.decrypt_str(enc, aad=webhooks_api.secret_aad(hook_id)))  # type: ignore[union-attr]
    headers: dict[str, str] | None = None
    header_digest: str | None = None
    if doc.get('headers_enc') and svc.keyring is not None:
        enc = str(doc['headers_enc'])
        raw = _decrypted(lambda: svc.keyring.decrypt_str(enc, aad=webhooks_api.headers_aad(hook_id)))  # type: ignore[union-attr]
        try:
            headers = json.loads(raw) if raw and raw != UNREADABLE else None
        except ValueError:
            headers = None
        header_digest = _headers_digest(headers) if headers is not None else _digest(UNREADABLE)
    return {
        'description': doc.get('description', ''),
        'url': doc.get('url'),
        'events': list(doc.get('events') or []),
        'repositories': list(doc.get('repositories') or []),
        'enabled': bool(doc.get('enabled', True)),
        'allow_private': bool(doc.get('allow_private')),
        'timeout_s': doc.get('timeout_s'),
        'max_attempts': doc.get('max_attempts'),
        'header_names': list(doc.get('header_names') or []),
        'secret': _digest(secret),
        'header_secrets': header_digest,
        'managed_by': doc.get('managed_by'),
    }


async def _plan_webhooks(p: _Planner, specs: list[WebhookSpec], prune: bool) -> None:
    svc, db = p.svc, p.svc.db
    docs = await webhooks_db.list_webhooks(db)
    by_name = {str(d['name']).lower(): d for d in docs}
    declared = {s.name.lower() for s in specs}
    for i, spec in enumerate(specs):
        prefix = f'webhooks.{i}'
        secret = p.secrets.get(f'{prefix}.secret')
        headers = {
            name: p.secrets[f'{prefix}.headers.{name}']
            for name in spec.headers
            if f'{prefix}.headers.{name}' in p.secrets
        }
        if secret is None or len(headers) != len(spec.headers):
            continue  # an unresolved reference, already reported
        data = {**spec.model_dump(mode='python', exclude={'secret', 'headers'}), 'secret': secret, 'headers': headers}
        body = _validated(webhooks_api.WebhookWrite, data, prefix, p.errors)
        if body is None:
            continue
        existing = by_name.get(spec.name.lower())
        try:
            webhooks_api.validate(svc, body, creating=existing is None)
        except FieldErrors as exc:
            p.errors.extend(_problems(exc, prefix))
            continue
        before = _webhook_current(svc, existing) if existing is not None else None
        after = {
            'description': body.description,
            'url': body.url,
            'events': list(dict.fromkeys(body.events)),
            'repositories': list(dict.fromkeys(body.repositories)),
            'enabled': body.enabled,
            'allow_private': body.allow_private,
            'timeout_s': body.timeout_s,
            'max_attempts': body.max_attempts,
            'header_names': sorted(headers, key=str.lower),
            'secret': _digest(secret),
            'header_secrets': _headers_digest(headers),
            'managed_by': MANAGED_BY,
        }
        secret_changed = before is None or before['secret'] != after['secret']
        headers_changed = before is None or (before['header_secrets'], before['header_names']) != (
            after['header_secrets'],
            after['header_names'],
        )

        async def run(
            body: webhooks_api.WebhookWrite = body,
            existing: Document | None = existing,
            secret_changed: bool = secret_changed,
            headers_changed: bool = headers_changed,
        ) -> AuditTarget:
            write = body.model_copy(
                update={
                    'secret': body.secret if secret_changed else None,
                    'headers': body.headers if headers_changed else None,
                }
            )
            now = datetime.now(UTC)
            if existing is None:
                hook_id = new_id()
                doc: Document = {
                    '_id': hook_id,
                    'header_names': [],
                    'headers_enc': None,
                    **webhooks_api.stored_fields(svc, write, hook_id),
                    'managed_by': MANAGED_BY,
                    'created_at': now,
                    'created_by': p.ctx.principal.id,
                    'updated_at': now,
                    'updated_by': p.ctx.principal.id,
                }
                await webhooks_db.insert_webhook(db, doc)
            else:
                hook_id = str(existing['_id'])
                fields = {
                    **webhooks_api.stored_fields(svc, write, hook_id),
                    'managed_by': MANAGED_BY,
                    'updated_at': now,
                    'updated_by': p.ctx.principal.id,
                }
                stored = await webhooks_db.update_webhook(db, hook_id, fields)
                if stored is None:
                    raise NotFound('the webhook was deleted meanwhile')
                doc = stored
            svc.webhooks.invalidate()
            return audit.target('webhook', hook_id, str(doc.get('name')))

        p.steps.append(
            _change_step(
                'webhook',
                spec.name,
                before,
                after,
                run,
                AuditAction.WEBHOOK_CREATE if existing is None else AuditAction.WEBHOOK_UPDATE,
                audit.target('webhook', str(existing['_id']), str(existing['name'])) if existing is not None else None,
            )
        )
    for doc in docs:
        if doc.get('managed_by') != MANAGED_BY or str(doc['name']).lower() in declared:
            continue
        target = audit.target('webhook', str(doc['_id']), str(doc['name']))
        if prune:

            async def delete(doc: Document = doc) -> AuditTarget:
                if not await webhooks_db.delete_webhook(db, str(doc['_id'])):
                    raise NotFound('the webhook was deleted meanwhile')
                svc.webhooks.invalidate()
                return audit.target('webhook', str(doc['_id']), str(doc['name']))

            before = webhooks_api.audit_view(doc)
            p.prunes.append(
                _prune_step('webhook', str(doc['name']), before, delete, AuditAction.WEBHOOK_DELETE, target)
            )
        else:

            async def release(doc: Document = doc) -> AuditTarget:
                await webhooks_db.update_webhook(db, str(doc['_id']), {'managed_by': None})
                svc.webhooks.invalidate()
                return audit.target('webhook', str(doc['_id']), str(doc['name']))

            p.steps.append(_release_step('webhook', str(doc['name']), release, AuditAction.WEBHOOK_UPDATE, target))


# ---------------------------------------------------------------------------- settings
async def _plan_settings(p: _Planner, spec: SettingsSpec, declared_repos: list[RepositorySpec] | None) -> None:
    svc, db = p.svc, p.svc.db
    by = p.ctx.by
    if spec.instance_name is not None:
        doc = await settings_db.get(db, 'general') or {}
        current = doc.get('instance_name') or svc.settings.instance_name
        name = spec.instance_name

        async def general(name: str = name) -> AuditTarget:
            await svc.instance.save_general(name, by)
            return audit.target('settings', 'general', 'general')

        p.steps.append(
            _change_step(
                'settings',
                'general',
                {'instance_name': current},
                {'instance_name': name},
                general,
                AuditAction.SETTINGS_UPDATE,
                audit.target('settings', 'general', 'general'),
            )
        )
    if spec.require_totp_for_admins is not None:
        doc = await settings_db.get(db, 'security') or {}
        wanted = spec.require_totp_for_admins

        async def security(wanted: bool = wanted) -> AuditTarget:
            await svc.instance.save_security(wanted, by)
            return audit.target('settings', 'security', 'security')

        p.steps.append(
            _change_step(
                'settings',
                'security',
                {'require_totp_for_admins': doc.get('require_totp_for_admins') is True},
                {'require_totp_for_admins': wanted},
                security,
                AuditAction.SETTINGS_UPDATE,
                audit.target('settings', 'security', 'security'),
            )
        )
    if spec.anonymous_access is not None:
        roles = list(dict.fromkeys(spec.anonymous_access))
        for i, role in enumerate(roles):
            if role != ANONYMOUS_ROLE and not access.is_role(role, p.table):
                p.errors.append(Problem(f'settings.anonymous_access.{i}', f'unknown role {role!r}'))
                continue
            grants = access.materialise((role,), p.table)
            if grants.is_admin or access.global_actions(grants):
                p.errors.append(
                    Problem(f'settings.anonymous_access.{i}', 'anonymous access can only hold repository permissions')
                )
        stored = await roles_db.anonymous_roles(db)
        current = stored if stored is not None else [ANONYMOUS_ROLE]

        async def anonymous(roles: list[str] = roles) -> AuditTarget:
            await roles_db.set_anonymous_roles(db, roles)
            await svc.rbac.changed()
            return audit.target(*ANONYMOUS_TARGET)

        p.steps.append(
            _change_step(
                'settings',
                'anonymous_access',
                {'roles': current},
                {'roles': roles},
                anonymous,
                AuditAction.ROLE_UPDATE,
                audit.target(*ANONYMOUS_TARGET),
            )
        )
    if spec.storage_guard is not None:
        guard = spec.storage_guard
        now_guard = await storage_guard.load(db, default_min_free_bytes=svc.settings.min_free_bytes)
        saved = storage_guard.GuardSettings(
            min_free_bytes=guard.min_free_bytes, min_free_percent=guard.min_free_percent
        )

        async def save_guard(saved: storage_guard.GuardSettings = saved) -> AuditTarget:
            svc.guard.apply(await storage_guard.save(db, saved, by=p.ctx.principal.id))
            return audit.target('settings', 'storage', 'Low-disk guard')

        p.steps.append(
            _change_step(
                'settings',
                'storage_guard',
                {'min_free_bytes': now_guard.min_free_bytes, 'min_free_percent': now_guard.min_free_percent},
                {'min_free_bytes': guard.min_free_bytes, 'min_free_percent': float(guard.min_free_percent)},
                save_guard,
                AuditAction.SETTINGS_UPDATE,
                audit.target('settings', 'storage', 'Low-disk guard'),
            )
        )
    if spec.default_docker_repository is not None and not any(
        r.name.lower() == spec.default_docker_repository.lower() for r in declared_repos or []
    ):
        await _plan_default_docker(p, spec.default_docker_repository)


async def _plan_default_docker(p: _Planner, name: str) -> None:
    """The default docker repository when the file does not declare that repository (else its options say it)."""
    svc = p.svc
    current = await repos_db.list_all(svc.db)
    repo = next((r for r in current if r.name.lower() == name.lower()), None)
    if repo is None or repo.format != 'docker':
        p.errors.append(Problem('settings.default_docker_repository', f'there is no docker repository {name!r}'))
        return
    default = next((r.name for r in current if r.format == 'docker' and r.format_options.get('default_repo')), None)

    async def run(repo_name: str = repo.name) -> AuditTarget:
        fresh = await repos_db.get_by_name_ci(svc.db, repo_name)
        if fresh is None:
            raise NotFound('the repository was deleted meanwhile')
        write = existing_write(fresh, {**fresh.format_options, 'default_repo': True})
        await repos_service.update(svc, fresh, write, p.ctx.principal)
        return audit.target('repository', fresh.id, fresh.name)

    p.steps.append(
        _change_step(
            'settings',
            'default_docker_repository',
            {'repository': default},
            {'repository': repo.name},
            run,
            AuditAction.REPOSITORY_UPDATE,
            audit.target('repository', repo.id, repo.name),
        )
    )


def existing_write(repo: RepoConfig, format_options: dict[str, Any]) -> RepositoryWrite:
    """The write body that saves ``repo`` as it is (the stored upstream secret kept) with other format options."""
    proxy = repos_api.proxy_view(repo)
    return RepositoryWrite(
        name=repo.name,
        format=repo.format,
        type=repo.type,
        online=repo.online,
        description=repo.description,
        proxy=proxy,
        hosted=repos_api.hosted_view(repo),
        group=repos_api.group_view(repo),
        storage=None
        if repo.type is RepoType.GROUP
        else StorageConfig(quota_bytes=repo.storage.quota_bytes, quota_mode=repo.storage.quota_mode),
        format_options=format_options,
    )


# ---------------------------------------------------------------------------- plan / apply
def _default_docker(p: _Planner, doc: ProvisioningFile) -> list[RepositorySpec] | None:
    """Fold ``settings.default_docker_repository`` into the declared repository's format options."""
    repos = doc.repositories
    wanted = doc.settings.default_docker_repository if doc.settings else None
    if repos is None or wanted is None:
        return repos
    out = []
    for i, spec in enumerate(repos):
        options = dict(spec.format_options or {})
        if spec.name.lower() == wanted.lower():
            if spec.format != 'docker':
                p.errors.append(
                    Problem('settings.default_docker_repository', f'{spec.name} is not a docker repository')
                )
            options['default_repo'] = True
            spec = spec.model_copy(update={'format_options': options})
        elif options.get('default_repo'):
            p.errors.append(
                Problem(f'repositories.{i}.format_options.default_repo', f'settings make {wanted} the default instead')
            )
        out.append(spec)
    return out


async def _make_plan(ctx: Context, loaded: Loaded) -> tuple[ProvisioningPlan, list[Step]]:
    result = ProvisioningPlan(file=loaded.path, sha256=loaded.sha256, valid=False)
    if not loaded.valid:
        result.errors = [ProvisioningProblem(field=e.field, message=e.message) for e in loaded.errors]
        return result, []
    doc = loaded.document
    assert doc is not None
    p = _Planner(ctx=ctx, doc=doc)
    ctx.svc.catalog.invalidate()
    await _resolve_all(p)
    prune = doc.prune
    if doc.roles is not None:
        await _plan_roles(p, doc.roles, prune.roles)
    else:
        await _role_table(p)
    policies = {str(d['name']).lower(): str(d['format']) for d in await cleanup_db.list_policies(ctx.svc.db)}
    if doc.cleanup_policies is not None:
        before = len(p.prunes)
        await _plan_policies(p, doc.cleanup_policies, prune.cleanup_policies)
        for step in p.prunes[before:]:
            policies.pop(step.item.name.lower(), None)
        policies.update({s.name.lower(): s.format for s in doc.cleanup_policies})
    repos = _default_docker(p, doc)
    if repos is not None:
        await _plan_repositories(p, repos, prune.repositories, policies)
    if doc.groups is not None:
        await _plan_groups(p, doc.groups, prune.groups)
    if doc.service_accounts is not None:
        await _plan_accounts(p, doc.service_accounts, prune.service_accounts)
    if doc.auth_providers is not None:
        await _plan_providers(p, doc.auth_providers, prune.auth_providers)
    if doc.webhooks is not None:
        await _plan_webhooks(p, doc.webhooks, prune.webhooks)
    if doc.settings is not None:
        await _plan_settings(p, doc.settings, repos)
    steps = p.steps + list(reversed(p.prunes))
    result.items = [s.item for s in steps]
    result.errors = [ProvisioningProblem(field=e.field, message=e.message) for e in p.errors]
    result.valid = not p.errors
    result.summary = _summary(result.items)
    return result, steps


def _summary(items: list[ProvisioningItem]) -> ProvisioningSummary:
    summary = ProvisioningSummary()
    for item in items:
        setattr(summary, item.action, getattr(summary, item.action) + 1)
        if item.outcome == 'failed':
            summary.failed += 1
    return summary


async def plan(ctx: Context, loaded: Loaded) -> ProvisioningPlan:
    """What applying the file would change (nothing is written)."""
    result, _ = await _make_plan(ctx, loaded)
    return result


def _error_text(exc: Exception) -> str:
    if isinstance(exc, FieldErrors):
        return '; '.join(f'{e.field}: {e.message}' if e.field else e.message for e in exc.errors) or exc.detail
    if isinstance(exc, WaystationError):
        return exc.detail
    return 'internal error (see the server log)'


async def apply(ctx: Context, loaded: Loaded) -> ProvisioningPlan:
    """Plan, then make the changes (only when the whole file is valid). Records the outcome (``status``)."""
    async with _lock:
        result, steps = await _make_plan(ctx, loaded)
        if result.valid:
            details = {'source': 'provisioning', 'file_sha256': loaded.sha256}
            for step in steps:
                if step.run is None or step.action is None:
                    continue
                try:
                    target = await step.run()
                except Exception as exc:
                    if not isinstance(exc, WaystationError):
                        log.exception(
                            'provisioning step failed', extra={'kind': step.item.kind, 'item': step.item.name}
                        )
                    step.item.outcome, step.item.error = 'failed', _error_text(exc)
                    _record(ctx, step, step.target, details, Outcome.FAILURE, step.item.error)
                    continue
                step.item.outcome = 'applied'
                _record(ctx, step, target or step.target, details, Outcome.SUCCESS, None)
            result.applied = True
            result.summary = _summary(result.items)
        await _save_status(ctx, result)
        log.info(
            'provisioning file applied' if result.applied else 'provisioning file invalid: nothing applied',
            extra={'sha256': loaded.sha256, **result.summary.model_dump()},
        )
        return result


def _record(
    ctx: Context, step: Step, target: AuditTarget | None, details: dict[str, Any], outcome: Outcome, reason: str | None
) -> None:
    assert step.action is not None
    audit.record(
        step.action,
        outcome=outcome,
        target=target,
        actor=ctx.actor,
        reason=reason,
        details=details,
        changes=step.changes,
        writer=ctx.writer,
        notifier=ctx.notifier,
    )


async def _save_status(ctx: Context, result: ProvisioningPlan) -> None:
    outcome = 'invalid' if not result.valid else 'failed' if result.summary.failed else 'applied'
    record = ProvisioningLastApply(
        at=datetime.now(UTC), by=ctx.by, sha256=result.sha256, outcome=outcome, summary=result.summary
    )
    try:
        await settings_db.put(ctx.svc.db, STATUS_SECTION, record.model_dump(mode='json', exclude={'at', 'by'}), ctx.by)
    except Exception as exc:  # the apply happened; a missing status record must not fail it
        log.warning('provisioning status not saved', extra={'error': type(exc).__name__})


async def last_apply(svc: Services) -> ProvisioningLastApply | None:
    doc = await settings_db.get(svc.db, STATUS_SECTION)
    if not doc or not isinstance(doc.get('updated_at'), datetime):
        return None
    try:
        return ProvisioningLastApply.model_validate(
            {**doc, 'at': doc['updated_at'], 'by': doc.get('updated_by') or '?'}
        )
    except ValidationError:
        return None
