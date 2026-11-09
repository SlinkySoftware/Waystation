"""Format routing (ARCHITECTURE §4 step 1, SPECIFICATION §4.4, ADR 0003).

- ``/repository/<repo>/<path>``: every format, Nexus-compatible. The repository name matches case-sensitively.
- ``/v2/<name>/...``: the Docker registry API. The first segment of ``<name>`` is a docker repository's name, else a
  docker repository's upstream alias (``docker.io``, ``ghcr.io``...), else the request goes to the default docker
  repository with the whole name (mirror mode).

The router resolves the repository, normalises the path (``waystation_common.paths``), authorises the request and
hands it to the format plugin. Unknown repositories answer the format's native 404.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Request
from starlette.responses import Response

from waystation_common.errors import NotFound, PathRejected
from waystation_common.paths import normalise
from waystation_server import registry_auth
from waystation_server.formats.base import FormatRequest, Root
from waystation_server.problems import native_error, oci_error
from waystation_server.repos.model import RepoConfig
from waystation_server.security import access
from waystation_server.security.actions import Action
from waystation_server.security.authn import authenticate
from waystation_server.security.principal import AuthMethod
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


async def _dispatch(request: Request, repo: RepoConfig, root: Root, path: str) -> Response:
    services = services_of(request)
    request.state.log.update(repo=repo.name, format=repo.format)
    action = access.method_action(request.method)
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
        return oci_error(404, 'catalog is not available yet', code='UNSUPPORTED', headers=DOCKER_API_VERSION)
    route = await resolve_docker(services, clean)
    if route is None:
        return oci_error(404, 'repository name not known to registry', headers=DOCKER_API_VERSION)
    if not registry_auth.token_allows(principal, route.repo.name, access.method_action(request.method)):
        return registry_auth.challenge(services, scope=scope, error='insufficient_scope')
    return await _dispatch(request, route.repo, 'v2', route.path)
