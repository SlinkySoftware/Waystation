# Waystation tasks

Rules: pick a task whose dependencies are all DONE, set it IN PROGRESS, do only that task, and finish it with
tests, docs, CHANGELOG and SPECIFICATION §14 updated in the same change (CLAUDE.md). Statuses: TODO,
IN PROGRESS, DONE, BLOCKED (say why). One branch and PR per task; commit messages start with the task ID.
Acceptance criteria are the minimum; the Definition of Done for formats is in FORMATS.md.

Milestones: **M0** feedback build (fast track below, first priority) · **M1** platform complete (Phases 0-1) ·
**M2** Nexus parity + cutover (Phases 2-4 + T-O01..O03 + T-M01..M03) · **M3** broad formats (Phase 5) ·
**M4** v1.0 (Phase 6 + release tasks).

## Fast track: M0 "feedback build" (do this first, in parallel lanes)
Goal: as early as possible, a stack the owner can run, sign in to and click through: the dashboard shows real live
transfers and stats, repositories can be created and browsed with "Set me up" snippets, and real clients pull
through Docker Hub, npm, apt and raw proxies. Then collect UI feedback while M1/M2 continue.

`(min)` = smallest end-to-end version of that task: deliver it, leave the full task TODO, note what's left in
the task row. A `(min)` slice only needs the `(min)` slices of the tasks before it in its lane (e.g. the M0 apt
proxy runs without coalescing, T-S08). Lanes run concurrently (subagents / separate worktrees); arrows are the
only ordering.

| Lane | Sequence |
|---|---|
| Platform | T-F01 → T-F02 → T-F04 (min: settings, logging, paths) → T-F05 (min: dev compose with web, server, worker, mongodb, valkey) → T-F06 (min) and T-F07 (min) → T-F09 (min: events + live transfers + per-minute counters) |
| Contract + UI | T-V01 (after T-F02) → T-F10 → T-U01 (min) → T-U03 (min: KPI tiles, live transfers, traffic chart) and T-U04 (min: list, create docker/npm/apt/raw proxy, detail, Set me up) → T-U05 (min: docker tags, npm versions, apt suites/packages, raw folders); all against mocks first, real API as it lands |
| Server | T-S01 → T-S02 (min: bootstrap admin + local login) → T-S03 (min: decision point + admin/anonymous roles only, route-enumeration test) → T-S05 (min: proxy repos for docker, npm, apt, raw) → T-S06 → T-S07 (min: fetch + tee + freshness + validation; no auto-block/stale) → T-S11 (min: live transfers, counters, SSE) → T-S12 (min: anonymous pull tokens) |
| Formats | T-P2-HARNESS (min: docker, npm, apt, raw clients against live upstreams) → T-P2-RAW (min) → T-P2-DOCKERP (min: Docker Hub + mirror mode), T-P2-NPM (min: packument + tarballs) and T-P2-APT (min: Debian + Ubuntu suites incl. security, `InRelease`/index consistency, by-hash, pool validation) in parallel |
| Release | T-V02 once all lanes reach M0 |

| ID | Task | Depends | Status | Acceptance |
|---|---|---|---|---|
| T-V01 | Contract-first M0 API: OpenAPI for auth/session, repositories, stats, live transfers, events (SSE schema), browse; generated TS client; MSW handlers + seeded fixtures + a fake live-event generator (bursty realistic transfers across formats); `npm run dev:mock` | F02 | DONE (UI side; the server-lane contract test against `Application/Web/openapi/waystation-m0.yaml` belongs to T-S01/T-S11) | UI runs fully on mocks with lively, realistic data; server lane implements the same schema (contract test) |
| T-V02 | M0 feedback build: tag `v0.1.0-m0`, images built, `compose.yaml` runnable on a fresh host with a short "try it" guide; screenshot tour (both themes, desktop + mobile) for review; feedback log `Documents/Notes/UI_FEEDBACK.md` | all M0 lane items | TODO | the owner can deploy it, sign in, pull `nginx`, install an npm package and run `apt update && apt install` in a Debian container through it, and watch all three on the dashboard |

