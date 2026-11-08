"""``GET /api/v1/search``: global search across the repositories the principal can browse (OpenAPI tag ``search``).

Results and facet counts only ever come from ``access.filter_repos(principal, browse, ...)``: a hidden repository is
never searched, so it cannot show up in a hit, a count or a facet value (SPECIFICATION §4.6, §4.10).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from waystation_server.api.deps import ServicesDep
from waystation_server.repos.model import RepoType
from waystation_server.repos.schemas import Format
from waystation_server.search import engine
from waystation_server.search.models import SearchFacets, SearchKind, SearchPage
from waystation_server.security import access
from waystation_server.security.access import Action
from waystation_server.security.authn import authenticate

router = APIRouter(tags=['search'])

PROBLEM = {'content': {'application/problem+json': {'schema': {'$ref': '#/components/schemas/Problem'}}}}
BAD_REQUEST: dict[int | str, dict[str, Any]] = {400: PROBLEM}


@router.get('/search', response_model=SearchPage, operation_id='search', responses=BAD_REQUEST)
@access.filtered(Action.BROWSE)
async def search(
    request: Request,
    svc: ServicesDep,
    q: Annotated[
        str,
        Query(
            max_length=256,
            description='`<name prefix>[ <version prefix>]` (case-insensitive; also matches the last `/` segment of '
            'a name), or a digest: `sha256:<8-64 hex>` / 64 hex digits. Empty lists everything',
        ),
    ] = '',
    format: Annotated[list[Format] | None, Query(max_length=20)] = None,
    repository: Annotated[list[str] | None, Query(max_length=50)] = None,
    kind: Annotated[list[SearchKind] | None, Query(max_length=3)] = None,
    type: Annotated[list[RepoType] | None, Query(max_length=3)] = None,
    cached: Annotated[bool, Query(description='Only what is in the cache (leaves out apt/yum index listings)')] = False,
    facets: Annotated[bool, Query(description='Facet counts on the first page')] = True,
    cursor: Annotated[
        str | None, Query(max_length=512, description="Opaque cursor from a previous page's `next_cursor`")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> SearchPage:
    principal = await authenticate(request)
    visible = access.filter_repos(principal, Action.BROWSE, await svc.catalog.all())
    names = {r.name for r in visible}
    query = engine.SearchQuery(
        q=q,
        formats=frozenset(format or ()),
        # Only visible names are kept: a hidden repository is never echoed back as a facet value.
        repositories=frozenset(n for n in repository or () if n in names),
        kinds=frozenset(kind or ()),
        types=frozenset(t.value for t in type or ()),
        cached=cached,
        cursor=cursor,
        limit=limit,
        facets=facets,
    )
    if repository and not query.repositories:
        empty = SearchFacets(format=[], repository=[], kind=[], type=[], total=0, capped=False)
        return SearchPage(mode='name', items=[], next_cursor=None, facets=empty if facets and not cursor else None)
    return await engine.search(svc.db, query, visible)
