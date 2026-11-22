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
WEBHOOKS = 'webhooks'
# --- identity and access
USERS = 'users'
GROUPS = 'groups'
ROLES = 'roles'
API_TOKENS = 'api_tokens'
SERVICE_ACCOUNTS = 'service_accounts'
SESSIONS = 'sessions'
LOGIN_CHALLENGES = 'login_challenges'  # pending second sign-in steps (TOTP), minutes long
RBAC_STATE = 'rbac_state'
# --- content metadata
COMPONENTS = 'components'
ASSETS = 'assets'
BLOBS = 'blobs'
DOCKER_TAGS = 'docker_tags'
DOCKER_MANIFESTS = 'docker_manifests'
APT_POOL_INDEX = 'apt_pool_index'
APT_PACKAGES = 'apt_packages'
APT_HOSTED = 'apt_hosted_packages'  # packages uploaded to hosted apt repositories (T-P5-APTH)
NPM_PACKAGES = 'npm_packages'
NPM_VERSIONS = 'npm_versions'
PYPI_PACKAGES = 'pypi_packages'
GO_MODULES = 'go_modules'
TERRAFORM_PROVIDERS = 'terraform_providers'
TERRAFORM_MODULES = 'terraform_modules'  # module summaries and versions (T-P5-TFM)
YUM_PACKAGES = 'yum_packages'
HELM_CHARTS = 'helm_charts'
GALAXY_COLLECTIONS = 'galaxy_collections'
GALAXY_IMPORTS = 'galaxy_imports'  # hosted collection uploads waiting for (or done with) the import job
SWIFT_RELEASES = 'swift_releases'  # Swift package releases: published (hosted) or pinned from a Git tag (proxy)
SWIFT_PACKAGES = 'swift_packages'  # one summary per Swift package (browse, global search)
UPLOAD_SESSIONS = 'upload_sessions'
REPO_USAGE = 'repo_usage'
# --- operations, stats, audit
AUDIT = 'audit'
STATS_MINUTE = 'stats_minute'
STATS_HOUR = 'stats_hour'  # closed hours rolled up from stats_minute (T-F09)
STATS_DAY = 'stats_day'  # closed days rolled up from stats_hour
STORAGE_SNAPSHOTS = 'storage_snapshots'  # hourly per-repository (and instance-wide) storage figures
DOWNLOADS_DAILY = 'downloads_daily'  # downloads per component per day (top artifacts over a window)
MIGRATIONS = 'migrations'
# --- jobs (ARCHITECTURE §10)
JOB_DEFINITIONS = 'job_definitions'
JOB_RUNS = 'job_runs'
JOB_OUTBOX = 'job_outbox'
WORKERS = 'workers'
WEBHOOK_DELIVERIES = 'webhook_deliveries'  # one per (event, webhook), test or redelivery (T-O05)

DAY_S = 86_400
STATS_MINUTE_RETENTION_DAYS = 7  # per-minute statistics (time-series TTL)
STATS_HOUR_RETENTION_DAYS = 90  # hourly statistics (SPECIFICATION §4.7)
STATS_DAY_RETENTION_DAYS = 730  # daily statistics: 2 years
STORAGE_SNAPSHOT_RETENTION_DAYS = 730
DOWNLOADS_DAILY_RETENTION_DAYS = 400
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


def _ts_indexes() -> tuple[IndexModel, ...]:
    """Secondary indexes of the stats time-series collections: a repository's documents in time order (the stats
    API's per-repository ranges) and every repository's in time order (instance-wide ranges, rollups)."""
    return (_ix('repo_ts', [('m.repo_id', ASCENDING), ('ts', ASCENDING)]), _ix('ts', [('ts', ASCENDING)]))


