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
DEFAULT_AUTO_BLOCK_FAILURES = 5
DEFAULT_AUTO_BLOCK_INITIAL_S = 30
DEFAULT_AUTO_BLOCK_MAX_S = 1800


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


class WritePolicy(StrEnum):
    """Hosted repositories (SPECIFICATION §4.2): may an existing version (docker: a tag) be overwritten?"""

    ALLOW_REDEPLOY = 'allow_redeploy'
    DISABLE_REDEPLOY = 'disable_redeploy'
    READ_ONLY = 'read_only'


class _Stored(BaseModel):
    model_config = ConfigDict(extra='ignore', frozen=True, populate_by_name=True)


class StoredUpstreamAuth(_Stored):
    kind: AuthKind = AuthKind.NONE
    username: str | None = None
    secret_enc: str | None = None


class StoredAutoBlock(_Stored):
    """Auto-block (SPECIFICATION §4.1): after ``failures`` consecutive upstream failures stop calling the upstream
    for ``initial_s``, doubling after every failed probe up to ``max_s``. The live state is in Valkey
    (``upstream:<id>:health``, ``proxy.breaker``)."""

    enabled: bool = True
    failures: int = DEFAULT_AUTO_BLOCK_FAILURES
    initial_s: int = DEFAULT_AUTO_BLOCK_INITIAL_S
    max_s: int = DEFAULT_AUTO_BLOCK_MAX_S


class StoredProxy(_Stored):
    upstream_url: str
    auth: StoredUpstreamAuth = Field(default_factory=StoredUpstreamAuth)
    metadata_max_age_s: int = DEFAULT_METADATA_MAX_AGE_S
    content_max_age_s: int | None = None
    negative_ttl_s: int = DEFAULT_NEGATIVE_TTL_S
    stale_on_error: bool = True
    offline: bool = False
    allow_private_upstream: bool = False
    ca_pem: str | None = None  # extra trusted CA certificates (PEM) for this repository's upstream TLS
    http_proxy: str | None = None  # outbound proxy for this repository (null: WAYSTATION_HTTP_PROXY / NO_PROXY)
    auto_block: StoredAutoBlock = Field(default_factory=StoredAutoBlock)


class StoredHosted(_Stored):
    write_policy: WritePolicy = WritePolicy.ALLOW_REDEPLOY
    # Regular expressions (Python ``re``, full match): matching docker tags never move once pushed.
    immutable_tag_patterns: tuple[str, ...] = ()


class RepoConfig(_Stored):
    id: str = Field(alias='_id')
    name: str
    format: str
    type: RepoType
    online: bool = True
    description: str = ''
    proxy: StoredProxy | None = None
    hosted: StoredHosted | None = None  # hosted repositories (read as defaults when absent)
    format_options: dict[str, Any] = Field(default_factory=dict)
    cleanup_policy_ids: tuple[str, ...] = ()  # attached cleanup policies, in run order
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    updated_by: str | None = None
    config_version: int = 1

    def to_doc(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode='python')
