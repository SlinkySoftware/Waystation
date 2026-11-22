"""Browse response models (OpenAPI contract ``Application/Web/openapi/waystation-m0.yaml``, tag ``browse``), the
context a browse view gets, the view protocols format plugins implement, and opaque cursors.

A format plugin that supports format-aware browsing exposes a ``browser`` attribute implementing one of the protocols
below (``NpmBrowser``, ``AptBrowser``, ``DockerBrowser``, ``MavenBrowser``, ``PypiBrowser``, ``GoBrowser``,
``HelmBrowser``, ``GalaxyBrowser``, ``YumBrowser``). The REST API finds it through the format registry and never
imports the format package (ARCHITECTURE §3).

Download counters (``downloads``, ``pulls``, ``last_downloaded_at``, ``last_pulled_at``) are the assets' batched
counters (files, npm tarballs, apt packages) or the component index's (docker tags and images; T-S09).
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
    readme: str | None = Field(
        default=None, description='The package README (Markdown, at most 256 KiB; first page only), untrusted text'
    )


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


class DockerReferrer(BaseModel):
    digest: str
    media_type: str
    artifact_type: str | None = Field(default=None, description='`artifactType`, else the config media type')
    size: int | None = Field(default=None, description="The referrer manifest's size in bytes")
    annotations: dict[str, str] = Field(default_factory=dict)


class DockerReferrerList(BaseModel):
    image: str
    subject: str = Field(description='Digest the referrers point at (`subject`)')
    items: list[DockerReferrer]


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


# ---------------------------------------------------------------------------- nuget
class NugetPackage(BaseModel):
    name: str = Field(description='Package id in lower case (as in package URLs)')
    id: str = Field(description='Package id as its author spelled it')
    latest_version: str | None = Field(default=None, description='Newest stable version, else the newest version')
    version_count: int
    description: str | None = None
    authors: str | None = None
    updated_at: datetime | None = None


class NugetPackagePage(BaseModel):
    items: list[NugetPackage]
    next_cursor: str | None


class NugetVersion(BaseModel):
    version: str = Field(description='Normalised version')
    prerelease: bool
    listed: bool
    cached: bool
    size_bytes: int | None = None
    published_at: datetime | None = None
    uploaded_by: str | None = None
    path: str
    download_url: str


class NugetVersionPage(BaseModel):
    package: NugetPackage
    items: list[NugetVersion]
    next_cursor: str | None


class NugetListing(BaseModel):
    package: str = Field(max_length=100)
    version: str = Field(max_length=200)
    listed: bool = Field(description='False: unlist the version; true: relist it')


# ---------------------------------------------------------------------------- composer
class ComposerPackage(BaseModel):
    name: str = Field(description='Package name vendor/name (lower case)')
    latest_version: str | None = Field(default=None, description='Newest stable version, else the newest version')
    version_count: int
    description: str | None = None
    type: str | None = Field(default=None, description='Package type, e.g. library')
    updated_at: datetime | None = None


class ComposerPackagePage(BaseModel):
    items: list[ComposerPackage]
    next_cursor: str | None


class ComposerVersion(BaseModel):
    version: str = Field(description='Version as published, e.g. v1.2.3 or dev-main')
    version_normalized: str = Field(
        description='Normalised version (four numbers and a stability suffix, as Composer writes it)'
    )
    stability: Literal['stable', 'RC', 'beta', 'alpha', 'dev']
    cached: bool = Field(description='The archive is stored')
    size_bytes: int | None = None
    published_at: datetime | None = None
    uploaded_by: str | None = None
    reference: str | None = Field(default=None, description='Source reference (commit) of the archive')
    path: str | None = Field(default=None, description="The archive's repository path")
    download_url: str | None = None


class ComposerVersionPage(BaseModel):
    package: ComposerPackage
    items: list[ComposerVersion]
    next_cursor: str | None


# ---------------------------------------------------------------------------- rubygems
class RubygemsGem(BaseModel):
    name: str = Field(description='Gem name as published (case-sensitive)')
    latest_version: str | None = Field(default=None, description='Newest release version, else the newest version')
    version_count: int
    summary: str | None = None
    updated_at: datetime | None = None


class RubygemsGemPage(BaseModel):
    items: list[RubygemsGem]
    next_cursor: str | None


class RubygemsDependency(BaseModel):
    name: str
    requirement: str = Field(description='Version requirement, e.g. >= 1.0, < 2')


class RubygemsVersion(BaseModel):
    version: str
    platform: str = Field(description='ruby for pure-Ruby gems, else e.g. x86_64-linux')
    prerelease: bool
    cached: bool = Field(description='The .gem file is stored')
    size_bytes: int | None = None
    sha256: str | None = None
    published_at: datetime | None = None
    uploaded_by: str | None = None
    ruby_requirement: str | None = Field(default=None, description='Required Ruby version, e.g. >= 3.0')
    dependencies: list[RubygemsDependency] = Field(description='Runtime dependencies')
    path: str = Field(description='Repository path gems/<name>-<version>[-<platform>].gem')
    download_url: str


class RubygemsVersionPage(BaseModel):
    gem: RubygemsGem
    items: list[RubygemsVersion]
    next_cursor: str | None


# ---------------------------------------------------------------------------- yum
class YumPackage(BaseModel):
    name: str
    latest_version: str = Field(description='Newest `[epoch:]version-release` (RPM version order)')
    arches: list[str]
    version_count: int = Field(description='Distinct epoch-version-release values')
    summary: str | None = None


class YumPackagePage(BaseModel):
    items: list[YumPackage]
    next_cursor: str | None


class YumVersion(BaseModel):
    evr: str = Field(description='`[epoch:]version-release`')
    epoch: str
    version: str
    release: str
    arch: str
    path: str = Field(description='Path of the package file in the repository')
    directory: str = Field(description='The yum repository directory (holding `repodata/`) listing it; empty: the root')
    size_bytes: int | None = None
    checksum: str | None = Field(default=None, description='`<algorithm>:<hex>` from the listing or the upload')
    cached: bool = Field(description='The package file is stored (always true in hosted repositories)')
    uploaded_at: datetime | None = None
    download_url: str


class YumVersionPage(BaseModel):
    package: YumPackage
    items: list[YumVersion]
    next_cursor: str | None


# ---------------------------------------------------------------------------- galaxy
class GalaxyNamespace(BaseModel):
    namespace: str
    collection_count: int


class GalaxyNamespacePage(BaseModel):
    items: list[GalaxyNamespace]
    next_cursor: str | None


class GalaxyCollection(BaseModel):
    namespace: str
    name: str
    latest_version: str
    description: str | None = None
    requires_ansible: str | None = None
    version_count: int


class GalaxyCollectionPage(BaseModel):
    items: list[GalaxyCollection]
    next_cursor: str | None


class GalaxyVersion(BaseModel):
    version: str
    prerelease: bool = False
    created_at: datetime | None = None
    requires_ansible: str | None = None
    sha256: str | None = Field(default=None, description='sha256 of the collection artifact (hex), when known')
    size_bytes: int | None = None
    cached: bool = Field(description='The artifact is stored (always true in hosted repositories)')
    dependencies: dict[str, str] = Field(default_factory=dict, description='Collection name -> version range')
    uploaded_by: str | None = None
    path: str = Field(description='Path of the artifact in the repository')
    download_url: str


class GalaxyVersionPage(BaseModel):
    collection: GalaxyCollection
    items: list[GalaxyVersion]
    next_cursor: str | None


# ---------------------------------------------------------------------------- swift
class SwiftScope(BaseModel):
    scope: str
    package_count: int


class SwiftScopePage(BaseModel):
    items: list[SwiftScope]
    next_cursor: str | None


class SwiftPackage(BaseModel):
    scope: str
    name: str
    id: str = Field(description='The package identity clients use: <scope>.<name>')
    latest_version: str | None = Field(default=None, description='The highest published version')
    release_count: int = Field(description='Published releases (a proxy: releases pinned so far)')
    removed_count: int = 0
    description: str | None = None
    repository_url: str | None = Field(default=None, description='Source repository (release metadata)')


class SwiftPackagePage(BaseModel):
    items: list[SwiftPackage]
    next_cursor: str | None


class SwiftManifest(BaseModel):
    filename: str = Field(description='Package.swift or Package@swift-<version>.swift')
    swift_version: str | None = None
    tools_version: str | None = None
    size_bytes: int | None = None
    url: str


class SwiftRelease(BaseModel):
    version: str
    prerelease: bool = False
    state: Literal['published', 'removed']
    source: Literal['hosted', 'scm'] = Field(description='hosted: published; scm: built from a Git tag and pinned')
    published_at: datetime | None = None
    published_by: str | None = None
    removed_at: datetime | None = None
    removed_by: str | None = None
    checksum: str = Field(description='sha256 of the source archive (hex); pinned, never changes')
    size_bytes: int | None = None
    tools_version: str | None = None
    manifests: list[SwiftManifest] = Field(default_factory=list)
    signed: bool = False
    signer: str | None = Field(default=None, description='Subject of the signing certificate (not verified)')
    tag: str | None = Field(default=None, description='The Git tag the archive was built from (proxy)')
    commit: str | None = None
    path: str = Field(description='Path of the source archive in the repository')
    download_url: str


class SwiftReleasePage(BaseModel):
    package: SwiftPackage
    items: list[SwiftRelease]
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


# ---------------------------------------------------------------------------- cargo
class CargoCrate(BaseModel):
    name: str = Field(description='Crate name as published')
    description: str | None = None
    latest: str | None = Field(description='Highest non-yanked release (SemVer), else the highest version')
    version_count: int = Field(description='Versions in the index')
    cached_versions: int = Field(description='.crate files held: cached (proxy) or published (hosted)')
    size_bytes: int


class CargoCratePage(BaseModel):
    items: list[CargoCrate]
    next_cursor: str | None


class CargoVersion(BaseModel):
    version: str
    prerelease: bool
    yanked: bool
    cksum: str = Field(description='SHA-256 of the .crate (hex)')
    rust_version: str | None = None
    dependencies: int
    features: list[str]
    cached: bool = Field(description='The .crate is held')
    size: int | None = None
    published_by: str | None = Field(default=None, description='Hosted: who published it')
    published_at: datetime | None = None
    cached_at: datetime | None = None
    last_downloaded_at: datetime | None = None
    downloads: int


class CargoVersionPage(BaseModel):
    crate: CargoCrate
    items: list[CargoVersion] = Field(description='Newest first (SemVer)')
    next_cursor: str | None


# ---------------------------------------------------------------------------- conda
class CondaPackage(BaseModel):
    name: str
    latest: str | None = Field(description='Highest version listed (conda version order)')
    subdirs: list[str] = Field(description='Subdirs listing the package (noarch, linux-64...)')
    file_count: int = Field(description='Package files listed (every build of every version)')
    summary: str | None = None
    license: str | None = None


class CondaPackagePage(BaseModel):
    items: list[CondaPackage]
    next_cursor: str | None


class CondaFile(BaseModel):
    filename: str
    subdir: str
    version: str
    build: str
    build_number: int
    prerelease: bool
    sha256: str | None = Field(description='SHA-256 from the listing (hex)')
    md5: str | None = None
    size: int | None = None
    timestamp: datetime | None = Field(default=None, description='Build time from the listing')
    verified: bool = Field(description='The checksum comes from a repodata listing (or an upload), not a fetch')
    depends: list[str] = Field(description='Hosted: the dependencies of the build')
    license: str | None = None
    cached: bool = Field(description='The file is held (cached or uploaded)')
    cached_at: datetime | None = None
    downloads: int
    last_downloaded_at: datetime | None = None
    uploaded_by: str | None = Field(default=None, description='Hosted: who uploaded it')


class CondaFilePage(BaseModel):
    package: CondaPackage
    items: list[CondaFile] = Field(description='Newest version first (conda version order), then build number')
    next_cursor: str | None


# ---------------------------------------------------------------------------- huggingface
class HuggingfaceModel(BaseModel):
    name: str = Field(description='The model id, `datasets/<id>` for a dataset')
    repo_id: str = Field(description='The Hub repo id')
    kind: Literal['model', 'dataset']
    latest_commit: str | None = Field(description='The commit the default branch resolved to last')
    revisions: int = Field(description='Revisions clients asked for (branches, tags)')
    files: int = Field(description='Files cached (every commit)')
    size_bytes: int
    downloads: int = 0
    last_downloaded_at: datetime | None = None
    updated_at: datetime | None = None


class HuggingfaceModelPage(BaseModel):
    items: list[HuggingfaceModel]
    next_cursor: str | None


class HuggingfaceRef(BaseModel):
    revision: str = Field(description='Branch, tag or refs/pr/<n> a client asked for')
    commit: str
    resolved_at: datetime | None = None


class HuggingfaceCommit(BaseModel):
    commit: str
    revisions: list[str] = Field(description='Revisions that resolved to this commit when last checked')
    files: int = Field(description='Files of this commit cached')
    size_bytes: int
    cached_at: datetime | None = Field(default=None, description='First file cached')
    downloads: int = 0
    last_downloaded_at: datetime | None = None


class HuggingfaceRevisionList(BaseModel):
    model: HuggingfaceModel
    refs: list[HuggingfaceRef] = Field(description='Most recently resolved first')
    commits: list[HuggingfaceCommit] = Field(description='Commits with cached files, most recently cached first')


class HuggingfaceFile(BaseModel):
    path: str = Field(description='Path in the Hub repository')
    size: int
    sha256: str = Field(description='SHA-256 of the cached file (hex)')
    cached_at: datetime | None = None
    downloads: int = 0
    last_downloaded_at: datetime | None = None


class HuggingfaceFilePage(BaseModel):
    model: str
    commit: str
    items: list[HuggingfaceFile] = Field(description='By path')
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


# ---------------------------------------------------------------------------- go
class GoModule(BaseModel):
    module: str = Field(description='Module path (decoded: upper-case letters as they are)')
    latest: str | None = Field(
        default=None, description='Newest release in the cached version list, else the newest version'
    )
    version_count: int = Field(description='Versions in the cached version list (`@v/list`)')
    cached_versions: int = Field(description='Versions whose module zip is cached')
    size_bytes: int = Field(description='Bytes of the cached module zips')


class GoModulePage(BaseModel):
    items: list[GoModule]
    next_cursor: str | None


class GoFile(BaseModel):
    kind: Literal['info', 'mod', 'zip']
    size: int
    sha256: str
    cached_at: datetime | None = None
    last_downloaded_at: datetime | None = None
    downloads: int = 0


class GoVersion(BaseModel):
    version: str
    time: datetime | None = Field(default=None, description='Commit time from the cached `.info`')
    prerelease: bool
    pseudo: bool = Field(description='A pseudo-version (an untagged commit)')
    incompatible: bool = Field(description='`+incompatible`: a v2+ version of a module without a go.mod')
    listed: bool = Field(description='In the cached version list (tagged)')
    cached: bool = Field(description='The module zip is cached')
    files: list[GoFile] = Field(description='Files of the version held by this repository')
    size_bytes: int


class GoVersionPage(BaseModel):
    module: GoModule
    items: list[GoVersion] = Field(description='Newest first (semantic version order)')
    next_cursor: str | None


# ---------------------------------------------------------------------------- terraform
class TerraformProvider(BaseModel):
    provider: str = Field(
        description='Provider address: `<hostname>/<namespace>/<type>` (the hosted repository: `<namespace>/<type>`, '
        "the host name being this instance's)"
    )
    latest: str | None = Field(
        default=None, description='Newest release in the cached version list, else the newest version'
    )
    version_count: int = Field(description='Versions in the cached registry version list')
    cached_archives: int = Field(description='Provider archives (one per version and platform) cached')
    size_bytes: int = Field(description='Bytes of the cached archives')


class TerraformProviderPage(BaseModel):
    items: list[TerraformProvider]
    next_cursor: str | None


class TerraformArchive(BaseModel):
    os: str
    arch: str
    filename: str
    size: int
    sha256: str
    h1: str | None = Field(default=None, description='`h1:` hash of the archive contents (as in the lock file)')
    cached_at: datetime | None = None
    last_downloaded_at: datetime | None = None
    downloads: int = 0


class TerraformVersion(BaseModel):
    version: str
    prerelease: bool
    protocols: list[str] = Field(description='Plugin protocol versions the registry lists')
    platforms: list[str] = Field(description='`<os>_<arch>` platforms the registry lists')
    listed: bool = Field(description='In the cached registry version list')
    archives: list[TerraformArchive] = Field(description='Archives of the version held by this repository')
    size_bytes: int
    key_id: str | None = Field(default=None, description='Hosted: ID of the key that signed the SHA256SUMS')
    uploaded_by: str | None = Field(default=None, description='Hosted: who published the release')


class TerraformVersionPage(BaseModel):
    provider: TerraformProvider
    items: list[TerraformVersion] = Field(description='Newest first (semantic version order)')
    next_cursor: str | None


class TerraformModule(BaseModel):
    module: str = Field(description='Module address on its registry: `<namespace>/<name>/<system>`')
    latest: str | None = Field(default=None, description='Newest release (proxy: of the cached version list)')
    version_count: int = Field(description='Versions listed (proxy: the cached registry list; hosted: uploaded)')
    cached_versions: int = Field(description='Versions whose archive this repository holds')
    size_bytes: int = Field(description='Bytes of the archives held')


class TerraformModulePage(BaseModel):
    items: list[TerraformModule]
    next_cursor: str | None


class TerraformModuleArchive(BaseModel):
    filename: str = Field(description='`archive.tar.gz` or `archive.zip`')
    path: str = Field(description='Repository path of the archive (a Browse tree path)')
    size: int
    sha256: str = Field(description='Digest of the archive (proxy: pinned when it was first fetched)')
    source: str | None = Field(default=None, description='Proxy: the source address the origin registry gave')
    uploaded_by: str | None = Field(default=None, description='Hosted: who published the version')
    created_at: datetime | None = None
    last_downloaded_at: datetime | None = None
    downloads: int = 0


class TerraformModuleVersion(BaseModel):
    version: str
    prerelease: bool
    listed: bool = Field(description='Proxy: in the cached registry version list; hosted: always')
    archive: TerraformModuleArchive | None = Field(description='The archive this repository holds, if any')


class TerraformModuleVersionPage(BaseModel):
    module: TerraformModule
    items: list[TerraformModuleVersion] = Field(description='Newest first (semantic version order)')
    next_cursor: str | None


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
class CargoBrowser(Protocol):
    async def crates(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> CargoCratePage:
        """Crates the repository has (cached index file or publishes), by lower-case name."""

    async def versions(self, ctx: BrowseContext, *, crate: str, cursor: str | None, limit: int) -> CargoVersionPage:
        """One crate's versions (newest first); raises ``NotFound`` for an unknown crate or a group."""


