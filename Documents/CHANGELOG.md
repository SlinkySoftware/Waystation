# Changelog

All notable changes, newest first. Each entry: date, task ID, summary.

## 2026-11-08
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