# Global search (SPECIFICATION §4.6, ``waystation_common.searchkeys``): prefix ranges on the lower-cased name, results
# in (key, repo_id, _id) order straight from the index; ``base_lc`` (last ``/`` segment) carries ``name_lc`` so the
# "name did not match already" filter is evaluated on index keys. Partial: only searchable documents are indexed.
# Substring matches (adr/0009): ``search_sfx`` = a prefix range over the multikey ``name_sfx`` (name suffixes), read in
# (suffix, repo_id, _id) order with min/max key bounds as the keyset.
# ``only``: an extra partial filter (``components``: Maven only), which every search query on the collection repeats.
def _search_indexes(*, base: bool, only: dict[str, Any] | None = None) -> tuple[IndexModel, ...]:
    extra = only or {}
    indexes = [
        _ix(
            'search_name',
            [('name_lc', ASCENDING), ('repo_id', ASCENDING), ('_id', ASCENDING)],
            partialFilterExpression={'name_lc': {'$exists': True}, **extra},
        ),
        _ix(
            'search_sfx',
            [('name_sfx', ASCENDING), ('repo_id', ASCENDING), ('_id', ASCENDING)],
            partialFilterExpression={'name_sfx': {'$exists': True}, **extra},
        ),
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
            # SPECIFICATION §4.2: one hosted Terraform repository per instance (``/.well-known/terraform.json``).
            _ix(
                'terraform_hosted_single',
                [('format', ASCENDING)],
                unique=True,
                partialFilterExpression={'format': 'terraform', 'type': 'hosted'},
            ),
        ),
    ),
    CollectionSpec(
        CLEANUP_POLICIES,
        (_ix('name_unique', [('name', ASCENDING)], unique=True, collation=CASE_INSENSITIVE),),
    ),
    CollectionSpec(SETTINGS),
    CollectionSpec(WEBHOOKS, (_ix('name_unique', [('name', ASCENDING)], unique=True, collation=CASE_INSENSITIVE),)),
    CollectionSpec(AUTH_PROVIDERS, (_ix('order', [('order', ASCENDING)]),)),
    CollectionSpec(
        USERS,
        (
            _ix('username_unique', [('username', ASCENDING)], unique=True),
            _ix('source', [('source', ASCENDING)]),
            _ix('groups', [('groups', ASCENDING)]),
            # Users holding a role (role delete/usage checks, "is there another administrator").
            _ix('roles', [('roles', ASCENDING)]),
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
    # A password accepted, the second factor (TOTP) not yet: read by ``_id`` (sha256 of the cookie) only.
    CollectionSpec(
        LOGIN_CHALLENGES,
        (
            _ix('expires_at_ttl', [('expires_at', ASCENDING)], expireAfterSeconds=0),
            # A user's pending challenges are dropped at sign-in, password change and when the user is changed.
            _ix('user_id', [('user_id', ASCENDING)]),
        ),
    ),
    CollectionSpec(RBAC_STATE),
    CollectionSpec(
        COMPONENTS,
        (
            # Versions of a component in order (``version_sort``, waystation_common.versionsort).
            _ix(
                'repo_coordinates',
                [('repo_id', ASCENDING), ('namespace', ASCENDING), ('name', ASCENDING), ('version_sort', ASCENDING)],
            ),
            # Maven (T-P5-MAVEN): one component per groupId:artifactId:version of a repository.
            _ix(
                'maven_gav_unique',
                [('repo_id', ASCENDING), ('namespace', ASCENDING), ('name', ASCENDING), ('version', ASCENDING)],
                unique=True,
                partialFilterExpression={'format': 'maven'},
            ),
            _ix('format_name_ci', [('format', ASCENDING), ('name', ASCENDING)], collation=CASE_INSENSITIVE),
            _ix('last_downloaded_at', [('last_downloaded_at', ASCENDING)]),
            _ix('text', [('name', TEXT), ('attributes.description', TEXT)]),
            # T-S09: one component per (repository, format, namespace, name, version) for every format.
            _ix(
                'component_key_unique',
                [
                    ('repo_id', ASCENDING),
                    ('format', ASCENDING),
                    ('namespace', ASCENDING),
                    ('name', ASCENDING),
                    ('version', ASCENDING),
                ],
                unique=True,
            ),
            # Top artifacts (stats API): per repository, best first; a `$in` of visible repositories merges them.
            _ix('repo_downloads', [('repo_id', ASCENDING), ('download_count', DESCENDING)]),
            _ix('repo_bytes_served', [('repo_id', ASCENDING), ('bytes_served', DESCENDING)]),
            _ix('repo_size', [('repo_id', ASCENDING), ('size_bytes', DESCENDING)]),
            _ix('repo_created', [('repo_id', ASCENDING), ('created_at', DESCENDING)]),
            # Global search over Maven components (artifactId; T-P5-MAVEN). Other formats are found through their
            # own package indexes, so only Maven components are indexed.
            *_search_indexes(base=False, only={'format': 'maven'}),
        ),
    ),
    CollectionSpec(
        ASSETS,
        (
            _ix('repo_path_unique', [('repo_id', ASCENDING), ('path', ASCENDING)], unique=True),
            _ix('blob_sha256', [('blob_sha256', ASCENDING)]),
            _ix('component_id', [('component_id', ASCENDING)]),
            _ix('repo_last_downloaded_at', [('repo_id', ASCENDING), ('last_downloaded_at', ASCENDING)]),
            # Prefetch (T-O12): a repository's most downloaded metadata, recently used (``db.prefetch.popular``).
            _ix(
                'prefetch_popular',
                [('repo_id', ASCENDING), ('download_count', DESCENDING), ('last_downloaded_at', ASCENDING)],
                partialFilterExpression={'kind': 'metadata', 'download_count': {'$gt': 0}},
            ),
            # Blob verify marks (T-O07): only marked assets are indexed, so /metrics counts them cheaply.
            _ix(
                'blob_missing',
                [('blob_missing.hosted', ASCENDING)],
                partialFilterExpression={'blob_missing': {'$exists': True}},
            ),
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
            # Browse: package names, their entries newest EVR first (``version_sort``, formats.yum.evr).
            _ix('repo_name_sort', [('repo_id', ASCENDING), ('name', ASCENDING), ('version_sort', DESCENDING)]),
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
        APT_HOSTED,
        (
            # One record per uploaded .deb: its pool path, and (name, version, architecture) once per repository.
            _ix('repo_path_unique', [('repo_id', ASCENDING), ('path', ASCENDING)], unique=True),
            _ix(
                'repo_package_unique',
                [('repo_id', ASCENDING), ('name', ASCENDING), ('version', ASCENDING), ('architecture', ASCENDING)],
                unique=True,
            ),
            # Index generation (worker): a component's packages by name, dpkg version order, architecture.
            _ix(
                'repo_component_order',
                [
                    ('repo_id', ASCENDING),
                    ('component', ASCENDING),
                    ('name', ASCENDING),
                    ('version_sort', ASCENDING),
                    ('architecture', ASCENDING),
                    ('path', ASCENDING),
                ],
            ),
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
        NPM_VERSIONS,
        (
            # One published version per hosted npm repository (T-P5-NPMH): packuments are generated from these.
            _ix(
                'repo_name_version_unique',
                [('repo_id', ASCENDING), ('name', ASCENDING), ('version', ASCENDING)],
                unique=True,
            ),
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
        GO_MODULES,
        (
            # One summary per module path a Go proxy has cached something of (version list or files).
            _ix('repo_name_unique', [('repo_id', ASCENDING), ('name', ASCENDING)], unique=True),
            *_search_indexes(base=True),
        ),
    ),
    CollectionSpec(
        TERRAFORM_PROVIDERS,
        (
            # One summary per provider address (<hostname>/<namespace>/<type>) a Terraform mirror has cached
            # something of, with the h1 hashes of its cached archives; in the hosted repository one per
            # <namespace>/<type> with its uploaded releases.
            _ix('repo_name_unique', [('repo_id', ASCENDING), ('name', ASCENDING)], unique=True),
            *_search_indexes(base=True),
        ),
    ),
    CollectionSpec(
        TERRAFORM_MODULES,
        (
            # One document per module (<namespace>/<name>/<system>) of a repository: a proxy's cached version list
            # and archives, the hosted repository's uploaded versions (a version is pushed at most once).
            _ix('repo_name_unique', [('repo_id', ASCENDING), ('name', ASCENDING)], unique=True),
            *_search_indexes(base=True),
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
        HELM_CHARTS,
        (
            # One chart version per repository: a hosted upload, or an entry of a proxy's cached index.yaml.
            _ix(
                'repo_name_version_unique',
                [('repo_id', ASCENDING), ('name', ASCENDING), ('version', ASCENDING)],
                unique=True,
            ),
            # Browse (charts by name, versions newest first) and hosted index generation.
            _ix('repo_name_sort', [('repo_id', ASCENDING), ('name', ASCENDING), ('version_sort', DESCENDING)]),
            # Chart file path -> entry (proxy content validation, hosted provenance uploads).
            _ix('repo_path', [('repo_id', ASCENDING), ('path', ASCENDING)]),
            # A proxy listing's generation (sha256 of the index blob it came from): replaced on re-index.
            _ix('repo_gen', [('repo_id', ASCENDING), ('gen', ASCENDING)]),
        ),
    ),
    CollectionSpec(
        GALAXY_COLLECTIONS,
        (
            # One collection version per repository: a hosted import, or a version a proxy's cached list names.
            _ix(
                'repo_coll_version_unique',
                [('repo_id', ASCENDING), ('namespace', ASCENDING), ('name', ASCENDING), ('version', ASCENDING)],
                unique=True,
            ),
            # Browse and the served version lists: a collection's versions newest first.
            _ix(
                'repo_coll_sort',
                [('repo_id', ASCENDING), ('namespace', ASCENDING), ('name', ASCENDING), ('version_sort', DESCENDING)],
            ),
        ),
    ),
    CollectionSpec(
        GALAXY_IMPORTS,
        (
            # Uploads of one collection version still waiting for the import job (version immutability).
            _ix(
                'repo_coll_version_state',
                [
                    ('repo_id', ASCENDING),
                    ('namespace', ASCENDING),
                    ('name', ASCENDING),
                    ('version', ASCENDING),
                    ('state', ASCENDING),
                ],
            ),
            _ix('expires_at_ttl', [('expires_at', ASCENDING)], expireAfterSeconds=0),
        ),
    ),
    CollectionSpec(
        SWIFT_RELEASES,
        (
            # One release per package version and repository; a removed release keeps its record (the version stays
            # taken: releases are immutable, SE-0292 §4.6).
            _ix(
                'repo_pkg_version_unique',
                [('repo_id', ASCENDING), ('scope_key', ASCENDING), ('name_key', ASCENDING), ('version', ASCENDING)],
                unique=True,
            ),
            # Release lists and browse: a package's releases newest first (SemVer precedence).
            _ix(
                'repo_pkg_sort',
                [
                    ('repo_id', ASCENDING),
                    ('scope_key', ASCENDING),
                    ('name_key', ASCENDING),
                    ('version_sort', DESCENDING),
                ],
            ),
            # ``GET /identifiers?url=``: the packages whose release metadata names a source repository URL.
            _ix('repo_urls', [('repo_id', ASCENDING), ('repository_urls', ASCENDING)]),
        ),
    ),
    CollectionSpec(
        SWIFT_PACKAGES,
        (
            # Browse: scopes, then the packages of a scope, by name.
            _ix(
                'repo_pkg_unique',
                [('repo_id', ASCENDING), ('scope_key', ASCENDING), ('name_key', ASCENDING)],
                unique=True,
            ),
            *_search_indexes(base=False),
        ),
    ),
    CollectionSpec(
        AUDIT,
        (
            _ix('ts_desc', [('ts', DESCENDING)]),
            # The audit log's order (newest first, id as the tie-break of the keyset cursor) without an in-memory sort.
            _ix('ts_id_desc', [('ts', DESCENDING), ('_id', DESCENDING)]),
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
        _ts_indexes(),
        options={
            'timeseries': {'timeField': 'ts', 'metaField': 'm', 'granularity': 'minutes'},
            'expireAfterSeconds': STATS_MINUTE_RETENTION_DAYS * DAY_S,
        },
    ),
    CollectionSpec(
        STATS_HOUR,
        _ts_indexes(),
        options={
            'timeseries': {'timeField': 'ts', 'metaField': 'm', 'granularity': 'hours'},
            'expireAfterSeconds': STATS_HOUR_RETENTION_DAYS * DAY_S,
        },
    ),
    CollectionSpec(
        STATS_DAY,
        _ts_indexes(),
        options={
            'timeseries': {'timeField': 'ts', 'metaField': 'm', 'granularity': 'hours'},
            'expireAfterSeconds': STATS_DAY_RETENTION_DAYS * DAY_S,
        },
    ),
    CollectionSpec(
        STORAGE_SNAPSHOTS,
        _ts_indexes(),
        options={
            'timeseries': {'timeField': 'ts', 'metaField': 'm', 'granularity': 'hours'},
            'expireAfterSeconds': STORAGE_SNAPSHOT_RETENTION_DAYS * DAY_S,
        },
    ),
    CollectionSpec(
        DOWNLOADS_DAILY,
        (
            _ix('repo_day', [('repo_id', ASCENDING), ('day', ASCENDING)]),
            _ix('component_day', [('component_id', ASCENDING), ('day', ASCENDING)]),
            _ix('day_ttl', [('day', ASCENDING)], expireAfterSeconds=DOWNLOADS_DAILY_RETENTION_DAYS * DAY_S),
        ),
    ),
    CollectionSpec(MIGRATIONS),
    CollectionSpec(JOB_DEFINITIONS),
    CollectionSpec(
        JOB_RUNS,
        (
            _ix('started_at', [('started_at', DESCENDING)]),
            # The Tasks page's run list (newest first, id as the keyset tie-break) without an in-memory sort.
            _ix('started_id_desc', [('started_at', DESCENDING), ('_id', DESCENDING)]),
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
    CollectionSpec(
        WEBHOOK_DELIVERIES,
        (
            # A webhook's delivery log, newest first.
            _ix('webhook_created', [('webhook_id', ASCENDING), ('created_at', DESCENDING), ('_id', DESCENDING)]),
            # Deliveries due for an attempt (the retry sweep); only pending ones are indexed.
            _ix(
                'pending_due',
                [('next_attempt_at', ASCENDING)],
                partialFilterExpression={'status': 'pending'},
            ),
            _ix('expires_at_ttl', [('expires_at', ASCENDING)], expireAfterSeconds=0),
        ),
    ),
    CollectionSpec(WORKERS, (_ix('heartbeat_at_ttl', [('heartbeat_at', ASCENDING)], expireAfterSeconds=300),)),
)

COLLECTIONS: dict[str, CollectionSpec] = {spec.name: spec for spec in SPECS}
