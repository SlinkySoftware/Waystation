# Changelog

All notable changes, newest first. Each entry: date, task ID, summary.

## 2026-11-08
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
