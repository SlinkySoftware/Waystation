"""External sign-in providers (SPECIFICATION §4.9; DATA_MODEL ``auth_providers``, ``groups``).

A provider record holds its kind (``ldap`` or ``oidc``), display name, sign-in ``order``, ``enabled`` and the kind's
configuration (``ldap`` or ``oidc``; the other one is null). Secrets (the LDAP service-bind password, the OIDC client
secret) are stored only encrypted with ``WAYSTATION_SECRET_KEYS`` (associated data ``auth_provider:<id>``) and never
leave the server: reads return ``bind_password_set`` / ``client_secret_set``. A provider's group mapping (external
group → roles) is stored as ``groups`` documents (``source`` = the kind, ``provider_id``, ``external_id``), so the
RBAC side reads one collection whatever the provider (``security.groups``).

``ProviderCatalog`` keeps the records in memory for sign-in (refreshed every few seconds, invalidated on a local
write), like the repository catalog.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from waystation_common.crypto import Keyring

SECRET_AAD_PREFIX = 'auth_provider:'  # noqa: S105 - associated data label, not a secret
CATALOG_TTL_S = 5.0


def secret_aad(provider_id: str) -> bytes:
    """Associated data binding an encrypted provider secret to its provider."""
    return f'{SECRET_AAD_PREFIX}{provider_id}'.encode()


class ProviderKind(StrEnum):
    LDAP = 'ldap'
    OIDC = 'oidc'


class LdapSecurity(StrEnum):
    LDAPS = 'ldaps'
    STARTTLS = 'starttls'
    NONE = 'none'


class GroupMode(StrEnum):
    NONE = 'none'
    MEMBER_OF = 'member_of'
    SEARCH = 'search'


class MemberValue(StrEnum):
    DN = 'dn'
    USERNAME = 'username'


ServerName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]
Attribute = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Dn = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]


class _Model(BaseModel):
    # Reads document every default as present (contract "required" on responses).
    model_config = ConfigDict(extra='forbid', json_schema_serialization_defaults_required=True)


class LdapConnectionBase(_Model):
    servers: list[ServerName] = Field(
        min_length=1, max_length=8, description='`host`, `host:port`, `ldap://host[:port]` or `ldaps://host[:port]`'
    )
    security: LdapSecurity = LdapSecurity.STARTTLS
    ca_pem: str | None = Field(None, max_length=65536, description='PEM CA bundle trusted instead of the system store')
    bind_dn: str | None = Field(None, max_length=1024, description='Service account for searches; empty = anonymous')
    connect_timeout_s: float = Field(5, ge=1, le=30)
    timeout_s: float = Field(10, ge=1, le=60)


class LdapConnectionWrite(LdapConnectionBase):
    bind_password: str | None = Field(
        None, max_length=1024, json_schema_extra={'writeOnly': True}, description='Omit or null to keep the stored one'
    )


class LdapConnectionView(LdapConnectionBase):
    bind_password_set: bool = Field(json_schema_extra={'readOnly': True})


class StoredLdapConnection(LdapConnectionBase):
    model_config = ConfigDict(extra='ignore')

    bind_password_enc: str | None = None


class LdapUsers(_Model):
    base_dn: Dn
    filter: str = Field('(objectClass=person)', max_length=1024)
    username_attribute: Attribute = 'uid'
    display_name_attribute: Attribute = 'cn'
    email_attribute: Attribute = 'mail'


class LdapGroups(_Model):
    mode: GroupMode = GroupMode.MEMBER_OF
    base_dn: str | None = Field(None, max_length=1024)
    filter: str = Field('(objectClass=groupOfNames)', max_length=1024)
    member_attribute: Attribute = 'member'
    member_value: MemberValue = MemberValue.DN
    name_attribute: Attribute = 'cn'
    member_of_attribute: Attribute = 'memberOf'
    nested: bool = False
    require_group: str | None = Field(None, max_length=1024)


class LdapConfigBase(_Model):
    connection: LdapConnectionBase
    users: LdapUsers
    groups: LdapGroups
    cache_ttl_s: int = Field(60, ge=0, le=900, description='How long a successful password check is reused (0 = never)')


class LdapConfigWrite(LdapConfigBase):
    connection: LdapConnectionWrite


class LdapConfigView(LdapConfigBase):
    connection: LdapConnectionView


class StoredLdapConfig(LdapConfigBase):
    model_config = ConfigDict(extra='ignore')

    connection: StoredLdapConnection


# ---------------------------------------------------------------------------- OIDC (T-A02)
# Asymmetric signature algorithms only: ``none`` and the HMAC family (keyed with the client secret, open to algorithm
# confusion with public keys) are never accepted for ID tokens.
OIDC_SIGNING_ALGS = (
    'RS256',
    'RS384',
    'RS512',
    'PS256',
    'PS384',
    'PS512',
    'ES256',
    'ES384',
    'ES512',
    'EdDSA',
    'Ed25519',
)
OidcAlg = Literal['RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384', 'ES512', 'EdDSA', 'Ed25519']


class OidcClientAuth(StrEnum):
    CLIENT_SECRET_BASIC = 'client_secret_basic'  # noqa: S105 - a method name
    CLIENT_SECRET_POST = 'client_secret_post'  # noqa: S105 - a method name
    NONE = 'none'  # public client: PKCE only


Claim = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256, pattern=r'^\S+$')]
ScopeName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128, pattern=r'^[!#-\[\]-~]+$')
]
Url = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]


def _default_scopes() -> list[str]:
    return ['openid', 'profile', 'email']


def _default_algs() -> list[OidcAlg]:
    return list(OIDC_SIGNING_ALGS)  # type: ignore[arg-type]


class OidcConfigBase(_Model):
    issuer: Url = Field(description='Issuer URL exactly as the provider states it (discovery: `/.well-known/...`)')
    client_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
    client_auth: OidcClientAuth = OidcClientAuth.CLIENT_SECRET_BASIC
    scopes: list[ScopeName] = Field(
        default_factory=_default_scopes, max_length=20, description='`openid` is always sent'
    )
    username_claim: Claim = 'preferred_username'
    display_name_claim: Claim = 'name'
    email_claim: Claim = 'email'
    groups_claim: Claim | None = Field('groups', description="Claim listing the user's groups (dotted path allowed)")
    require_group: str | None = Field(None, max_length=1024, description='Only members of this group may sign in')
    auto_provision: bool = Field(True, description='Create a Waystation user at the first sign-in')
    use_userinfo: bool = Field(False, description='Also read claims from the userinfo endpoint')
    rp_logout: bool = Field(True, description='Also sign out at the provider when it supports RP-initiated logout')
    allow_private: bool = Field(False, description='The provider may resolve to private addresses (SSRF guard)')
    signing_algs: list[OidcAlg] = Field(
        default_factory=_default_algs, min_length=1, max_length=12, description='Accepted ID token algorithms'
    )
    timeout_s: float = Field(10, ge=1, le=60)


class OidcConfigWrite(OidcConfigBase):
    client_secret: str | None = Field(
        None, max_length=4096, json_schema_extra={'writeOnly': True}, description='Omit or null to keep the stored one'
    )


class OidcConfigView(OidcConfigBase):
    client_secret_set: bool = Field(json_schema_extra={'readOnly': True})
    redirect_uri: str = Field(json_schema_extra={'readOnly': True}, description='Register at the provider')
    post_logout_redirect_uri: str = Field(json_schema_extra={'readOnly': True}, description='Register at the provider')


class StoredOidcConfig(OidcConfigBase):
    model_config = ConfigDict(extra='ignore')

    client_secret_enc: str | None = None


class GroupMapping(_Model):
    external_group: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)] = Field(
        description='Group DN or name (case-insensitive)'
    )
    roles: list[str] = Field(max_length=20)


class AuthProviderWrite(_Model):
    kind: ProviderKind
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    enabled: bool = True
    order: int = Field(100, ge=0, le=1000)
    ldap: LdapConfigWrite | None = Field(None, description='Kind `ldap` only')
    oidc: OidcConfigWrite | None = Field(None, description='Kind `oidc` only')
    group_mapping: list[GroupMapping] = Field(default_factory=list, max_length=500)


class ProviderRecord(BaseModel):
    """An ``auth_providers`` document."""

    model_config = ConfigDict(extra='ignore', populate_by_name=True)

    id: str = Field(alias='_id')
    kind: ProviderKind
    name: str
    enabled: bool = True
    order: int = 100
    ldap: StoredLdapConfig | None = None
    oidc: StoredOidcConfig | None = None
    config_version: int = 1
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    updated_by: str | None = None

    @property
    def source(self) -> str:
        """``users.source`` / ``external_groups.provider`` value of this provider's users."""
        return f'{self.kind.value}:{self.id}'


