"""``/api/v1/repositories/{name}/browse`` and the format-aware browse views (OpenAPI tag ``browse``).

Every route needs ``browse`` on the repository (yanking: ``write``; hidden repositories answer 404, ARCHITECTURE §7).
The generic folder listing works for every format; the format views (``/npm/*``, ``/apt/*``, ``/docker/*``,
``/maven/*``, ``/pypi/*``, ``/go/*``) answer 404 for a repository of another format, or whose plugin has no browser
yet. The REST layer reaches a format's views only through its registered plugin's ``browser`` attribute and the
protocols in ``waystation_server.browse.models`` (ARCHITECTURE §3).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from waystation_common.errors import NotFound
from waystation_server.api.auth import registry_host
from waystation_server.api.deps import ServicesDep
from waystation_server.browse import tree
from waystation_server.browse.models import (
    AptBrowser,
    AptPackagePage,
    AptSuite,
    BrowseContext,
    DockerBrowser,
    DockerImagePage,
    DockerManifest,
    DockerTagPage,
    FolderListing,
    GalaxyBrowser,
    GalaxyCollectionPage,
    GalaxyNamespacePage,
    GalaxyVersionPage,
    GoBrowser,
    GoModulePage,
    GoVersionPage,
    HelmBrowser,
    HelmChartPage,
    HelmVersionPage,
    MavenArtifactPage,
    MavenBrowser,
    MavenVersionPage,
    NpmBrowser,
    NpmPackagePage,
    NpmVersionPage,
    PypiBrowser,
    PypiProjectPage,
    PypiVersionPage,
    PypiYank,
    PypiYankResult,
    TerraformBrowser,
    TerraformProviderPage,
    TerraformVersionPage,
)
from waystation_server.repos.model import RepoConfig
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.services import Services

router = APIRouter(tags=['browse'])

PROBLEM = {'content': {'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}}}
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: PROBLEM}

Cursor = Annotated[str | None, Query(max_length=512, description="Opaque cursor from a previous page's `next_cursor`")]
Limit = Annotated[int, Query(ge=1, le=500)]


def _context(svc: Services, repo: RepoConfig) -> BrowseContext:
    return BrowseContext(
        repo=repo, db=svc.db, proxy=svc.proxy, base_url=svc.base_url, registry_host=registry_host(svc.base_url)
    )


def _browser(svc: Services, repo: RepoConfig, fmt: str) -> object:
    """The format plugin's browse views, when ``repo`` is of format ``fmt``."""
    browser = getattr(svc.formats.plugin(repo.format), 'browser', None) if repo.format == fmt else None
    if browser is None:
        raise NotFound(f'not a {fmt} repository')
    return browser


def _npm(svc: Services, repo: RepoConfig) -> NpmBrowser:
    browser = _browser(svc, repo, 'npm')
    if not isinstance(browser, NpmBrowser):
        raise NotFound('not an npm repository')
    return browser


def _apt(svc: Services, repo: RepoConfig) -> AptBrowser:
    browser = _browser(svc, repo, 'apt')
    if not isinstance(browser, AptBrowser):
        raise NotFound('not an apt repository')
    return browser


def _pypi(svc: Services, repo: RepoConfig) -> PypiBrowser:
    browser = _browser(svc, repo, 'pypi')
    if not isinstance(browser, PypiBrowser):
        raise NotFound('not a PyPI repository')
    return browser


def _go(svc: Services, repo: RepoConfig) -> GoBrowser:
    browser = _browser(svc, repo, 'go')
    if not isinstance(browser, GoBrowser):
        raise NotFound('not a Go repository')
    return browser


def _terraform(svc: Services, repo: RepoConfig) -> TerraformBrowser:
    browser = _browser(svc, repo, 'terraform')
    if not isinstance(browser, TerraformBrowser):
        raise NotFound('not a Terraform repository')
    return browser


def _docker(svc: Services, repo: RepoConfig) -> DockerBrowser:
    browser = _browser(svc, repo, 'docker')
    if not isinstance(browser, DockerBrowser):
        raise NotFound('not a docker repository')
    return browser


def _maven(svc: Services, repo: RepoConfig) -> MavenBrowser:
    browser = _browser(svc, repo, 'maven')
    if not isinstance(browser, MavenBrowser):
        raise NotFound('not a maven repository')
    return browser


