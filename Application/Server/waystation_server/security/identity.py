"""User and session records (DATA_MODEL ``users``, ``sessions``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid', json_schema_serialization_defaults_required=True)

    theme: Literal['light', 'dark', 'system'] = 'system'
    density: Literal['comfortable', 'compact'] = 'comfortable'


class ExternalGroupRef(BaseModel):
    """A group an external provider reported at the user's last sign-in through it (``users.external_groups``)."""

    model_config = ConfigDict(extra='ignore')

    provider: str  # provider source, ``ldap:<id>`` / ``oidc:<id>``
    id: str  # LDAP group DN, OIDC claim value
    name: str


class LinkedIdentity(BaseModel):
    """An external account linked to the user (``users.identities``): same username, SPECIFICATION §4.9."""

    model_config = ConfigDict(extra='ignore')

    provider: str  # provider source, ``ldap:<id>`` / ``oidc:<id>``
    subject: str  # LDAP entry DN, OIDC ``sub``
    linked_at: datetime
    last_login: datetime | None = None


class UserRecord(BaseModel):
    model_config = ConfigDict(extra='ignore', populate_by_name=True)

    id: str = Field(alias='_id')
    username: str  # lower-case
    display_name: str
    email: str | None = None
    source: str = 'local'  # ``local`` or the provider that created the user (``ldap:<id>``, ``oidc:<id>``)
    password_hash: str | None = None
    disabled: bool = False
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)  # local group ids
    external_groups: list[ExternalGroupRef] = Field(default_factory=list)
    identities: list[LinkedIdentity] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)
    must_change_password: bool = False
    # Two-factor sign-in (TOTP, local accounts; ``security.second_factor``). The secret is AES-GCM encrypted (aad
    # ``user-totp:<id>``); recovery codes are keyed hashes; ``totp_last_step`` is the last accepted time step.
    totp_enc: str | None = None
    totp_enabled_at: datetime | None = None
    totp_last_step: int | None = None
    totp_recovery: list[str] = Field(default_factory=list)
    totp_pending_enc: str | None = None  # enrolment started, not yet confirmed with a code
    totp_pending_expires_at: datetime | None = None
    failed_logins: int = 0
    locked_until: datetime | None = None
    last_login: datetime | None = None
    created_at: datetime


class TokenOwner(BaseModel):
    """``api_tokens.principal``: whose grants the token narrows."""

    model_config = ConfigDict(extra='ignore')

    kind: Literal['user', 'service_account']
    id: str


class ApiTokenRecord(BaseModel):
    """``api_tokens``: only a keyed hash of the secret is stored (``security.tokens``), plus a display prefix."""

    model_config = ConfigDict(extra='ignore', populate_by_name=True)

    id: str = Field(alias='_id')
    principal: TokenOwner
    name: str
    prefix: str
    hash: str
    scopes: list[str] = Field(default_factory=list)
    repo_globs: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    last_used_ip: str | None = None
    created_at: datetime
    created_by: str | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None


class ServiceAccountRecord(BaseModel):
    """``service_accounts``: a non-human principal that holds roles and authenticates only with API tokens."""

    model_config = ConfigDict(extra='ignore', populate_by_name=True)

    id: str = Field(alias='_id')
    name: str  # lower-case, unique
    description: str = ''
    roles: list[str] = Field(default_factory=list)
    disabled: bool = False
    owner_user_id: str | None = None
    managed_by: str | None = None  # ``provisioning``: declared in the provisioning file
    created_at: datetime
    updated_at: datetime | None = None


class SessionRecord(BaseModel):
    """``_id`` is the sha256 of the cookie value: a database dump does not reveal usable session ids."""

    model_config = ConfigDict(extra='ignore', populate_by_name=True)

    id: str = Field(alias='_id')
    user_id: str
    csrf: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip: str | None = None
    user_agent: str | None = None
    provider_id: str | None = None  # the OIDC provider the session came from (RP-initiated logout)
    id_token_enc: str | None = None  # its ID token, encrypted (``id_token_hint`` at logout)


class RolePermission(BaseModel):
    """One ``(action, scope)`` of a custom role (validated by ``security.access.permission_error``)."""

    model_config = ConfigDict(extra='ignore')

    action: str
    scope: str


class RoleRecord(BaseModel):
    """``roles``: a custom role (built-in roles live in code, ``security.access.BUILTIN_ROLES``)."""

    model_config = ConfigDict(extra='ignore', populate_by_name=True)

    id: str = Field(alias='_id')  # slug, also the name users/groups/service accounts store in ``roles[]``
    name: str
    description: str = ''
    builtin: bool = False
    permissions: list[RolePermission] = Field(default_factory=list)
    managed_by: str | None = None  # ``provisioning``: declared in the provisioning file
    created_at: datetime
    created_by: str | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None
