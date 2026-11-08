"""Global search (SPECIFICATION §4.6; OpenAPI tag ``search``, ``GET /api/v1/search``).

- ``models``: the response models of the contract (``SearchPage``, ``SearchHit``, ``SearchFacets``).
- ``engine``: query parsing (name prefix + optional version prefix, or a digest), the sources searched (npm packages,
  docker tags, apt and yum listings, cached files), merging their index-ordered streams into one keyset-paged result
  list, facet counts and the Browse deep links.

The repositories searched are always the principal's ``filter_repos(..., browse)`` set (narrowed by the facet
filters): documents of hidden repositories are outside every index bound, so they never reach results or counts.
"""