@router.get('/repositories/{name}/browse', response_model=FolderListing, operation_id='browsePath', responses=NOT_FOUND)
@access.requires_repo(Action.BROWSE)
async def browse_path(
    name: str,
    request: Request,
    svc: ServicesDep,
    path: Annotated[
        str, Query(max_length=1024, description='Folder path relative to the repository root; empty = root')
    ] = '',
    cursor: Cursor = None,
    limit: Limit = 50,
) -> FolderListing:
    repo: RepoConfig = request.state.repo
    return await tree.list_folder(svc.db, repo.id, path, cursor=cursor, limit=limit)


# ---------------------------------------------------------------------------- npm
@router.get(
    '/repositories/{name}/npm/packages',
    response_model=NpmPackagePage,
    operation_id='listNpmPackages',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_npm_packages(
    name: str,
    request: Request,
    svc: ServicesDep,
    q: Annotated[str | None, Query(max_length=214)] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> NpmPackagePage:
    repo: RepoConfig = request.state.repo
    browser = _npm(svc, repo)
    return await browser.packages(_context(svc, repo), q=q, cursor=cursor, limit=limit)


@router.get(
    '/repositories/{name}/npm/versions',
    response_model=NpmVersionPage,
    operation_id='listNpmVersions',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_npm_versions(
    name: str,
    request: Request,
    svc: ServicesDep,
    package: Annotated[str, Query(max_length=214, description='Package name, scoped names as `@scope/name`')],
    cursor: Cursor = None,
    limit: Limit = 50,
) -> NpmVersionPage:
    repo: RepoConfig = request.state.repo
    browser = _npm(svc, repo)
    return await browser.versions(_context(svc, repo), package=package, cursor=cursor, limit=limit)


# ---------------------------------------------------------------------------- apt
@router.get(
    '/repositories/{name}/apt/suites',
    response_model=list[AptSuite],
    operation_id='listAptSuites',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_apt_suites(name: str, request: Request, svc: ServicesDep) -> list[AptSuite]:
    repo: RepoConfig = request.state.repo
    browser = _apt(svc, repo)
    return await browser.suites(_context(svc, repo))


@router.get(
    '/repositories/{name}/apt/packages',
    response_model=AptPackagePage,
    operation_id='listAptPackages',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_apt_packages(
    name: str,
    request: Request,
    svc: ServicesDep,
    suite: Annotated[str, Query(max_length=100)],
    component: Annotated[str | None, Query(max_length=100)] = None,
    arch: Annotated[str | None, Query(max_length=32)] = None,
    cached: Annotated[bool | None, Query(description='Only packages whose `.deb` is in the cache')] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> AptPackagePage:
    repo: RepoConfig = request.state.repo
    browser = _apt(svc, repo)
    return await browser.packages(
        _context(svc, repo),
        suite=suite,
        component=component,
        arch=arch,
        cached=cached,
        q=q,
        cursor=cursor,
        limit=limit,
    )


# ---------------------------------------------------------------------------- docker
Image = Annotated[str, Query(max_length=255, description='Image name within the repository, e.g. `library/nginx`')]


@router.get(
    '/repositories/{name}/docker/images',
    response_model=DockerImagePage,
    operation_id='listDockerImages',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_docker_images(
    name: str,
    request: Request,
    svc: ServicesDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> DockerImagePage:
    repo: RepoConfig = request.state.repo
    browser = _docker(svc, repo)
    return await browser.images(_context(svc, repo), q=q, cursor=cursor, limit=limit)


@router.get(
    '/repositories/{name}/docker/tags',
    response_model=DockerTagPage,
    operation_id='listDockerTags',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_docker_tags(
    name: str, request: Request, svc: ServicesDep, image: Image, cursor: Cursor = None, limit: Limit = 50
) -> DockerTagPage:
    repo: RepoConfig = request.state.repo
    browser = _docker(svc, repo)
    return await browser.tags(_context(svc, repo), image=image, cursor=cursor, limit=limit)


@router.get(
    '/repositories/{name}/docker/manifest',
    response_model=DockerManifest,
    operation_id='getDockerManifest',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def get_docker_manifest(
    name: str,
    request: Request,
    svc: ServicesDep,
    image: Image,
    reference: Annotated[str, Query(max_length=200, description='Tag or `sha256:` digest')],
    platform: Annotated[
        str | None, Query(max_length=64, description='For an index, select one platform (`os/arch[/variant]`)')
    ] = None,
) -> DockerManifest:
    repo: RepoConfig = request.state.repo
    browser = _docker(svc, repo)
    return await browser.manifest(_context(svc, repo), image=image, reference=reference, platform=platform)


# ---------------------------------------------------------------------------- galaxy
def _galaxy(svc: Services, repo: RepoConfig) -> GalaxyBrowser:
    browser = _browser(svc, repo, 'galaxy')
    if not isinstance(browser, GalaxyBrowser):
        raise NotFound('not a galaxy repository')
    return browser


@router.get(
    '/repositories/{name}/galaxy/namespaces',
    response_model=GalaxyNamespacePage,
    operation_id='listGalaxyNamespaces',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_galaxy_namespaces(
    name: str,
    request: Request,
    svc: ServicesDep,
    q: Annotated[str | None, Query(max_length=250)] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> GalaxyNamespacePage:
    repo: RepoConfig = request.state.repo
    return await _galaxy(svc, repo).namespaces(_context(svc, repo), q=q, cursor=cursor, limit=limit)


@router.get(
    '/repositories/{name}/galaxy/collections',
    response_model=GalaxyCollectionPage,
    operation_id='listGalaxyCollections',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_galaxy_collections(
    name: str,
    request: Request,
    svc: ServicesDep,
    namespace: Annotated[str | None, Query(max_length=64, description='Only this namespace')] = None,
    q: Annotated[str | None, Query(max_length=250)] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> GalaxyCollectionPage:
    repo: RepoConfig = request.state.repo
    browser = _galaxy(svc, repo)
    return await browser.collections(_context(svc, repo), namespace=namespace, q=q, cursor=cursor, limit=limit)


@router.get(
    '/repositories/{name}/galaxy/versions',
    response_model=GalaxyVersionPage,
    operation_id='listGalaxyVersions',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_galaxy_versions(
    name: str,
    request: Request,
    svc: ServicesDep,
    namespace: Annotated[str, Query(max_length=64, description='Collection namespace')],
    collection: Annotated[str, Query(max_length=64, description='Collection name')],
    cursor: Cursor = None,
    limit: Limit = 50,
) -> GalaxyVersionPage:
    repo: RepoConfig = request.state.repo
    browser = _galaxy(svc, repo)
    return await browser.versions(_context(svc, repo), namespace=namespace, name=collection, cursor=cursor, limit=limit)


# ---------------------------------------------------------------------------- helm
def _helm(svc: Services, repo: RepoConfig) -> HelmBrowser:
    browser = _browser(svc, repo, 'helm')
    if not isinstance(browser, HelmBrowser):
        raise NotFound('not a helm repository')
    return browser


@router.get(
    '/repositories/{name}/helm/charts',
    response_model=HelmChartPage,
    operation_id='listHelmCharts',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_helm_charts(
    name: str,
    request: Request,
    svc: ServicesDep,
    q: Annotated[str | None, Query(max_length=250)] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> HelmChartPage:
    repo: RepoConfig = request.state.repo
    browser = _helm(svc, repo)
    return await browser.charts(_context(svc, repo), q=q, cursor=cursor, limit=limit)


@router.get(
    '/repositories/{name}/helm/versions',
    response_model=HelmVersionPage,
    operation_id='listHelmVersions',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_helm_versions(
    name: str,
    request: Request,
    svc: ServicesDep,
    chart: Annotated[str, Query(max_length=250, description='Chart name')],
    cursor: Cursor = None,
    limit: Limit = 50,
) -> HelmVersionPage:
    repo: RepoConfig = request.state.repo
    browser = _helm(svc, repo)
    return await browser.versions(_context(svc, repo), chart=chart, cursor=cursor, limit=limit)


# ---------------------------------------------------------------------------- maven
@router.get(
    '/repositories/{name}/maven/artifacts',
    response_model=MavenArtifactPage,
    operation_id='listMavenArtifacts',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_maven_artifacts(
    name: str,
    request: Request,
    svc: ServicesDep,
    q: Annotated[str | None, Query(max_length=200, description='Substring of the groupId or artifactId')] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> MavenArtifactPage:
    repo: RepoConfig = request.state.repo
    browser = _maven(svc, repo)
    return await browser.artifacts(_context(svc, repo), q=q, cursor=cursor, limit=limit)


@router.get(
    '/repositories/{name}/maven/versions',
    response_model=MavenVersionPage,
    operation_id='listMavenVersions',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_maven_versions(
    name: str,
    request: Request,
    svc: ServicesDep,
    group_id: Annotated[str, Query(min_length=1, max_length=255)],
    artifact_id: Annotated[str, Query(min_length=1, max_length=255)],
    cursor: Cursor = None,
    limit: Limit = 50,
) -> MavenVersionPage:
    repo: RepoConfig = request.state.repo
    browser = _maven(svc, repo)
    return await browser.versions(
        _context(svc, repo), group_id=group_id, artifact_id=artifact_id, cursor=cursor, limit=limit
    )


# ---------------------------------------------------------------------------- pypi
Project = Annotated[str, Query(max_length=200, description='Project name (any spelling; normalised per PEP 503)')]


@router.get(
    '/repositories/{name}/pypi/projects',
    response_model=PypiProjectPage,
    operation_id='listPypiProjects',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_pypi_projects(
    name: str,
    request: Request,
    svc: ServicesDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> PypiProjectPage:
    repo: RepoConfig = request.state.repo
    browser = _pypi(svc, repo)
    return await browser.projects(_context(svc, repo), q=q, cursor=cursor, limit=limit)


@router.get(
    '/repositories/{name}/pypi/versions',
    response_model=PypiVersionPage,
    operation_id='listPypiVersions',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_pypi_versions(
    name: str, request: Request, svc: ServicesDep, project: Project, cursor: Cursor = None, limit: Limit = 50
) -> PypiVersionPage:
    repo: RepoConfig = request.state.repo
    browser = _pypi(svc, repo)
    return await browser.versions(_context(svc, repo), project=project, cursor=cursor, limit=limit)


@router.post(
    '/repositories/{name}/pypi/yank',
    response_model=PypiYankResult,
    operation_id='yankPypiVersion',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.WRITE)
async def yank_pypi_version(name: str, request: Request, svc: ServicesDep, body: PypiYank) -> PypiYankResult:
    """Hosted repositories: yank (PEP 592) or un-yank every file of a version. Needs `write`."""
    repo: RepoConfig = request.state.repo
    browser = _pypi(svc, repo)
    return await browser.yank(_context(svc, repo), body)


# ---------------------------------------------------------------------------- go
Module = Annotated[str, Query(max_length=500, description='Module path (decoded, e.g. `github.com/Azure/go-autorest`)')]


@router.get(
    '/repositories/{name}/go/modules',
    response_model=GoModulePage,
    operation_id='listGoModules',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_go_modules(
    name: str,
    request: Request,
    svc: ServicesDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> GoModulePage:
    repo: RepoConfig = request.state.repo
    browser = _go(svc, repo)
    return await browser.modules(_context(svc, repo), q=q, cursor=cursor, limit=limit)


@router.get(
    '/repositories/{name}/go/versions',
    response_model=GoVersionPage,
    operation_id='listGoVersions',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_go_versions(
    name: str, request: Request, svc: ServicesDep, module: Module, cursor: Cursor = None, limit: Limit = 50
) -> GoVersionPage:
    repo: RepoConfig = request.state.repo
    browser = _go(svc, repo)
    return await browser.versions(_context(svc, repo), module=module, cursor=cursor, limit=limit)


ProviderAddress = Annotated[
    str,
    Query(
        max_length=400,
        description='Provider address `<hostname>/<namespace>/<type>`, e.g. `registry.terraform.io/hashicorp/null`',
    ),
]


@router.get(
    '/repositories/{name}/terraform/providers',
    response_model=TerraformProviderPage,
    operation_id='listTerraformProviders',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_terraform_providers(
    name: str,
    request: Request,
    svc: ServicesDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> TerraformProviderPage:
    repo: RepoConfig = request.state.repo
    browser = _terraform(svc, repo)
    return await browser.providers(_context(svc, repo), q=q, cursor=cursor, limit=limit)


@router.get(
    '/repositories/{name}/terraform/versions',
    response_model=TerraformVersionPage,
    operation_id='listTerraformVersions',
    responses=NOT_FOUND,
)
@access.requires_repo(Action.BROWSE)
async def list_terraform_versions(
    name: str, request: Request, svc: ServicesDep, provider: ProviderAddress, cursor: Cursor = None, limit: Limit = 50
) -> TerraformVersionPage:
    repo: RepoConfig = request.state.repo
    browser = _terraform(svc, repo)
    return await browser.versions(_context(svc, repo), provider=provider, cursor=cursor, limit=limit)
