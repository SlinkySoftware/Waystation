# ADR 0001: Python async server with an nginx data plane; Vue/Quasar UI

Status: accepted (2026-11-08, approved by the owner).

## Context
Waystation must be fast for large binary transfers and highly concurrent metadata requests, and must be
maintainable by the same people and agents who maintain the owner's other projects (Python backends, Vue 3 /
Quasar frontends: DeviceVault, fw-data-manager, bgpmon). Pure-Python file streaming costs CPU per byte; a Go/Rust
data plane would split the codebase across languages.

## Decision
- Server: Python 3.14, FastAPI (pydantic v2), uvicorn + uvloop + httptools, fully async; httpx for upstreams.
- **Cache hits never stream through Python.** The server authorises and resolves, then answers with
  `X-Accel-Redirect` to an internal nginx location that maps onto the read-only blob store. nginx does sendfile,
  range requests and keep-alive at native speed.
- Cache misses stream through Python once (tee to client and to disk), which is bounded by upstream speed.
- UI: Quasar 2 / Vue 3 / TypeScript strict, Pinia, openapi-fetch with generated types; in-house SVG charts.

## Consequences
- The web container must mount the blob store read-only and share the path layout with the server.
- Throughput for hits matches nginx; Python CPU is spent on auth, routing and metadata only.
- If perf budgets for metadata paths cannot be met (SPEC §5.1), revisit with a profiled hot path in a compiled
  extension or a Go sidecar, via a new ADR. Not expected.
