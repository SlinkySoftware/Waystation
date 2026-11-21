"""``GET /api/v1/digests/{digest}/usage``: where a content digest is used (T-S09 "where used").

Lists the assets holding the blob (any format) and the docker manifests referencing it as a layer or config, in the
repositories the principal may browse (others are left out, never named). At most ``MAX_USES`` entries.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from waystation_common.errors import ValidationFailed
from waystation_server.api.deps import ServicesDep
from waystation_server.api.search import PROBLEM
from waystation_server.db import content as content_db
from waystation_server.repos.schemas import Format
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate

router = APIRouter(tags=['browse'])

MAX_USES = 500
_DIGEST = re.compile(r'^(?:sha256:)?([0-9a-fA-F]{64})$')


class DigestUse(BaseModel):
    repository: str
    format: Format
    kind: Literal['asset', 'docker_layer', 'docker_config']
    path: str | None = Field(description='Asset path (kind `asset`)')
    image: str | None
    manifest_digest: str | None


class DigestUsage(BaseModel):
    digest: str
    size: int | None
    items: list[DigestUse]
    truncated: bool


BAD_REQUEST: dict[int | str, dict[str, Any]] = {400: PROBLEM}


@router.get('/digests/{digest}/usage', response_model=DigestUsage, operation_id='getDigestUsage', responses=BAD_REQUEST)
@access.filtered(Action.BROWSE)
async def get_digest_usage(digest: str, request: Request, svc: ServicesDep) -> DigestUsage:
    match = _DIGEST.match(digest)
    if match is None:
        raise ValidationFailed('digest must be sha256:<64 hex characters>')
    hexdigest = match.group(1).lower()
    principal = await authenticate(request)
    visible = {r.id: r for r in access.filter_repos(principal, Action.BROWSE, await svc.catalog.all())}
    items: list[DigestUse] = []
    for doc in await content_db.assets_with_blob(svc.db, hexdigest, limit=MAX_USES + 1):
        repo = visible.get(str(doc['repo_id']))
        if repo is not None:
            items.append(
                DigestUse(
                    repository=repo.name,
                    format=repo.format,
                    kind='asset',
                    path=str(doc['path']),
                    image=None,
                    manifest_digest=None,
                )
            )
    for repo in sorted(visible.values(), key=lambda r: r.name):
        if repo.format != 'docker' or len(items) > MAX_USES:
            continue
        for image, manifest, kind in await content_db.manifests_using(
            svc.db, repo.id, f'sha256:{hexdigest}', limit=MAX_USES + 1
        ):
            items.append(
                DigestUse(
                    repository=repo.name,
                    format=repo.format,
                    kind='docker_layer' if kind == 'docker_layer' else 'docker_config',
                    path=None,
                    image=image,
                    manifest_digest=manifest,
                )
            )
    return DigestUsage(
        digest=f'sha256:{hexdigest}',
        size=await content_db.blob_size(svc.db, hexdigest) if items else None,  # nothing about hidden blobs
        items=items[:MAX_USES],
        truncated=len(items) > MAX_USES,
    )
