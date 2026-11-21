"""Index coverage of request-path queries (ARCHITECTURE §9: every MongoDB query on a request path is covered by an
index; the CI test runs ``explain()`` on them).

The queries are registered automatically: ``ws_testing.asgi_client`` wraps the app with ``request_scope``, which
marks everything that runs while an HTTP request is handled (API and format routes, and the background indexing a
request spawns: tasks copy the context). A global ``pymongo`` command listener (``AUDIT``, registered when the server
test package's ``conftest`` is imported, before any client exists) records every read and the filter of every
update/delete/findAndModify issued in that scope, one example per query *shape* (values replaced by their types).
``test_zz_index_coverage_integration`` (collected last) explains every shape against a database with the full schema
(``ensure_schema``) and fails on a collection scan.

Collections allowed to be scanned are listed in ``SCAN_ALLOWED`` with the reason (small configuration collections
whose size the administrator controls and that the server mostly reads through cached catalogs).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, MutableMapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from bson.codec_options import CodecOptions, DatetimeConversion
from pymongo import monitoring

from waystation_common.db.client import Database
from waystation_common.db.collections import (
    AUTH_PROVIDERS,
    BLOBS,
    CLEANUP_POLICIES,
    GROUPS,
    JOB_DEFINITIONS,
    MIGRATIONS,
    RBAC_STATE,
    REPO_USAGE,
    REPOSITORIES,
    ROLES,
    SERVICE_ACCOUNTS,
    SETTINGS,
    WEBHOOKS,
    WORKERS,
)

# Configuration collections: tens to hundreds of documents, written by administrators. A scan is a few pages.
SCAN_ALLOWED: dict[str, str] = {
    REPOSITORIES: 'repository catalog (configuration; also cached per process)',
    SETTINGS: 'singleton settings documents',
    ROLES: 'custom role definitions (configuration)',
    AUTH_PROVIDERS: 'authentication providers (configuration)',
    WEBHOOKS: 'webhook definitions (configuration)',
    CLEANUP_POLICIES: 'cleanup policies (configuration)',
    RBAC_STATE: 'singleton grant version',
    MIGRATIONS: 'applied migration ids',
    JOB_DEFINITIONS: 'scheduled task definitions (built-ins + one per scheduled cleanup policy)',
    WORKERS: 'one document per running worker process',
    GROUPS: 'local and provider-mapped groups (configuration; the group directory loads them all every 5 s)',
    SERVICE_ACCOUNTS: 'service accounts (configuration, created by administrators)',
    REPO_USAGE: 'one usage document per repository (read whole by the storage figures, cached 30 s per process)',
}

# Genuine exceptions on larger collections: (collection, matcher on the explained command, reason).
SCAN_EXCEPTIONS: tuple[tuple[str, Callable[[dict[str, Any]], bool], str], ...] = (
    (
        BLOBS,
        lambda c: (
            [list(stage) for stage in c.get('pipeline') or []] == [['$group']]
            and c['pipeline'][0]['$group'].get('_id') is None
        ),
        'blob count and bytes for the storage figures: one aggregation per process every 30 s (stats.storage cache), '
        'never per request',
    ),
)

GOOD_STAGES = frozenset(
    {
        'IXSCAN',
        'COUNT_SCAN',
        'DISTINCT_SCAN',
        'EXPRESS_IXSCAN',
        'IDHACK',
        'EXPRESS_IDHACK',
        'EXPRESS_CLUSTERED_IXSCAN',
        'EXPRESS_UPDATE',  # update/delete by _id or a unique index (express path)
        'EXPRESS_DELETE',
        'CLUSTERED_IXSCAN',  # time-series buckets: a bounded time range of the clustered collection
    }
)
# Keys of a recorded command that matter to the plan (everything else, session ids, read concerns..., is dropped).
PLAN_KEYS: dict[str, tuple[str, ...]] = {
    'find': ('filter', 'sort', 'projection', 'hint', 'collation', 'limit', 'skip', 'min', 'max'),
    'aggregate': ('pipeline', 'hint', 'collation'),
    'count': ('query', 'hint', 'collation', 'limit', 'skip'),
    'distinct': ('key', 'query', 'hint', 'collation'),
    'findAndModify': ('query', 'sort', 'update', 'remove', 'upsert', 'new', 'fields', 'hint', 'collation'),
}
# Aggregations that are not collection queries.
SKIP_FIRST_STAGES = frozenset(
    {'$changeStream', '$collStats', '$indexStats', '$currentOp', '$documents', '$listSessions'}
)

EXPLAIN_CODEC: CodecOptions[dict[str, Any]] = CodecOptions(datetime_conversion=DatetimeConversion.DATETIME_AUTO)

_in_request: ContextVar[bool] = ContextVar('ws_query_audit_in_request', default=False)


def request_scope(app: Any) -> Any:
    """ASGI wrapper: queries issued while ``app`` handles an HTTP request are request-path queries."""

    async def wrapped(scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get('type') != 'http':
            await app(scope, receive, send)
            return
        token = _in_request.set(True)
        try:
            await app(scope, receive, send)
        finally:
            _in_request.reset(token)

    return wrapped


def shape(value: Any) -> Any:
    """``value`` with every scalar replaced by its type name and lists of scalars collapsed (``$in`` lists of any
    length are one shape)."""
    if isinstance(value, dict):
        return tuple((str(k), shape(v)) for k, v in value.items())
    if isinstance(value, list | tuple):
        items = [shape(v) for v in value]
        if all(not isinstance(v, tuple) for v in items):
            return ('<list>',)
        return ('<array>', *items)
    return type(value).__name__


@dataclass
class Query:
    collection: str
    command: dict[str, Any]  # an explainable command (one example of the shape)
    origin: str  # the test that issued it first


def _statements(name: str, command: dict[str, Any]) -> list[dict[str, Any]]:
    """The explainable commands in a recorded command (updates/deletes: one per statement)."""
    collection = command.get(name)
    if not isinstance(collection, str):
        return []  # db-level aggregate (aggregate: 1)
    if name in PLAN_KEYS:
        if name == 'aggregate':
            pipeline = command.get('pipeline') or []
            if pipeline and next(iter(pipeline[0]), '') in SKIP_FIRST_STAGES:
                return []
        out = {name: collection, **{k: command[k] for k in PLAN_KEYS[name] if k in command}}
        if name == 'aggregate':
            out['cursor'] = {}
        return [out]
    if name == 'update':
        return [
            {
                'update': collection,
                'updates': [
                    {
                        k: v
                        for k, v in stmt.items()
                        if k in ('q', 'u', 'multi', 'upsert', 'collation', 'hint', 'arrayFilters')
                    }
                ],
            }
            for stmt in command.get('updates') or []
        ]
    if name == 'delete':
        return [
            {
                'delete': collection,
                'deletes': [{k: v for k, v in stmt.items() if k in ('q', 'limit', 'collation', 'hint')}],
            }
            for stmt in command.get('deletes') or []
        ]
    return []


class QueryAudit(monitoring.CommandListener):
    def __init__(self) -> None:
        self.queries: dict[Any, Query] = {}
        self.current_test = ''

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if not _in_request.get():
            return
        name = event.command_name
        if name not in PLAN_KEYS and name not in ('update', 'delete'):
            return
        command = dict(event.command)
        for stmt in _statements(name, command):
            key = (name, stmt[name], shape({k: v for k, v in stmt.items() if k != name}))
            if key not in self.queries:
                self.queries[key] = Query(str(stmt[name]), copy.deepcopy(stmt), self.current_test)

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        pass

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        pass


def plan_stages(node: Any, out: list[str] | None = None) -> list[str]:
    """Every ``stage`` of a query plan (winning plans only)."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'rejectedPlans':
                continue
            if key == 'stage' and isinstance(value, str):
                out.append(value)
            plan_stages(value, out)
    elif isinstance(node, list):
        for item in node:
            plan_stages(item, out)
    return out


