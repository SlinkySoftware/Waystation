# Changelog

All notable changes, newest first. Each entry: date, task ID, summary.

## 2026-11-09
- T-M03: deployment and reverse-proxy guides. `Documents/Operations/DEPLOYMENT.md` describes the single-host
  `compose.yaml` as it works today: containers and networks, requirements (Waystation owns the root of its host
  name), sizing, images, host layout and storage, the env file (with a compose override for `*_FILE` secrets), the
  environment reference (application settings from `.env.example` plus compose-only variables), start-up and first
  sign-in, health checks, TLS options, scaling and split-deployment status, manual backup/restore commands, upgrades
  and troubleshooting; planned pieces are marked with their task IDs. `Documents/Operations/REVERSE_PROXIES.md`:
  requirements for any proxy (no body limit, no request/response buffering, unbuffered event stream, long idle
  timeouts, keep-alive), `X-Forwarded-For` trust, and example configurations for nginx, Traefik v3 (static config,
  file provider, Docker labels in the new `Build/Containers/examples/traefik.override.example.yaml`), Caddy and
  HAProxy under `Build/Containers/examples/reverse-proxies/`, plus Synology DSM notes. `make proxies-check` runs each
  example in containers (`nginx -t`, `caddy validate`, `haproxy -c`, Traefik start-up, `docker compose config`), then
  against a stand-in web tier over TLS (redirect, unbuffered SSE, 1 GiB download and upload, forwarded headers;
  `SLOW=1` adds a 75 s idle response and an 80 s upload) and checks the doc blocks equal the files. `compose.yaml`
  now passes `WAYSTATION_INSTANCE_NAME`, `WAYSTATION_BOOTSTRAP_ADMIN_PASSWORD[_FILE]`, the session lifetimes and the
  sign-in limits through to the containers (they were silently ignored before).

