"""Search response models (OpenAPI contract ``Application/Web/openapi/waystation-m0.yaml``, tag ``search``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from waystation_server.repos.model import RepoType
from waystation_server.repos.schemas import Format

SearchKind = Literal['package', 'image', 'file']
SearchMode = Literal['name', 'digest']


class SearchBrowseLink(BaseModel):
    repository: str
    query: dict[str, str] = Field(
        description='Query of the repository Browse view: `image` + `ref` (docker), `package` (npm), '
        '`suite` + `arch` + `q` (apt), `path` (the folder listing of every other format)'
    )


class SearchHit(BaseModel):
    kind: SearchKind
    format: Format
    repository: str
    repository_type: RepoType
    name: str = Field(description='Package, image or file name')
    version: str | None = Field(description='Version (packages) or tag (images)')
    qualifier: str | None = Field(description='Where it is listed: suite/component and architecture (apt), arch (yum)')
    path: str | None = Field(description='File path in the repository (files, apt/yum packages)')
    size: int | None
    sha256: str | None = Field(description='Blob sha256 (files) or manifest digest (images)')
    updated_at: datetime | None
    cached: bool | None = Field(description='In the cache (a listed apt/yum package may not be); null if unknown')
    browse: SearchBrowseLink


class SearchFacetValue(BaseModel):
    value: str
    count: int


class SearchFacets(BaseModel):
    format: list[SearchFacetValue]
    repository: list[SearchFacetValue]
    kind: list[SearchFacetValue]
    type: list[SearchFacetValue]
    total: int = Field(description='Matches with every filter applied')
    capped: bool = Field(description='Counting stopped early: counts are lower bounds')


class SearchPage(BaseModel):
    mode: SearchMode = Field(description='`digest` when the query is a sha256 digest (or a prefix of one)')
    items: list[SearchHit]
    next_cursor: str | None
    facets: SearchFacets | None = Field(description='First page only, unless `facets=false`')