def _scanned(node: Any, out: set[str] | None = None) -> set[str]:
    """Collections a plan scans in full: every ``queryPlanner`` (the command's, and each ``$cursor`` stage's of an
    aggregation) whose winning plan has a ``COLLSCAN``."""
    out = set() if out is None else out
    if isinstance(node, dict):
        planner = node.get('queryPlanner')
        if (
            isinstance(planner, dict)
            and isinstance(planner.get('namespace'), str)
            and 'COLLSCAN' in plan_stages(planner.get('winningPlan'))
        ):
            out.add(str(planner['namespace']).split('.', 1)[-1])
        for key, value in node.items():
            if key not in ('queryPlanner', 'rejectedPlans'):
                _scanned(value, out)
    elif isinstance(node, list):
        for item in node:
            _scanned(item, out)
    return out


async def explain_problem(db: Database, command: dict[str, Any], allowed: frozenset[str] = frozenset()) -> str | None:
    """None when ``command`` is index-backed; else what is wrong. Collections in ``allowed`` may be scanned."""
    # Time-series plans carry out-of-range bucket bounds: decode dates leniently.
    plan = await db.command({'explain': command, 'verbosity': 'queryPlanner'}, codec_options=EXPLAIN_CODEC)
    stages = plan_stages(plan)
    if 'COLLSCAN' in stages:
        scanned = {name.removeprefix('system.buckets.') for name in _scanned(plan)}
        collection = next(iter(command.values()))
        if not scanned:  # the plan did not say where: attribute it to the command's collection
            scanned = {str(collection)}
        collection_name = str(collection)
        if any(name == collection_name and match(command) for name, match, _ in SCAN_EXCEPTIONS):
            scanned.discard(collection_name)
        bad = sorted(scanned - allowed)
        if bad:
            return f'collection scan on {", ".join(bad)}'
        return None
    if 'EOF' in stages and not any(s in GOOD_STAGES for s in stages):
        return None  # the collection does not exist (nothing to scan)
    if not any(s in GOOD_STAGES for s in stages):
        return f'no index stage in {sorted(set(stages))}'
    return None


AUDIT = QueryAudit()


def install() -> None:
    """Register the listener for every client created from now on (idempotent)."""
    if not getattr(install, 'done', False):
        monitoring.register(AUDIT)
        install.done = True  # type: ignore[attr-defined]
