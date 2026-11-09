"""Repository API shapes (OpenAPI contract ``RepositoryWrite``, ``ProxyConfig``, ``UpstreamAuth``).

``format_options`` is a free object here; its schema per format comes from the format descriptor's
``options_model`` (validated by ``repos.service``; published as ``FormatOptions`` = ``oneOf`` the registered option
models by ``api.openapi``).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from waystation_server.repos.model import (
    DEFAULT_METADATA_MAX_AGE_S,
    DEFAULT_NEGATIVE_TTL_S,
    AuthKind,
    RepoType,
    WritePolicy,
)

NAME_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$'

Format = Literal[
    'docker',
    'npm',
    'apt',
    'raw',
    'yum',
    'pypi',
    'maven',
    'go',
    'helm',
    'terraform',
    'galaxy',
    'swift',
    'nuget',
    'cargo',
    'rubygems',
    'composer',
    'conda',
    'apk',
    'huggingface',
]
RepositoryStatus = Literal['ok', 'degraded', 'blocked', 'offline']


class UpstreamAuth(BaseModel):
    """Upstream credentials. Secrets are write-only; reads return ``secret_set`` instead."""

    model_config = ConfigDict(extra='forbid', json_schema_serialization_defaults_required=True)

    kind: AuthKind = AuthKind.NONE
    username: str | None = Field(default=None, max_length=256)
    secret: str | None = Field(
        default=None,
        max_length=4096,
        exclude=True,
        description='Password or token. Omit or null on update to keep the stored secret.',
        json_schema_extra={'writeOnly': True},
    )
    secret_set: bool = Field(default=False, json_schema_extra={'readOnly': True})


class ProxyConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', json_schema_serialization_defaults_required=True)

    upstream_url: str = Field(max_length=2048, json_schema_extra={'format': 'uri'})
    auth: UpstreamAuth = Field(default_factory=UpstreamAuth)
    metadata_max_age_s: int = Field(default=DEFAULT_METADATA_MAX_AGE_S, ge=0)
    content_max_age_s: int | None = Field(default=None, ge=0, description='null = forever (immutable content)')
    negative_ttl_s: int = Field(default=DEFAULT_NEGATIVE_TTL_S, ge=0)
    stale_on_error: bool = True
    offline: bool = False
    allow_private_upstream: bool = False


class HostedConfig(BaseModel):
    """Hosted repositories: what clients may overwrite (SPECIFICATION §4.2)."""

    model_config = ConfigDict(extra='forbid', json_schema_serialization_defaults_required=True)

    write_policy: WritePolicy = Field(
        default=WritePolicy.ALLOW_REDEPLOY,
        description='`allow_redeploy`: versions/tags may be overwritten; `disable_redeploy`: never; `read_only`: '
        'no uploads at all',
    )
    immutable_tag_patterns: list[Annotated[str, StringConstraints(min_length=1, max_length=256)]] = Field(
        default_factory=list,
        max_length=50,
        description='Docker: regular expressions (full match); a matching tag never moves once pushed, e.g. '
        '`v[0-9]+\\.[0-9]+\\.[0-9]+`',
    )


class RepositoryWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(pattern=NAME_PATTERN)
    format: Format
    type: RepoType
    online: bool = Field(description='Accepts requests')
    description: str = Field(default='', max_length=500)
    proxy: ProxyConfig | None = None
    hosted: HostedConfig | None = Field(default=None, description='Hosted repositories only (defaults when omitted)')
    format_options: dict[str, Any] | None = Field(
        default=None, description="Format-specific options; the shape follows the repository's `format`"
    )


class UpstreamTestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    format: Format
    repository: str | None = Field(
        default=None,
        description='Existing repository name: reuse its stored secret when `proxy.auth.secret` is omitted',
    )
    proxy: ProxyConfig
    format_options: dict[str, Any] | None = None
