"""Format routing (ARCHITECTURE §4 step 1, SPECIFICATION §4.4, ADR 0003).

- ``/repository/<repo>/<path>``: every format, Nexus-compatible. The repository name matches case-sensitively.
- ``/v2/<name>/...``: the Docker registry API. The first segment of ``<name>`` is a docker repository's name, else a
  docker repository's upstream alias (``docker.io``, ``ghcr.io``...), else the request goes to the default docker
  repository with the whole name (mirror mode).

The router resolves the repository, normalises the path (``waystation_common.paths``), authorises the request and
hands it to the format plugin. Unknown repositories answer the format's native 404.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from waystation_common.errors import NotFound, PathRejected
from waystation_common.paths import normalise
from waystation_server import registry_auth
from waystation_server.db import docker as docker_db
from waystation_server.formats.base import FormatRequest, Root
from waystation_server.problems import native_error, oci_error
from waystation_server.repos.model import RepoConfig
from waystation_server.security import access
from waystation_server.security.actions import Action
from waystation_server.security.authn import authenticate
from waystation_server.security.principal import AuthMethod, Principal
from waystation_server.services import Services

FORMAT_METHODS = ['GET', 'HEAD', 'PUT', 'POST', 'PATCH', 'DELETE']
DOCKER_API_VERSION = {'Docker-Distribution-API-Version': 'registry/2.0'}

router = APIRouter(include_in_schema=False)


def services_of(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


@dataclass(frozen=True)
class DockerRoute:
    repo: RepoConfig
    path: str  # '<image>/<operation>...' within the repository


async def resolve_docker(services: Services, name_path: str) -> DockerRoute | None:
    """Resolve ``/v2/<name_path>`` to a docker repository (SPECIFICATION §4.4)."""
    first, _, rest = name_path.partition('/')
    if rest:
        repo = await services.catalog.get(first)
        if repo is not None and repo.format == 'docker':
            return DockerRoute(repo, rest)
        repo = await services.catalog.docker_alias(first)
        if repo is not None:
            return DockerRoute(repo, rest)
    repo = await services.catalog.default_docker()
    if repo is not None:
        return DockerRoute(repo, name_path)
    return None


async def _resolve_name(services: Services, name: str) -> tuple[RepoConfig, str] | None:
    route = await resolve_docker(services, name)
    return (route.repo, route.path) if route is not None else None


async def _dispatch(
    request: Request, repo: RepoConfig, root: Root, path: str, *, action: Action | None = None
) -> Response:
    services = services_of(request)
    request.state.log.update(repo=repo.name, format=repo.format)
    action = action or access.method_action(request.method)
    descriptor = services.formats.descriptor(repo.format)
    if request.method == 'POST' and descriptor is not None and path in descriptor.read_posts:
        action = Action.READ  # a query sent as POST (npm audit), declared by the format
    principal = await access.authorize_repo(request, repo, action)
    if not repo.online:
        if root == 'v2':
            return oci_error(503, 'repository is offline', code='UNAVAILABLE', headers=DOCKER_API_VERSION)
        return native_error(503, 'Repository is offline')
    plugin = services.formats.plugin(repo.format)
    if plugin is None:
        raise NotFound('Not Found')
    req = FormatRequest(
        request=request,
        repo=repo,
        root=root,
        path=path,
        principal=principal,
        proxy=services.proxy,
        hosted=services.hosted,
        base_url=services.base_url,
        _require=lambda action: access.require_repo(principal, action, repo),
        _allows=lambda action, other: access.check(principal, action, other),
        _resolve=lambda name: _resolve_name(services, name),
        _repository=services.catalog.get,
        _allows_member=lambda action, member: access.check(
            dataclasses.replace(principal, token_access=None), action, member
        ),
    )
    return await plugin.handle(req)


@router.api_route('/repository/{repo_name}', methods=FORMAT_METHODS)
@router.api_route('/repository/{repo_name}/{path:path}', methods=FORMAT_METHODS)
@access.handler_checked
async def repository(request: Request, repo_name: str, path: str = '') -> Response:
    services = services_of(request)
    repo = await services.catalog.get(repo_name)
    if repo is None:
        return native_error(404, 'Not Found')
    try:
        clean = normalise(path, allow_empty=True)
    except PathRejected as exc:
        return native_error(400, exc.detail)
    return await _dispatch(request, repo, 'repository', clean)


CATALOG_DEFAULT_N = 1000
CATALOG_MAX_N = 10_000


async def catalog(request: Request, services: Services, principal: Principal) -> Response:
    """``GET /v2/_catalog`` (``n``/``last``, ``Link``): ``<repository>/<image>`` for every tagged image of the docker
    repositories the principal can see (RBAC with its API token limits; the registry token's per-repository grants do
    not apply here), by name."""
    n_raw, last = request.query_params.get('n'), request.query_params.get('last') or None
    if n_raw is not None and (not n_raw.isdigit() or int(n_raw) > CATALOG_MAX_N):
        return oci_error(
            400, 'invalid number of results requested', code='PAGINATION_NUMBER_INVALID', headers=DOCKER_API_VERSION
        )
    limit = int(n_raw) if n_raw is not None else CATALOG_DEFAULT_N
    viewer = dataclasses.replace(principal, token_access=None)
    repos = sorted(
        (r for r in await services.catalog.all() if r.format == 'docker' and access.can_see(viewer, r)),
        key=lambda r: r.name,
    )
    last_repo, _, last_image = (last or '').partition('/')
    names: list[str] = []
    for repo in repos:
        if len(names) > limit:
            break
        if last and repo.name < last_repo:
            continue
        after = last_image if last and repo.name == last_repo else None
        images = await docker_db.image_names(services.db, repo.id, after=after, limit=limit + 1 - len(names))
        names.extend(f'{repo.name}/{image}' for image in images)
    headers = dict(DOCKER_API_VERSION)
    if len(names) > limit:
        names = names[:limit]
        if names:
            headers['Link'] = f'</v2/_catalog?{urlencode({"n": limit, "last": names[-1]})}>; rel="next"'
    return JSONResponse({'repositories': names}, headers=headers)


async def v2_base(request: Request) -> Response:
    """``GET /v2/``: API version check (only reached with a valid registry token)."""
    return Response('{}', media_type='application/json', headers=DOCKER_API_VERSION)


@router.api_route('/v2', methods=FORMAT_METHODS)
@router.api_route('/v2/{name_path:path}', methods=FORMAT_METHODS)
@access.handler_checked
async def docker_v2(request: Request, name_path: str = '') -> Response:
    """Docker registry API. Requires a registry token (``registry_auth``): without one, or with one that does not
    cover the repository, the answer is the 401 challenge naming the scope to request."""
    services = services_of(request)
    try:
        clean = normalise(name_path, allow_empty=True)
    except PathRejected:
        return oci_error(400, 'invalid repository name', code='NAME_INVALID', headers=DOCKER_API_VERSION)
    principal = await authenticate(request)
    scope = registry_auth.request_scope(clean, request.method)
    if principal.method is not AuthMethod.REGISTRY_TOKEN:
        return registry_auth.challenge(services, scope=scope)
    if clean == '':
        return await v2_base(request)
    if clean == '_catalog':
        if not registry_auth.token_allows(principal, registry_auth.CATALOG, Action.BROWSE):
            return registry_auth.challenge(services, scope=scope, error='insufficient_scope')
        return await catalog(request, services, principal)
    route = await resolve_docker(services, clean)
    if route is None:
        return oci_error(404, 'repository name not known to registry', headers=DOCKER_API_VERSION)
    action = registry_auth.request_action(clean, request.method)
    if not registry_auth.token_allows(principal, route.repo.name, action):
        return registry_auth.challenge(services, scope=scope, error='insufficient_scope')
    return await _dispatch(request, route.repo, 'v2', route.path, action=action)
