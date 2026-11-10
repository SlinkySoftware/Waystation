"""What a provisioning plan or apply reports (OpenAPI ``ProvisioningPlan``, ``ProvisioningStatus``).

Kept apart from the engine so the REST module can name these shapes without importing the engine at load time.
Changes come from ``audit.diff``: secrets appear only as ``redacted`` (``***`` / null), never as values.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ObjectKind = Literal[
    'role', 'cleanup_policy', 'repository', 'group', 'service_account', 'auth_provider', 'webhook', 'settings'
]
PlanAction = Literal['create', 'update', 'unchanged', 'prune']
ItemOutcome = Literal['applied', 'failed', 'skipped']


class ProvisioningChange(BaseModel):
    field: str
    before: Any = None
    after: Any = None
    redacted: bool = Field(default=False, description='A secret: before/after only say whether it is set')


class ProvisioningItem(BaseModel):
    kind: ObjectKind
    name: str
    action: PlanAction
    changes: list[ProvisioningChange] = Field(default_factory=list)
    note: str | None = Field(default=None, description='Why, for items that need explaining')
    outcome: ItemOutcome | None = Field(default=None, description='Apply only: what happened to this item')
    error: str | None = Field(default=None, description='Apply only: why it failed')


class ProvisioningProblem(BaseModel):
    field: str = Field(description='Dotted path into the file (`repositories.2.proxy.upstream_url`), or `file`')
    message: str


class ProvisioningSummary(BaseModel):
    create: int = 0
    update: int = 0
    unchanged: int = 0
    prune: int = 0
    failed: int = 0


class ProvisioningPlan(BaseModel):
    file: str = Field(description='The configured file (`WAYSTATION_PROVISION_FILE`)')
    sha256: str | None = Field(description='Of the file content; null when it could not be read')
    valid: bool = Field(description='False: errors lists why and nothing is (or was) applied')
    applied: bool = Field(default=False, description='Apply only: the changes were made')
    errors: list[ProvisioningProblem] = Field(default_factory=list)
    items: list[ProvisioningItem] = Field(default_factory=list)
    summary: ProvisioningSummary = Field(default_factory=ProvisioningSummary)


class ProvisioningLastApply(BaseModel):
    at: datetime
    by: str = Field(description='`init`, or the administrator who applied it')
    sha256: str | None
    outcome: Literal['applied', 'invalid', 'failed']
    summary: ProvisioningSummary


class ProvisioningStatus(BaseModel):
    configured: bool = Field(description='`WAYSTATION_PROVISION_FILE` is set')
    file: str | None
    last_apply: ProvisioningLastApply | None
