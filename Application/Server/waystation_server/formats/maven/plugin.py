"""Maven plugin (FORMATS.md "Maven"; T-P5-MAVEN): proxy (``proxy``), hosted (``hosted``) and group (``group``)
repositories under ``/repository/<repo>/``, plus the browse views (``browse``).

Errors are plain text (the router's native error shape); a ``401`` carries the Basic challenge Maven and Gradle
answer with the ``settings.xml`` / ``credentials`` username and API token (SPECIFICATION §4.9).
"""

from __future__ import annotations

from starlette.responses import Response

from waystation_server.db import maven as maven_db
from waystation_server.formats.base import FormatDescriptor, FormatRequest
from waystation_server.formats.common.background import Background
from waystation_server.formats.maven import coords
from waystation_server.formats.maven.browse import MavenBrowse
from waystation_server.formats.maven.common import Digests
from waystation_server.formats.maven.descriptor import DESCRIPTOR
from waystation_server.formats.maven.group import MavenGroup
from waystation_server.formats.maven.hosted import MavenHosted
from waystation_server.formats.maven.proxy import MavenProxy
from waystation_server.formats.maven.version import sort_key
from waystation_server.proxy.engine import OnStored, StoredAsset
from waystation_server.repos.model import RepoType


class MavenPlugin:
    def __init__(self, descriptor: FormatDescriptor = DESCRIPTOR) -> None:
        self.descriptor = descriptor
        self.browser = MavenBrowse()
        self.digests = Digests()
        self._indexing = Background('maven-components')
        self.proxy = MavenProxy(self.digests, self._on_artifact)
        self.hosted = MavenHosted(self.digests)
        self.group = MavenGroup(self._serve_member)

    async def drain(self) -> None:
        """Wait for background indexing (tests, shutdown)."""
        await self._indexing.drain()

    async def handle(self, req: FormatRequest) -> Response:
        if req.repo.type is RepoType.GROUP:
            return await self.group.handle(req)
        return await self._serve_member(req)

    async def _serve_member(self, req: FormatRequest) -> Response:
        if req.repo.type is RepoType.HOSTED:
            return await self.hosted.handle(req)
        return await self.proxy.handle(req)

    def _on_artifact(self, req: FormatRequest, artifact: coords.Artifact) -> OnStored:
        engine = req.proxy
        repo_id = req.repo.id

        async def stored(asset: StoredAsset, data: bytes | None) -> None:
            async def index() -> None:
                await maven_db.upsert_version(
                    engine.db,
                    repo_id,
                    artifact.group_id,
                    artifact.artifact_id,
                    artifact.version,
                    version_sort=sort_key(artifact.version),
                )

            self._indexing.spawn((repo_id, artifact.group_id, artifact.artifact_id, artifact.version), index)

        return stored