## Phase 0: Foundation
| ID | Task | Depends | Status | Acceptance |
|---|---|---|---|---|
| T-F01 | Repository bootstrap: git init with both remotes (SPEC §13.1), this plan, LICENSE (D1), VERSION `0.1.0`, `.gitignore`, `.dockerignore`, `.env.example` stub, directory skeleton (SPEC §13.2) | – | DONE (local; first push pending: both remote repos must be created) | Both remotes have `main`; tree matches §13.2; no environment details in the repo (manual grep) |
| T-F02 | Dev tooling: dev image (Python 3.14 + uv + Node LTS), Makefile (`help, dev-image, dev-up, dev-down, lock, fmt, lint, typecheck, test, check, frontend-*, gen-api, e2e, clients-test, perf, images, env-example, set-version, backup`; targets whose tooling arrives later print "not yet implemented (T-xxx)"), uv workspace with `waystation_common`, `waystation_server`, `waystation_worker`; ruff, mypy strict, import-linter contracts (ARCH §3), pre-commit | F01 | DONE | `make lint typecheck test` green on empty packages; contracts fail on a deliberate violation |
| T-F03 | CI: GitHub `ci.yml` (`make check`), `release.yml` (tags → multi-arch images to GHCR, SBOM, provenance); Gitea workflow building images to the Gitea registry with org secrets; `Build/ci/forbidden_strings.sh` (fails on hostnames/IPs not in `Build/ci/allowed_hosts.txt` (example domains, RFC 5737/3849 ranges, the two source-control URLs, public upstream registries such as docker.io, ghcr.io, registry.npmjs.org, deb.debian.org...) and on private keys); `budgets.json` skeleton | F02 | TODO | PR checks run on GitHub; push builds on Gitea; forbidden-strings check passes on the plan and blocks a planted internal hostname |
| T-F04 | `waystation_common` core: settings (pydantic-settings, `*_FILE`, generated `.env.example`), JSON logging with request context, ULIDs, error types, `paths.normalise` (hypothesis tests: traversal, encoding, unicode), AES-GCM multi-key crypto with rotation helper | F02 | TODO | 100% branch coverage on `paths` and `crypto`; `make env-example` idempotent |
| T-F05 | Containers and compose: Dockerfiles (server, worker, web: nginx unprivileged + SPA build stage + `nginx.conf.template` with internal `/_blobs/`), `compose.yaml` (web, server, worker, mongodb rs, valkey, init), `compose.dev.yaml` (bind-mounted source, hot reload), standalone files, hardening anchor, healthchecks, unique aliases, configurable subnet (SPEC §8.3) | F04 | TODO | `make dev-up` brings the stack healthy from clean; containers non-root, `cap_drop: [ALL]`; only web publishes a port |
| T-F06 | MongoDB layer: async client factory, collection registry, migration framework, `init` job (replica set initiate if bundled, indexes, data-dir ownership/modes per ARCH §4) | F05 | TODO | Fresh stack initialises; re-run is a no-op; index set matches DATA_MODEL |
| T-F07 | BlobStore (local FS) per ARCH §5: streaming writer with incremental sha256 and size limit, atomic commit with fsync, stat/exists, `path_for_xaccel`, delete (GC only), orphan upload sweeper, free-space guard | F04 | TODO | Kill-mid-write tests leave no partial blob; concurrent identical writes commit once; works on an NFS-mounted dir in CI (nfs container) |
| T-F08 | Job system on Valkey Streams (ARCH §10): typed jobs, enqueue with dirty-flag dedupe, MongoDB outbox + dispatcher for event-driven jobs, consumer groups, reclaim of stuck jobs, max attempts → dead letter, scheduler leader lease with cron schedules, `job_runs` progress/log recording, worker main + health | F06 | TODO | Chaos tests: kill worker mid-job → re-run once; flush Valkey → outbox jobs re-dispatched; upload during a running rebuild → exactly one rerun; schedule fires once with 3 workers |
| T-F09 | Events and stats plumbing (ARCH §8): event schema, non-blocking emitter, live channel (`transfers` hash + pub/sub) and `activity` stream, per-minute counters + latency histograms, closed-minute rollups into time-series collections, hour/day rollups, `storage_snapshots`, batched last-seen flush, retention | F08 | TODO | 10k progress events/s without measurable request latency impact; rollups exact vs synthetic load; no duplicate minute documents |
| T-F10 | Web shell: Quasar app with tokens/themes (DeviceVault base), fonts and icon sprite bundled, layout (rail, top bar, live indicator placeholder), router + auth guard, sign-in page (local), `openapi-fetch` client generation (`make gen-api`, CI staleness check), shared SSE `live` store; runs on MSW mocks (T-V01) before the server exists | V01 | DONE (bundle budget wired in `make frontend-build`: 158 kB gzip initial route; Lighthouse run deferred to the T-U09 audit) | Lighthouse ≥ 95 perf/a11y on sign-in; bundle budget check wired |

