"""Collection registry: every MongoDB collection, its options and indexes (DATA_MODEL §1).

``COLLECTIONS`` is the source of truth for the schema that ``ensure_schema`` creates. Use the name constants
(``REPOSITORIES``...) instead of string literals. Indexes carry explicit names so the schema check can compare them.
Collections not needed for M0 are added by the tasks that introduce them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pymongo import ASCENDING, DESCENDING, TEXT, IndexModel

# --- configuration
REPOSITORIES = 'repositories'
CLEANUP_POLICIES = 'cleanup_policies'
SETTINGS = 'settings'
AUTH_PROVIDERS = 'auth_providers'
# --- identity and access
USERS = 'users'
GROUPS = 'groups'
ROLES = 'roles'
API_TOKENS = 'api_tokens'
SERVICE_ACCOUNTS = 'service_accounts'
SESSIONS = 'sessions'
RBAC_STATE = 'rbac_state'
# --- content metadata
COMPONENTS = 'components'
ASSETS = 'assets'
BLOBS = 'blobs'
DOCKER_TAGS = 'docker_tags'
DOCKER_MANIFESTS = 'docker_manifests'
APT_POOL_INDEX = 'apt_pool_index'
APT_PACKAGES = 'apt_packages'
NPM_PACKAGES = 'npm_packages'
PYPI_PACKAGES = 'pypi_packages'
YUM_PACKAGES = 'yum_packages'
UPLOAD_SESSIONS = 'upload_sessions'
REPO_USAGE = 'repo_usage'
# --- operations, stats, audit
AUDIT = 'audit'
STATS_MINUTE = 'stats_minute'
MIGRATIONS = 'migrations'
# --- jobs (ARCHITECTURE §10)
JOB_DEFINITIONS = 'job_definitions'
JOB_RUNS = 'job_runs'
JOB_OUTBOX = 'job_outbox'
WORKERS = 'workers'

DAY_S = 86_400
STATS_MINUTE_RETENTION_DAYS = 7  # per-minute statistics (time-series TTL)
CASE_INSENSITIVE = {'locale': 'en', 'strength': 2}


@dataclass(frozen=True)
class CollectionSpec:
    name: str
    indexes: tuple[IndexModel, ...] = ()
    # Options for ``create_collection`` (time-series, TTL for time-series...). Empty = plain collection.
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def is_timeseries(self) -> bool:
        return 'timeseries' in self.options


def _ix(name: str, keys: list[tuple[str, Any]], **kwargs: Any) -> IndexModel:
    return IndexModel(keys, name=name, **kwargs)


# Global search (SPECIFICATION §4.6, ``waystation_common.searchkeys``): prefix ranges on the lower-cased name, results
# in (key, repo_id, _id) order straight from the index; ``base_lc`` (last ``/`` segment) carries ``name_lc`` so the
# "name did not match already" filter is evaluated on index keys. Partial: only searchable documents are indexed.
def _search_indexes(*, base: bool) -> tuple[IndexModel, ...]:
    indexes = [
        _ix(
            'search_name',
            [('name_lc', ASCENDING), ('repo_id', ASCENDING), ('_id', ASCENDING)],
            partialFilterExpression={'name_lc': {'$exists': True}},
        )
    ]
    if base:
        indexes.append(
            _ix(
                'search_base',
                [('base_lc', ASCENDING), ('repo_id', ASCENDING), ('_id', ASCENDING), ('name_lc', ASCENDING)],
                partialFilterExpression={'base_lc': {'$exists': True}},
            )
        )
    return tuple(indexes)


SPECS: tuple[CollectionSpec, ...] = (
    CollectionSpec(
        REPOSITORIES,
        (
            _ix('name_unique', [('name', ASCENDING)], unique=True),
            # SPECIFICATION §3: names are unique case-insensitively (stored as given).
            _ix('name_ci_unique', [('name', ASCENDING)], unique=True, collation=CASE_INSENSITIVE),
            _ix('format_type', [('format', ASCENDING), ('type', ASCENDING)]),
        ),
    ),
    CollectionSpec(
        CLEANUP_POLICIES,
        (_ix('name_unique', [('name', ASCENDING)], unique=True, collation=CASE_INSENSITIVE),),
    ),
    CollectionSpec(SETTINGS),
    CollectionSpec(AUTH_PROVIDERS, (_ix('order', [('order', ASCENDING)]),)),
    CollectionSpec(
        USERS,
        (
            _ix('username_unique', [('username', ASCENDING)], unique=True),
            _ix('source', [('source', ASCENDING)]),
            _ix('groups', [('groups', ASCENDING)]),
        ),
    ),
    CollectionSpec(
        GROUPS,
        (
            _ix('name_unique', [('name', ASCENDING)], unique=True),
            _ix('source_external_id', [('source', ASCENDING), ('external_id', ASCENDING)]),
            _ix('provider_id', [('provider_id', ASCENDING)]),
        ),
    ),
    CollectionSpec(ROLES),
    CollectionSpec(
        API_TOKENS,
        (
            _ix('hash_unique', [('hash', ASCENDING)], unique=True),
            _ix('principal_id', [('principal.id', ASCENDING)]),
        ),
    ),
    CollectionSpec(SERVICE_ACCOUNTS, (_ix('name_unique', [('name', ASCENDING)], unique=True),)),
    CollectionSpec(
        SESSIONS,
        (
            _ix('expires_at_ttl', [('expires_at', ASCENDING)], expireAfterSeconds=0),
            _ix('user_id', [('user_id', ASCENDING)]),
        ),
    ),
    CollectionSpec(RBAC_STATE),
    CollectionSpec(
        COMPONENTS,
        (
            # "unique-ish" in DATA_MODEL: docker components carry tag sets, so uniqueness is not enforced here.
            _ix(
                'repo_coordinates',
                [('repo_id', ASCENDING), ('namespace', ASCENDING), ('name', ASCENDING), ('version_sort', ASCENDING)],
            ),
            _ix('format_name_ci', [('format', ASCENDING), ('name', ASCENDING)], collation=CASE_INSENSITIVE),
            _ix('last_downloaded_at', [('last_downloaded_at', ASCENDING)]),
            _ix('text', [('name', TEXT), ('attributes.description', TEXT)]),
        ),
    ),
    CollectionSpec(
        ASSETS,
        (
            _ix('repo_path_unique', [('repo_id', ASCENDING), ('path', ASCENDING)], unique=True),
            _ix('blob_sha256', [('blob_sha256', ASCENDING)]),
            _ix('component_id', [('component_id', ASCENDING)]),
            _ix('repo_last_downloaded_at', [('repo_id', ASCENDING), ('last_downloaded_at', ASCENDING)]),
            *_search_indexes(base=False),
        ),
    ),
    CollectionSpec(BLOBS, (_ix('gc_marked_at', [('gc_marked_at', ASCENDING)]),)),
    CollectionSpec(
        DOCKER_TAGS,
        (
            _ix(
                'repo_image_tag_unique', [('repo_id', ASCENDING), ('image', ASCENDING), ('tag', ASCENDING)], unique=True
            ),
            _ix('repo_manifest_digest', [('repo_id', ASCENDING), ('manifest_digest', ASCENDING)]),
            # Browse: an image's tags, most recently updated first.
            _ix(
                'repo_image_updated',
                [('repo_id', ASCENDING), ('image', ASCENDING), ('updated_at', DESCENDING), ('tag', ASCENDING)],
            ),
            *_search_indexes(base=True),
        ),
    ),
    CollectionSpec(
        DOCKER_MANIFESTS,
        (
            _ix(
                'repo_image_digest_unique',
                [('repo_id', ASCENDING), ('image', ASCENDING), ('digest', ASCENDING)],
                unique=True,
            ),
            _ix('repo_subject_digest', [('repo_id', ASCENDING), ('subject_digest', ASCENDING)]),
            # Hosted: is a blob still referenced by a manifest of the repository (blob delete)?
            _ix('repo_config_digest', [('repo_id', ASCENDING), ('config_digest', ASCENDING)]),
            _ix('repo_layer_digest', [('repo_id', ASCENDING), ('layers.digest', ASCENDING)]),
        ),
    ),
    # Stored bytes per repository (quotas, SPECIFICATION §4.8): ``_id`` = repository id, read by ``_id`` only.
    CollectionSpec(REPO_USAGE),
    # Resumable hosted uploads (OCI chunked blob uploads); expire after the idle timeout.
    CollectionSpec(
        UPLOAD_SESSIONS,
        (
            _ix('expires_at_ttl', [('expires_at', ASCENDING)], expireAfterSeconds=0),
            _ix('repo_id', [('repo_id', ASCENDING)]),
        ),
    ),
    CollectionSpec(
        APT_POOL_INDEX,
        (
            _ix('repo_filename_unique', [('repo_id', ASCENDING), ('filename', ASCENDING)], unique=True),
            _ix('repo_suite', [('repo_id', ASCENDING), ('suite', ASCENDING), ('component', ASCENDING)]),
            _ix('repo_verified', [('repo_id', ASCENDING), ('verified', ASCENDING)]),
        ),
    ),
    CollectionSpec(
        APT_PACKAGES,
        (
            # One document per stanza of a cached Packages index (suite, component, arch); replaced per index.
            _ix(
                'repo_listing_filename_unique',
                [
                    ('repo_id', ASCENDING),
                    ('suite', ASCENDING),
                    ('component', ASCENDING),
                    ('arch', ASCENDING),
                    ('filename', ASCENDING),
                ],
                unique=True,
            ),
            # Browse: a suite's packages by name (arch, component and name filters evaluated on index keys).
            _ix(
                'repo_suite_name',
                [
                    ('repo_id', ASCENDING),
                    ('suite', ASCENDING),
                    ('name', ASCENDING),
                    ('filename', ASCENDING),
                    ('arch', ASCENDING),
                    ('component', ASCENDING),
                ],
            ),
            # Cached pool files -> the listings that name them ($lookup from assets).
            _ix('filename_repo', [('filename', ASCENDING), ('repo_id', ASCENDING)]),
            *_search_indexes(base=False),
        ),
    ),
    CollectionSpec(
        NPM_PACKAGES,
        (
            _ix('repo_name_unique', [('repo_id', ASCENDING), ('name', ASCENDING)], unique=True),
            *_search_indexes(base=True),
        ),
    ),
    CollectionSpec(
        PYPI_PACKAGES,
        (
            # One summary per project (normalised name) a repository has: cached page (proxy) or uploads (hosted).
            _ix('repo_name_unique', [('repo_id', ASCENDING), ('name', ASCENDING)], unique=True),
            *_search_indexes(base=False),
        ),
    ),
    CollectionSpec(
        YUM_PACKAGES,
        (
            # Package path -> checksum (from cached primary.xml files, or computed: unverified).
            _ix('repo_path_unique', [('repo_id', ASCENDING), ('path', ASCENDING)], unique=True),
            # A directory's listing generation: replaced on re-index; "is this primary indexed".
            _ix('repo_dir_gen', [('repo_id', ASCENDING), ('repo_dir', ASCENDING), ('gen', ASCENDING)]),
            _ix('repo_verified', [('repo_id', ASCENDING), ('verified', ASCENDING)]),
            *_search_indexes(base=False),
        ),
    ),
    CollectionSpec(
        AUDIT,
        (
            _ix('ts_desc', [('ts', DESCENDING)]),
            _ix('principal_ts', [('principal.id', ASCENDING), ('ts', DESCENDING)]),
            _ix('action_ts', [('action', ASCENDING), ('ts', DESCENDING)]),
            _ix('principal_name_ts', [('principal.name', ASCENDING), ('ts', DESCENDING)]),
            _ix('target_name_ts', [('target.name', ASCENDING), ('ts', DESCENDING)]),
            _ix('target_id_ts', [('target.id', ASCENDING), ('ts', DESCENDING)]),
            # Retention: each record carries ts + WAYSTATION_AUDIT_RETENTION_DAYS (SPEC §4.11).
            _ix('expires_at_ttl', [('expires_at', ASCENDING)], expireAfterSeconds=0),
        ),
    ),
    CollectionSpec(
        STATS_MINUTE,
        options={
            'timeseries': {'timeField': 'ts', 'metaField': 'm', 'granularity': 'minutes'},
            'expireAfterSeconds': STATS_MINUTE_RETENTION_DAYS * DAY_S,
        },
    ),
    CollectionSpec(MIGRATIONS),
    CollectionSpec(JOB_DEFINITIONS),
    CollectionSpec(
        JOB_RUNS,
        (
            _ix('started_at', [('started_at', DESCENDING)]),
            # Per-kind retention (90 d; 1 d for routine jobs such as the 10 s stats rollup).
            _ix('expires_at_ttl', [('expires_at', ASCENDING)], expireAfterSeconds=0),
            _ix('job_id', [('job_id', ASCENDING), ('started_at', DESCENDING)]),
            _ix('definition_started', [('definition_id', ASCENDING), ('started_at', DESCENDING)]),
            # A cleanup policy's runs (manual and scheduled share the dedupe key ``cleanup:<policy id>``).
            _ix('dedupe_started', [('dedupe_key', ASCENDING), ('started_at', DESCENDING)]),
            _ix('kind_started', [('kind', ASCENDING), ('started_at', DESCENDING)]),
            # The Tasks page's status filter (running, failed, dead).
            _ix('status_started', [('status', ASCENDING), ('started_at', DESCENDING)]),
        ),
    ),
    CollectionSpec(
        JOB_OUTBOX,
        (
            _ix('sent_at', [('sent_at', ASCENDING)]),
            _ix('done_at_ttl', [('done_at', ASCENDING)], expireAfterSeconds=7 * DAY_S),
        ),
    ),
    CollectionSpec(WORKERS, (_ix('heartbeat_at_ttl', [('heartbeat_at', ASCENDING)], expireAfterSeconds=300),)),
)

COLLECTIONS: dict[str, CollectionSpec] = {spec.name: spec for spec in SPECS}
