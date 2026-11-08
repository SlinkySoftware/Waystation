"""Repository configuration as stored in MongoDB (DATA_MODEL ``repositories``) and used by the request path.

Secrets are stored only encrypted (``auth.secret_enc``, AES-GCM with ``WAYSTATION_SECRET_KEYS``, associated data
``repo:<id>``) and never leave the server through the API (``secret_set`` is returned instead).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_METADATA_MAX_AGE_S = 1800
DEFAULT_NEGATIVE_TTL_S = 600


def secret_aad(repo_id: str) -> bytes:
    """Associated data binding an encrypted upstream secret to its repository."""
    return f'repo:{repo_id}'.encode()


class RepoType(StrEnum):
    PROXY = 'proxy'
    HOSTED = 'hosted'
    GROUP = 'group'


class AuthKind(StrEnum):
    NONE = 'none'
    BASIC = 'basic'
    BEARER = 'bearer'


class _Stored(BaseModel):
    model_config = ConfigDict(extra='ignore', frozen=True, populate_by_name=True)


class StoredUpstreamAuth(_Stored):
    kind: AuthKind = AuthKind.NONE
    username: str | None = None
    secret_enc: str | None = None


class StoredProxy(_Stored):
    upstream_url: str
    auth: StoredUpstreamAuth = Field(default_factory=StoredUpstreamAuth)
    metadata_max_age_s: int = DEFAULT_METADATA_MAX_AGE_S
    content_max_age_s: int | None = None
    negative_ttl_s: int = DEFAULT_NEGATIVE_TTL_S
    stale_on_error: bool = True
    offline: bool = False
    allow_private_upstream: bool = False


class RepoConfig(_Stored):
    id: str = Field(alias='_id')
    name: str
    format: str
    type: RepoType
    online: bool = True
    description: str = ''
    proxy: StoredProxy | None = None
    format_options: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    updated_by: str | None = None
    config_version: int = 1

    def to_doc(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode='python')