## Phase 1: Core server
| ID | Task | Depends | Status | Acceptance |
|---|---|---|---|---|
| T-S01 | Server app factory: routing framework for `/repository/<repo>/`, `/v2/`, `/api/v1/`; request context; native vs problem+json errors; `/healthz`, `/readyz`, `/metrics` | F04, F06 | TODO | Unknown repo → native 404; metrics exposed |
| T-S02 | Local authentication: users, argon2id, bootstrap admin (from `*_FILE` or a printed one-time password, via `init`), sessions (cookie flags, idle/absolute timeout, list/revoke own sessions API), CSRF, login rate limit + lockout, `/api/v1/auth/*`, profile endpoints | S01 | TODO | Auth tests incl. fixation, CSRF, lockout; bootstrap is idempotent |
| T-S03 | RBAC engine (SPEC §4.10, ARCH §7): roles, groups, permission scopes/globs, built-in roles, anonymous principal, grant cache + `grant_version` invalidation (change streams), route action decorator, **route enumeration test**, filtered queries | S02 | TODO | Matrix test: every route × {anon, reader, deployer, admin} gives expected codes; hidden repos 404 |
| T-S04 | API tokens and service accounts (hashing, scopes, repo globs, expiry, last used, revoke); basic + bearer auth for format clients | S03 | TODO | Token shown once; revoked token refused within 1 s across replicas |
| T-S05 | Repositories: model, REST CRUD, format `config_schema` registry, validation (names, reserved words, aliases), enable/disable, clone, test-upstream endpoint | S03 | TODO | OpenAPI documents every format's options; invalid configs rejected with field errors |
| T-S06 | Data plane: X-Accel response builder (headers, ETag, Content-Type, digest headers), nginx internal location, ranges, conditional GET, HEAD; payloads never stream through Python | F07, S01, F05 | TODO | 10 GiB file served at line rate in perf test; server CPU flat during hits |
| T-S07 | Proxy engine (SPEC §4.1, ARCH §6): pooled HTTP/2 upstream client, SSRF guard, CA bundles, HTTP proxy, credential plug-ins (basic, bearer), freshness/revalidation, negative cache, stale-on-error, offline mode, auto-block with backoff, digest/checksum validation hooks, tee streaming to client + BlobWriter | S05, S06, F09 | TODO | Fault-injection suite (timeouts, resets, 5xx, slow upstream, checksum mismatch) behaves per spec |
| T-S08 | Coalescing (ARCH §6.3): Valkey single-flight with lease renewal, tailing followers across replicas, owner failure takeover | S07 | TODO | 200 concurrent requests for one uncached 1 GiB blob across 2 replicas → exactly 1 upstream fetch, all clients get identical bytes |
| T-S09 | Content indexing: assets/components/blobs records, `version_sort` contract, download counters (batched), digest "where used" query | S07, F09 | TODO | Index-coverage test (explain) passes for all registered queries |
| T-S10 | Audit service + `/api/v1/audit` (filters, export) | S03 | TODO | Every SPEC §4.11 action recorded (test enumerates) |
| T-S11 | Stats and live APIs: ranges/aggregations per repo/format, top-N, storage summary, live transfers, SSE topics with RBAC filtering, `Last-Event-ID` resume, 4 Hz progress coalescing | F09, S03 | TODO | UI-independent contract tests; SSE survives replica restart with resume |
| T-S12 | Docker token service (`/auth/token`, EdDSA JWT, Docker scope grammar, anonymous tokens) + 401 challenge plumbing | S04 | TODO | `docker login` works with password and API token (manual + client test) |

