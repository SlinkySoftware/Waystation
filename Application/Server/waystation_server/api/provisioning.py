"""``/api/v1/provisioning``: the declarative provisioning file (``Documents/Operations/PROVISIONING.md``; ``admin``).

- ``GET /provisioning``: whether a file is configured (``WAYSTATION_PROVISION_FILE``) and how its last apply went.
- ``POST /provisioning/plan``: what applying the configured file would change (nothing is written).
- ``POST /provisioning/apply``: apply it (only when the whole file is valid); every change is audited with
  ``details.source = provisioning``.

Both act on the configured file only: the API never accepts a document, so a caller cannot make the server read
files or environment variables through secret references. 409 when no file is configured.

The engine is imported when a route runs: it builds on the other API modules (validation, views), which import
this package's ``__init__`` first.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from waystation_common.errors import Conflict
from waystation_server.api.deps import ServicesDep
from waystation_server.provisioning.report import ProvisioningPlan, ProvisioningStatus
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate
from waystation_server.services import Services

router = APIRouter(tags=['admin'])

PROBLEM = {'content': {'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}}}


def _file(svc: Services) -> str:
    path = svc.settings.provision_file
    if path is None:
        raise Conflict('no provisioning file is configured (WAYSTATION_PROVISION_FILE)')
    return str(path)


@router.get('/provisioning', response_model=ProvisioningStatus, operation_id='getProvisioning')
@access.requires(Action.ADMIN)
async def get_provisioning(svc: ServicesDep) -> ProvisioningStatus:
    from waystation_server.provisioning import engine as provisioning  # noqa: PLC0415 - see the module docstring

    path = svc.settings.provision_file
    return ProvisioningStatus(
        configured=path is not None,
        file=str(path) if path is not None else None,
        last_apply=await provisioning.last_apply(svc),
    )


@router.post(
    '/provisioning/plan', response_model=ProvisioningPlan, operation_id='planProvisioning', responses={409: PROBLEM}
)
@access.requires(Action.ADMIN)
async def plan_provisioning(request: Request, svc: ServicesDep) -> ProvisioningPlan:
    from waystation_server.provisioning import engine as provisioning  # noqa: PLC0415 - see the module docstring
    from waystation_server.provisioning import loader  # noqa: PLC0415

    principal = await authenticate(request)
    loaded = await loader.load(_file(svc))
    return await provisioning.plan(provisioning.Context(svc=svc, principal=principal, by=principal.name), loaded)


@router.post(
    '/provisioning/apply', response_model=ProvisioningPlan, operation_id='applyProvisioning', responses={409: PROBLEM}
)
@access.requires(Action.ADMIN)
async def apply_provisioning(request: Request, svc: ServicesDep) -> ProvisioningPlan:
    from waystation_server.provisioning import engine as provisioning  # noqa: PLC0415 - see the module docstring
    from waystation_server.provisioning import loader  # noqa: PLC0415

    principal = await authenticate(request)
    loaded = await loader.load(_file(svc))
    return await provisioning.apply(provisioning.Context(svc=svc, principal=principal, by=principal.name), loaded)
