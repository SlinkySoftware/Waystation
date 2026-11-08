# Changelog

All notable changes, newest first. Each entry: date, task ID, summary.

## 2026-11-08
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