## 2026-11-08
- T-F08: job system on Valkey Streams (ARCHITECTURE §10). `waystation_common.jobs`: typed payloads (`JobPayload`
  with `kind`, `queue`, `max_attempts`, `dedupe_key()`, `routine`; `JOB_TYPES` registry), `JobEnvelope`, the queue
  state machine as atomic Lua scripts (`JobQueue`: enqueue with dirty-flag dedupe, start, finish/retry/dead, heartbeat,
  reclaim via XAUTOCLAIM, delayed set, epoch), cron/`@every` schedules, and `JobClient` for the server
  (`Services.jobs`: `enqueue_durable` into `job_outbox` inside the caller's transaction, `enqueue` straight to Valkey).
  `waystation_common.db.jobs` for `job_outbox`, `job_runs`, `job_definitions`, `workers` (new collections and indexes
  in the registry); `waystation_common.leases` (shared lease helpers). Worker: `JobSystem` = runner (concurrency,
  heartbeats, retries with backoff, dead letters, `job_runs` with progress/log/result, `job.*` activity events),
  coordinator under `lease:scheduler` (outbox dispatcher with re-dispatch when Valkey lost its data, scheduler with
  compare-and-set fires, delayed promotion, consumer pruning), presence in `workers`, liveness watchdog for the
  healthcheck; graceful stop lets jobs finish for 8 s. The stats rollup now runs as the scheduled job `stats.rollup`
  (`@every 10s`, queue `stats`), still under `lease:rollup`, so a minute is written exactly once. Integration tests
  cover a killed worker (exactly one re-run), a flushed Valkey (outbox re-dispatched), uploads during a running
  rebuild (exactly one rerun that sees them all), one fire per schedule with three workers, retries and dead letters.
- T-F03: CI. `.github/workflows/ci.yml` runs `make check`, the stale-generated-client check (`make gen-api` +
  `git diff --exit-code`), the budget report (job summary) and, in a parallel job, `make clients-test FORMAT=all
  MODE=replay`; the dev image is built once per job with the GitHub Actions layer cache (`SKIP_DEV_IMAGE_BUILD=1`
  stops make rebuilding it). `release.yml` builds server/worker/web for amd64 and arm64 on `v*` tags (checked against
  `VERSION`) and pushes them to GHCR with SBOM, `mode=max` provenance and a GitHub artifact attestation, using only
  `GITHUB_TOKEN`; the web image now builds its SPA stage on the build platform. `.gitea/workflows/images.yml` pushes
  images to the Gitea registry on every push to `main` (`vars.REGISTRY_HOST`, `secrets.REGISTRY_USER`/
  `REGISTRY_TOKEN`). Actions pinned by commit SHA; `make workflow-lint` (actionlint + shellcheck). Repository-wide
  forbidden-strings check `make forbidden-strings` (`Build/ci/forbidden_strings.sh`): the `clients-check` scanner now
  also scans every tracked/untracked file, with the base allowlist moved to `Build/ci/allowed_hosts.txt` (adds `url`
  and `not-a-host` entries), CIDR definitions of the special ranges accepted, lockfiles host-checked on download
  lines only, SVG path data ignored, and a `# forbidden-strings: allow` line marker used by the SSRF and client-IP
  tests. `Build/ci/budgets.json` (SPEC §5.1) + `make budgets` report; the SPA bundle budget reads its limit there and
  records the measured size. Both `forbidden-strings`, `workflow-lint` and `budgets` are part of `make check`.
  `Documents/Operations/CI.md`.
- T-S08: request coalescing across replicas (ARCHITECTURE §6.3). New `waystation_server.proxy.coalesce`: one
  Valkey hash per asset (`fill:<repo_id>:<sha1(key)>`) claimed by a Lua script (owner or counted follower in one
  round trip), lease renewed by a background publisher (`WAYSTATION_COALESCE_LEASE_S`, default 15 s) that also
  publishes progress (every 20 ms while followed, 250 ms otherwise) and `fdatasync`s for a `synced` offset while
  followers are attached; one shared poller per key and process for followers of other replicas, in-process
  followers read the owner's lease directly. Engine: `serve`/`ensure` claim before contacting the upstream for a
  missing or stale asset; followers of a streaming content fill tail the upload file (`COALESCED`, last piece held
  back until the owner committed), others wait and are served by X-Accel (metadata, `HEAD`, `COALESCE_TAIL=wait`,
  other host or network filesystem from `/proc/mounts`, host id = kernel boot id); failures and mismatches fail
  followers with the same error (stale-on-error per follower); an aborted owner (client gone) or a dead one (lease
  expiry) is replaced by a follower, which resumes after checking the bytes it already sent are a prefix of the new
  fill; Valkey down disables coalescing only. The tee moved into `_tee` (unchanged behaviour for a single client).
  Live transfers carry `followers` (`Transfer.followers`, `EventEmitter.transfer_followers`), new metric
  `waystation_coalesce{repo,event}`; `BlobWriter.flushed`/`fileno()`. No format plugin changes. Tests: two uvicorn
  replicas with 200 clients for one 64 MiB blob (1 upstream fetch, identical bytes; `WAYSTATION_TEST_COALESCE_MIB`),
  owner replica SIGKILLed mid-transfer, and in-process two-instance tests (mismatch, takeover, metadata, 404, wait
  mode, Valkey down). The fake upstream can pause mid-body (`gate`/`pause_at`) or stall (`hang_after`).
- T-S09 (M0 slice): browse API to the M0 contract (`api/browse.py`, contract test extended):
  `GET /api/v1/repositories/{name}/browse` (generic folder tree of cached assets, every format: folders first, keyset
  cursors, one index seek per sub-folder, 404 for a missing folder), npm `packages`/`versions`, apt
  `suites`/`packages`, docker `images`/`tags`/`manifest`; `browse` permission, hidden repositories 404, format views
  404 on other formats. Format-neutral `waystation_server.browse` package (response models, `BrowseContext`, the
  `NpmBrowser`/`AptBrowser`/`DockerBrowser` protocols a plugin exposes as `browser`, cursors). Background indexing
  after cache fills (`formats/common/background.py`): `npm_packages` (packument summaries), `apt_packages` (each
  cached `Packages` stanza; `deb822` now also reads Package/Version/Architecture/Section and the descriptive
  `Release` fields), `docker_tags`/`docker_manifests` (every cached manifest; `repo_image_updated` index). Repository
  delete also drops these records and `apt_pool_index`. Integration tests populate repositories through a fake
  upstream and explain every browse query (no collection scans); the raw, npm, apt and docker harness scripts check
  the browse API after their installs and pulls. The UI's browse renderers now work against the real server.
- T-P2-DOCKERP (M0 slice): Docker / OCI proxy plugin (`formats/docker/plugin.py`, `protocol.py`, `upstream_auth.py`,
  registered in `plugins.py`). Pull side of the registry API at `/v2/<repo|alias|default>/...` and
  `/repository/<repo>/v2/...`: tag manifests as metadata (one asset per Accept variant, checked against the upstream
  `Docker-Content-Digest`, never converted), stale tags re-checked with an upstream `HEAD` first; digest manifests and
  blobs as content verified against their digest (blobs keyed by digest, CDN redirects followed without
  Authorization); tags/list with paging; `HEAD` misses answered by an upstream `HEAD`; OCI error codes. Upstream auth
  pings `/v2/` once per repository and process to learn the challenge, fetches anonymous or credentialed pull tokens
  (Docker Hub realm built in) and caches them per scope; Docker Hub one-segment names get `library/` and
  `ratelimit-*` headers are recorded for upstream health. Engine: `AssetSpec.revalidate_head` (keep a stale asset
  that an upstream `HEAD` confirms unchanged) and `AssetSpec.on_upstream` (see each upstream response's headers).
  Real-client harness format `docker` (dockerd mirror mode, prefix and alias pulls from Docker Hub and GHCR; skopeo
  inspect, list-tags and copy) with 2.6 MB of fixtures; the `client-docker` service is now `docker:29-dind` plus curl
  and skopeo with its entrypoint cleared. Client guide `Documents/Operations/clients/docker.md`.
- T-P2-APT (M0 slice): apt proxy plugin (`formats/apt/plugin.py`, bounded `Release`/`Packages` parsers and
  decompression in `formats/apt/deb822.py`, `apt_pool_index` access in `db/apt.py`, registered in `plugins.py`):
  `InRelease`/`Release`/`Release.gpg` passed through untouched as metadata; index files served only by the SHA256 the
  cached `Release` lists (stored under their `by-hash` key, fetched by-hash when offered, one `Release` revalidation
  on mismatch, then 502; unlisted indexes 404); `by-hash/SHA256|SHA512` immutable and self-validating; cached
  `Packages` files indexed in the background into `apt_pool_index`, pool files validated against their listing or
  served `unverified` and dropped when a later listing disagrees. New `apt_pool_index` index `repo_id,verified`. "Set
  me up" snippets use the Ubuntu keyring for Ubuntu upstreams and no `-updates` suffix for `-security` suites.
  Real-client harness format `apt` (Debian 13 + Ubuntu 26.04, four repositories, `*-updates`/`*-security` suites,
  2.3 MB of fixtures, 32 MiB budget per the owner's fixture decision); `CLIENT` in `format.env` may list several
  clients (run in turn, `HARNESS_CLIENT` set); `lib.sh`'s `wait_for`/`ws_login`/`ws_api` work without curl (bash
  `/dev/tcp`). Client guide `Documents/Operations/clients/apt.md`.
- T-P2-NPM (M0 slice): npm proxy plugin. Packuments full and abbreviated (`application/vnd.npm.install-v1+json`) cached
  separately as metadata and revalidated, scoped names as `@scope%2fname` or `@scope/name`, every
  `versions.*.dist.tarball` rewritten to `<base>/repository/<repo>/` in a thread; tarballs cached as content and
  verified while streaming against `dist.integrity` (SRI) or `dist.shasum`, looked up in the cached abbreviated
  packument (fetched on a miss, refreshed once for an unlisted version, otherwise 404). `/-/ping` answered locally,
  `/-/v1/search` cached 5 minutes per query, audit POSTs relayed uncached when `audit_passthrough` is on. Engine:
  `AssetSpec.expect` (expected digests resolved only on an upstream fetch) and `ProxyEngine.relay` (bounded, uncached
  pass-through); `FormatDescriptor.read_posts` lets a format declare query-only POST paths that the router authorises as
  `read`. Real-client harness: `tests/clients/npm/` (npm 11, lockfile-pinned and lockfile-free installs incl. a scoped
  package, `npm audit`) with recorded registry.npmjs.org fixtures; `setup.sh` scripts can create repositories as a
  random per-run bootstrap admin; new `DROP_JSON_KEYS` format setting removes personal and registry-internal fields
  (readmes, maintainer e-mails, `_npmOperationalInternal`) from recorded JSON. Client guide
  `Documents/Operations/clients/npm.md`.
- T-P2-RAW (M0 slice): raw proxy plugin (`formats/raw/plugin.py`, registered in `plugins.py`): `GET`/`HEAD
  /repository/<repo>/<path>` cached by path; `metadata_path_regex` (searched in `/<path>`) selects revalidated
  metadata, everything else is immutable content; Content-Type by extension from Python's built-in table, else the
  sanitised upstream type; `Content-Disposition` (`attachment` default) and `X-Content-Type-Options: nosniff` on every
  answer, and `inline` never renders HTML/SVG/XML; upstream paths percent-escaped (`:` too); upstream 404/410 →
  native 404 (negative cache), other methods 405, directories 404 until listings land. Real-client harness format
  `raw` (curl + wget, fixtures from `deb.debian.org`); the harness now creates a per-run bootstrap admin
  (`HARNESS_ADMIN_PASSWORD`), `lib.sh` gains `ws_login`/`ws_api` for `setup.sh` scripts, and `make clients-check`
  scans a format's own files (`tests/clients/<format>/`) with its `allowed_hosts.txt`. import-linter: the
  engine's typing-only import of `formats.base` is ignored in the "formats never import each other" contract.
  Client guide `Documents/Operations/clients/raw.md`.
- T-V01: npm `overrides` pin `minimatch` ≥ 11.0.1 and `glob` ≥ 14.0.2 (dev-only transitive dependencies of eslint,
  `@vue/test-utils` and `openapi-typescript`) for two moderate advisories; `npm audit` clean.
- T-U03: mock data mirrors the owner's repository set: 13 proxies (`dockerhub-proxy` default/mirror + `docker.io`
  alias with the Hub rate-limit gauge, `ghcr-proxy` with the `ghcr.io` alias, apt `debian`, `debian-security`,
  `ubuntu` (resolute), `ubuntu-security`, `raspberrypi` (arm64/armhf), yum `rocky`, `almalinux`, `epel` (auto-blocked
  with a rolling retry countdown), `oracle` (degraded: slow, intermittent 5xx, stale repomd.xml), `npm` and one example
  raw repo `raw-releases`), public canonical upstreams only, per-repo freshness (docker metadata 6 h / negative 60 min,
  apt and yum 60 min with security pockets at 30 min, npm 30 min, content 24 h outside docker). The live generator adds
  dnf makecache/install runs and per-suite apt updates; storage-by-format and the traffic split include yum. yum
  displays everywhere (Rocky/Alma/Fedora marks by upstream, list and live format filters, settings edit without
  format options), gets a dnf **Set me up** (`/etc/yum.repos.d/<repo>.repo`, `gpgcheck=1`) and a "coming with
  T-P2-YUM" browse state; creating yum repos stays unavailable. Docker Set me up leads with the upstream-name pull for
  aliased non-default repos (`ghcr.io/...`). The settings form now keeps `content_max_age_s` instead of resetting it.
  Screenshot tour regenerated (new shots: `repo-ghcr-proxy-setup`, `repo-rocky-setup`, `browse-yum`).
- T-S12 (M0 slice): Docker token service `GET /auth/token` (`security.registry_token`, `registry_auth`): 5-minute
  EdDSA JWTs signed with an Ed25519 key kept encrypted in `keys/registry-token.key`; Docker scope grammar; grants are
  what RBAC allows on the repository each name resolves to (prefix, alias, default repo); anonymous tokens and
  `docker login` with a local password. `/v2/` now requires a registry token and otherwise answers the Bearer
  challenge (`realm`, `service`, `scope`, `error="insufficient_scope"`); token principals are limited to token
  grants ∩ current RBAC (`access.check`). Verified with a real `docker login` against the dev stack.
- T-S11 (M0 slice): stats and live APIs per the M0 contract: `GET /api/v1/stats/summary`, `/stats/traffic`,
  `/stats/storage`, `/stats/upstreams`, `/repositories/{name}/stats/traffic`, `/transfers`, `/activity`, and the
  repository list's 24 h `stats` and `degraded` status. Series merge rolled-up minutes with the live Valkey counters
  (a minute present in both counts once). `GET /api/v1/events/stream`: one hub per process (pub/sub `live`, blocking
  `XREAD` on `activity`, 1 Hz `stats.tick`), per-subscriber topic and RBAC filtering, bounded queues,
  `transfers.snapshot` on connect, `Last-Event-ID` resume without gaps or duplicates, 15 s heartbeats. OpenAPI now
  documents the SSE payloads (`LiveEvent`); the contract test covers every M0 operation the server implements.
- T-S07 (M0 slice): proxy engine `waystation_server.proxy.engine`. Formats describe files as `AssetSpec` (kind,
  upstream path/URL/headers, `Expected` digests incl. npm SRI, stored/response headers, size and age limits,
  `validate`/`transform` hooks run in a thread, `on_stored`, pluggable `UpstreamAuthenticator`) and call
  `serve`/`ensure`/`read`. Hits go out by X-Accel; content misses are teed to the client and the blob store with the
  last chunk held until verified and committed; metadata is fetched bounded, verified, transformed and committed.
  Conditional revalidation (`REVALIDATED`), stale-on-error (`STALE`), offline mode and a Valkey negative cache.
  Redirects followed manually with the SSRF guard on every hop and credentials only for the upstream origin.
  Records `assets`/`blobs`, live transfers (new `Transfer` fields `artifact`, `component`, `client_agent`, `error`),
  activity (`cache.hit`, transfers), per-minute counters (new `upstream_requests`, `upstream_errors`), the upstream
  health hash and Prometheus metrics. Fault-injection integration tests (timeouts, slow body, reset, 5xx, digest
  and size mismatch, validation failure, SSRF on redirect, credential stripping) and a dev-stack run against a
  public Debian mirror.
- T-S06: data plane `waystation_server.dataplane.xaccel.serve_blob`: `If-None-Match`/`If-Modified-Since` → 304 by
  the server, HEAD answered by the server, GET → empty response with `X-Accel-Redirect` to `/_blobs/...`, digest
  ETag and `X-Ws-*` headers, format headers replayed. nginx `/_blobs/` also re-adds `Docker-Distribution-API-Version`,
  `X-Ws-Cache` and `X-Request-Id`. Verified through the dev stack's nginx (exact bytes, 206 ranges, 304, HEAD).
- T-S05 (M0 slice): repositories. Format descriptors with option models and "Set me up" snippets for docker
  (aliases, Docker Hub, default repo), npm, apt (distribution, flat) and raw (metadata regex) proxies, registered by
  `waystation_server.plugins`. `repos.service` validates names (reserved words, docker aliases, case-insensitive
  uniqueness → 409), formats/types, upstream URLs and options (422 field errors), encrypts upstream secrets
  (write-only, kept on update) and keeps one default docker repository. REST: list (filters, cursor paging,
  RBAC-filtered), create, read, replace, delete, test upstream, setup guide, per the M0 contract; OpenAPI
  `FormatOptions` is `oneOf` the registered option models. Upstream HTTP clients (`proxy.http`: HTTP/2, pooled per
  host, no automatic redirects) with the SSRF guard enforced at connect time (`proxy.ssrf`). Contract test compares
  the generated OpenAPI with `Application/Web/openapi/waystation-m0.yaml`. DATA_MODEL `repositories.proxy` fields
  aligned with the API.
- T-S03 (M0 slice): RBAC decision point `waystation_server.security.access`: `check`, `can_see`, `filter_repos`,
  `require`, `require_repo`, `capabilities`; built-in roles (`admin`, `reader`, `deployer`, `viewer`,
  `anonymous` = browse + read on proxy repositories); scopes `*`, name, glob, `format:`, `type:` (new, SPEC §4.10).
  Hidden repositories answer 404; visible-but-denied answers 401 (anonymous) or 403. Every route declares its rule
  with a decorator, applied by the app-wide `enforce` dependency (fails closed; CSRF for session requests on unsafe
  methods); a test enumerates all routes and fails on a missing rule.
- T-S02 (M0 slice): local authentication. argon2id passwords (hashing in a thread; unknown users cost the same),
  `init` creates the first `admin` (configured or one-time password printed once, idempotent), sessions
  (`ws_session` cookie, sha256-hashed ids, fixation-safe, idle and absolute timeouts, last-seen kept in Valkey),
  double-submit CSRF, per-address login rate limit (429 + `Retry-After`) and account lockout. REST:
  `GET /api/v1/config`, `GET /auth/providers`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`,
  `PUT /auth/me/preferences` (contract shapes). New settings: `INSTANCE_NAME`, `SESSION_IDLE_MINUTES`,
  `SESSION_MAX_HOURS`, `LOGIN_RATE_LIMIT`, `LOGIN_LOCKOUT_FAILURES`, `LOGIN_LOCKOUT_MINUTES`. The integration-test
  MongoDB gets a higher open-files limit (every test creates a database).
- T-S01: server app factory. `Services` (MongoDB, Valkey, event emitter, blob store, keyring, repository catalog,
  format registry, metrics) created and started in the lifespan; start-up refuses an unknown or unmigrated schema.
  Routing for `/repository/<repo>/<path>` and `/v2/<name>/...` (SPEC §4.4 name resolution) hands requests to format
  plugins (`formats.base.FormatPlugin`, `formats.registry`); unknown repositories get the native 404. Pure ASGI
  middleware for request id, client address (`X-Real-IP` from the web tier, `X-Forwarded-For` only through trusted
  proxies) and the access log; `HEAD` for API routes. Errors: problem+json for the API, OCI JSON under `/v2/`, plain
  text under `/repository/`. `/readyz` checks MongoDB and Valkey; `/metrics` serves Prometheus metrics to
  `WAYSTATION_METRICS_ALLOW_CIDRS` only (SPEC §4.4 default corrected to loopback). New `repositories.name_ci_unique`
  index (case-insensitive uniqueness, SPEC §3). Dependencies: `prometheus-client`; dev: `pyyaml`.
- T-P2-HARNESS (M0 slice): real-client harness in `tests/clients/`. `make clients-test FORMAT=<f|all>
  MODE=replay|record|live` (default replay) runs each format as its own compose project (own container names, no
  published port) on top of `compose.yaml` + `compose.dev.yaml`. A stdlib fake upstream impersonates the public
  upstream hosts (`extra_hosts` + per-run throwaway CA trusted via `WAYSTATION_EXTRA_CA_FILE`), replays fixtures
  from `tests/fixtures/upstreams/<format>/` and fails the run on any miss; record mode scrubs (response header
  allowlist, signed-URL query values, JSON credentials; the auth *scheme* only is keyed) and installs fixtures only
  after the client passed and `make clients-check` is clean. `make clients-check` (now part of `make check`) rejects
  non-allowlisted hosts, internal suffixes, non-documentation IPs, private keys/tokens, local deny-list strings and
  oversize fixtures. Pinned client services: curl, node, debian, ubuntu, skopeo, docker (dind). `echo` self-test
  format with a test-only origin passes in record, replay and live mode (also with the stack up). Nightly
  `clients-live.yml` workflow stub. Harness Python is linted, type-checked and unit-tested with the workspace.
- T-F09 (M0 slice): `waystation_common.events`: SPEC §4.7 cache states, `Transfer`/`ActivityEvent`/`LiveMessage`
  models, Valkey key helpers and fixed log-scale latency buckets with query-time percentiles, and a non-blocking
  `EventEmitter` (synchronous O(1) producer calls, bounded queue, one pipeline per 250 ms flush, progress coalesced to
  ≤ 4 Hz per transfer, Valkey outages drop stats and never affect serving). Worker `rollup` loop (leader lease)
  writes closed minutes (+5 s grace) to `stats_minute` exactly once and sweeps stale live transfers. DATA_MODEL:
  documented `stats:pending`, counter fields and bucket names. Usage is in the `waystation_common.events` docstring.
- T-F07 (M0 slice): `waystation_common.storage.BlobStore` per ARCHITECTURE §5: streaming `BlobWriter` (incremental
  sha256, writes and hashing in a worker thread in 1 MiB chunks, size limit, expected size/digest), `commit`
  (fsync, atomic rename into `blobs/sha256/aa/bb/<hex>`, fsync dirs; 0640 files, 2750 dirs), `exists`, `stat`,
  `path_for_xaccel`, `delete`, `sweep_uploads`, free-space guard. Tests cover kill -9 mid-write, concurrent
  identical commits (placed once) and event-loop responsiveness during a 64 MiB write. NFS CI scenario pending.
- T-F06 (M0 slice): MongoDB layer in `waystation_common.db`: `create_client` (PyMongo `AsyncMongoClient`,
  tz-aware), collection registry with named indexes for the M0 collections (new: `apt_pool_index` for apt pool
  validation, added to DATA_MODEL), `ensure_schema` (creates only what is missing, reports index conflicts),
  forward-only migrations with `check_schema` for newer/pending schemas, replica-set initiation. `init` now runs data
  dir → replica set → schema → migrations; a re-run changes nothing. `make test-integration` starts throwaway
  MongoDB/Valkey containers (project name unique per checkout) and runs `pytest -m integration`; it is part of
  `make check`.
- T-F05 (M0 slice): containers and dev stack. Server/worker images (uv, non-editable venv, UID/GID 10001), web image
  (nginx-unprivileged, SPA build stage with placeholder fallback, `nginx.conf.template`: `/` → `/ui/`, SPA with
  immutable assets, unbuffered proxy of the dynamic paths with no body limit, SSE location, internal `/_blobs/`).
  `compose.yaml` (hardening anchor, healthchecks, `service_healthy` ordering, frontend/backend networks, only web
  publishes a port), `compose.dev.yaml` (hot reload), `make dev-up/dev-down/dev-clean/dev-logs/dev-ps/images`.
  Minimal `waystation_server.app:create_app()` and worker main with a heartbeat-file healthcheck; `init` prepares
  the data dir (ownership/modes, then drops root) and initiates the replica set. Third-party loggers (pymongo,
  httpx...) are clamped to WARNING because their debug output includes commands and URLs.
  Doc fix: ARCHITECTURE §4.5 no longer says `default_type` comes from `$upstream_http_x_ws_content_type` (nginx
  cannot do that); the server's own `Content-Type` survives the internal redirect, verified with a real probe.
- T-F04: `waystation_common` core: settings (pydantic-settings, `WAYSTATION_` prefix, `<NAME>_FILE` for every secret
  including the Mongo/Valkey URIs, `make env-example` regenerates `.env.example` and a test fails when it is stale),
  JSON logging with contextvars request context and secret redaction, ULIDs, error types, `paths.normalise`
  (hypothesis property tests), AES-256-GCM keyring (first key encrypts, all decrypt, `rotate`). `make test` now
  excludes `integration` tests and enforces 100% branch coverage on `paths` and `crypto`.
- T-U05 (min): browse renderers (docker images → tags → manifest with platforms and layers, npm packages →
  versions, apt suites → packages, raw folder listing), Browse page, and the M0 screenshot tour
  (`scripts/screenshots.mjs`, PNGs in `Documents/Notes/screenshots/m0/`).
- T-U04 (min): repositories. List with URL-backed filters, create flow (format tiles → type → form with upstream
  presets and "Test upstream"), problem+json field errors on the form, detail page with KPI strip and Overview /
  Browse / Settings tabs, delete with typed confirmation, Set me up side panel (docker, containerd, podman, npm,
  pnpm, yarn, bun, apt, curl, wget). Secrets stay write-only in the form.
- T-U03 (min): dashboard. KPI tiles, the live transfers panel (also in the top-bar live drawer and per repository),
  traffic chart kept current from stats ticks without polling, storage-by-format donut, upstream health with the
  Docker Hub pull gauge and auto-block countdown, recent activity.
- T-U01 (min): design tokens (brand teal `#0f766e` family, status and cache colours, chart slots validated for
  colour-blind separation in both themes), format icon sprite (`src/assets/formats.svg`, regenerated by
  `scripts/gen-format-sprite.mjs`), base components (`PageCard`, `PageHeader`, `FormDialog`, `StatusPill`,
  `CacheBadge`, `RepoChip`, `FormatIcon`, `CopyField`, `EmptyState`, `TimeCell`, `SizeCell`), SVG charts
  (`Sparkline`, `StackedAreaChart`, `DonutChart`), formatters (binary sizes, SI rates, relative times).
