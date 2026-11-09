"""Browse response models (OpenAPI contract ``Application/Web/openapi/waystation-m0.yaml``, tag ``browse``), the
context a browse view gets, the view protocols format plugins implement, and opaque cursors.

A format plugin that supports format-aware browsing exposes a ``browser`` attribute implementing one of the protocols
below (``NpmBrowser``, ``AptBrowser``, ``DockerBrowser``, ``MavenBrowser``). The REST API finds it through the
format registry and never imports the format package (ARCHITECTURE §3).

Download counters (``downloads``, ``pulls``, ``last_downloaded_at``, ``last_pulled_at``) are not recorded yet (T-S09
full): they answer ``0`` / ``null``.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from waystation_common.errors import ValidationFailed
from waystation_server.repos.model import RepoConfig

if TYPE_CHECKING:
    from waystation_common.db.client import Database
    from waystation_server.proxy.engine import ProxyEngine

MAX_CURSOR = 512


# ---------------------------------------------------------------------------- generic folder listing
class FolderEntry(BaseModel):
    name: str
    path: str
    kind: Literal['folder', 'file']
    size: int | None = None
    content_type: str | None = None
    sha256: str | None = None
    last_modified: datetime | None = None
    last_downloaded_at: datetime | None = None
    download_count: int | None = None
    child_count: int | None = Field(default=None, description='Folders only')


class FolderListing(BaseModel):
    path: str
    entries: list[FolderEntry]
    next_cursor: str | None


# ---------------------------------------------------------------------------- npm
class NpmPackage(BaseModel):
    name: str
    description: str | None = None
    latest: str
    version_count: int = Field(description='Versions in the upstream packument')
    cached_versions: int = Field(description='Versions whose tarball is cached')
    size_bytes: int
    license: str | None = None
    last_downloaded_at: datetime | None = None
    downloads: int = 0


class NpmPackagePage(BaseModel):
    items: list[NpmPackage]
    next_cursor: str | None


class NpmVersion(BaseModel):
    version: str
    published_at: datetime
    size_bytes: int | None = Field(default=None, description='Tarball size')
    unpacked_size: int | None = None
    cached: bool
    downloads: int = 0
    deprecated: str | None = Field(default=None, description='Deprecation message')
    integrity: str | None = None
    dist_tags: list[str] = Field(default_factory=list)
    last_downloaded_at: datetime | None = None


class NpmVersionPage(BaseModel):
    package: NpmPackage
    dist_tags: dict[str, str] = Field(default_factory=dict)
    items: list[NpmVersion]
    next_cursor: str | None


# ---------------------------------------------------------------------------- apt
class AptSuite(BaseModel):
    suite: str
    origin: str | None = None
    codename: str | None = None
    components: list[str]
    architectures: list[str]
    release_date: datetime | None = None
    last_refreshed_at: datetime | None = None
    package_count: int = 0
    cached_package_count: int = 0


class AptPackage(BaseModel):
    name: str
    version: str
    architecture: str
    component: str
    section: str | None = None
    size: int | None = None
    filename: str = Field(description='Path under the repository, `pool/...`')
    sha256: str | None = None
    cached: bool
    verified: bool | None = Field(default=None, description='Cached file matched the index hash; null if not cached')
    last_downloaded_at: datetime | None = None
    downloads: int = 0


class AptPackagePage(BaseModel):
    items: list[AptPackage]
    next_cursor: str | None


# ---------------------------------------------------------------------------- docker
class Platform(BaseModel):
    os: str
    architecture: str
    variant: str | None = None


class DockerImage(BaseModel):
    name: str
    tag_count: int
    size_bytes: int = Field(description='Unique bytes stored for this image')
    last_pulled_at: datetime | None = None
    pulls: int = 0


class DockerImagePage(BaseModel):
    items: list[DockerImage]
    next_cursor: str | None


class DockerTag(BaseModel):
    tag: str
    digest: str
    media_type: str
    platforms: list[Platform]
    size_bytes: int
    created_at: datetime | None = Field(default=None, description='Image config `created`')
    updated_at: datetime
    last_pulled_at: datetime | None = None
    pulls: int = 0


class DockerTagPage(BaseModel):
    image: str
    items: list[DockerTag]
    next_cursor: str | None


class DockerLayer(BaseModel):
    digest: str
    size: int
    media_type: str
    cached: bool = Field(default=False, description='Blob present in the blob store')
    created_by: str | None = Field(default=None, description='History entry (`created_by`) for this layer')


class DockerPlatformManifest(BaseModel):
    platform: Platform
    digest: str
    size: int


class DockerManifest(BaseModel):
    image: str
    reference: str
    digest: str = Field(description='Digest of the requested reference (index digest for multi-arch)')
    media_type: str
    is_index: bool
    manifests: list[DockerPlatformManifest] = Field(description='Index entries (empty for a single-platform manifest)')
    platform: Platform | None = Field(default=None, description='Platform whose layers are listed')
    platform_digest: str | None = None
    config_digest: str | None = None
    created_at: datetime | None = None
    layers: list[DockerLayer]
    total_size: int = Field(description='Sum of layer sizes for the selected platform')
    labels: dict[str, str] = Field(default_factory=dict)
    pull_by_tag: str = ''
    pull_by_digest: str = ''


# ---------------------------------------------------------------------------- helm
class HelmChart(BaseModel):
    name: str
    latest_version: str
    app_version: str | None = None
    description: str | None = None
    icon: str | None = Field(default=None, description="The chart's icon URL (http/https), as its metadata gives it")
    deprecated: bool = False
    version_count: int


class HelmChartPage(BaseModel):
    items: list[HelmChart]
    next_cursor: str | None


class HelmVersion(BaseModel):
    version: str
    app_version: str | None = None
    description: str | None = None
    created_at: datetime | None = None
    digest: str | None = Field(default=None, description='sha256 of the chart package (hex), when known')
    size_bytes: int | None = None
    cached: bool = Field(description='The chart package is stored (always true in hosted repositories)')
    deprecated: bool = False
    path: str = Field(description='Path of the chart package in the repository')
    download_url: str


class HelmVersionPage(BaseModel):
    chart: HelmChart
    items: list[HelmVersion]
    next_cursor: str | None


# ---------------------------------------------------------------------------- maven
class MavenArtifact(BaseModel):
    group_id: str
    artifact_id: str
    latest: str | None = Field(default=None, description='Highest version held (Maven order)')
    version_count: int
    updated_at: datetime | None = None


class MavenArtifactPage(BaseModel):
    items: list[MavenArtifact]
    next_cursor: str | None


class MavenVersion(BaseModel):
    version: str
    snapshot: bool
    path: str = Field(description='The version folder (browse it for the files)')
    file_count: int
    size_bytes: int
    created_at: datetime | None = None
    updated_at: datetime | None = None
    uploaded_by: str | None = Field(default=None, description='Hosted: who deployed it last')


class MavenVersionPage(BaseModel):
    group_id: str
    artifact_id: str
    items: list[MavenVersion]
    next_cursor: str | None


# ---------------------------------------------------------------------------- pypi
class PypiProject(BaseModel):
    name: str = Field(description='Normalised project name (PEP 503)')
    display_name: str
    summary: str | None = None
    latest: str | None = Field(default=None, description='Newest final release (PEP 440), else the newest version')
    version_count: int = Field(description='Versions on the project page')
    files: int = Field(description='Distribution files held: cached (proxy) or uploaded (hosted)')
    size_bytes: int
    last_downloaded_at: datetime | None = None
    downloads: int = 0


class PypiProjectPage(BaseModel):
    items: list[PypiProject]
    next_cursor: str | None


class PypiFile(BaseModel):
    filename: str
    kind: Literal['wheel', 'sdist', 'other']
    size: int | None = None
    sha256: str | None = None
    requires_python: str | None = None
    yanked: bool = False
    yanked_reason: str | None = None
    core_metadata: bool = Field(default=False, description='A PEP 658 `.metadata` file is available')
    cached: bool = Field(description='Held by this repository (cached or uploaded)')
    uploaded_at: datetime | None = None
    uploaded_by: str | None = Field(default=None, description='Hosted: who uploaded the file')
    last_downloaded_at: datetime | None = None
    downloads: int = 0


class PypiVersion(BaseModel):
    version: str
    prerelease: bool
    yanked: bool = Field(description='Every file of the version is yanked')
    files: list[PypiFile]
    cached_files: int
    size_bytes: int = Field(description='Bytes of the files held')


class PypiVersionPage(BaseModel):
    project: PypiProject
    items: list[PypiVersion] = Field(description='Newest first (PEP 440)')
    next_cursor: str | None


class PypiYank(BaseModel):
    project: str = Field(max_length=200)
    version: str = Field(max_length=200)
    yanked: bool = Field(description='True: yank every file of the version (PEP 592); false: un-yank')
    reason: str | None = Field(default=None, max_length=500)


class PypiYankResult(BaseModel):
    project: str
    version: str
    yanked: bool
    files: int = Field(description='Files changed')


# ---------------------------------------------------------------------------- views
@dataclass(frozen=True)
class BrowseContext:
    """What a browse view may use: the (already authorised) repository, the database (through ``*.db`` modules),
    the proxy engine (to read cached metadata blobs, bounded) and the host names clients use."""

    repo: RepoConfig
    db: Database
    proxy: ProxyEngine
    base_url: str
    registry_host: str


@runtime_checkable
class NpmBrowser(Protocol):
    async def packages(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> NpmPackagePage:
        """Packages whose packument is cached, by name."""

    async def versions(self, ctx: BrowseContext, *, package: str, cursor: str | None, limit: int) -> NpmVersionPage:
        """One package's versions (newest first); raises ``NotFound`` when its packument is not cached."""


