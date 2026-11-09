"""Import repositories from Nexus Repository 3 (SPECIFICATION §4.12): a dry-run report, then apply.

``preview`` reads the Nexus configuration (``nexus_client``), maps every repository (``nexus_mapping``) and plans it
against this Waystation:

- ``create``: the repository would be created with ``repository`` (already checked by the same validation as the
  repositories API);
- ``exists``: a repository with that name already exists (case-insensitively): never changed; ``differences`` lists
  the fields where the import would differ from it;
- ``unsupported`` / ``invalid``: with the reason.

Docker routing: an upstream alias already used (by an existing repository or an earlier import) or equal to a
repository name is dropped with a difference. The default docker repository (unprefixed names, mirror mode) is the
Nexus repository that received unprefixed pulls when that can be told: the one the request names, else a Docker Hub
proxy served by a Nexus connector (directly or as the first Docker Hub member of a connector group), else the only
Docker Hub proxy. It is never changed when Waystation already has one.

``apply`` reads Nexus again (nothing from the preview is kept, credentials included), and creates the selected
``create`` items through ``repos.service.create`` (members before groups), each audited as ``repository.create``.
It is idempotent: a name that exists by then is reported ``exists`` and left alone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from waystation_common.errors import Conflict, WaystationError
from waystation_server import audit
from waystation_server.audit import AuditAction
from waystation_server.migration import nexus_client
from waystation_server.migration.nexus_client import NexusSource
from waystation_server.migration.nexus_mapping import Mapped, map_repository
from waystation_server.problems import FieldError, FieldErrors, validation_errors
from waystation_server.proxy.http import Network, check_ca_pem
from waystation_server.repos import service
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.repos.schemas import Format, RepositoryWrite
from waystation_server.security.principal import Principal
from waystation_server.services import Services

log = logging.getLogger('waystation.migration.nexus')

Outcome = Literal['created', 'exists', 'unsupported', 'invalid', 'failed', 'not_selected']


class NexusImportRequest(BaseModel):
    """Where to read from. The credentials are used for this request only: never stored, logged or returned."""

    model_config = ConfigDict(extra='forbid')

    url: str = Field(
        max_length=2048,
        description='Nexus base URL (with its context path, if any), e.g. `https://nexus.example.com`',
        json_schema_extra={'format': 'uri'},
    )
    username: str = Field(min_length=1, max_length=256, description='A Nexus administrator (reads the configuration)')
    password: SecretStr = Field(min_length=1, max_length=1024, json_schema_extra={'writeOnly': True})
    allow_private: bool = Field(
        default=False, description='Allow a Nexus on a private or loopback address (SSRF guard exception)'
    )
    ca_pem: str | None = Field(
        default=None, max_length=262144, description='Extra CA certificates (PEM) trusted for the Nexus TLS certificate'
    )
    docker_aliases: bool = Field(
        default=True,
        description='Give each docker proxy its upstream host as an alias (`docker.io` for Docker Hub, `ghcr.io`, ...)',
    )
    default_docker_repo: str | None = Field(
        default=None,
        max_length=63,
        description='Nexus docker repository to make the default docker repository; null = detect it',
    )


class NexusApplyRequest(NexusImportRequest):
    repositories: list[str] | None = Field(
        default=None,
        max_length=1000,
        description='Names to import (from the preview); null = every repository the preview would create',
    )


class ImportDifference(BaseModel):
    field: str
    nexus: str | None
    waystation: str | None
    note: str


class NexusImportItem(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    name: str
    nexus_format: str
    nexus_type: str
    format: Format | None = None
    type: RepoType | None = None
    action: Literal['create', 'exists', 'unsupported', 'invalid']
    outcome: Outcome | None = Field(default=None, description='Apply only: what happened')
    reason: str | None = None
    repository: RepositoryWrite | None = Field(
        default=None,
        description='The Waystation repository: created when `action` is `create`; for `exists` what the import '
        'would have created',
    )
    differences: list[ImportDifference] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DefaultDockerChoice(BaseModel):
    name: str | None = Field(description='Repository that receives unprefixed docker names after the import')
    reason: str


class ImportSummary(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    total: int = 0
    create: int = 0
    exists: int = 0
    unsupported: int = 0
    invalid: int = 0
    created: int = 0
    failed: int = 0


class NexusImportReport(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    applied: bool
    nexus_version: str | None = Field(description='From the Nexus `Server` header, when it sends one')
    items: list[NexusImportItem]
    summary: ImportSummary
    default_docker_repo: DefaultDockerChoice | None = None


def source_of(body: NexusImportRequest) -> NexusSource:
    errors: list[FieldError] = []
    if problem := nexus_client.url_problem(body.url):
        errors.append(FieldError(field='url', message=problem, code='format'))
    ca_pem = (body.ca_pem or '').strip() or None
    if errors:
        raise FieldErrors(errors)
    return NexusSource(
        url=body.url.strip(),
        username=body.username,
        password=body.password.get_secret_value(),
        network=Network(allow_private=body.allow_private, ca_pem=ca_pem),
    )


async def _check_ca(body: NexusImportRequest) -> None:
    pem = (body.ca_pem or '').strip()
    if pem and (problem := await asyncio.to_thread(check_ca_pem, pem)):
        raise FieldErrors([FieldError(field='ca_pem', message=problem, code='format')])


# ---------------------------------------------------------------------------------------------------- planning
def _flatten(value: Any, prefix: str = '') -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, inner in value.items():
            out.update(_flatten(inner, f'{prefix}.{key}' if prefix else str(key)))
        return out
    return {prefix: value}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (list, tuple)):
        return ', '.join(str(v) for v in value)
    return str(value)[:500]


def _existing_view(repo: RepoConfig) -> dict[str, Any]:
    view: dict[str, Any] = {'format': repo.format, 'type': repo.type.value, 'online': repo.online}
    if repo.proxy is not None:
        p = repo.proxy
        view['proxy'] = {
            'upstream_url': p.upstream_url,
            'metadata_max_age_s': p.metadata_max_age_s,
            'content_max_age_s': p.content_max_age_s,
            'negative_ttl_s': p.negative_ttl_s,
            'offline': p.offline,
            'auto_block': {'enabled': p.auto_block.enabled},
        }
    group = getattr(repo, 'group', None)
    if group is not None:
        view['group'] = {'members': list(group.members), 'writable_member': group.writable_member}
    hosted = getattr(repo, 'hosted', None)
    if hosted is not None:
        view['hosted'] = {'write_policy': str(getattr(hosted.write_policy, 'value', hosted.write_policy))}
    view['format_options'] = dict(repo.format_options)
    return view


def _compare(m: Mapped, existing: RepoConfig) -> None:
    assert m.body is not None
    wanted = _flatten({k: v for k, v in m.body.items() if k not in ('name', 'description')})
    have = _flatten(_existing_view(existing))
    for key, value in wanted.items():
        current = have.get(key)
        if key.startswith('format_options.default_repo'):
            continue
        if _text(value) != _text(current) and not (value in (None, False, []) and current in (None, False, [])):
            m.differ(key, value, current, 'differs from the existing repository (left unchanged)')


def _supported(svc: Services) -> dict[str, frozenset[str]]:
    return {d.name: frozenset(t.value for t in d.types) for d in svc.formats.descriptors()}


def _aliases(repos: Iterable[RepoConfig]) -> dict[str, str]:
    return {
        str(a).lower(): r.name for r in repos if r.format == 'docker' for a in r.format_options.get('aliases') or []
    }


def _plan_aliases(items: list[Mapped], existing: list[RepoConfig]) -> None:
    taken = _aliases(existing)
    names = {r.name.lower() for r in existing} | {m.name.lower() for m in items if m.action == 'create'}
    # Docker Hub proxies claim docker.io before anything else does.
    for m in sorted(items, key=lambda i: not i.docker_hub):
        if m.action != 'create' or m.body is None or m.format != 'docker':
            continue
        options = m.body.get('format_options') or {}
        kept: list[str] = []
        for alias in options.get('aliases') or []:
            if alias in taken:
                m.differ('format_options.aliases', alias, None, f'the alias is already routed to {taken[alias]}')
            elif alias in names:
                m.differ('format_options.aliases', alias, None, 'the alias is a repository name')
            else:
                taken[alias] = m.name
                kept.append(alias)
        options['aliases'] = kept


def _plan_groups(items: list[Mapped], existing: list[RepoConfig]) -> None:
    have = {r.name.lower(): r for r in existing}
    planned = {m.name.lower(): m for m in items}
    for m in items:
        if m.action != 'create' or m.body is None or m.type != 'group':
            continue
        group = m.body['group']
        kept: list[str] = []
        for member in group['members']:
            current = have.get(member.lower())
            plan = planned.get(member.lower())
            if current is not None and current.format == m.format:
                kept.append(current.name)
            elif plan is not None and plan.action == 'create':
                kept.append(member)
            else:
                why = plan.reason if plan is not None and plan.reason else 'not imported'
                m.differ('group.members', member, None, f'member left out: {why}')
        if not kept:
            m.refuse('invalid', 'none of the group members can be imported')
            continue
        group['members'] = kept
        if group.get('writable_member') and group['writable_member'] not in kept:
            m.differ('group.writable_member', group.pop('writable_member'), None, 'the writable member is left out')


def _usable(m: Mapped) -> bool:
    return m.action in ('create', 'exists')


def _default_docker(
    items: list[Mapped], existing: list[RepoConfig], requested: str | None
) -> DefaultDockerChoice | None:
    docker = [m for m in items if m.format == 'docker']
    current = next((r for r in existing if r.format == 'docker' and r.format_options.get('default_repo')), None)
    if current is not None:
        return DefaultDockerChoice(
            name=current.name, reason=f'Waystation already routes unprefixed names to {current.name}; unchanged'
        )
    if not any(_usable(m) for m in docker):
        return None
    by_name = {m.name.lower(): m for m in docker}
    hub = [m for m in docker if m.docker_hub and m.type == 'proxy' and _usable(m)]
    chosen: Mapped | None = None
    reason = ''
    if requested:
        chosen = by_name.get(requested.lower())
        if chosen is None or chosen.type == 'hosted' or not _usable(chosen):
            return DefaultDockerChoice(
                name=None, reason=f'{requested} is not a docker proxy or group the import creates'
            )
        reason = 'chosen in the import request'
    else:
        # The repository behind a Nexus connector (a host root) received the unprefixed pulls.
        for m in (m for m in docker if m.connector):
            if m in hub:
                chosen, reason = m, 'its Nexus connector served unprefixed Docker Hub pulls'
                break
            members = [by_name[n.lower()] for n in m.members if n.lower() in by_name and by_name[n.lower()] in hub]
            if members:
                chosen, reason = members[0], f'the Docker Hub proxy behind the Nexus connector of group {m.name}'
                break
        if chosen is None and len(hub) == 1:
            chosen, reason = hub[0], 'the only Docker Hub proxy'
        if chosen is None:
            why = 'several Docker Hub proxies' if hub else 'no Docker Hub proxy'
            return DefaultDockerChoice(name=None, reason=f'not determinable ({why}): choose one in the request')
    if chosen.action == 'exists':
        return DefaultDockerChoice(
            name=None, reason=f'{chosen.name} already exists in Waystation: set it as the default there'
        )
    assert chosen.body is not None
    chosen.body.setdefault('format_options', {})['default_repo'] = True
    return DefaultDockerChoice(name=chosen.name, reason=reason)


async def _validate(svc: Services, principal: Principal, m: Mapped, pending: set[str]) -> RepositoryWrite | None:
    """The checked body, or None with the item marked invalid."""
    assert m.body is not None
    try:
        body = RepositoryWrite.model_validate(m.body)
    except ValidationError as exc:
        messages = [f'{e.field}: {e.message}' for e in validation_errors(exc.errors())]
        m.refuse('invalid', '; '.join(messages))
        return None
    # A group whose members are created first when applied is fully checked then.
    deferred = m.type == 'group' and any(n.lower() in pending for n in m.members)
    if not deferred and (errors := await service.check(svc, body, principal)):
        m.refuse('invalid', '; '.join(f'{e.field}: {e.message}' for e in errors))
        return None
    descriptor = svc.formats.descriptor(body.format)
    if descriptor is not None:  # show the options exactly as they will be stored (defaults filled in)
        try:
            options = descriptor.parse_options(body.format_options).model_dump(mode='json')
        except ValueError as exc:
            m.refuse('invalid', f'format_options: {exc}')
            return None
        body = body.model_copy(update={'format_options': options})
    return body


async def plan(
    svc: Services, principal: Principal, body: NexusImportRequest
) -> tuple[str | None, list[Mapped], dict[str, RepositoryWrite], DefaultDockerChoice | None]:
    await _check_ca(body)
    source = source_of(body)
    snapshot = await nexus_client.read(svc.upstream, source)
    supported = _supported(svc)
    items = [map_repository(r, supported=supported, aliases=body.docker_aliases) for r in snapshot.repositories]
    existing = await svc.catalog.all()
    by_lower = {r.name.lower(): r for r in existing}
    seen: set[str] = set()
    for m in items:
        lowered = m.name.lower()
        if lowered in seen:
            m.refuse('invalid', 'another Nexus repository has the same name (Waystation names ignore case)')
            continue
        seen.add(lowered)
        if m.action != 'create':
            continue
        if (current := by_lower.get(lowered)) is not None:
            m.action = 'exists'
            if m.format != current.format or m.type != current.type.value:
                m.reason = f'a {current.format} {current.type.value} repository named {current.name} already exists'
            else:
                m.reason = 'already in Waystation; left unchanged'
            _compare(m, current)
            try:  # what the import would have created, for reference
                m.shown = RepositoryWrite.model_validate(m.body)
            except ValidationError:
                m.shown = None
    _plan_aliases(items, existing)
    _plan_groups(items, existing)
    default = _default_docker(items, existing, body.default_docker_repo)
    pending = {m.name.lower() for m in items if m.action == 'create'}
    bodies: dict[str, RepositoryWrite] = {}
    for m in items:
        if m.action == 'create' and (checked := await _validate(svc, principal, m, pending)) is not None:
            bodies[m.name] = checked
    if (
        default is not None
        and default.name
        and default.name not in bodies
        and not any(r.name == default.name for r in existing)
    ):
        default = DefaultDockerChoice(name=None, reason=f'{default.name} cannot be imported')
    return snapshot.version, items, bodies, default


def _item(m: Mapped, body: RepositoryWrite | None, outcome: Outcome | None = None) -> NexusImportItem:
    return NexusImportItem(
        name=m.name,
        nexus_format=m.nexus_format,
        nexus_type=m.nexus_type,
        format=m.format,
        type=RepoType(m.type) if m.type else None,
        action=m.action,
        outcome=outcome,
        reason=m.reason,
        repository=body or m.shown,
        differences=[ImportDifference(**d.__dict__) for d in m.differences],
        notes=m.notes,
    )


def _summary(items: list[NexusImportItem]) -> ImportSummary:
    s = ImportSummary(total=len(items))
    for item in items:
        setattr(s, item.action, getattr(s, item.action) + 1)
        if item.outcome in ('created', 'failed'):
            setattr(s, item.outcome, getattr(s, item.outcome) + 1)
    return s


async def preview(svc: Services, principal: Principal, body: NexusImportRequest) -> NexusImportReport:
    version, items, bodies, default = await plan(svc, principal, body)
    out = [_item(m, bodies.get(m.name)) for m in items]
    return NexusImportReport(
        applied=False, nexus_version=version, items=out, summary=_summary(out), default_docker_repo=default
    )


async def apply(svc: Services, principal: Principal, body: NexusApplyRequest) -> NexusImportReport:
    version, items, bodies, default = await plan(svc, principal, body)
    selected = None if body.repositories is None else set(body.repositories)
    outcomes: dict[str, tuple[Outcome, str | None]] = {}
    order = sorted(items, key=lambda m: m.type == 'group')  # members before groups
    for m in order:
        if m.action != 'create':
            outcomes[m.name] = (m.action, m.reason)
            continue
        if selected is not None and m.name not in selected:
            outcomes[m.name] = ('not_selected', None)
            continue
        write = bodies.get(m.name)
        if write is None:
            outcomes[m.name] = ('invalid', m.reason)
            continue
        try:
            repo = await service.create(svc, write, principal)
        except Conflict:
            outcomes[m.name] = ('exists', 'created by someone else meanwhile; left unchanged')
            continue
        except FieldErrors as exc:
            outcomes[m.name] = ('failed', '; '.join(f'{e.field}: {e.message}' for e in exc.errors))
            continue
        except WaystationError as exc:
            outcomes[m.name] = ('failed', exc.detail)
            continue
        audit.record(
            AuditAction.REPOSITORY_CREATE,
            target=audit.target('repository', repo.id, repo.name),
            details={'source': 'nexus_import', 'nexus_format': m.nexus_format, 'nexus_type': m.nexus_type},
            changes=audit.diff(None, repo.to_doc()),
        )
        outcomes[m.name] = ('created', None)
    out: list[NexusImportItem] = []
    for m in items:
        outcome, reason = outcomes[m.name]
        item = _item(m, bodies.get(m.name), outcome)
        if reason and outcome in ('failed', 'exists') and m.action == 'create':
            item.reason = reason
        out.append(item)
    if default is not None and default.name in bodies and outcomes[default.name][0] != 'created':
        default = DefaultDockerChoice(name=None, reason=f'{default.name} was not created')
    created = sum(1 for o, _ in outcomes.values() if o == 'created')
    log.info('nexus import applied', extra={'created': created, 'total': len(items)})
    return NexusImportReport(
        applied=True, nexus_version=version, items=out, summary=_summary(out), default_docker_repo=default
    )