@runtime_checkable
class CondaBrowser(Protocol):
    async def packages(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> CondaPackagePage:
        """Package names the repository lists (indexed repodata or uploads), by name."""

    async def files(
        self, ctx: BrowseContext, *, package: str, subdir: str | None, cursor: str | None, limit: int
    ) -> CondaFilePage:
        """One package's files (newest version first); raises ``NotFound`` for an unknown package."""


@runtime_checkable
class HuggingfaceBrowser(Protocol):
    async def models(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> HuggingfaceModelPage:
        """Hub models and datasets the repository has served, by name."""

    async def revisions(self, ctx: BrowseContext, *, model: str) -> HuggingfaceRevisionList:
        """One model's resolved revisions and cached commits; raises ``NotFound`` for an unknown model."""

    async def files(
        self, ctx: BrowseContext, *, model: str, commit: str, cursor: str | None, limit: int
    ) -> HuggingfaceFilePage:
        """One commit's cached files, by path."""


@runtime_checkable
class PypiBrowser(Protocol):
    async def projects(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> PypiProjectPage:
        """Projects the repository has (cached page or uploads), by normalised name."""

    async def versions(self, ctx: BrowseContext, *, project: str, cursor: str | None, limit: int) -> PypiVersionPage:
        """One project's versions with their files (newest first); raises ``NotFound`` for an unknown project."""

    async def yank(self, ctx: BrowseContext, body: PypiYank) -> PypiYankResult:
        """Hosted: yank or un-yank every file of a version; raises ``NotFound`` when there is none."""


@runtime_checkable
class GoBrowser(Protocol):
    async def modules(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> GoModulePage:
        """Modules the repository has cached something of, by path."""

    async def versions(self, ctx: BrowseContext, *, module: str, cursor: str | None, limit: int) -> GoVersionPage:
        """One module's versions (cached list and cached files, newest first); raises ``NotFound`` for an unknown
        module."""


@runtime_checkable
class TerraformBrowser(Protocol):
    async def providers(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> TerraformProviderPage:
        """Providers the repository has cached something of, by address."""

    async def versions(
        self, ctx: BrowseContext, *, provider: str, cursor: str | None, limit: int
    ) -> TerraformVersionPage:
        """One provider's versions (cached version list and cached archives, newest first); raises ``NotFound`` for
        an unknown provider."""

    async def modules(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> TerraformModulePage:
        """Modules (proxy: with a cached version list or archive; hosted: published), by address."""

    async def module_versions(
        self, ctx: BrowseContext, *, module: str, cursor: str | None, limit: int
    ) -> TerraformModuleVersionPage:
        """One module's versions, newest first; raises ``NotFound`` for an unknown module."""


@runtime_checkable
class DockerBrowser(Protocol):
    async def images(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> DockerImagePage:
        """Images with at least one cached tag manifest, by name."""

    async def tags(self, ctx: BrowseContext, *, image: str, cursor: str | None, limit: int) -> DockerTagPage:
        """Cached tags of one image, most recently updated first; raises ``NotFound`` for an unknown image."""

    async def manifest(self, ctx: BrowseContext, *, image: str, reference: str, platform: str | None) -> DockerManifest:
        """A cached manifest (tag or digest) with its platforms and layers; raises ``NotFound`` when not cached."""

    async def referrers(self, ctx: BrowseContext, *, image: str, digest: str) -> DockerReferrerList:
        """Manifests stored in the repository whose ``subject`` is ``digest`` (signatures, SBOMs, attestations)."""


@runtime_checkable
class HelmBrowser(Protocol):
    async def charts(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> HelmChartPage:
        """Charts by name (hosted: uploaded; proxy: listed by the cached index; group: the members')."""

    async def versions(self, ctx: BrowseContext, *, chart: str, cursor: str | None, limit: int) -> HelmVersionPage:
        """One chart's versions, newest first; raises ``NotFound`` for an unknown chart."""


@runtime_checkable
class YumBrowser(Protocol):
    async def packages(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> YumPackagePage:
        """Package names (proxy: listed by cached ``primary.xml`` files; hosted: uploaded), by name."""

    async def versions(self, ctx: BrowseContext, *, package: str, cursor: str | None, limit: int) -> YumVersionPage:
        """One package's files, newest EVR first; raises ``NotFound`` for an unknown package."""


@runtime_checkable
class ComposerBrowser(Protocol):
    async def packages(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> ComposerPackagePage:
        """Packages by name (hosted: uploaded; proxy: metadata cached; group: the members')."""

    async def versions(
        self, ctx: BrowseContext, *, package: str, cursor: str | None, limit: int
    ) -> ComposerVersionPage:
        """One package's versions, newest first; raises ``NotFound`` for an unknown package."""


@runtime_checkable
class RubygemsBrowser(Protocol):
    async def gems(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> RubygemsGemPage:
        """Gems by name (hosted: pushed; proxy: info file cached; group: the members')."""

    async def versions(self, ctx: BrowseContext, *, gem: str, cursor: str | None, limit: int) -> RubygemsVersionPage:
        """One gem's versions (one per platform), newest first; raises ``NotFound`` for an unknown gem."""


@runtime_checkable
class NugetBrowser(Protocol):
    async def packages(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> NugetPackagePage:
        """Packages by id (hosted: pushed; proxy: version list cached; group: the members')."""

    async def versions(self, ctx: BrowseContext, *, package: str, cursor: str | None, limit: int) -> NugetVersionPage:
        """One package's versions, newest first; raises ``NotFound`` for an unknown package."""

    async def listing(self, ctx: BrowseContext, body: NugetListing) -> NugetListing:
        """Hosted: unlist or relist a version; raises ``NotFound`` when there is none."""


@runtime_checkable
class GalaxyBrowser(Protocol):
    async def namespaces(
        self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int
    ) -> GalaxyNamespacePage:
        """Namespaces with their number of collections."""

    async def collections(
        self, ctx: BrowseContext, *, namespace: str | None, q: str | None, cursor: str | None, limit: int
    ) -> GalaxyCollectionPage:
        """Collections by namespace and name (hosted: imported; proxy: version lists cached; group: the members')."""

    async def versions(
        self, ctx: BrowseContext, *, namespace: str, name: str, cursor: str | None, limit: int
    ) -> GalaxyVersionPage:
        """One collection's versions, newest first; raises ``NotFound`` for an unknown collection."""


@runtime_checkable
class SwiftBrowser(Protocol):
    async def scopes(self, ctx: BrowseContext, *, q: str | None, cursor: str | None, limit: int) -> SwiftScopePage:
        """Scopes with their number of packages."""

    async def packages(
        self, ctx: BrowseContext, *, scope: str | None, q: str | None, cursor: str | None, limit: int
    ) -> SwiftPackagePage:
        """Packages by scope and name (hosted: published; proxy: with a pinned release)."""

    async def releases(
        self, ctx: BrowseContext, *, scope: str, name: str, cursor: str | None, limit: int
    ) -> SwiftReleasePage:
        """One package's releases, newest first; raises ``NotFound`` for an unknown package."""


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
