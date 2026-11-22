"""``/.well-known/terraform.json``: Terraform/OpenTofu remote service discovery for this host (SPECIFICATION §4.4,
FORMATS.md "Terraform / OpenTofu"; T-P5-TFM).

Discovery is per host, so it names the instance's one hosted Terraform repository: ``modules.v1`` and ``providers.v1``
are its module and provider registries (``/repository/<repo>/v1/modules/``, ``/repository/<repo>/v1/providers/``,
relative to this document as the protocol allows). The route is public (clients discover before they authenticate),
but the answer follows the repository's visibility: Terraform and OpenTofu send the host's credentials with the
discovery request, and a caller who cannot see the repository (or when there is none, or it is offline) gets an empty
document, which clients read as "this host offers no registry". Invalid credentials count as none here.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from waystation_common.errors import WaystationError
from waystation_server.repos.model import RepoType
from waystation_server.security import access
from waystation_server.security.authn import authenticate
from waystation_server.security.principal import Principal
from waystation_server.services import Services

router = APIRouter(include_in_schema=False)

TERRAFORM_PATH = '/.well-known/terraform.json'


async def _principal(request: Request) -> Principal | None:
    try:
        return await authenticate(request)
    except WaystationError:
        return None


@router.api_route(TERRAFORM_PATH, methods=['GET', 'HEAD'])
@access.public
async def terraform_discovery(request: Request) -> Response:
    services: Services = request.app.state.services
    body: dict[str, str] = {}
    repo = next(
        (r for r in await services.catalog.all() if r.format == 'terraform' and r.type is RepoType.HOSTED), None
    )
    if repo is not None and repo.online:
        principal = await _principal(request)
        if principal is not None and access.can_see(principal, repo):
            body = {
                'modules.v1': f'/repository/{repo.name}/v1/modules/',
                'providers.v1': f'/repository/{repo.name}/v1/providers/',
            }
    return JSONResponse(body, headers={'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff'})
