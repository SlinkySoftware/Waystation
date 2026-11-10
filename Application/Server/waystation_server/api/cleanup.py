"""Cleanup policies (SPECIFICATION §4.8; OpenAPI tag ``cleanup``).

- ``/cleanup-policies``: named, reusable policies. Creating, changing, deleting and running them needs ``admin``;
  reading them needs ``admin`` or ``manage`` on some repository (to pick policies to attach). Others get 403 on the
  list and 404 on a single policy.
- ``/cleanup-policies/preview``: the dry run. It evaluates the criteria with ``waystation_common.cleanup`` (the code a
  run uses) on the repositories' current rows, so the preview equals what a run would remove now. Large
  repositories are read up to ``PREVIEW_CAP`` assets each (``truncated``); runs read everything.
- ``/repositories/{name}/cleanup``: the policies attached to a repository (``manage`` on it). Only policies for the
  repository's format (or ``*``) can be attached.
- ``/cleanup-policies/{id}/run`` enqueues a ``cleanup.run`` job; scheduled runs come from the policy's
  ``job_definitions`` entry (``cleanup:<id>``); both are listed by ``/cleanup-policies/{id}/runs`` (``job_runs``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from waystation_common.cleanup import Candidate, Criteria, check_schedule, plan_repository
from waystation_common.db import cleanup as cleanup_db
from waystation_common.db import jobs as jobs_db
from waystation_common.db.client import Document
from waystation_common.errors import NotFound, ValidationFailed
from waystation_common.ids import new_id
from waystation_common.jobs import CleanupRun
from waystation_common.jobs.schedule import parse_schedule
from waystation_server import audit
from waystation_server.api.deps import ServicesDep
from waystation_server.audit import AuditAction
from waystation_server.problems import FieldError, FieldErrors
from waystation_server.repos.model import RepoConfig
from waystation_server.repos.schemas import NAME_PATTERN, Format
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate
from waystation_server.security.principal import Principal
from waystation_server.services import Services

router = APIRouter(tags=['cleanup'])

PROBLEM = {'content': {'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}}}
PREVIEW_CAP = 100_000  # assets (and docker tags/manifests) read per repository for a preview
MAX_ATTACHED = 20
PolicyId = Annotated[str, Path(min_length=1, max_length=64)]
CleanupFormat = Literal[
    '*',
    'docker',
    'npm',
    'apt',
    'raw',
    'yum',
    'pypi',
    'maven',
    'go',
    'helm',
    'terraform',
    'galaxy',
    'swift',
    'nuget',
    'cargo',
    'rubygems',
    'composer',
    'conda',
    'apk',
    'huggingface',
]
RunStatus = Literal['running', 'succeeded', 'failed', 'dead', 'interrupted']


# ---------------------------------------------------------------------------- schemas
class CleanupPolicyWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(pattern=NAME_PATTERN)
    description: str = Field(default='', max_length=500)
    format: CleanupFormat
    criteria: Criteria
    schedule: str | None = Field(
        default=None,
        max_length=100,
        description='When it runs: 5-field UTC cron (`0 3 * * *`) or a macro such as `@daily`; null = manual only',
    )
    enabled: bool = Field(default=True, description='Scheduled runs happen only while enabled')

    @field_validator('schedule')
    @classmethod
    def _schedule(cls, value: str | None) -> str | None:
        try:
            return check_schedule(value)
        except ValueError as exc:
            raise ValueError(f'not a valid schedule: {exc}') from None


class JobProgress(BaseModel):
    done: int
    total: int | None
    message: str | None


class CleanupRunSummary(BaseModel):
    id: str
    status: RunStatus
    trigger: Literal['schedule', 'manual']
    started_at: datetime
    finished_at: datetime | None
    progress: JobProgress
    removed: int
    bytes: int
    error: str | None


class CleanupRunList(BaseModel):
    items: list[CleanupRunSummary]


class CleanupPolicy(BaseModel):
    id: str
    name: str
    description: str
    format: CleanupFormat
    criteria: Criteria
    schedule: str | None
    enabled: bool
    repositories: list[str] = Field(description='Attached repositories the caller can see')
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None
    last_run: CleanupRunSummary | None


class CleanupPolicyList(BaseModel):
    items: list[CleanupPolicy]


class CleanupPreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    format: CleanupFormat
    criteria: Criteria
    policy_id: str | None = Field(default=None, max_length=64)
    repositories: list[Annotated[str, Field(max_length=64)]] | None = Field(default=None, max_length=100)
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=50, ge=0, le=500)


class CleanupCandidate(BaseModel):
    repository: str
    kind: Literal['asset', 'tag', 'manifest', 'layer']
    name: str
    version: str | None
    path: str
    size: int
    created_at: datetime | None
    last_downloaded_at: datetime | None
    reason: str


class CleanupRepositoryTotal(BaseModel):
    name: str
    format: Format
    count: int
    bytes: int
    truncated: bool


class CleanupPreview(BaseModel):
    count: int
    bytes: int
    truncated: bool
    repositories: list[CleanupRepositoryTotal]
    items: list[CleanupCandidate]
    next_cursor: str | None


class RepositoryCleanup(BaseModel):
    repository: str
    policies: list[CleanupPolicy]


class RepositoryCleanupUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    policy_ids: list[Annotated[str, Field(max_length=64)]] = Field(max_length=MAX_ATTACHED)


class JobAccepted(BaseModel):
    job_id: str
    status: Literal['enqueued', 'merged', 'rerun', 'running']


# ---------------------------------------------------------------------------- helpers
def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def run_trigger(doc: Document) -> Literal['schedule', 'manual', 'retry']:
    """Why a ``job_runs`` document ran (runs recorded before the trigger existed: from ``definition_id``)."""
    match doc.get('trigger'):
        case 'schedule':
            return 'schedule'
        case 'manual':
            return 'manual'
        case 'retry':
            return 'retry'
    return 'schedule' if doc.get('definition_id') else 'manual'


def run_view(doc: Document) -> CleanupRunSummary:
    result = doc.get('result') or {}
    progress = doc.get('progress') or {}
    return CleanupRunSummary(
        id=str(doc['_id']),
        status=doc.get('status', 'running'),
        trigger='schedule' if run_trigger(doc) == 'schedule' else 'manual',
        started_at=doc['started_at'],
        finished_at=doc.get('finished_at'),
        progress=JobProgress(
            done=int(progress.get('done') or 0), total=progress.get('total'), message=progress.get('message')
        ),
        removed=int(result.get('removed') or 0),
        bytes=int(result.get('bytes') or 0),
        error=doc.get('error'),
    )


def _next_run(policy: Document, definition: Document | None, now: datetime) -> datetime | None:
    schedule = policy.get('schedule')
    if not schedule or not policy.get('enabled', True):
        return None
    if definition is not None and definition.get('next_run_at') is not None:
        return _aware(definition['next_run_at'])
    try:
        return datetime.fromtimestamp(parse_schedule(schedule).next_after(now.timestamp()), UTC)
    except ValueError:
        return None


async def _views(svc: Services, principal: Principal, policies: list[Document]) -> list[CleanupPolicy]:
    if not policies:
        return []
    ids = [str(p['_id']) for p in policies]
    definitions = await cleanup_db.schedules(svc.db, ids)
    attached: dict[str, list[str]] = {i: [] for i in ids}
    for repo in await svc.catalog.all():
        if not access.can_see(principal, repo):
            continue
        for policy_id in repo.cleanup_policy_ids:
            if policy_id in attached:
                attached[policy_id].append(repo.name)
    runs = await asyncio.gather(*(jobs_db.runs_by_dedupe(svc.db, cleanup_db.dedupe_key(i), limit=1) for i in ids))
    now = datetime.now(UTC)
    out = []
    for policy, last in zip(policies, runs, strict=True):
        policy_id = str(policy['_id'])
        out.append(
            CleanupPolicy(
                id=policy_id,
                name=policy['name'],
                description=policy.get('description', ''),
                format=policy['format'],
                criteria=Criteria.model_validate(policy.get('criteria') or {}),
                schedule=policy.get('schedule'),
                enabled=bool(policy.get('enabled', True)),
                repositories=sorted(attached[policy_id], key=str.lower),
                created_at=policy['created_at'],
                updated_at=policy['updated_at'],
                next_run_at=_next_run(policy, definitions.get(policy_id), now),
                last_run=run_view(last[0]) if last else None,
            )
        )
    return out


AUDIT_FIELDS = ('name', 'description', 'format', 'criteria', 'schedule', 'enabled')


def audit_view(policy: Document) -> dict[str, Any]:
    """What the audit trail compares for a policy."""
    return {key: policy.get(key) for key in AUDIT_FIELDS}


def policy_target(policy: Document) -> audit.AuditTarget:
    return audit.target('cleanup_policy', str(policy['_id']), policy.get('name'))


def _may_read(principal: Principal) -> bool:
    return access.check(principal, Action.ADMIN) or access.check(principal, Action.MANAGE)


def _check_criteria(fmt: str, criteria: Criteria, prefix: str = 'criteria') -> None:
    errors: list[FieldError] = []
    if not criteria.selects_anything:
        errors.append(
            FieldError(field=prefix, message='set at least one criterion that selects content', code='required')
        )
    if criteria.untagged_older_than_days is not None and fmt not in ('docker', '*'):
        errors.append(
            FieldError(
                field=f'{prefix}.untagged_older_than_days', message='only docker policies have tags', code='invalid'
            )
        )
    if errors:
        raise FieldErrors(errors)


async def _policy_or_404(svc: Services, principal: Principal, policy_id: str) -> Document:
    if not _may_read(principal):
        raise NotFound('Not Found')
    policy = await cleanup_db.get_policy(svc.db, policy_id)
    if policy is None:
        raise NotFound('Not Found')
    return policy


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip('=')


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        value = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)).decode()
    except binascii.Error, UnicodeDecodeError:
        raise ValidationFailed('invalid cursor') from None
    if not value.isdigit():
        raise ValidationFailed('invalid cursor')
    return int(value)


async def plan_for(
    svc: Services, repo: RepoConfig, criteria: Criteria, now: datetime, *, cap: int | None = PREVIEW_CAP
) -> tuple[list[Candidate], bool]:
    """The dry run for one repository: (what a run would remove, whether the read stopped at ``cap``)."""
    data = await cleanup_db.load_repo_data(svc.db, repo.id, repo.format, cap=cap)
    plan = await asyncio.to_thread(plan_repository, data, criteria, now)
    return plan, data.truncated


async def _preview_repos(svc: Services, principal: Principal, body: CleanupPreviewRequest) -> list[RepoConfig]:
    if body.repositories:
        repos: list[RepoConfig] = []
        for i, name in enumerate(body.repositories):
            repo = await svc.catalog.get(name) or await svc.catalog.get_ci(name)
            if repo is None or not access.can_see(principal, repo):
                raise NotFound('repository not found')
            access.require_repo(principal, Action.MANAGE, repo)
            if body.format not in ('*', repo.format):
                raise FieldErrors(
                    [FieldError(field=f'repositories.{i}', message=f'is a {repo.format} repository', code='format')]
                )
            if repo not in repos:
                repos.append(repo)
        return repos
    access.require(principal, Action.ADMIN)
    if body.policy_id:
        if await cleanup_db.get_policy(svc.db, body.policy_id) is None:
            raise NotFound('Not Found')
        return [r for r in await svc.catalog.all() if body.policy_id in r.cleanup_policy_ids]
    return [r for r in await svc.catalog.all() if body.format in ('*', r.format)]


# ---------------------------------------------------------------------------- policies
@router.get('/cleanup-policies', response_model=CleanupPolicyList, operation_id='listCleanupPolicies')
@access.handler_checked
async def list_policies(request: Request, svc: ServicesDep) -> CleanupPolicyList:
    principal = await authenticate(request)
    if not _may_read(principal):
        access.require(principal, Action.ADMIN)  # 401/403
    return CleanupPolicyList(items=await _views(svc, principal, await cleanup_db.list_policies(svc.db)))


@router.post(
    '/cleanup-policies',
    status_code=201,
    response_model=CleanupPolicy,
    operation_id='createCleanupPolicy',
    responses={409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.ADMIN)
async def create_policy(
    body: CleanupPolicyWrite, request: Request, response: Response, svc: ServicesDep
) -> CleanupPolicy:
    principal = await authenticate(request)
    _check_criteria(body.format, body.criteria)
    now = datetime.now(UTC)
    doc: Document = {
        '_id': new_id(),
        **body.model_dump(mode='python', exclude={'criteria'}),
        'criteria': body.criteria.model_dump(mode='json', exclude_defaults=True),
        'created_at': now,
        'created_by': principal.id,
        'updated_at': now,
        'updated_by': principal.id,
    }
    await cleanup_db.insert_policy(svc.db, doc)
    await cleanup_db.sync_schedule(svc.db, doc)
    audit.record(
        AuditAction.CLEANUP_POLICY_CREATE, target=policy_target(doc), changes=audit.diff(None, audit_view(doc))
    )
    response.headers['Location'] = f'/api/v1/cleanup-policies/{doc["_id"]}'
    return (await _views(svc, principal, [doc]))[0]


@router.get('/cleanup-policies/{policy_id}', response_model=CleanupPolicy, operation_id='getCleanupPolicy')
@access.handler_checked
async def get_policy(policy_id: PolicyId, request: Request, svc: ServicesDep) -> CleanupPolicy:
    principal = await authenticate(request)
    policy = await _policy_or_404(svc, principal, policy_id)
    return (await _views(svc, principal, [policy]))[0]


@router.put(
    '/cleanup-policies/{policy_id}',
    response_model=CleanupPolicy,
    operation_id='updateCleanupPolicy',
    responses={409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.ADMIN)
async def update_policy(
    policy_id: PolicyId, body: CleanupPolicyWrite, request: Request, svc: ServicesDep
) -> CleanupPolicy:
    principal = await authenticate(request)
    existing = await _policy_or_404(svc, principal, policy_id)
    _check_criteria(body.format, body.criteria)
    if body.format != existing['format'] and body.format != '*':
        clash = [
            r.name for r in await svc.catalog.all() if policy_id in r.cleanup_policy_ids and r.format != body.format
        ]
        if clash:
            raise FieldErrors(
                [FieldError(field='format', message=f'attached to {", ".join(clash[:3])}', code='attached')]
            )
    fields: dict[str, Any] = {
        **body.model_dump(mode='python', exclude={'criteria'}),
        'criteria': body.criteria.model_dump(mode='json', exclude_defaults=True),
        'updated_at': datetime.now(UTC),
        'updated_by': principal.id,
    }
    policy = await cleanup_db.update_policy(svc.db, policy_id, fields)
    if policy is None:
        raise NotFound('Not Found')
    await cleanup_db.sync_schedule(svc.db, policy)
    if changes := audit.diff(audit_view(existing), audit_view(policy)):
        audit.record(AuditAction.CLEANUP_POLICY_UPDATE, target=policy_target(policy), changes=changes)
    return (await _views(svc, principal, [policy]))[0]


@router.delete('/cleanup-policies/{policy_id}', status_code=204, operation_id='deleteCleanupPolicy')
@access.requires(Action.ADMIN)
async def delete_policy(policy_id: PolicyId, request: Request, svc: ServicesDep) -> Response:
    existing = await cleanup_db.get_policy(svc.db, policy_id)
    if existing is None or not await cleanup_db.delete_policy(svc.db, policy_id):
        raise NotFound('Not Found')
    svc.catalog.invalidate()
    audit.record(
        AuditAction.CLEANUP_POLICY_DELETE, target=policy_target(existing), changes=audit.diff(audit_view(existing), None)
    )
    return Response(status_code=204)


@router.post(
    '/cleanup-policies/{policy_id}/run', status_code=202, response_model=JobAccepted, operation_id='runCleanupPolicy'
)
@access.requires(Action.ADMIN)
async def run_policy(policy_id: PolicyId, request: Request, svc: ServicesDep) -> JobAccepted:
    principal = await authenticate(request)
    await _policy_or_404(svc, principal, policy_id)
    result = await svc.jobs.enqueue(CleanupRun(policy_id=policy_id))
    return JobAccepted(job_id=result.job_id, status=result.status)


@router.get('/cleanup-policies/{policy_id}/runs', response_model=CleanupRunList, operation_id='listCleanupRuns')
@access.handler_checked
async def list_runs(policy_id: PolicyId, request: Request, svc: ServicesDep) -> CleanupRunList:
    principal = await authenticate(request)
    await _policy_or_404(svc, principal, policy_id)
    docs = await jobs_db.runs_by_dedupe(svc.db, cleanup_db.dedupe_key(policy_id), limit=50)
    return CleanupRunList(items=[run_view(d) for d in docs])


@router.post(
    '/cleanup-policies/preview',
    response_model=CleanupPreview,
    operation_id='previewCleanup',
    responses={404: PROBLEM, 422: PROBLEM},
)
@access.handler_checked
async def preview(body: CleanupPreviewRequest, request: Request, svc: ServicesDep) -> CleanupPreview:
    principal = await authenticate(request)
    repos = await _preview_repos(svc, principal, body)
    _check_criteria(body.format, body.criteria)
    offset = _decode_cursor(body.cursor)
    now = datetime.now(UTC)
    repos.sort(key=lambda r: r.name.lower())
    plans = await asyncio.gather(*(plan_for(svc, r, body.criteria, now) for r in repos))
    totals: list[CleanupRepositoryTotal] = []
    rows: list[tuple[str, Candidate]] = []
    for repo, (plan, truncated) in zip(repos, plans, strict=True):
        totals.append(
            CleanupRepositoryTotal(
                name=repo.name,
                format=repo.format,
                count=len(plan),
                bytes=sum(c.size for c in plan),
                truncated=truncated,
            )
        )
        rows.extend((repo.name, c) for c in plan)
    page = rows[offset : offset + body.limit] if body.limit else []
    end = offset + len(page)
    return CleanupPreview(
        count=len(rows),
        bytes=sum(t.bytes for t in totals),
        truncated=any(t.truncated for t in totals),
        repositories=totals,
        items=[
            CleanupCandidate(
                repository=name,
                kind=c.kind,
                name=c.name,
                version=c.version,
                path=c.path,
                size=c.size,
                created_at=c.created_at,
                last_downloaded_at=c.last_downloaded_at,
                reason=c.reason,
            )
            for name, c in page
        ],
        next_cursor=_encode_cursor(end) if body.limit and end < len(rows) else None,
    )


# ---------------------------------------------------------------------------- attachment
async def _repo_view(svc: Services, principal: Principal, repo: RepoConfig) -> RepositoryCleanup:
    policies = await cleanup_db.get_policies(svc.db, list(repo.cleanup_policy_ids))
    return RepositoryCleanup(repository=repo.name, policies=await _views(svc, principal, policies))


@router.get('/repositories/{name}/cleanup', response_model=RepositoryCleanup, operation_id='getRepositoryCleanup')
@access.requires_repo(Action.MANAGE)
async def get_repository_cleanup(name: str, request: Request, svc: ServicesDep) -> RepositoryCleanup:
    principal = await authenticate(request)
    return await _repo_view(svc, principal, request.state.repo)


@router.put(
    '/repositories/{name}/cleanup',
    response_model=RepositoryCleanup,
    operation_id='setRepositoryCleanup',
    responses={422: PROBLEM},
)
@access.requires_repo(Action.MANAGE)
async def set_repository_cleanup(
    name: str, body: RepositoryCleanupUpdate, request: Request, svc: ServicesDep
) -> RepositoryCleanup:
    principal = await authenticate(request)
    repo: RepoConfig = request.state.repo
    ids = list(dict.fromkeys(body.policy_ids))
    found = {str(p['_id']): p for p in await cleanup_db.get_policies(svc.db, ids)}
    errors: list[FieldError] = []
    for i, policy_id in enumerate(ids):
        policy = found.get(policy_id)
        if policy is None:
            errors.append(FieldError(field=f'policy_ids.{i}', message='no such cleanup policy', code='unknown'))
        elif policy['format'] not in ('*', repo.format):
            errors.append(FieldError(field=f'policy_ids.{i}', message=f'is a {policy["format"]} policy', code='format'))
    if errors:
        raise FieldErrors(errors)
    if not await cleanup_db.set_repository_policies(svc.db, repo.id, ids):
        raise NotFound('repository not found')
    svc.catalog.invalidate()
    updated = await svc.catalog.by_id(repo.id)
    return await _repo_view(svc, principal, updated or repo)
