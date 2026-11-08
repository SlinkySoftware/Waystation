# Changelog

All notable changes, newest first. Each entry: date, task ID, summary.

## 2026-11-08
- T-F01: repository bootstrap: git repo, GPL-3.0-or-later LICENSE (D1 decided), VERSION 0.1.0, ignore files,
  `.env.example` stub, directory skeleton (SPEC §13.2). ADRs 0001-0003 accepted. Fixed encoding damage in
  SPECIFICATION (stray BOM, mis-encoded `§`, `→`, `×`, `≥`).
- Plan drafted: SPECIFICATION, ARCHITECTURE, DATA_MODEL, FORMATS, UI_DESIGN, TASKS, ADRs 0001-0003.
- Fast-track milestone M0 "feedback build" (contract-first API with mocks, parallel lanes; Docker Hub, npm, apt,
  raw proxies) and speed/parallelism rules in CLAUDE.md.
- Review fixes: apt index/pool validation model, X-Accel header/permission rules, coalescing on network
  filesystems, live vs activity event channels, closed-minute rollups with latency histograms, job outbox and
  dirty-flag dedupe, Valkey noeviction + AOF, client auth per format, task dependency fixes, new ops tasks.
