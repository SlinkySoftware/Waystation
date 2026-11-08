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
SETTINGS = 'settings'
# --- identity and access
USERS = 'users'
GROUPS = 'groups'
ROLES = 'roles'
API_TOKENS = 'api_tokens'
SESSIONS = 'sessions'
RBAC_STATE = 'rbac_state'
# --- content metadata
COMPONENTS = 'components'
ASSETS = 'assets'
BLOBS = 'blobs'
DOCKER_TAGS = 'docker_tags'
DOCKER_MANIFESTS = 'docker_manifests'
APT_POOL_INDEX = 'apt_pool_index'
# --- operations, stats, audit
AUDIT = 'audit'
STATS_MINUTE = 'stats_minute'
MIGRATIONS = 'migrations'

DAY_S = 86_400
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
    CollectionSpec(SETTINGS),
    CollectionSpec(
        USERS,
        (
            _ix('username_unique', [('username', ASCENDING)], unique=True),
            _ix('source', [('source', ASCENDING)]),
        ),
    ),
    CollectionSpec(
        GROUPS,
        (
            _ix('name_unique', [('name', ASCENDING)], unique=True),
            _ix('source_external_id', [('source', ASCENDING), ('external_id', ASCENDING)]),
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
        ),
    ),
    CollectionSpec(
        APT_POOL_INDEX,
        (
            _ix('repo_filename_unique', [('repo_id', ASCENDING), ('filename', ASCENDING)], unique=True),
            _ix('repo_suite', [('repo_id', ASCENDING), ('suite', ASCENDING), ('component', ASCENDING)]),
        ),
    ),
    CollectionSpec(
        AUDIT,
        (
            _ix('ts_desc', [('ts', DESCENDING)]),
            _ix('principal_ts', [('principal.id', ASCENDING), ('ts', DESCENDING)]),
            _ix('action_ts', [('action', ASCENDING), ('ts', DESCENDING)]),
        ),
    ),
    CollectionSpec(
        STATS_MINUTE,
        options={
            'timeseries': {'timeField': 'ts', 'metaField': 'm', 'granularity': 'minutes'},
            'expireAfterSeconds': 7 * DAY_S,
        },
    ),
    CollectionSpec(MIGRATIONS),
)

COLLECTIONS: dict[str, CollectionSpec] = {spec.name: spec for spec in SPECS}
