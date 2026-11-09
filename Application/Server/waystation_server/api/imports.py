"""``/api/v1/import/nexus/*``: import repositories from Nexus Repository 3 (SPECIFICATION §4.12; needs ``admin``).

``preview`` is a dry run (nothing is written); ``apply`` creates the selected repositories. Both read Nexus with the
credentials in the request, which are never stored, logged or returned.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from waystation_server.api.deps import ServicesDep
from waystation_server.migration import nexus_import
from waystation_server.migration.nexus_import import NexusApplyRequest, NexusImportReport, NexusImportRequest
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate

router = APIRouter(tags=['import'])

PROBLEM = {'content': {'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}}}


@router.post(
    '/import/nexus/preview',
    response_model=NexusImportReport,
    operation_id='previewNexusImport',
    responses={422: PROBLEM, 502: PROBLEM},
)
@access.requires(Action.ADMIN)
async def preview_nexus_import(body: NexusImportRequest, request: Request, svc: ServicesDep) -> NexusImportReport:
    principal = await authenticate(request)
    return await nexus_import.preview(svc, principal, body)


@router.post(
    '/import/nexus/apply',
    response_model=NexusImportReport,
    operation_id='applyNexusImport',
    responses={422: PROBLEM, 502: PROBLEM},
)
@access.requires(Action.ADMIN)
async def apply_nexus_import(body: NexusApplyRequest, request: Request, svc: ServicesDep) -> NexusImportReport:
    principal = await authenticate(request)
    return await nexus_import.apply(svc, principal, body)