## Phase 2: Nexus-parity formats
| ID | Task | Depends | Status | Acceptance |
|---|---|---|---|---|
| T-P2-HARNESS | Real-client test harness: client containers per format, record/replay fake upstream (record mode against live upstreams, fixtures committed, scrubbed), nightly live smoke workflow | F05 | TODO | A stub "echo" format and `curl` pass through the harness in record and replay modes; fixtures pass the forbidden-strings check |
| T-P2-RAW | Raw proxy + hosted + group | S08, S09, P2-HARNESS | TODO | FORMATS DoD |
| T-P2-DOCKERP | Docker proxy: Docker Hub (token, `library/`, rate-limit tracking, HEAD), generic OCI (GHCR, Quay), upstream aliases, default docker repo, mirror mode, containerd `hosts.toml` | S08, S12, P2-HARNESS | TODO | docker/podman/containerd/skopeo/crane pull via alias, prefix and mirror mode; Hub limit headers surfaced |
| T-P2-DOCKERH | Docker hosted: chunked/monolithic uploads, cross-repo mount, manifest validation, tags, immutability, delete, referrers API, catalog | P2-DOCKERP | TODO | OCI conformance suite passes; buildx multi-arch push; cosign sign/verify; oras push/pull |
| T-P2-DOCKERG | Docker group + writable member; merged tag lists | P2-DOCKERH | TODO | push to group lands in member; pull resolves in order |
| T-P2-NPM | npm proxy (packument + abbreviated, tarball rewrite, integrity, search/audit pass-through, scoped packages) + group | S08, P2-HARNESS | TODO | npm/pnpm/yarn1/berry/bun install lockfile-pinned projects; audit works |
| T-P2-APT | apt proxy (Release/InRelease consistency, by-hash, hash validation, multi-suite) | S08, P2-HARNESS | TODO | apt update/install on Debian 13, Ubuntu 26.04, Raspberry Pi OS arm64 containers |
| T-P2-YUM | yum/dnf proxy (repomd consistency, checksum validation) | S08, P2-HARNESS | TODO | dnf makecache/install on Rocky 10, Alma 10, Oracle 9, EPEL |
| T-P2-PERF | k6 perf suite + budget enforcement (SPEC §5.1) in CI (smaller budgets profile) and nightly (full) | S06, S08, P2-RAW, P2-DOCKERP | TODO | Budgets enforced; report artifact per run |

## Phase 3: User interface
| ID | Task | Depends | Status | Acceptance |
|---|---|---|---|---|
| T-U01 | Design system: tokens (brand, status, cache colours), light/dark/system, typography, density, format icon sprite, base components (`PageCard`, `FormDialog`, `StatusPill`, `RepoChip`, `FormatIcon`, `CopyField`, `EmptyState`, `TimeCell`, `SizeCell`) | F10 | TODO (M0 min delivered 2026-11-08: tokens in `src/css/app.css` (brand teal, status, cache, validated chart slots), themes, Inter/JetBrains Mono, format sprite `src/assets/formats.svg` (24 marks, Simple Icons CC0), all listed base components plus `PageHeader`, `CacheBadge`, SVG charts (sparkline, stacked area with crosshair + hidden data table, donut). Left: density toggle, showcase route, gauge/bar-list charts) | Storybook-style showcase route (dev only) with every component in both themes |
| T-U02 | Brand mark, favicon set, empty-state illustrations (UI_DESIGN §8) | U01 | TODO | SVG master + PNG sizes; legible at 16 px |
| T-U03 | Dashboard (UI_DESIGN §4.2): KPI tiles + sparklines, live transfers, traffic chart with range picker, format donut, upstream health (incl. Docker Hub gauge), top/recent, activity feed; SSE-driven; in-house SVG charts | S11, U01 | TODO (M0 min delivered 2026-11-08 on mocks: KPI tiles with sparklines and live values, live transfers panel (animated rows, cache badges incl. coalesced follower count, progress, speed, ETA, upstream latency, pause, format/repo filters, 5 s / 30 s retention), traffic chart (range picker, cache or format split, bytes or requests, crosshair, live-updated from stats ticks), storage-by-format donut, upstream health incl. Docker Hub gauge and auto-block countdown, recent activity. Left: top artifacts / recently added, real-server wiring, 200-transfer 60 fps check) | Updates < 1 s from event; 60 fps with 200 live transfers; mobile layout |
| T-U04 | Repositories: list/grid, filters, create flow (format tiles → type → form), test upstream, detail page tabs, **Set me up** panel framework (each format task adds its own snippets) | S05, U01 | TODO (M0 min delivered 2026-11-08 on mocks: list with search + format/type/status filters in the URL (cards on phones), create flow format tiles → type → sectioned form with presets and test upstream, field-level errors from problem+json, detail page header + KPI strip + Overview/Browse/Settings tabs, settings edit + delete, Set me up side panel for docker (daemon.json mirror, login, prefix/alias/default pulls, containerd hosts.toml, podman), npm (.npmrc, npm login, pnpm, yarn, bun), apt (deb822, one-line, auth.conf), raw (curl, wget). Left: card-grid toggle, Packages/Activity/Cleanup/Members tabs, hosted/group types, OS variants) | Snippets render for every format implemented so far; forms show field-level errors from the API |
| T-U05 | Browse: virtual tree + detail + renderer framework; generic tree renderer works for any format; format-specific renderers (docker tags/manifest/layers/referrers, npm, apt, yum, raw) delivered with or after their format tasks | S09, U04 | TODO (M0 min delivered 2026-11-08 on mocks: renderer framework (`components/browse/RepoBrowser.vue`) with state in the URL, docker images → tags (digest, platform badges, size, created, last pulled, pulls) → manifest (platform switcher, layers with stacked size bar and history, pull by tag/digest), npm packages → package (install snippet, dist-tags, versions with cached/deprecated flags), apt suites → packages (arch, cached-only, unverified flag), raw/generic folder listing with download URL copy. Left: two-pane virtual tree, referrers, README rendering, yum, 100k-entry performance) | 100k-entry repo browses smoothly; renderers exist for every format implemented so far |
| T-U06 | Search page with facets + command palette (`Ctrl/⌘ K`) | S09, U01 | TODO | p95 < 150 ms on 1M assets fixture |
| T-U07 | Activity page (live tail, filters, CSV export) and Storage page | S11, U01 | TODO | Tail follows/pauses correctly |
| T-U08 | Administration: users, groups, roles matrix, API tokens, audit log, settings, system | S03, S04, S10, U01 | TODO | All admin APIs reachable from UI; RBAC hides items without permission |
| T-U09 | Profile; responsive and accessibility pass (WCAG 2.2 AA audit); Playwright E2E + visual regression baseline (2 themes × 3 widths) | U03-U08 | TODO | axe: zero serious issues; E2E green |

