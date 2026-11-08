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
| `repositories` | `_id`, `name` (unique), `format`, `type` (`proxy`/`hosted`/`group`), `online`, `description`, `storage{quota_bytes, quota_mode}`, `proxy{upstream_url, auth{kind (`none`/`basic`/`bearer`), username, secret_enc}, metadata_max_age_s, content_max_age_s, negative_ttl_s, stale_on_error, offline, allow_private_upstream, ca_pem, http_proxy, auto_block{failures, state, until}}`, `hosted{write_policy, immutable_tag_patterns[], signing_key_id}`, `group{members[], writable_member}`, `format_options{...}` (validated by the format's `config_schema`; docker: `aliases[]` (upstream aliases, lower-case), `docker_hub`, `default_repo` (at most one docker repo has it)), `cleanup_policy_ids[]`, `created_at/by`, `updated_at/by`, `config_version` | `name` unique; `name` unique case-insensitive (`name_ci_unique`, collation strength 2); `format,type` |
| `cleanup_policies` | `_id`, `name` (unique case-insensitively), `description`, `format` (a format or `*`), `criteria{last_downloaded_older_than_days, published_older_than_days, keep_latest_n_versions, name_include, name_exclude, version_include, version_exclude, prerelease_only, untagged_older_than_days}` (only the criteria that are set), `schedule` (cron/macro or null = manual), `enabled`, `created_at/by`, `updated_at/by`. A scheduled policy has the `job_definitions` entry `cleanup:<id>`; its runs are the `job_runs` with `dedupe_key` `cleanup:<id>` (`waystation_common.db.cleanup`) | `name_unique` (collation strength 2) |
| `settings` | singleton docs by `_id` (`general`, `docker_routing`, `security`, `stats`, `ui`) | – |
| `auth_providers` | `_id`, `kind` (`ldap`; `oidc` with T-A02), `name` (unique case-insensitively, checked by the API), `enabled`, `order`, `ldap{connection{servers[], security (`ldaps`/`starttls`/`none`), ca_pem, bind_dn, bind_password_enc (AES-GCM, associated data `auth_provider:<id>`), connect_timeout_s, timeout_s}, users{base_dn, filter, username_attribute, display_name_attribute, email_attribute}, groups{mode (`none`/`member_of`/`search`), base_dn, filter, member_attribute, member_value (`dn`/`username`), name_attribute, member_of_attribute, nested, require_group}, cache_ttl_s}`, `config_version` (bumped on every update; part of the bind-cache key), `created_at/by`, `updated_at/by`. The group mapping is stored as `groups` documents (`provider_id`) | `order` |
| `signing_keys` | `_id`, `repo_id`, `algorithm`, `fingerprint`, `public_armored`, `private_enc_path` (under `keys/`), `created_at` | `repo_id` |

### Identity and access
| Collection | Key fields | Indexes |
|---|---|---|
| `users` | `_id`, `username` (unique, lower-case), `display_name`, `email`, `source` (`local`/`ldap:<provider>`/`oidc:<provider>`), `password_hash` (local only), `totp_enc`, `disabled`, `roles[]`, `groups[]` (local group ids) + `external_groups[{provider (`ldap:<id>`), id (group DN / claim value), name}]` (replaced per provider at each sign-in through it, ≤ 500), `identities[{provider, subject (entry DN / `sub`), linked_at, last_login}]` (linked external accounts, ≤ 20), `last_login`, `failed_logins`, `locked_until`, `preferences{theme, density}`, `must_change_password` (bootstrap one-time password), `created_at` | `username` unique; `source` |
| `groups` | `_id`, `name` unique (mapped groups: `<kind>:<provider name>:<external group>`), `source` (`local`/`ldap`/`oidc`), `provider_id` (mapped groups), `external_id` (normalised group DN or name: lower case, no spaces around `,` `=` `+`), `external_group` (as entered), `position` (order in the provider's mapping), `roles[]`, `created_at`, `updated_at`. A user gets a mapped group's roles when one of its `external_groups` of that provider has a matching id or name | `name`; `source,external_id`; `provider_id` |
| `service_accounts` | `_id`, `name` unique (lower-case), `description`, `roles[]`, `disabled`, `owner_user_id`, `created_at`, `updated_at` | `name` unique |
| `roles` | `_id` (slug), `name`, `builtin`, `permissions[{action, scope}]` | – |
| `api_tokens` | `_id`, `principal{kind: user\|service_account, id}`, `name`, `prefix` (`wst_` + 8 chars, display), `hash` (HMAC-SHA256 hex under a `SECRET_KEYS`-derived key; ARCH §7), `scopes[]`, `repo_globs[]` (RBAC scope syntax), `expires_at`, `last_used_at`, `last_used_ip`, `created_at`, `created_by`, `revoked_at`, `revoked_by` (revoked tokens are kept for the record) | `hash` unique; `principal.id` |
| `sessions` | `_id` (sha256 hex of the random cookie token; the token itself is never stored), `user_id`, `csrf` (double-submit token), `created_at`, `last_seen_at`, `expires_at`, `ip`, `user_agent` | TTL on `expires_at`; `user_id` |
| `rbac_state` | singleton `grant_version` counter | – |

### Content metadata
| Collection | Key fields | Indexes |
|---|---|---|
| `components` | `_id`, `repo_id`, `format`, `namespace` (scope/group/org; may be empty), `name`, `version` (or tag set for docker), `version_sort` (format-normalised sortable key), `attributes{...}` (format-specific: platforms, license, description...), `size_bytes` (sum of assets), `created_at`, `last_downloaded_at`, `download_count`, `uploaded_by` (hosted) | `repo_id,namespace,name,version_sort` unique-ish; `format,name` (search, collation case-insensitive); `last_downloaded_at`; text index on `name, attributes.description` |
| `assets` | `_id`, `repo_id`, `path` (normalised, unique per repo), `kind` (`content`/`metadata`), `component_id?`, `blob_sha256`, `size`, `content_type`, `checksums{sha1, sha256, sha512, md5}` (as provided/computed), `headers{}` (format headers replayed on every response, e.g. `Docker-Content-Digest`), `upstream{url (final, after redirects), etag, last_modified (verbatim header), fetched_at (last confirmed by the upstream: fetch or 304), status}`, `created_at`, `updated_at`, `last_downloaded_at`, `download_count` (both flushed in bulk from `seen:assets`/`dl:assets` by `stats.rollup`; absent until the first download after a fill) | `repo_id,path` unique; `blob_sha256` (GC + "where used"); `component_id`; `repo_id,last_downloaded_at` |
| `docker_tags` | `repo_id`, `image` (name; the upstream name for proxies, `library/nginx`), `tag`, `manifest_digest`, `updated_at`, `pushed_by` (proxies: recorded when a tag manifest is cached) | `repo_id,image,tag` unique; `repo_id,manifest_digest`; `repo_id,image,updated_at desc,tag` (browse) |
| `docker_manifests` | `repo_id`, `image`, `digest`, `media_type`, `is_index`, `subject_digest?` (referrers), `artifact_type?`, `manifests[{digest,size,media_type,platform{os,architecture,variant}}]` (index entries, ≤ 256), `platforms[]` (index: entry platforms without attestations), `layers[{digest,size,media_type}]` (≤ 1000), `config_digest`, `config_size`, `size_total` (config + layers; index: sum of entry sizes), `created_at`, `updated_at` (proxies: recorded when a manifest is cached) | `repo_id,image,digest` unique; `repo_id,subject_digest` |
| `apt_pool_index` | `repo_id`, `filename` (pool path), `sha256`, `size`, `suite`, `component`, `arch`, `verified`, `updated_at` (FORMATS apt: `Filename → (SHA256, Size)` from cached `Packages` files) | `repo_id,filename` unique; `repo_id,suite,component`; `repo_id,verified` |
| `apt_packages` | One document per stanza of a cached `Packages` index: `repo_id`, `suite` (`dists/<suite>` name; flat repositories: the directory, `.` for the root), `component`, `arch` (the index's `binary-<arch>`), `filename`, `name`, `version`, `architecture` (the stanza's, e.g. `all`), `section`, `sha256`, `size`, `gen` (sha256 of the index blob; a new generation replaces the listing), `updated_at`. Browse only, never used to validate | `repo_id,suite,component,arch,filename` unique; `repo_id,suite,name,filename,arch,component` (browse pages); `filename,repo_id` (cached files → listings) |
| `yum_packages` | `repo_id`, `path` (package path in the repository = `repo_dir` + `location href`), `algorithm` (`sha1`/`sha256`/`sha512`), `digest`, `size`, `verified`, `repo_dir` (the directory holding `repodata/`; null when unverified), `name`, `epoch`, `version`, `release`, `arch`, `gen` (sha256 of the `primary` blob the entry came from; a new generation replaces the directory's listing), `updated_at` (FORMATS yum: packages validated against cached `primary.xml` files; unverified = computed SHA-256 of a package no listing named yet) | `repo_id,path` unique; `repo_id,repo_dir,gen`; `repo_id,verified` |
| `npm_packages` | `repo_id`, `name` (`@scope/name` or `name`), `latest`, `dist_tags{}` (≤ 100), `version_count`, `modified`, `description?` / `license?` (only from a full packument; kept when an abbreviated one is stored), `created_at`, `updated_at`. Written in the background when a packument is cached | `repo_id,name` unique |
| `blobs` | `_id` = sha256 hex, `size`, `first_seen`, `claimed_at` (last time a commit put or found the file in place: the GC claim, written before the rename), `adopted?` (a file GC found without a document), `gc_marked_at?` (first seen unreferenced; cleared when referenced or claimed), `gc_state?` = `deleting` + `gc_deleting_at` (GC is removing the file; claims wait, or take over after 10 min). Protocol in `waystation_common.db.blobs` (ARCHITECTURE §5) | `gc_marked_at` |
| `upload_sessions` | `_id` (uuid), `repo_id`, `kind` (`oci-chunked`/`single`), `principal`, `bytes_received`, `started_at`, `expires_at` | TTL `expires_at` |

### Operations, stats, audit
| Collection | Key fields | Indexes |
|---|---|---|
| `job_definitions` | `_id` (slug, e.g. `stats.rollup`), `kind`, `params` (the payload), `schedule` (5-field UTC cron, `@daily`-style macro or `@every <n>s\|m\|h`), `enabled`, `queue?` (overrides the kind's queue), `next_run_at` (advanced by compare-and-set: one fire is enqueued once; reset to null when a policy's schedule changes), `last_fired_at`. Built-ins are seeded by the worker if missing: `stats.rollup` (`@every 10s`), `blob.gc` (`0 */6 * * *`, `params{grace_s, upload_age_s}`); cleanup policies with a schedule have `cleanup:<policy id>` (kind `cleanup.run`) | – |
| `job_runs` | one per attempt: `_id`, `job_id` (envelope id; same across retries), `definition_id?`, `kind`, `queue`, `dedupe_key`, `attempt`, `status` (`running`, `succeeded`, `failed` (will retry), `dead`, `interrupted` (worker lost)), `progress{done,total,message}`, `started_at`, `finished_at`, `worker_id`, `log[]` (capped 500 lines), `result{...}`, `error?`, `expires_at` (90 d; 1 d for routine jobs such as the 10 s stats rollup) | `started_at` desc; `job_id,started_at`; `definition_id,started_at`; `dedupe_key,started_at` (a policy's runs); `kind,started_at` (GC runs); TTL `expires_at` |
| `workers` | `_id` = worker id (`<hostname>-<pid>-<suffix>`, also its consumer name), `hostname`, `version`, `queues[]`, `started_at`, `heartbeat_at` (every 10 s), `leader` (holds `lease:scheduler`), `running_jobs[]{job_id,kind,queue}`; removed on graceful stop | TTL on `heartbeat_at` (+5 min) |
| `stats_minute` | **time-series** (`timeField: ts`, `metaField: m{repo_id, format}`, granularity minutes), one document per closed minute per repo (written once; time-series collections can't upsert): (the rollup checks for an existing (repo, minute) document before inserting, so a crashed rollup never duplicates) `requests`, `hits`, `misses`, `coalesced`, `stale`, `errors`, `bytes_out_cache`, `bytes_out_upstream`, `bytes_in_upstream`, `bytes_in_upload`, `lat_hist` (fixed log-scale bucket counts; percentiles computed at query time) | expireAfterSeconds 7 d |
| `job_outbox` | `_id`, `job` (`JobEnvelope`), `queue`, `dedupe_key`, `created_at`, `sent_at` (null until dispatched), `sent_epoch` (the `jobs:epoch` it was dispatched under), `done_at` (set by the worker whose run covered it, or when it was dead-lettered). Written in the same transaction as the triggering change (ARCH §10); in-flight entries (sent, not done) are re-dispatched when the epoch changes or after 1 h | `sent_at`; `done_at` TTL 7 d |
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
  Today the repository catalog, the provider catalog and the group directory are refreshed every 5 s instead (and
  at once on the replica that made the change).

## 2. Valkey keys (all with TTLs unless noted)
| Key | Type | Purpose |
|---|---|---|
| `fill:<repo_id>:<sha1(asset_key)>` | hash: `owner` (token), `status` (`pending`/`streaming`/`done`/`failed`/`aborted`), `host` (owner's boot id), `upload` (upload file id), `written`, `synced`, `total`, `content_type`, `headers` (JSON), `sha256`, `size`, `outcome`, `error`, `error_code`, `followers` | single-flight lock + progress for coalescing (ARCH §6.3); TTL = the lease (`WAYSTATION_COALESCE_LEASE_S`), renewed by the owner; finished fills linger 10 s |
| `neg:<repo_id>:<asset_key>` | string | negative cache (404) |
| `meta:<repo_id>:<asset_key>` | string/bytes | hot metadata response cache (rewritten/merged docs), short TTL |
| `cnt:<minute>:<repo_id>` | hash | per-minute counters (`requests`, `hits`, `misses`, `coalesced`, `stale`, `errors`, `bytes_*`, `upstream_requests`, `upstream_errors`) + `format`; `<minute>` = epoch minute (`ts // 60`); rolled up by the worker after the minute closes; TTL 3 h safety net |
| `lat:<minute>:<repo_id>` | hash of bucket → count | latency histogram, fixed log-scale buckets `le_1`, `le_2`, `le_5` ... `le_60000`, `le_inf` (ms upper bounds, `waystation_common.events.keys`) |
| `stats:pending` | set (no TTL) | `<minute>:<repo_id>` pairs with counters awaiting rollup (lets the rollup find keys without SCAN) |
| `transfers` | hash `<transfer_id>` → JSON (state, bytes, rate, updated_at); stale entries swept by the rollup job | live transfers: current state |
| `live` | pub/sub channel | transfer progress (≤ 4 Hz per transfer), no history |
| `activity` | stream (MAXLEN ~200000) | durable-ish activity events for SSE resume, Activity page, dashboard feed |
| `revocations` | pub/sub channel | revoked API token ids, or `*` (drop every cached token) (ARCH §7) |
| `seen:sessions`, `seen:tokens` | hashes id → `<epoch s>` (sessions) / `<epoch s> <client address>` (tokens) | batched `last_seen_at` / `last_used_at` + `last_used_ip`, drained (`RENAME` to `<key>:flushing`) and flushed to MongoDB by the `stats.rollup` job; only moves timestamps forward |
| `seen:assets`, `dl:assets` | hashes asset id → `<epoch s>` (last download) / count since the last flush | written by the proxy engine through the emitter on every served download; drained and flushed to `assets.last_downloaded_at` (`$max`) / `download_count` (`$inc`) by `stats.rollup` |
| `maintenance:totals` | hash (no TTL): `cleanup_removed`, `cleanup_bytes`, `gc_freed_blobs`, `gc_freed_bytes`, `gc_uploads_removed`, `gc_runs` | running totals written by the cleanup and GC jobs; exported on `/metrics` as `waystation_cleanup_*_total` / `waystation_gc_*_total` |
| `jobs:<queue>` | stream (no TTL), consumer group `workers`, field `job` = `JobEnvelope` JSON; entries are deleted when acked | job queue (`index`, `maintenance`, `stats`, `prefetch`, `import`) |
| `jobs:<queue>:delayed` | sorted set (no TTL): envelope JSON scored by due time (ms) | `run_after` jobs and retry backoff; promoted into the stream by the scheduler leader |
| `jobs:dead` | stream (MAXLEN ~10000): `job` (envelope, `attempt` = attempts made), `error`, `ts` | dead letters |
| `jobs:epoch` | string (no TTL) | random token; when it is missing or changes, Valkey lost its data and in-flight outbox entries are re-dispatched |
| `job:dedupe:<key>` | hash (TTL 24 h, refreshed on every transition): `state` (`pending`/`running`), `job` (envelope id), `owner`, `outbox`, `rerun` (envelope JSON), `rerun_outbox` | dirty-flag dedupe (ARCH §10); key = the payload's `dedupe_key()`, `def:<definition>` for scheduled jobs without one, else `id:<job id>` |
| `lease:scheduler`, `lease:<name>` | string (owner id, PX TTL) | leader election: `lease:scheduler` (15 s, outbox dispatcher + scheduler), `lease:rollup` (30 s, held for one stats rollup run) |
| `grants:<principal>:<grant_version>` | bytes | cached effective permissions |
| `rl:<scope>:<id>` | string counters | rate limiting (logins, token use) |
| `upstream:<repo_id>:health` | hash (no TTL; one per proxy repo, removed with the repo) | `last_ok_at`, `last_error`, `last_error_at`, `consecutive_failures`, `last_latency_ms`: written by the proxy engine through the emitter; read by upstream health / repository status |
| `upstream:<repo_id>:ratelimit` | hash | last seen upstream rate-limit headers |

## 3. Blob store (`WAYSTATION_DATA_DIR`)
See ARCHITECTURE §5. `blobs/sha256/aa/bb/<hex>`, `uploads/<uuid>`, `keys/<key-id>.enc`. Nothing else is written there.
