# Waystation data model

MongoDB is the system of record; Valkey holds only ephemeral state; the blob store holds file content.
Collection access goes through the `*.db` modules only (ARCHITECTURE §3). IDs are ULIDs (string) unless noted.
All timestamps are UTC `datetime`. Collections and indexes are declared in `waystation_common.db.collections` and
created by `init` (`db.schema.ensure_schema`, idempotent; index names are part of the schema); data changes are
forward-only migrations (`db.migrations`, recorded in `migrations`).

## 1. MongoDB (database `WAYSTATION_MONGO_DB`, default `waystation`)

### Configuration
| Collection | Key fields | Indexes |
|---|---|---|
| `repositories` | `_id`, `name` (unique), `format`, `type` (`proxy`/`hosted`/`group`), `online`, `description`, `storage{quota_bytes, quota_mode}`, `proxy{url, aliases[], auth{kind, secret_enc}, metadata_max_age_s, content_max_age_s, negative_ttl_s, stale_on_error, offline, allow_private_upstream, ca_pem, http_proxy, auto_block{failures, state, until}}`, `hosted{write_policy, immutable_tag_patterns[], signing_key_id}`, `group{members[], writable_member}`, `format_options{...}` (validated by the format's `config_schema`), `cleanup_policy_ids[]`, `created_at/by`, `updated_at/by`, `config_version` | `name` unique; `format,type` |
| `cleanup_policies` | `name` unique, `format` or `*`, `criteria{...}`, `schedule` (cron), `enabled`, `last_run` | `name` |
| `settings` | singleton docs by `_id` (`general`, `docker_routing`, `security`, `stats`, `ui`) | – |
| `auth_providers` | `_id`, `kind` (`ldap`/`oidc`), `name`, `enabled`, `order`, `config{...}` (secrets encrypted), `group_mapping[]` | `order` |
| `signing_keys` | `_id`, `repo_id`, `algorithm`, `fingerprint`, `public_armored`, `private_enc_path` (under `keys/`), `created_at` | `repo_id` |

### Identity and access
| Collection | Key fields | Indexes |
|---|---|---|
| `users` | `_id`, `username` (unique, lower-case), `display_name`, `email`, `source` (`local`/`ldap:<provider>`/`oidc:<provider>`), `password_hash` (local only), `totp_enc`, `disabled`, `roles[]`, `groups[]` (local) + `external_groups[]` (last sync), `last_login`, `failed_logins`, `locked_until` | `username` unique; `source` |
| `service_accounts` | `_id`, `name` unique, `description`, `roles[]`, `owner_user_id` | `name` |
| `groups` | `_id`, `name` unique, `source` (`local`/`ldap`/`oidc`), `external_id`, `roles[]` | `name`; `source,external_id` |
| `roles` | `_id` (slug), `name`, `builtin`, `permissions[{action, scope}]` | – |
| `api_tokens` | `_id`, `principal{kind, id}`, `name`, `prefix` (first 8 chars, display), `hash` (sha256), `scopes[]`, `repo_globs[]`, `expires_at`, `last_used_at`, `last_used_ip`, `created_at`, `revoked_at` | `hash` unique; `principal.id` |
| `sessions` | `_id` (random), `user_id`, `created_at`, `last_seen_at`, `expires_at`, `ip`, `user_agent` | TTL on `expires_at`; `user_id` |
| `rbac_state` | singleton `grant_version` counter | – |

### Content metadata
| Collection | Key fields | Indexes |
|---|---|---|
| `components` | `_id`, `repo_id`, `format`, `namespace` (scope/group/org; may be empty), `name`, `version` (or tag set for docker), `version_sort` (format-normalised sortable key), `attributes{...}` (format-specific: platforms, license, description...), `size_bytes` (sum of assets), `created_at`, `last_downloaded_at`, `download_count`, `uploaded_by` (hosted) | `repo_id,namespace,name,version_sort` unique-ish; `format,name` (search, collation case-insensitive); `last_downloaded_at`; text index on `name, attributes.description` |
| `assets` | `_id`, `repo_id`, `path` (normalised, unique per repo), `kind` (`content`/`metadata`), `component_id?`, `blob_sha256`, `size`, `content_type`, `checksums{sha1, sha256, sha512, md5}` (as provided/computed), `upstream{url, etag, last_modified, fetched_at, status}`, `created_at`, `last_downloaded_at`, `download_count` | `repo_id,path` unique; `blob_sha256` (GC + "where used"); `component_id`; `repo_id,last_downloaded_at` |
| `docker_tags` | `repo_id`, `image` (name), `tag`, `manifest_digest`, `updated_at`, `pushed_by` | `repo_id,image,tag` unique; `repo_id,manifest_digest` |
| `docker_manifests` | `repo_id`, `image`, `digest`, `media_type`, `subject_digest?` (referrers), `artifact_type?`, `platforms[]`, `layers[{digest,size,media_type}]`, `config_digest`, `size_total` | `repo_id,image,digest` unique; `repo_id,subject_digest` |
| `apt_pool_index` | `repo_id`, `filename` (pool path), `sha256`, `size`, `suite`, `component`, `arch`, `verified`, `updated_at` (FORMATS apt: `Filename → (SHA256, Size)` from cached `Packages` files) | `repo_id,filename` unique; `repo_id,suite,component` |
| `blobs` | `_id` = sha256 hex, `size`, `first_seen`, `refcount_hint` (advisory; GC is mark-and-sweep), `gc_marked_at?` | `gc_marked_at` |
| `upload_sessions` | `_id` (uuid), `repo_id`, `kind` (`oci-chunked`/`single`), `principal`, `bytes_received`, `started_at`, `expires_at` | TTL `expires_at` |

### Operations, stats, audit
| Collection | Key fields | Indexes |
|---|---|---|
| `job_definitions` | `_id`, `kind`, `params`, `schedule`, `enabled`, `queue` | – |
| `job_runs` | `_id`, `definition_id?`, `kind`, `queue`, `status`, `progress{done,total,message}`, `started_at`, `finished_at`, `worker_id`, `log[]` (capped 500 lines), `result{...}` | `started_at` desc; TTL 90 d |
| `workers` | `_id` = worker id, `hostname`, `version`, `queues[]`, `started_at`, `heartbeat_at`, `running_jobs[]` | TTL on `heartbeat_at` (+5 min) |
| `stats_minute` | **time-series** (`timeField: ts`, `metaField: m{repo_id, format}`, granularity minutes), one document per closed minute per repo (written once; time-series collections can't upsert): `requests`, `hits`, `misses`, `coalesced`, `stale`, `errors`, `bytes_out_cache`, `bytes_out_upstream`, `bytes_in_upstream`, `bytes_in_upload`, `lat_hist` (fixed log-scale bucket counts; percentiles computed at query time) | expireAfterSeconds 7 d |
| `job_outbox` | `_id`, `job` (payload), `queue`, `dedupe_key`, `created_at`, `sent_at?`, `done_at?` (written in the same transaction as the triggering change; ARCH §10) | `sent_at`; `done_at` TTL 7 d |
| `stats_hour` / `stats_day` | same shape, time-series, rolled up | 90 d / 2 y |
| `storage_snapshots` | time-series per repo: `bytes`, `assets`, `components` (hourly) | 2 y |
| `downloads_daily` | `component_id`, `day`, `count`, `bytes` | `component_id,day` unique; TTL 400 d |
| `audit` | `_id`, `ts`, `principal{kind,id,name}`, `ip`, `action`, `target{kind,id,name}`, `outcome`, `details{...}` | `ts` desc; `principal.id,ts`; `action,ts`; TTL per settings |
| `migrations` | `_id` = applied migration id (`NNNN_slug`), `description`, `applied_at` | – |

Notes:
- `components.version_sort` is computed by each format (semver, PEP 440, Maven ComparableVersion, Debian
  dpkg order, RPM EVR, Go semver) so "latest N" and sorting are index-backed.
- Large lists (docker layers, file lists) stay in their own documents; no unbounded arrays in hot documents.
- Change streams on `repositories`, `settings`, `rbac_state`, `auth_providers` invalidate replica caches.

## 2. Valkey keys (all with TTLs unless noted)
| Key | Type | Purpose |
|---|---|---|
| `fill:<repo_id>:<asset_key>` | string (JSON: upload id, owner replica, progress, total) | single-flight lock + progress for coalescing (lease, renewed) |
| `neg:<repo_id>:<asset_key>` | string | negative cache (404) |
| `meta:<repo_id>:<asset_key>` | string/bytes | hot metadata response cache (rewritten/merged docs), short TTL |
| `cnt:<minute>:<repo_id>` | hash | per-minute counters (rolled up by worker after the minute closes) |
| `lat:<minute>:<repo_id>` | hash of bucket → count | latency histogram (fixed log-scale buckets) |
| `transfers` | hash `<transfer_id>` → JSON (state, bytes, rate, updated_at); stale entries swept by the rollup job | live transfers: current state |
| `live` | pub/sub channel | transfer progress (≤ 4 Hz per transfer), no history |
| `activity` | stream (MAXLEN ~200000) | durable-ish activity events for SSE resume, Activity page, dashboard feed |
| `revocations` | set + pub/sub channel | revoked token/session ids (ARCH §7) |
| `seen:sessions`, `seen:tokens` | hashes id → timestamp | batched `last_seen_at` / `last_used_at`, flushed to MongoDB by the rollup job |
| `jobs:<queue>` / `jobs:dead` | streams + consumer groups (no TTL) | job queue |
| `job:dedupe:<key>` | string | enqueue de-duplication |
| `lease:scheduler`, `lease:<name>` | string | leader election |
| `grants:<principal>:<grant_version>` | bytes | cached effective permissions |
| `rl:<scope>:<id>` | string counters | rate limiting (logins, token use) |
| `upstream:<repo_id>:ratelimit` | hash | last seen upstream rate-limit headers |

## 3. Blob store (`WAYSTATION_DATA_DIR`)
See ARCHITECTURE §5. `blobs/sha256/aa/bb/<hex>`, `uploads/<uuid>`, `keys/<key-id>.enc`. Nothing else is written there.
