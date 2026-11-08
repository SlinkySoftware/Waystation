# Changelog

All notable changes, newest first. Each entry: date, task ID, summary.

## 2026-11-08
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