## Phase 4: External authentication
| ID | Task | Depends | Status | Acceptance |
|---|---|---|---|---|
| T-A01 | LDAP provider: multiple servers with failover, LDAPS/StartTLS, custom CA, service bind, search base/filter, attribute mapping, group discovery (memberOf / group search, optional nested), require-group gate, test-connection/test-user endpoints | S03 | TODO | Tests against a 389-DS/FreeIPA-schema container and a Samba AD container |
| T-A02 | OIDC provider: discovery, code + PKCE, scopes, claim mapping, group claim → groups, auto-provision toggle, RP-initiated logout, multiple providers | S03 | TODO | Tests against Keycloak and authentik containers |
| T-A03 | Identity linking + group sync on login; provider admin forms (with U08) | A01, A02, U08 | TODO | Same-username rules per SPEC §4.9 enforced |
| T-A04 | Client-auth matrix for Phase 2 formats: docker login (LDAP password, OIDC-user API token), npm login/token, apt `auth.conf`, dnf basic, raw basic (later formats cover auth in their own DoD) | A01, S12, P2-* | TODO | Matrix green in client harness |

## Cutover readiness (M2)
| ID | Task | Depends | Status | Acceptance |
|---|---|---|---|---|
| T-O01 | Cleanup policies: criteria, schedules, dry-run preview API, execution job, UI (UI_DESIGN §4.8) | S09, F08, U04 | TODO | Preview equals execution result on fixtures |
| T-O02 | Blob GC (mark-and-sweep with grace), orphan uploads, docker untagged cleanup; verify job (missing blobs → re-fetch marks) | O01, F07 | TODO | GC never removes a blob referenced or < grace old (property test) |
| T-O03 | Quotas per repo + global low-disk guard + alerts (event + metric) | F07, S05 | TODO | Hard quota blocks writes, hits still served |
| T-M01 | Nexus import: read repos via Nexus REST API, create equivalents (names, formats, types, upstreams, docker aliases/default), dry-run report | S05, P2-* | TODO | Imports a Nexus 3 test instance config fully |
| T-M02 | `Operations/MIGRATING_FROM_NEXUS.md` + side-by-side validation script (same request against both, compare status/digest) | M01 | TODO | Script report used for cutover |
| T-M03 | `Operations/DEPLOYMENT.md`, `REVERSE_PROXIES.md` (nginx, Traefik, Caddy, HAProxy, Synology DSM settings for large transfers), `BACKUP.md` | F05 | TODO | Fresh-host deployment from docs alone |

