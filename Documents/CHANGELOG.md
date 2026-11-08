# Changelog

All notable changes, newest first. Each entry: date, task ID, summary.

## 2026-11-08
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
