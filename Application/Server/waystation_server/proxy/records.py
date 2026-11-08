"""Cached asset records (DATA_MODEL ``assets``): what the proxy engine knows about a file it holds."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AssetKind(StrEnum):
    CONTENT = 'content'  # immutable (layers, tarballs, .deb): cached until cleanup (unless content_max_age_s)
    METADATA = 'metadata'  # mutable indexes: revalidated after metadata_max_age_s


class UpstreamMeta(BaseModel):
    model_config = ConfigDict(extra='ignore')

    url: str
    etag: str | None = None
    last_modified: str | None = None  # the upstream's Last-Modified header, verbatim
    fetched_at: datetime  # last time the upstream confirmed this content (fetch or 304)
    status: int = 200


class StoredAsset(BaseModel):
    model_config = ConfigDict(extra='ignore', populate_by_name=True)

    id: str = Field(alias='_id')
    repo_id: str
    path: str
    kind: AssetKind
    blob_sha256: str
    size: int
    content_type: str
    checksums: dict[str, str] = Field(default_factory=dict)  # algorithm -> hex, as verified/computed
    headers: dict[str, str] = Field(default_factory=dict)  # format headers replayed on every response
    upstream: UpstreamMeta
    created_at: datetime
    updated_at: datetime
