"""``/api/v1/repositories``: list, create, read, replace, delete, test upstream, "Set me up" (OpenAPI contract)."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field

from waystation_common.errors import ValidationFailed
from waystation_server import audit
from waystation_server.api.auth import registry_host
from waystation_server.api.deps import ServicesDep
from waystation_server.audit import AuditAction
from waystation_server.formats.base import SetupClient, SetupContext
from waystation_server.proxy import probe as probe_mod
from waystation_server.repos import service
from waystation_server.repos.model import RepoConfig, RepoType
from waystation_server.repos.schemas import (
    Format,
    ProxyConfig,
    RepositoryStatus,
    RepositoryWrite,
    UpstreamAuth,
    UpstreamTestRequest,
)
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate
from waystation_server.services import Services
from waystation_server.stats import repos as repo_stats

router = APIRouter(tags=['repositories'])

PROBLEM = {'content': {'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}}}


class RepositorySummaryStats(BaseModel):
    size_bytes: int
    artifact_count: int
    hit_ratio_24h: float | None = Field(ge=0, le=1, description='null when there were no requests')
    requests_24h: int
    bytes_served_24h: int
    bytes_saved_24h: int = 0
    requests_spark_24h: list[int] = Field(description='Requests per hour, oldest first (24 values)')
    last_activity_at: datetime | None


class Repository(BaseModel):
    name: str
    format: Format
    type: RepoType
    online: bool
    description: str = ''
    status: RepositoryStatus
    url: str = Field(description='Client URL, `<base>/repository/<name>/`')
    proxy: ProxyConfig | None = None
    format_options: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    stats: RepositorySummaryStats


class RepositoryPage(BaseModel):
    items: list[Repository]
    next_cursor: str | None
    total: int = Field(description='Matching repositories across all pages')


class UpstreamCheck(BaseModel):
    name: str
    ok: bool
    message: str


class RateLimit(BaseModel):
    limit: int
    remaining: int
    window_s: int | None = None
    reset_at: datetime | None = None
    source: str | None = None


class UpstreamTestResult(BaseModel):
    reachable: bool
    http_status: int | None = None
    latency_ms: float | None
    auth: Literal['anonymous', 'authenticated', 'failed', 'not_required'] | None = None
    tls: str | None = None
    checks: list[UpstreamCheck]
    rate_limit: RateLimit | None = None


class SetupGuide(BaseModel):
    repository: str
    format: Format
    clients: list[SetupClient]
    notes: list[str] = Field(default_factory=list)


def _proxy_view(repo: RepoConfig) -> ProxyConfig | None:
    if repo.proxy is None:
        return None
    p = repo.proxy
    return ProxyConfig(
        upstream_url=p.upstream_url,
        auth=UpstreamAuth(kind=p.auth.kind, username=p.auth.username, secret_set=bool(p.auth.secret_enc)),
        metadata_max_age_s=p.metadata_max_age_s,
        content_max_age_s=p.content_max_age_s,
        negative_ttl_s=p.negative_ttl_s,
        stale_on_error=p.stale_on_error,
        offline=p.offline,
        allow_private_upstream=p.allow_private_upstream,
    )


def to_view(svc: Services, repo: RepoConfig, status: str, summary: repo_stats.RepoSummary) -> Repository:
    return Repository(
        name=repo.name,
        format=repo.format,
        type=repo.type,
        online=repo.online,
        description=repo.description,
        status=status,
        url=f'{svc.base_url}/repository/{repo.name}/',
        proxy=_proxy_view(repo),
        format_options=repo.format_options,
        created_at=repo.created_at,
        updated_at=repo.updated_at,
        stats=RepositorySummaryStats(**summary.__dict__),
    )


def _target(repo: RepoConfig) -> audit.AuditTarget:
    return audit.target('repository', repo.id, repo.name)


async def _view(svc: Services, repo: RepoConfig) -> Repository:
    status = (await repo_stats.statuses(svc, [repo]))[repo.id]
    summary = (await repo_stats.summaries(svc, [repo]))[repo.id]
    return to_view(svc, repo, status, summary)


def _encode_cursor(name: str) -> str:
    return base64.urlsafe_b64encode(name.lower().encode()).decode().rstrip('=')


def _decode_cursor(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)).decode()
    except binascii.Error, UnicodeDecodeError:
        raise ValidationFailed('invalid cursor') from None


@router.get('/repositories', response_model=RepositoryPage, operation_id='listRepositories')
@access.filtered(Action.BROWSE)
async def list_repositories(
    request: Request,
    svc: ServicesDep,
    format: Format | None = None,
    type: RepoType | None = None,
    status: RepositoryStatus | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> RepositoryPage:
    principal = await authenticate(request)
    repos = access.filter_repos(principal, Action.BROWSE, await svc.catalog.all())
    if format:
        repos = [r for r in repos if r.format == format]
    if type:
        repos = [r for r in repos if r.type is type]
    if q:
        needle = q.lower()
        repos = [r for r in repos if needle in r.name.lower() or needle in r.description.lower()]
    statuses = await repo_stats.statuses(svc, repos)
    if status:
        repos = [r for r in repos if statuses[r.id] == status]
    total = len(repos)
    if cursor:
        after = _decode_cursor(cursor)
        repos = [r for r in repos if r.name.lower() > after]
    page = repos[:limit]
    summaries = await repo_stats.summaries(svc, page)
    next_cursor = _encode_cursor(page[-1].name) if len(repos) > limit else None
    return RepositoryPage(
        items=[to_view(svc, r, statuses[r.id], summaries[r.id]) for r in page], next_cursor=next_cursor, total=total
    )


@router.post(
    '/repositories',
    status_code=201,
    response_model=Repository,
    operation_id='createRepository',
    responses={409: PROBLEM, 422: PROBLEM},
)
@access.requires(Action.ADMIN)
async def create_repository(
    body: RepositoryWrite, request: Request, response: Response, svc: ServicesDep
) -> Repository:
    principal = await authenticate(request)
    repo = await service.create(svc, body, principal)
    audit.record(AuditAction.REPOSITORY_CREATE, target=_target(repo), changes=audit.diff(None, repo.to_doc()))
    response.headers['Location'] = f'/api/v1/repositories/{repo.name}'
    return await _view(svc, repo)


@router.post(
    '/repositories/test-upstream',
    response_model=UpstreamTestResult,
    operation_id='testUpstream',
    responses={422: PROBLEM},
)
@access.handler_checked
async def test_upstream(body: UpstreamTestRequest, request: Request, svc: ServicesDep) -> UpstreamTestResult:
    principal = await authenticate(request)
    existing: RepoConfig | None = None
    if body.repository:
        existing = await svc.catalog.get(body.repository) or await svc.catalog.get_ci(body.repository)
        if existing is None:
            access.require(principal, Action.ADMIN)
        else:
            access.require_repo(principal, Action.MANAGE, existing)
    else:
        access.require(principal, Action.ADMIN)
    descriptor = svc.formats.descriptor(body.format)
    if descriptor is None:
        raise ValidationFailed(f'format {body.format} is not available yet')
    try:
        options = descriptor.parse_options(body.format_options)
    except ValueError as exc:
        raise ValidationFailed(f'invalid format_options: {exc}') from None
    auth = body.proxy.auth
    secret = auth.secret
    if secret is None and existing is not None and existing.proxy is not None and existing.proxy.auth.kind is auth.kind:
        secret = service.decrypt_secret(svc, existing.id, existing.proxy.auth)
    result = await probe_mod.probe(
        svc.upstream,
        upstream_url=body.proxy.upstream_url,
        probe_path=descriptor.probe_path(options),
        allow_private=body.proxy.allow_private_upstream,
        headers=probe_mod.auth_headers(auth.kind.value, auth.username, secret),
    )
    outcome = UpstreamTestResult(
        reachable=result.reachable,
        http_status=result.http_status,
        latency_ms=result.latency_ms,
        auth=result.auth,
        tls=result.tls,
        checks=[UpstreamCheck(name=c.name, ok=c.ok, message=c.message) for c in result.checks],
    )
    audit.record(
        AuditAction.REPOSITORY_TEST_UPSTREAM,
        target=audit.target('repository', existing.id, existing.name) if existing else audit.target('upstream'),
        details={'format': body.format, 'upstream_url': body.proxy.upstream_url, 'reachable': result.reachable},
    )
    return outcome


@router.get('/repositories/{name}', response_model=Repository, operation_id='getRepository')
@access.requires_repo(Action.BROWSE)
async def get_repository(name: str, request: Request, svc: ServicesDep) -> Repository:
    repo: RepoConfig = request.state.repo
    return await _view(svc, repo)


@router.put(
    '/repositories/{name}',
    response_model=Repository,
    operation_id='updateRepository',
    responses={409: PROBLEM, 422: PROBLEM},
)
@access.requires_repo(Action.MANAGE)
async def update_repository(name: str, body: RepositoryWrite, request: Request, svc: ServicesDep) -> Repository:
    principal = await authenticate(request)
    before: RepoConfig = request.state.repo
    repo = await service.update(svc, before, body, principal)
    audit.record(
        AuditAction.REPOSITORY_UPDATE, target=_target(repo), changes=audit.diff(before.to_doc(), repo.to_doc())
    )
    return await _view(svc, repo)


@router.delete('/repositories/{name}', status_code=204, operation_id='deleteRepository')
@access.requires_repo(Action.MANAGE)
async def delete_repository(name: str, request: Request, svc: ServicesDep) -> Response:
    principal = await authenticate(request)
    access.require(principal, Action.ADMIN)
    repo: RepoConfig = request.state.repo
    await service.delete(svc, repo)
    audit.record(AuditAction.REPOSITORY_DELETE, target=_target(repo), changes=audit.diff(repo.to_doc(), None))
    return Response(status_code=204)


@router.get('/repositories/{name}/setup', response_model=SetupGuide, operation_id='getSetupGuide')
@access.requires_repo(Action.BROWSE)
async def get_setup_guide(name: str, request: Request, svc: ServicesDep) -> SetupGuide:
    repo: RepoConfig = request.state.repo
    principal = await authenticate(request)
    descriptor = svc.formats.descriptor(repo.format)
    clients: list[SetupClient] = []
    notes: list[str] = []
    if descriptor is not None:
        ctx = SetupContext(
            repo=repo,
            base_url=svc.base_url,
            registry_host=registry_host(svc.base_url),
            username=None if principal.is_anonymous else principal.name,
        )
        clients = descriptor.setup_snippets(ctx)
        notes = list(descriptor.notes)
    if svc.formats.plugin(repo.format) is None:
        notes.append(f'The {repo.format} protocol is not served yet: these snippets work once its plugin lands.')
    return SetupGuide(repository=repo.name, format=repo.format, clients=clients, notes=notes)