@runtime_checkable
class AptBrowser(Protocol):
    async def suites(self, ctx: BrowseContext) -> list[AptSuite]:
        """Suites described by the cached ``InRelease``/``Release`` files."""

    async def packages(
        self,
        ctx: BrowseContext,
        *,
        suite: str,
        component: str | None,
        arch: str | None,
        cached: bool | None,
        q: str | None,
        cursor: str | None,
        limit: int,
    ) -> AptPackagePage:
        """Packages listed by the suite's cached ``Packages`` indexes."""


@runtime_checkable
class PypiBrowser(Protocol):
    async def projects(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> PypiProjectPage:
        """Projects the repository has (cached page or uploads), by normalised name."""

    async def versions(self, ctx: BrowseContext, *, project: str, cursor: str | None, limit: int) -> PypiVersionPage:
        """One project's versions with their files (newest first); raises ``NotFound`` for an unknown project."""

    async def yank(self, ctx: BrowseContext, body: PypiYank) -> PypiYankResult:
        """Hosted: yank or un-yank every file of a version; raises ``NotFound`` when there is none."""


@runtime_checkable
class DockerBrowser(Protocol):
    async def images(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> DockerImagePage:
        """Images with at least one cached tag manifest, by name."""

    async def tags(self, ctx: BrowseContext, *, image: str, cursor: str | None, limit: int) -> DockerTagPage:
        """Cached tags of one image, most recently updated first; raises ``NotFound`` for an unknown image."""

    async def manifest(self, ctx: BrowseContext, *, image: str, reference: str, platform: str | None) -> DockerManifest:
        """A cached manifest (tag or digest) with its platforms and layers; raises ``NotFound`` when not cached."""


@runtime_checkable
class HelmBrowser(Protocol):
    async def charts(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> HelmChartPage:
        """Charts by name (hosted: uploaded; proxy: listed by the cached index; group: the members')."""

    async def versions(self, ctx: BrowseContext, *, chart: str, cursor: str | None, limit: int) -> HelmVersionPage:
        """One chart's versions, newest first; raises ``NotFound`` for an unknown chart."""


@runtime_checkable
class MavenBrowser(Protocol):
    async def artifacts(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> MavenArtifactPage:
        """Artifacts (groupId, artifactId) with at least one version held, in order."""

    async def versions(
        self, ctx: BrowseContext, *, group_id: str, artifact_id: str, cursor: str | None, limit: int
    ) -> MavenVersionPage:
        """One artifact's versions, highest first (Maven order); raises ``NotFound`` when it has none."""


# ---------------------------------------------------------------------------- cursors
def encode_cursor(*values: Any) -> str:
    """An opaque page cursor holding ``values`` (JSON scalars)."""
    raw = json.dumps(list(values), separators=(',', ':'), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def decode_cursor(cursor: str, *types: type) -> list[Any]:
    """The values of a cursor made by ``encode_cursor``, checked against ``types`` (raises ``ValidationFailed``)."""
    if len(cursor) > MAX_CURSOR:
        raise ValidationFailed('invalid cursor')
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
    except binascii.Error, ValueError, UnicodeDecodeError:
        raise ValidationFailed('invalid cursor') from None
    if not isinstance(values, list) or len(values) != len(types):
        raise ValidationFailed('invalid cursor')
    for value, kind in zip(values, types, strict=True):
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ValidationFailed('invalid cursor')
    return values