- T-F10: web shell. Themes (light/dark/system; first paint from `public/theme-init.js`, no inline script so the CSP
  needs no `unsafe-inline`), bundled fonts and SVG icons, rail + top bar + live indicator and live drawer, auth guard
  (no open redirects), sign-in page, `stores/live.ts` (single event stream, topics, reconnect, 4 Hz flush, 5 s / 30 s
  row retention), i18n `t()` with an English catalogue, `make frontend-build` + bundle budget script.
- T-V01: contract-first M0 API. OpenAPI 3.1 at `Application/Web/openapi/waystation-m0.yaml` (auth/session with
  double-submit CSRF, repositories, test-upstream, Set me up, stats, live transfers, activity, browse, SSE event
  schemas, problem+json errors); generated types + `openapi-fetch` client in `src/api/`; MSW handlers with seeded
  fixtures and a live-event generator (`src/mocks/`); Make targets `frontend-install`, `frontend-mock`,
  `frontend-lint`, `frontend-test`, `gen-api`, `gen-api-check`.
- T-F02: dev tooling. Dev image (`Build/Containers/dev/`: Python 3.14, uv 0.13, Node 26 LTS); Makefile with every
  planned target (later ones say which task delivers them); uv workspace with the three packages and the server/worker
  subpackage skeleton; ruff (single quotes, 120 columns), mypy strict, pytest with coverage; six import-linter
  contracts for ARCHITECTURE §3, each confirmed to fail on a planted violation; git pre-commit hook (`make hooks`).
- T-F01: repository bootstrap: git repo, GPL-3.0-or-later LICENSE (D1 decided), VERSION 0.1.0, ignore files,
  `.env.example` stub, directory skeleton (SPEC §13.2). ADRs 0001-0003 accepted. Fixed encoding damage in
  SPECIFICATION (stray BOM, mis-encoded `§`, `→`, `×`, `≥`).
- Plan drafted: SPECIFICATION, ARCHITECTURE, DATA_MODEL, FORMATS, UI_DESIGN, TASKS, ADRs 0001-0003.
- Fast-track milestone M0 "feedback build" (contract-first API with mocks, parallel lanes; Docker Hub, npm, apt,
  raw proxies) and speed/parallelism rules in CLAUDE.md.
- Review fixes: apt index/pool validation model, X-Accel header/permission rules, coalescing on network
  filesystems, live vs activity event channels, closed-minute rollups with latency histograms, job outbox and
  dirty-flag dedupe, Valkey noeviction + AOF, client auth per format, task dependency fixes, new ops tasks.
