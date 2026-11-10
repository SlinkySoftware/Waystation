"""The provisioning file schema (``Documents/Operations/PROVISIONING.md``; JSON Schema: ``make provision-schema``).

One YAML document, ``version: 1``. Every section is optional; a section that is absent is not managed at all (its
objects are never touched), a section that is present declares the objects it manages. ``prune`` deletes the
objects of a present section that the file manages (``managed_by: provisioning``) but no longer declares.

Secrets are **never inline**: wherever a value is a password, token or signing key the file holds a reference,
``{file: /run/secrets/name}`` (the file's content, trailing newline removed) or ``{env: NAME}`` (an environment
variable of the process applying the file). A plain string there is refused without echoing it.

The shapes reuse the REST API's write models (same field names, same limits) so a repository, role or webhook reads
the same in the file as in the API; only the secret fields differ.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from waystation_server.api.cleanup import CleanupPolicyWrite
from waystation_server.api.roles import MAX_PERMISSIONS, RolePermissionBody
from waystation_server.api.storage_guard import StorageGuardSettings
from waystation_server.api.webhooks import WebhookWrite
from waystation_server.repos.model import AuthKind
from waystation_server.repos.schemas import ProxyConfig, RepositoryWrite
from waystation_server.security.providers import (
    GroupMapping,
    LdapConfigBase,
    LdapConnectionBase,
    OidcConfigBase,
    ProviderKind,
)

MANAGED_BY = 'provisioning'
FILE_VERSION = 1
MAX_ITEMS = 1000  # per section
SECRET_MAX_BYTES = 65536
_ENV_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')
INLINE_SECRET = 'inline secrets are not allowed: use {file: /path/to/secret} or {env: VARIABLE}'  # noqa: S105


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SecretRef(_Strict):
    """Where a secret comes from: exactly one of ``file`` (absolute path, read when the file is applied) or ``env``."""

    file: str | None = Field(default=None, min_length=2, max_length=4096, description='Absolute path of a file')
    env: str | None = Field(default=None, description='Name of an environment variable')

    @model_validator(mode='before')
    @classmethod
    def _reference(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise ValueError(INLINE_SECRET)  # never echo the value
        return data

    @model_validator(mode='after')
    def _one(self) -> SecretRef:
        if (self.file is None) == (self.env is None):
            raise ValueError('give exactly one of file or env')
        if self.file is not None and (not self.file.startswith('/') or '\0' in self.file):
            raise ValueError('file must be an absolute path')
        if self.env is not None and not _ENV_NAME.fullmatch(self.env):
            raise ValueError('env must be a variable name (letters, digits, _)')
        return self

    def describe(self) -> str:
        return f'file {self.file}' if self.file is not None else f'env {self.env}'


# ---------------------------------------------------------------------------- sections
class Prune(_Strict):
    """Per section: delete objects the file manages but no longer declares (default: never delete)."""

    roles: bool = False
    cleanup_policies: bool = False
    repositories: bool = False
    groups: bool = False
    service_accounts: bool = False
    auth_providers: bool = False
    webhooks: bool = False


class RoleSpec(_Strict):
    id: str = Field(pattern=r'^[a-z][a-z0-9-]{1,47}$', description='What users, groups and accounts store')
    name: str = Field(min_length=1, max_length=64)
    description: str = Field('', max_length=500)
    permissions: list[RolePermissionBody] = Field(min_length=1, max_length=MAX_PERMISSIONS)


class CleanupPolicySpec(CleanupPolicyWrite):
    """As ``CleanupPolicyWrite``; repositories attach policies by name (``repositories[].cleanup_policies``)."""


class UpstreamAuthSpec(_Strict):
    kind: AuthKind = AuthKind.NONE
    username: str | None = Field(default=None, max_length=256)
    secret: SecretRef | None = Field(default=None, description='Password or token (basic and bearer)')


class ProxySpec(ProxyConfig):
    auth: UpstreamAuthSpec = Field(default_factory=UpstreamAuthSpec)  # type: ignore[assignment]


class RepositorySpec(RepositoryWrite):
    online: bool = Field(default=True, description='Accepts requests')
    proxy: ProxySpec | None = None
    cleanup_policies: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=20, description='Names of cleanup policies to attach, in run order'
    )

    def write(self, secret: str | None) -> RepositoryWrite:
        """The API write body with the resolved upstream secret (None: keep the stored one)."""
        data = self.model_dump(mode='python', exclude={'cleanup_policies', 'proxy'})
        if self.proxy is not None:
            proxy = self.proxy.model_dump(mode='python', exclude={'auth'})
            auth = self.proxy.auth
            proxy['auth'] = {'kind': auth.kind, 'username': auth.username, 'secret': secret}
            data['proxy'] = proxy
        return RepositoryWrite.model_validate(data)


class GroupSpec(_Strict):
    """A local group (groups mapped from a provider are declared in its ``group_mapping``)."""

    name: str = Field(min_length=1, max_length=64)
    description: str = Field('', max_length=500)
    roles: list[str] = Field(default_factory=list, max_length=20)


class ServiceAccountSpec(_Strict):
    name: str = Field(pattern=r'^[a-z0-9][a-z0-9._-]{1,62}$')
    description: str = Field('', max_length=500)
    roles: list[str] = Field(default_factory=list, max_length=20)
    disabled: bool = False


class LdapConnectionSpec(LdapConnectionBase):
    bind_password: SecretRef | None = Field(default=None, description='Needed with a bind DN')


class LdapConfigSpec(LdapConfigBase):
    connection: LdapConnectionSpec


class OidcConfigSpec(OidcConfigBase):
    client_secret: SecretRef | None = Field(default=None, description='Needed unless client_auth is none')


class AuthProviderSpec(_Strict):
    kind: ProviderKind
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    enabled: bool = True
    order: int = Field(100, ge=0, le=1000)
    ldap: LdapConfigSpec | None = None
    oidc: OidcConfigSpec | None = None
    group_mapping: list[GroupMapping] = Field(
        default_factory=list, max_length=500, description='External group -> roles'
    )


class WebhookSpec(WebhookWrite):
    secret: SecretRef = Field(description='HMAC-SHA256 signing key')  # type: ignore[assignment]
    headers: dict[str, SecretRef] = Field(  # type: ignore[assignment]
        default_factory=dict, description='Extra request headers; values are secret references'
    )


class SettingsSpec(_Strict):
    """Instance settings; a field left out is not managed."""

    instance_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None = None
    require_totp_for_admins: bool | None = None
    anonymous_access: list[str] | None = Field(
        default=None, max_length=20, description='Roles of requests without credentials ([] = no anonymous access)'
    )
    default_docker_repository: str | None = Field(
        default=None, max_length=64, description='The docker repository that receives unprefixed names'
    )
    storage_guard: StorageGuardSettings | None = None


class ProvisioningFile(_Strict):
    version: Literal[1] = Field(description='File format version')
    prune: Prune = Field(default_factory=Prune)
    roles: list[RoleSpec] | None = Field(default=None, max_length=MAX_ITEMS)
    cleanup_policies: list[CleanupPolicySpec] | None = Field(default=None, max_length=MAX_ITEMS)
    repositories: list[RepositorySpec] | None = Field(default=None, max_length=MAX_ITEMS)
    groups: list[GroupSpec] | None = Field(default=None, max_length=MAX_ITEMS)
    service_accounts: list[ServiceAccountSpec] | None = Field(default=None, max_length=MAX_ITEMS)
    auth_providers: list[AuthProviderSpec] | None = Field(default=None, max_length=MAX_ITEMS)
    webhooks: list[WebhookSpec] | None = Field(default=None, max_length=MAX_ITEMS)
    settings: SettingsSpec | None = None

    @field_validator('roles', 'cleanup_policies', 'repositories', 'groups', 'service_accounts', 'webhooks')
    @classmethod
    def _unique(cls, items: list[Any] | None) -> list[Any] | None:
        if items:
            seen: set[str] = set()
            for item in items:
                key = str(getattr(item, 'id', None) or item.name).lower()
                if key in seen:
                    raise ValueError(f'{key!r} is declared twice')
                seen.add(key)
        return items

    @field_validator('auth_providers')
    @classmethod
    def _unique_providers(cls, items: list[AuthProviderSpec] | None) -> list[AuthProviderSpec] | None:
        names = [p.name.strip().lower() for p in items or []]
        for name in names:
            if names.count(name) > 1:
                raise ValueError(f'{name!r} is declared twice')
        return items


def json_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12, as pydantic writes it) of the file (``make provision-schema``; for editors)."""
    schema = ProvisioningFile.model_json_schema(mode='validation')
    schema['title'] = 'Waystation provisioning file'
    return schema