def decrypt_bind_password(keyring: Keyring | None, record: ProviderRecord) -> str | None:
    enc = record.ldap.connection.bind_password_enc if record.ldap else None
    if not enc or keyring is None:
        return None
    return keyring.decrypt_str(enc, aad=secret_aad(record.id))


def decrypt_client_secret(keyring: Keyring | None, record: ProviderRecord) -> str | None:
    enc = record.oidc.client_secret_enc if record.oidc else None
    if not enc or keyring is None:
        return None
    return keyring.decrypt_str(enc, aad=secret_aad(record.id))


_DN_SPACES = re.compile(r'\s*([,=+])\s*')


def group_key(value: str) -> str:
    """Case- and spacing-insensitive key for matching group DNs/names (``CN=Ops, DC=x`` == ``cn=ops,dc=x``)."""
    return _DN_SPACES.sub(r'\1', value.strip().lower())


class ProviderCatalog:
    """In-memory provider list for sign-in (refreshed every ``CATALOG_TTL_S``; ``invalidate()`` after a write)."""

    def __init__(self, load: Callable[[], Awaitable[list[ProviderRecord]]], ttl_s: float = CATALOG_TTL_S) -> None:
        self._load = load
        self._ttl_s = ttl_s
        self._records: list[ProviderRecord] = []
        self._loaded_at = -1e18
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._loaded_at = -1e18

    async def all(self) -> list[ProviderRecord]:
        if time.monotonic() - self._loaded_at < self._ttl_s:
            return self._records
        async with self._lock:
            if time.monotonic() - self._loaded_at >= self._ttl_s:
                self._records = sorted(await self._load(), key=lambda r: (r.order, r.name.lower()))
                self._loaded_at = time.monotonic()
        return self._records

    async def enabled(self, kind: str | None = None) -> list[ProviderRecord]:
        return [r for r in await self.all() if r.enabled and (kind is None or r.kind == kind)]

    async def get(self, provider_id: str) -> ProviderRecord | None:
        return next((r for r in await self.all() if r.id == provider_id), None)


def dump_stored(config: LdapConfigWrite, bind_password_enc: str | None) -> dict[str, Any]:
    """The stored form of an LDAP configuration (the secret replaced by its ciphertext)."""
    data = config.model_dump(mode='json')
    data['connection'].pop('bind_password', None)
    data['connection']['bind_password_enc'] = bind_password_enc
    return data


def dump_stored_oidc(config: OidcConfigWrite, client_secret_enc: str | None) -> dict[str, Any]:
    """The stored form of an OIDC configuration (the client secret replaced by its ciphertext)."""
    data = config.model_dump(mode='json', exclude={'client_secret'})
    data['client_secret_enc'] = client_secret_enc
    return data
