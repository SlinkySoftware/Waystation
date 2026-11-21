"""Index coverage of every request-path query (ARCHITECTURE §9, TASKS T-S09/T-F06).

``ws_query_audit`` records, one example per shape, every query the server issued while handling an HTTP request in
the integration tests collected before this module (the file name sorts it last). Each one is explained here against
a database with the full schema: none may scan a collection, except the small configuration collections in
``ws_query_audit.SCAN_ALLOWED``.
"""

import pytest
from ws_query_audit import AUDIT, SCAN_ALLOWED, explain_problem

from waystation_common.db.client import Database
from waystation_common.db.schema import ensure_schema

pytestmark = pytest.mark.integration


async def test_request_path_queries_use_an_index(mongo_db: Database) -> None:
    if not AUDIT.queries:
        pytest.skip('no request-path queries recorded (run with the server integration tests)')
    await ensure_schema(mongo_db)
    allowed = frozenset(SCAN_ALLOWED)
    problems = []
    for query in list(AUDIT.queries.values()):
        try:
            problem = await explain_problem(mongo_db, query.command, allowed)
        except Exception as exc:  # an unexplainable command is a failure too, with the reason
            problem = f'explain failed: {exc!r}'
        if problem is not None:
            problems.append(f'{problem}\n    {query.command}\n    first issued by {query.origin}')
    assert not problems, (
        f'{len(problems)} of {len(AUDIT.queries)} request-path query shapes are not index-backed:\n'
        + ('\n'.join(problems))
    )