## Phase 5: Broad formats (one task each)
Acceptance for every row: the FORMATS.md Definition of Done, including client authentication, Set me up snippets
and the browse renderer for that format. Order = priority (PyPI and Maven first, D3).

| ID | Task | Depends | Status |
|---|---|---|---|
| T-P5-PYPI | PyPI proxy + hosted + group (PEP 503/691/658/700, twine/uv publish, yank) | S08, P2-HARNESS, U05 | TODO |
| T-P5-MAVEN | Maven proxy + hosted (release/snapshot, metadata regeneration) + group | S08, P2-HARNESS, U05 | TODO |
| T-P5-NPMH | npm hosted (publish, unpublish, deprecate, dist-tags) | P2-NPM, S04 | TODO |
| T-P5-HELM | Helm classic proxy + hosted + group (index rewrite/regeneration) | S08, F08, P2-HARNESS, U05 | TODO |
| T-P5-GO | Go module proxy + sumdb proxy + group | S08, P2-HARNESS, U05 | TODO |
| T-P5-APTH | apt hosted with signing (ADR for OpenPGP implementation), atomic index publish | P2-APT, F08 | TODO |
| T-P5-YUMH | yum hosted with repomd signing, atomic publish | P2-YUM, F08 | TODO |
| T-P5-TFP | Terraform/OpenTofu provider network mirror (proxy) with SHA256SUMS + signature verification | S08, P2-HARNESS, U05 | TODO |
| T-P5-TFM | Terraform modules proxy + the single hosted Terraform repo (modules/providers) + `/.well-known/terraform.json` | P5-TFP | TODO |
| T-P5-GALAXY | Ansible Galaxy v3 proxy + hosted collections (incl. import polling endpoint) + group; roles read-only | S08, F08, P2-HARNESS, U05 | TODO |
| T-P5-SWIFT | Swift Package Registry: hosted + SCM-backed proxy (deterministic archives, pinned checksums, `/identifiers`) | S08, P2-HARNESS, U05 | TODO |

## Phase 6: Operations, extra formats, v1.0
| ID | Task | Depends | Status |
|---|---|---|---|
| T-O04 | Tasks & workers UI (UI_DESIGN §4.9), dead-letter retry | F08, U01 | TODO |
| T-O05 | Webhooks (signed) for push/publish/delete | S09 | TODO |
| T-O06 | Monitoring: Zabbix template + Grafana dashboard in `Build/monitoring/` | S11 | TODO |
| T-O07 | Backup/restore tooling (`make backup`, hosted-only mode) + restore verification job | O02 | TODO |
| T-O08 | TOTP 2FA for local users | S02 | TODO |
| T-O09 | Declarative provisioning file (repos, roles, providers, policies) applied idempotently | S05, S03 | TODO |
| T-M04 | Nexus hosted-content migration job | M01, F08 | TODO |
| T-O10 | Support bundle (redacted config, versions, health, recent errors) from System page | S11, U08 | TODO |
| T-O11 | Secret-key rotation job (re-encrypt stored credentials with the first key in `SECRET_KEYS`) | F08, F04 | TODO |
| T-O12 | Prefetch jobs (refresh popular metadata, warm configured images/packages) | F08, S07 | TODO |
| T-P6-DOCKERC | Docker dedicated port connectors on the web container (SPEC §4.4) | P2-DOCKERP | TODO |
| T-P6-NUGET / -CARGO / -GEMS / -COMPOSER / -CONDA / -APK / -HF | One task per format (FORMATS.md) | S08 | TODO |
| T-R01 | Security review: ASVS L2 checklist, auth bypass matrix, SSRF/path/archive corpora, dependency audit, container scan | all M2 tasks | TODO |
| T-R02 | Documentation pass: user guide per format, admin guide, API reference publish | all M3 tasks | TODO |
| T-R03 | v1.0.0 release: multi-arch images, SBOM, provenance, release notes | R01, R02 | TODO |
