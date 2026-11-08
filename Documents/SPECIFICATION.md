# Waystation specification

Living document: it describes what Waystation does **now** plus the agreed target. §14 tracks build status.
Architecture rules are in `Architecture/ARCHITECTURE.md`, per-format protocol detail in `Architecture/FORMATS.md`,
the UI in `UI_DESIGN.md`, the work breakdown in `TASKS.md`.

## 1. Purpose

Waystation is a self-hosted artifact repository for a home lab or small organisation:

1. **Cache**: a pull-through proxy for public package ecosystems, so every machine pulls packages and images
   once over the internet and then at LAN speed, keeps working through upstream outages and rate limits, and is
   shielded from upstream disappearance of versions.
2. **Registry**: a private registry for the owner's own container images and packages (Artifactory-style), used
   by CI runners and deployments.
3. **Window**: a fast, live, professional web UI that shows what is being fetched right now, what is cached,
   how much bandwidth it saves and how healthy each upstream is.

It replaces Sonatype Nexus Repository CE (40k-component / 100k-requests-per-day caps, mandatory telemetry) and
should be a credible open-source alternative to Nexus and Artifactory for small deployments.

## 2. Goals and non-goals

Goals:
- Feature parity with the owner's existing Nexus setup on day one of cutover (Phase 2, §4.5: the formats configured
  today are Docker/OCI, apt, yum and npm, plus raw), then PyPI and Maven first, then broad format coverage (Phase 5).
  PyPI and Maven do not gate cutover (D3, decided).
- Drop-in URLs: Nexus-compatible path layout `/repository/<repo-name>/...`, so clients switch with no changes.
- Fast: cache hits served by nginx `sendfile` from disk; the API and UI feel instant (§5.1 budgets).
- Clean separation: web / server / workers / database / cache as separate containers; workers scale
  independently; the server is stateless.
- One storage path (`WAYSTATION_DATA_DIR`) holds every artifact; it can live on local disk or a NAS share.
- Local, LDAP and OIDC sign-in; RBAC down to repository and action; API tokens for CI and clients.
- Zero telemetry, no usage caps, no phone-home.

Non-goals (v1): clustering across sites/replication, vulnerability scanning (planned post-v1, §15), S3 storage
(storage driver interface allows it later), build tooling, a CLI beyond what format clients already provide,
Windows containers support for the server.

## 3. Concepts

| Term | Meaning |
|---|---|
| **Format** | A package ecosystem protocol (docker, npm, pypi, maven, apt, yum, go, helm, terraform, galaxy, swift, nuget, cargo, rubygems, composer, conda, raw). Implemented as a server plugin. |
| **Repository** | A named, configured endpoint of one format and one type. Name: `^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$`, stored as given, unique case-insensitively (so Nexus names import unchanged); URL matching is case-sensitive like Nexus. |
| **Type** | `proxy` (caches one upstream), `hosted` (stores uploads), `group` (ordered union of members of the same format; reads try members in order; writes go to the configured member). |
| **Component** | A logical package version (e.g. `nginx:1.27`, `lodash@4.17.21`, `requests 2.32.3`, `curl 8.5.0-2`). Format-specific coordinates. |
| **Asset** | A file within a repository at a path (manifest, tarball, wheel, `.deb`, `Packages.xz`, `index.json`). Belongs to zero or one component (metadata assets have none). |
| **Blob** | Immutable content addressed by sha256, stored once in the blob store and shared by every asset with that content in any repository (global de-duplication). |
| **Metadata asset** | Mutable index files (`Packages`, `repomd.xml`, npm packuments, PyPI simple pages, `maven-metadata.xml`, docker tag manifests). Revalidated after `metadata_max_age`. |
| **Content asset** | Immutable files (layers, tarballs, wheels, rpms). Cached until cleanup removes them. |
| **Upstream** | The remote URL a proxy repository fetches from, with optional credentials. |
| **Principal** | A user (local / LDAP / OIDC), a service account, an API token, or `anonymous`. |

## 4. Functional requirements

### 4.1 Proxy repositories
- Fetch on first request, stream to the client **while** writing to the blob store (no "download then serve").
- **Request coalescing**: concurrent requests for the same uncached asset cause one upstream fetch; followers
  stream from the partially written file as it grows (ARCHITECTURE §6.3).
- **Freshness**: `metadata_max_age` (default 30 min, per repo) and `content_max_age` (default: forever for
  immutable assets). Revalidate with `If-None-Match` / `If-Modified-Since` when the upstream supports it.
- **Negative cache** of 404s (default 10 min, per repo).
- **Serve stale on error**: if the upstream errors or times out and a cached copy exists, serve it and flag the
  repo "degraded" (default on). **Offline mode** per repo: never contact the upstream.
- **Auto-block**: after N consecutive upstream failures, stop calling it for an exponential backoff period
  (visible in the UI); probes resume automatically.
- Upstream credentials per repo (basic, bearer, Docker Hub token login, GHCR PAT), stored encrypted.
- Upstream TLS: verify by default; per-repo custom CA bundle; optional HTTP proxy per repo or global.
- Respect and surface upstream rate limits (Docker Hub `ratelimit-remaining`, `Retry-After`); HEAD where the
  protocol allows to avoid consuming quota.
- Content validation: verify digests/checksums the protocol provides (OCI digests, npm `integrity`, PyPI
  `#sha256=`, Maven `.sha1/.sha256`, apt `Packages` hashes, yum `primary.xml` checksums, Go sumdb) before an asset
  is committed to the cache. Mismatch = not cached, client gets 502, event logged.
- Rewrite URLs inside metadata so clients fetch content through Waystation (npm `dist.tarball`, PyPI file links,
  Helm `index.yaml` URLs, Terraform download URLs).
- SSRF guard: upstream URLs and redirects may not resolve to loopback/link-local/private ranges unless the repo
  sets `allow_private_upstream: true`.

### 4.2 Hosted repositories
- Upload via each format's native client (docker push, npm publish, twine upload, mvn deploy, helm push,
  curl PUT for raw/apt/yum, swift package-registry publish, ...).
- **Write policy** per repo: `allow_redeploy` / `disable_redeploy` (default for release repos) / `read_only`.
  SNAPSHOT-style repos (Maven) allow redeploy for snapshot versions only.
- Docker: tag immutability rules (regex) per repo, multi-arch indexes, cross-repo blob mount, referrers API
  (signatures/SBOMs attach), delete by tag or digest.
- Index generation for apt and yum hosted repos by workers, signed with a per-repo GPG key (key generated or
  imported; public key downloadable from the repo root and shown in "Set me up").
- Quotas per repo (soft warn / hard block) on stored bytes.

### 4.3 Group repositories
- Ordered members of the same format (proxy and hosted mixed). Reads resolve first match in order; metadata that
  must be merged is merged (npm packuments, PyPI simple pages, Maven `maven-metadata.xml`, Helm `index.yaml`,
  Go `@v/list`, docker tag lists). apt/yum groups are not supported (index merging + signing is out of scope).
- Docker group may have a **writable member** so `docker push` to the group lands in that hosted repo.

### 4.4 Routing and URLs
- One base URL (`WAYSTATION_BASE_URL`, e.g. `https://registry.example.com`). TLS is terminated by the web tier or
  by an external reverse proxy in front of it (both supported; `WAYSTATION_TRUST_FORWARDED`).
- **Nexus-compatible layout**: every format under `/repository/<repo>/...`.
- **Docker/OCI** additionally answers the registry API at `/v2/`:
  - `/v2/<repo>/<image>` addresses repository `<repo>` (path routing).
  - **Upstream aliases**: a docker proxy can declare aliases (e.g. `docker.io`, `ghcr.io`), so
    `registry.example.com/ghcr.io/org/app:1` works (the owner's current convention).
  - A **default docker repository** (setting) receives unprefixed names, so `registry.example.com/library/nginx`
    and the Docker daemon `registry-mirrors` setting work (mirror mode only ever asks for Docker Hub names).
  - Optional per-repo dedicated port connectors (Nexus style) for clients that need a whole host:port: extra
    listeners on the **web** container (still the only container publishing ports), each mapped to one docker repo
    (Phase 6, T-P6-DOCKERC).
- Reserved top-level paths: `/api/`, `/ui/`, `/v2/`, `/repository/`, `/healthz`, `/readyz`, `/metrics`,
  `/.well-known/` (Terraform service discovery), `/auth/` (OIDC callbacks, docker token service).
- The UI lives under `/ui/` (SPA history routes `/ui/...`, hashed assets `/ui/assets/...`); `/` redirects to `/ui/`.
  API at `/api/v1/`.
- `/metrics` is restricted: allowed source CIDRs (`WAYSTATION_METRICS_ALLOW_CIDRS`, default loopback only; add the
  scraper's network) or a bearer token with the `metrics` scope (T-S04); everyone else gets 404. The client address
  is the web tier's `X-Real-IP` when the request comes from a container network (loopback, RFC 1918, ULA), extended
  through `X-Forwarded-For` only when `WAYSTATION_TRUST_FORWARDED` is on and the hop is a `TRUSTED_PROXIES` address.
  `/healthz` (liveness) and `/readyz` (MongoDB and Valkey answer within 2 s, else 503) reveal nothing beyond status.
- The server refuses to start against a database with an unknown newer or a pending migration (`check_schema`).
- Errors: format endpoints answer natively (OCI error JSON under `/v2/` and `/auth/token`, plain text under
  `/repository/`); everything else uses RFC 9457 problem+json (422 with `errors[]` for invalid fields).
  `HEAD` is answered for every API `GET`. Each response carries `X-Request-Id` (the web tier's id when present).

### 4.5 Formats and delivery phases
Phase 2 = parity with the owner's current Nexus (required for cutover). Detail per format in FORMATS.md.

| Format | Proxy | Hosted | Group | Phase |
|---|---|---|---|---|
| Raw / generic files | yes | yes | yes | 2 |
| Docker / OCI (images, Helm OCI, any OCI artifact) | yes (Docker Hub, GHCR, Quay, any OCI) | yes | yes | 2 |
| npm | yes | yes (P5) | yes | 2 (proxy) |
| apt (Debian, Ubuntu, Raspberry Pi) | yes | yes, signed (P5) | no | 2 (proxy) |
| yum / dnf (Rocky, Alma, Oracle, EPEL) | yes | yes, signed (P5) | no | 2 (proxy) |
| PyPI | yes | yes | yes | 5 |
| Maven / Gradle | yes | yes (+snapshots) | yes | 5 |
| Go modules (+ sumdb proxy) | yes | no | yes | 5 |
| Helm (classic index.yaml) | yes | yes | yes | 5 |
| Terraform / OpenTofu providers (network mirror) and modules | yes | yes (modules, providers) | no | 5 |
| Ansible Galaxy (collections; roles read-only) | yes | yes (collections) | yes | 5 |
| Swift Package Registry (SE-0292) | yes (source-archive proxy from git hosts) | yes | no | 5 |
| NuGet v3 | yes | yes | yes | 6 |
| Cargo (sparse index) | yes | yes | no | 6 |
| RubyGems | yes | yes | yes | 6 |
| Composer (PHP) | yes | no | no | 6 |
| Conda | yes | no | no | 6 |
| Alpine apk | yes | no | no | 6 |
| Hugging Face (models/datasets) | yes | no | no | 6 |

### 4.6 Browse and search
- Browse any repository as a tree (paths) or as components (format-aware: images → tags → platforms → layers;
  npm packages → versions; apt suites → packages).
- Global search across repositories the principal can read: name (prefix and substring), version, format,
  repository, digest/checksum lookup ("where is this sha256 used?"), with facets (format, repo, type).
  Target: results in < 150 ms p95 for 1M assets.
- Component pages show: coordinates, versions/tags, size, digests, created/last downloaded/last modified,
  download count and sparkline, which repos hold it, upstream URL, uploader (hosted), install/pull snippet.

### 4.7 Stats and live activity
- **Live transfers**: every in-flight download and upload with repo, asset, client (user/IP), bytes done/total,
  throughput, cache status, upstream latency. Cache states (one vocabulary everywhere: API, events, metrics, UI):
  `HIT` (served from cache), `FETCHING` (miss; this request owns the upstream fetch), `COALESCED` (miss; waiting on
  or tailing another request's fetch), `STALE` (served from cache because the upstream failed), `REVALIDATED`
  (metadata checked upstream, unchanged), `ERROR`.
- **Counters** per repo/format/minute: requests, hits, misses, bytes served from cache, bytes fetched upstream,
  errors, p50/p95 latency. **Bandwidth saved** = bytes served from cache that would otherwise have been fetched.
- **Storage**: bytes per repo/format, blob count, de-duplication savings, growth over time.
- **Top N**: most downloaded components, largest components, most active clients.
- **Upstream health**: per proxy repo status (ok/degraded/blocked/offline), error rate, latency, rate-limit headroom.
- Retention: minute resolution 7 days, hourly 90 days, daily 2 years (MongoDB time-series collections, §DATA_MODEL).
- Exposed via API, SSE event stream (live), Prometheus `/metrics`.

### 4.8 Cleanup, retention, garbage collection, quotas
- **Cleanup policies** (named, reusable, attached to repos): criteria `last_downloaded_older_than`,
  `published_older_than`, `keep_latest_n_versions` (per component), name/version regex include/exclude,
  `prerelease_only`, docker `untagged_older_than`. Every policy has a **dry-run preview** (counts, bytes, sample).
- Policies run on a schedule (cron per policy) in workers; they delete assets/components, never blobs directly.
- **Blob GC**: mark-and-sweep in workers; a blob is deleted only if unreferenced for a grace period (default 24 h)
  so in-flight uploads/fetches are never broken.
- Docker hosted: untagged manifest cleanup; layer GC via blob GC.
- Quotas per repo (§4.2) and a global low-disk guard (refuse new cache writes below a free-space threshold,
  keep serving hits, alert).

### 4.9 Authentication
- **Local** users (argon2id), optional TOTP 2FA (Phase 6).
- **LDAP / LDAPS / StartTLS**: multiple servers with failover, service bind, user search base + filter, attribute
  mapping (uid, name, email), group discovery (memberOf or group search), nested groups optional,
  "require group" gate, custom CA file. Tested against FreeIPA and Active Directory schemas.
- **OIDC**: authorization code + PKCE, discovery URL, client secret, scopes, claim mapping (username, email,
  groups), group claim → Waystation groups, auto-provision on first login (configurable), RP-initiated logout.
  Multiple providers allowed; login page shows a button per provider.
- Identity linking: one Waystation user per username across providers (never link to an account that has a
  usable local password).
- **API tokens**: per user and per service account; scopes (`read`, `write`, `delete`, `admin`) and optional
  repo restriction; expiry; last used; shown once at creation, stored hashed.
- **Format client auth**: docker (Bearer token service at `/auth/token` per Docker token spec; `docker login` with
  username + password or API token), npm (`npm login --auth-type=legacy` and `_authToken` = API token), PyPI/twine
  (basic: username + token), Maven (basic in `settings.xml`), Helm classic repos (basic: `helm repo add --username
  --password`), Terraform/OpenTofu (`credentials` block → `Authorization: Bearer`), Ansible Galaxy
  (`Authorization: Token <api-token>`), Swift (`swift package-registry login`: basic or token), NuGet
  (`X-NuGet-ApiKey` for push, basic for read), apt/yum (basic auth in URL or `auth.conf`/`.repo`), Go (`.netrc` basic).
  Each format's auth is part of its Definition of Done (FORMATS.md).
- Anonymous access: a configurable `anonymous` principal with its own roles (default: read on proxy repos only).
- Sessions: HttpOnly, Secure (https base URL), SameSite=Lax cookie `ws_session`; a new session on every sign-in;
  stored as a sha256 of the token; CSRF on unsafe methods (double submit: readable cookie `ws_csrf`, header
  `X-CSRF-Token`; bearer requests exempt); idle (`SESSION_IDLE_MINUTES`, default 8 h) and absolute
  (`SESSION_MAX_HOURS`, default 7 days) timeouts; login rate limiting per client address (`LOGIN_RATE_LIMIT` per
  5 min, then 429 + `Retry-After`); account lockout after `LOGIN_LOCKOUT_FAILURES` failures for
  `LOGIN_LOCKOUT_MINUTES` (local only). Sign-in failures never say which part was wrong.
- First administrator: `init` creates local user `admin` (role `admin`) when there are no users, with
  `BOOTSTRAP_ADMIN_PASSWORD[_FILE]` or a one-time password printed once to the init container's stderr.

### 4.10 RBAC
- **Permissions** are `(action, scope)`: actions `browse`, `read` (download/pull), `write` (upload/push),
  `delete`, `manage` (repo settings, cleanup, run tasks), plus global `admin`, `user-admin`, `audit-read`,
  `stats-read`, `settings-admin`.
- **Scopes**: `*`, a repository name, a repository name glob (`docker-*`), a format (`format:npm`), a repository
  type (`type:proxy`). Global actions are granted only with scope `*`; `write`, `delete` and `manage` imply `browse`
  and `read` on the same scope. A repository the principal can neither browse nor read is hidden (404 everywhere).
- **Roles** bundle permissions; built-ins: `admin`, `reader` (browse+read all), `deployer` (read+write on scope),
  `viewer` (browse + stats), `anonymous` (browse + read on `type:proxy`). Custom roles via UI/API (not yet: M0 has
  the built-in roles only, assigned on the user record).
- **Groups** (local, or mapped from LDAP groups / OIDC claims) hold roles; users hold roles directly or via groups.
- Effective permissions are computed per request from a cached, versioned grant table (ARCHITECTURE §7) and
  checked in one place. Content listings and search are filtered, never just hidden.

### 4.11 Audit
Append-only audit log of security-relevant actions: sign-in/out/failure, token create/revoke/use-refused,
user/group/role/provider changes, repo create/update/delete, uploads and deletes on hosted repos, cleanup runs,
settings changes. Searchable in the UI, exported via API; retention configurable (default 400 days).

### 4.12 Administration
- Repository CRUD with format-aware forms and validation, test-upstream button, clone repo, enable/disable.
  Names: §3 pattern; not a reserved top-level path (`api`, `ui`, `v2`, `auth`, `repository`, `healthz`, `readyz`,
  `metrics`) and not a docker upstream alias (case-insensitive). Upstream URLs: http(s), no credentials in the URL,
  literal private addresses only with `allow_private_upstream`. Upstream secrets are write-only (reads return
  `secret_set`) and kept when an update omits them. Name, format and type cannot change. Setting a docker repository
  as `default_repo` clears it on the others. "Test upstream" checks DNS, the SSRF guard, TLS, HTTP and
  authentication of the format's probe path (docker `v2/`, npm `-/ping`, apt `dists/<distribution>/Release`, raw the
  URL itself) and always answers 200 with the findings.
- **Set me up** per repo per client: generated config snippets (docker daemon.json, `docker login`, `.npmrc`,
  `pip.conf`, `settings.xml`, `sources.list`/`.sources`, `.repo`, Go env, Helm repo add, Terraform CLI config,
  `ansible.cfg`, Swift registry commands) with the current user's token placeholder.
- Tasks page: scheduled jobs (cleanup, GC, index rebuild, stats rollup, upstream metadata prefetch), run now,
  history with logs.
- Workers page: live worker list, queues, throughput, failures.
- System: storage path usage/free, MongoDB/Valkey health, version, licence, support bundle download (redacted).
- **Import from Nexus**: read a Nexus instance's repository list via its REST API and create equivalent repos
  (names, formats, types, upstreams, docker routing); cached content is re-fetched on demand, hosted content
  can be migrated by a worker job (Phase 6).
- Webhooks (Phase 6): on push/publish/delete, signed payloads.

## 5. Non-functional requirements

### 5.1 Performance budgets (enforced in CI by `make perf`, `Build/ci/budgets.json`)
| Path | Budget |
|---|---|
| Cache hit, content asset (any size), time to first byte | p95 < 15 ms server-side; throughput limited by disk/network, not CPU |
| Cache hit, metadata asset | p95 < 25 ms |
| Docker manifest HEAD/GET (cached) | p95 < 20 ms |
| Cache miss overhead vs direct upstream fetch | < 5% throughput, < 30 ms added TTFB |
| API list/search endpoints | p95 < 150 ms at 1M assets |
| Dashboard first meaningful paint (cold, LAN) | < 1.0 s; JS for the initial route < 250 kB gzip |
| Live event latency (transfer progress to UI) | < 1 s |
| Concurrent clients | 500 concurrent downloads on 4 vCPU without errors |

### 5.2 Availability and scale
- Server and workers are stateless; run N replicas behind the web tier. Single-node MongoDB replica set by default;
  external replica set supported. Valkey loss degrades live stats and coalescing, never correctness of downloads.
- Graceful shutdown: in-flight downloads finish (configurable grace), uploads resumable (OCI chunked uploads).

### 5.3 Security
OWASP ASVS L2 as the target. CSP without `unsafe-inline`; strict cookies; dependency audit in CI (`pip-audit`,
`npm audit`); containers run as non-root, read-only root filesystem where possible, `cap_drop: [ALL]`,
`no-new-privileges`; secrets via env or `*_FILE`; encryption at rest for stored credentials (AES-GCM, key rotation
with multiple keys). Zip-slip/tar-slip safe archive handling. Size limits on every parser.

### 5.4 UX quality
Professional and consistent (UI_DESIGN.md). Responsive from 360 px to 4K. Light/dark/system themes.
WCAG 2.2 AA. Keyboard navigable, command palette. No external CDNs or fonts (works fully offline).

### 5.5 Observability
Structured JSON logs (request id, principal, repo), Prometheus metrics, health/readiness endpoints per container,
OpenTelemetry tracing hooks (off by default). Zabbix template in `Build/monitoring/` (Phase 6).

## 6. User interface
See `UI_DESIGN.md`. Summary: dashboard with live transfers and KPIs, repositories, browse/search, component
pages with pull/install snippets, activity log, storage, cleanup policies, tasks/workers, administration
(users, groups, roles, auth providers, tokens, audit, settings).

## 7. API
- REST JSON under `/api/v1/`, described by OpenAPI 3.1 generated from the server (FastAPI + pydantic v2).
  The UI uses only this API. Pagination by opaque cursor; filtering and sorting params documented.
- **Event stream**: `GET /api/v1/events/stream` (Server-Sent Events) with topic filters (`transfers`, `stats`,
  `repo:<name>`, `tasks`, `workers`); RBAC-filtered per subscriber; heartbeat 15 s; resumable with `Last-Event-ID`.
- Auth: session cookie (UI) or `Authorization: Bearer <api-token>`. CSRF for cookie sessions is a double-submit
  token: `POST /auth/login` and `GET /auth/me` return `csrf_token` (also set in the readable `ws_csrf` cookie);
  unsafe methods must echo it in `X-CSRF-Token`. Bearer requests are exempt.
- Versioned: breaking changes only in `/api/v2/`.
- **Contract (M0)**: the API the UI depends on is written contract-first in
  `Application/Web/openapi/waystation-m0.yaml` (OpenAPI 3.1): config, auth/session, repositories (CRUD for proxy
  repos with format options, test-upstream, Set me up snippets), stats (summary with sparklines, traffic series by
  range `15m|1h|24h|7d|30d` split hit/upstream or by format, storage, upstream health incl. Docker Hub rate limit),
  live transfers, activity, browse (generic folder listing, docker images/tags/manifest, npm packages/versions, apt
  suites/packages) and the SSE event schemas (`LiveEvent`: `transfers.snapshot`, `transfer.started|progress|finished|failed`,
  `stats.tick`, `upstream.state`, `activity`). Errors are RFC 9457 problem+json with field-level `errors[]`.
  `make gen-api` regenerates the UI's TypeScript types (`src/api/schema.d.ts`); CI fails if they are stale. Once
  the server generates its own document, a contract test keeps it compatible with this file.
- EventSource cannot set headers, so a client that reopens the stream itself passes `last_event_id` as a query
  parameter (same meaning as `Last-Event-ID`). On (re)connect the server first sends `transfers.snapshot`.

## 8. Deployment

### 8.1 Containers
| Container | Image | Role | Scales |
|---|---|---|---|
| `web` | `waystation-web` (nginx + built SPA) | TLS (optional), routes requests, serves SPA, serves blobs via `X-Accel-Redirect`, applies size/time limits | 1..N |
| `server` | `waystation-server` | All format protocols + REST API + SSE + docker token service; decides every request | 1..N |
| `worker` | `waystation-worker` | Background jobs: index generation/signing, cleanup, GC, stats rollups, prefetch, import | 1..N (queues per job class) |
| `mongodb` | `mongo:8` (official) | Metadata, users, stats (time series), audit, jobs history | single-node replica set (or external) |
| `valkey` | `valkey/valkey:8-alpine` | Coalescing locks, live counters, pub/sub, job streams, grant cache. Runs with `maxmemory-policy noeviction` and AOF (`appendfsync everysec`) so job streams and locks are never evicted | 1 |
| `init` | `waystation-server` (one-shot) | Mongo replica set init, migrations, first admin bootstrap | run-to-completion |

Only `web` publishes ports (`WAYSTATION_HTTP_PORT`, default 8080). `web`, `server` and `worker` share the
`frontend` project network (server and worker reach upstreams through it); MongoDB and Valkey sit on the
`backend` network (`internal: true`, no egress) shared with `server`, `worker` and `init`. Long-running containers
run as non-root users with read-only root filesystems (except MongoDB's) and `cap_drop: [ALL]`; `init` alone starts
as root with `CHOWN`, `FOWNER`, `DAC_OVERRIDE`, `SETUID`, `SETGID` to fix data-dir ownership, then drops to
`WAYSTATION_UID:GID` before touching MongoDB.

### 8.2 Storage
- `WAYSTATION_DATA_DIR` (host path or volume) is mounted at `/var/lib/waystation` in `server` and `worker` (rw)
  and `web` (ro). It contains `blobs/` (content-addressed), `uploads/` (in-progress, **same filesystem** so
  commits are atomic renames), `keys/` (repo signing keys, encrypted) and nothing else. Everything else is in
  MongoDB. Works on local disk, NFSv4 or SMB (no hard links needed; rename atomicity required).
- MongoDB data in its own volume (`WAYSTATION_MONGO_DIR`), not on the blob path.

### 8.3 Compose conventions (match the owner's existing stacks)
- One `compose.yaml` per deployable unit in `Build/Containers/`: `compose.yaml` (all-in-one reference stack) and
  `compose.standalone.{web,server,worker}.yaml` for split hosts, `compose.dev.yaml` (source bind-mounted).
- Top-level `name: waystation` (project name = directory name on the target host).
- A shared hardening anchor on every service:
  `restart: unless-stopped`, `cap_drop: [ALL]` (add back only what is needed), `security_opt: ["no-new-privileges:true"]`,
  `logging: {driver: json-file, options: {max-size: "20m", max-file: "5"}}`.
- `container_name` set (`waystation-web`, `waystation-server`, ...), and **unique network aliases**
  (`waystation-server`, never a bare `server`) because deployments join shared reverse-proxy networks.
- Images: `${WAYSTATION_IMAGE_PREFIX}/waystation-<role>:${WAYSTATION_IMAGE_TAG:?set WAYSTATION_IMAGE_TAG}`, pinned
  tags, no `:latest` in examples.
- Environment: variables listed without values are passed through from the env file; the env file is outside the
  repo (`root:docker 640` on hosts). Secrets also accepted as `*_FILE`.
- Healthchecks on every service; `depends_on` with `condition: service_healthy`; web restarts with server.
- Reverse-proxy integration (Traefik labels, extra networks, TLS) lives in a site override file **outside** the
  repo; the repo ships `Build/Containers/examples/traefik.override.example.yaml` with `example.com` names.
- Explicit subnet for the project network is configurable (`WAYSTATION_SUBNET`, and `WAYSTATION_BACKEND_SUBNET`
  for the backend network), because Docker's default pools can collide with routed networks. Unset = Docker picks.
- Dev stack (`make dev-up`): `compose.yaml` + `compose.dev.yaml` as project `waystation-dev` (containers
  `waystation-dev-*`), images built locally, Python source bind-mounted read-only with hot reload, throwaway secrets
  generated once into the gitignored `.dev-data/dev.env`; `compose.dev-spa.yaml` is added automatically to serve a
  locally built SPA (`Application/Web/dist/spa`), otherwise the image's copy (or a placeholder page) is served.

### 8.4 TLS and reverse proxies
Either `web` terminates TLS (cert/key paths, HTTP→HTTPS redirect, HTTP/2) or it runs plain HTTP behind a trusted
proxy (`WAYSTATION_TRUST_FORWARDED=true`, trusted CIDRs). Large uploads/downloads must pass through untouched:
docs list the required proxy settings (no buffering, no body size limit, long timeouts) for nginx, Traefik,
Caddy, HAProxy and Synology DSM.

### 8.5 Backup and restore
`make backup` / documented commands: `mongodump --archive --gzip` (consistent with a snapshot of the blob path
taken after the dump; blobs are immutable so order is dump-then-copy) plus the blob directory. Restore = restore
both, then run the `verify` task (detects missing blobs, marks affected assets for re-fetch). Proxy-only content
can be skipped from backups (option `--hosted-only` copies only blobs referenced by hosted repos).

### 8.6 Upgrades
Forward-only migrations run by `init`; `server` refuses to start on an unknown newer schema; release notes flag
breaking changes. Zero-downtime for patch releases with ≥2 server replicas.

## 9. Configuration reference (environment)
All variables are `WAYSTATION_*`; every secret also accepts `<NAME>_FILE` (setting both is an error; empty values mean "use the default"). The authoritative list is
`.env.example` (generated from `waystation_common.settings` by `make env-example`; CI checks it is current).
Key ones: `BASE_URL`, `DATA_DIR`, `MONGO_URI`, `MONGO_DB`, `VALKEY_URL`, `SECRET_KEYS` (comma list, first is
active), `SESSION_SECRET`, `TRUST_FORWARDED`, `TRUSTED_PROXIES`, `LOG_LEVEL`, `LOG_FORMAT`, `SERVER_WORKERS`,
`WORKER_QUEUES`, `WORKER_CONCURRENCY`, `MAX_UPLOAD_GB`, `MIN_FREE_GB`, `BOOTSTRAP_ADMIN_PASSWORD_FILE`,
`HTTP_PROXY`/`NO_PROXY`, `EXTRA_CA_FILE`, `SSRF_ALLOW_CIDRS`. Auth providers, repositories and policies are
configured in the UI/API (stored in MongoDB). Planned (Phase 6, T-O09): optional **declarative provisioning** from
a YAML file (`WAYSTATION_PROVISION_FILE`, applied idempotently at start; GitOps-friendly).

## 10. Observability
See §5.5. Metric names: `waystation_requests_total{repo,format,status,cache}`, `waystation_bytes_total{repo,direction}`,
`waystation_upstream_latency_seconds`, `waystation_inflight_transfers`, `waystation_storage_bytes{repo}`,
`waystation_jobs_total{queue,status}`, `waystation_upstream_state{repo}`.

## 11. Testing strategy
- Unit tests next to code (pytest, Vitest). Property-based tests (hypothesis) for path normalisation, version
  ordering, metadata merging, range handling.
- **Real-client tests** (`tests/clients/<format>/`): containers run the actual clients (docker/podman/skopeo/crane,
  npm/pnpm/yarn, pip/uv/twine, mvn/gradle, apt, dnf, go, helm, terraform/tofu, ansible-galaxy, swift, dotnet nuget,
  cargo, gem, composer, conda) against a Waystation stack with **recorded upstream fixtures** (an offline fake
  upstream replays captured responses; a nightly job runs against live upstreams). `make clients-test
  FORMAT=<f> MODE=replay|record|live`; fixtures are scrubbed and pass `make clients-check` (forbidden strings,
  size budget). How a format plugs in: `tests/clients/README.md`.
- **OCI distribution-spec conformance suite** must pass for docker hosted repos.
- Integration tests against real MongoDB and Valkey containers.
- Playwright E2E for the UI (login with each provider type, dashboard live updates, repo CRUD, set-me-up).
- Load/perf tests (k6) enforcing §5.1 budgets; bundle-size budget for the SPA.
- Security tests: path traversal corpus, SSRF corpus, auth bypass matrix (every route × principal type).

## 12. CI/CD and release
- GitHub Actions (primary): `make check` on every PR (lint, typecheck, unit, integration, client tests for changed
  formats, budgets); nightly full client matrix + live-upstream smoke; `release.yml` on `vX.Y.Z` tags builds
  multi-arch (amd64, arm64) images, SBOMs and provenance, publishes to GHCR.
- Gitea Actions (secondary) mirror workflow `.gitea/workflows/` building images on every push to the Gitea
  registry. Registry credentials only from org secrets (`REGISTRY_USER` / `REGISTRY_TOKEN`).
- `VERSION` file at the root is the single source; `make set-version V=X.Y.Z` updates everything.
- Semantic versioning; CHANGELOG kept by hand per task.

## 13. Repository layout and bootstrap

### 13.1 Git initialisation (task T-F01)
```bash
cd ~/development/Waystation
git init -b main
git remote add origin https://github.com/SlinkySoftware/Waystation.git
git remote add gitea https://git.example.com/SlinkySoftware/Waystation.git
git add -A && git commit -m "T-F01: plan and repository skeleton"
git push -u origin main && git push gitea main
```
Both remote repositories must exist first (the owner creates them; empty, no README/licence).

### 13.2 Layout
```
Waystation/
  CLAUDE.md  README.md  appidea.txt  VERSION  LICENSE  Makefile  pyproject.toml (uv workspace)  uv.lock
  .env.example  .gitignore  .dockerignore  .github/workflows/  .gitea/workflows/
  Application/
    Common/   waystation_common/   settings, logging, ids, crypto, events schema, job protocol, storage API, errors
    Server/   waystation_server/   app factory, api/ (REST), security/ (authn, access), formats/<format>/,
                                    proxy/ (fetch, coalesce, freshness), dataplane/ (x-accel, ranges), stats/, sse/
    Worker/   waystation_worker/   runner, jobs/ (cleanup, gc, index_apt, index_yum, rollups, prefetch, import)
    Web/      Quasar SPA (src/api, components, composables, layouts, pages, stores, router, css, mocks/),
              branding/ (logo masters; public/ holds web-sized copies)
  Build/
    Containers/  compose*.yaml, web/ (Dockerfile, nginx.conf.template), server/, worker/, dev/ (dev image), examples/
    githooks/    pre-commit hook (`make hooks`)
    ci/          budgets.json, scripts used by workflows
    monitoring/  zabbix template, grafana dashboard (Phase 6)
  tests/  clients/<format>/  fixtures/upstreams/<format>/  perf/  security/  e2e/
  Documents/  SPECIFICATION.md  TASKS.md  CHANGELOG.md  UI_DESIGN.md
              Architecture/ ARCHITECTURE.md DATA_MODEL.md FORMATS.md adr/
              Operations/  DEPLOYMENT.md  BACKUP.md  REVERSE_PROXIES.md  MIGRATING_FROM_NEXUS.md
```

## 14. Build status
| Area | Status |
|---|---|
| Plan and documents | Drafted 2026-11-08 |
| Repository bootstrap (T-F01) | Done 2026-11-08: skeleton, LICENSE (GPL-3.0-or-later), VERSION 0.1.0; pushed to both remotes |
| Dev tooling (T-F02) | Done 2026-11-08: dev image (Python 3.14, uv, Node 26 LTS), Makefile, uv workspace (`waystation_common`, `waystation_server`, `waystation_worker`), ruff, mypy strict, import-linter contracts (ARCH §3), pre-commit hook |
| Common core (T-F04) | Done 2026-11-08: `waystation_common.settings` (env `WAYSTATION_*`, `<NAME>_FILE` for secrets, generated `.env.example`), `logs` (JSON lines + contextvars request context, redaction), `ids` (ULID), `errors`, `paths.normalise`, `crypto` (AES-256-GCM keyring, rotation) |
| Containers and compose (T-F05) | M0 slice 2026-11-08: server/worker/web images, `compose.yaml` + dev override, `make dev-up` brings web, server, worker, mongodb (rs0), valkey and init up healthy; server answers only `/healthz` and `/readyz`; web serves a placeholder page until the SPA is built. Standalone compose files and TLS pending |
| MongoDB layer (T-F06) | M0 slice 2026-11-08: async client factory, collection registry and indexes for the M0 collections, declarative schema + forward-only migrations run by `init` (idempotent), `make test-integration` against real MongoDB/Valkey |
| Blob store (T-F07) | M0 slice 2026-11-08: local-filesystem `BlobStore` (streaming sha256, size limits, verified atomic commit, X-Accel paths, orphan sweeper, free-space guard); NFS CI scenario pending |
| Events and live stats (T-F09) | M0 slice 2026-11-08: event schema and non-blocking emitter (live transfers, activity stream, per-minute counters and latency histograms in Valkey); the worker rolls closed minutes into `stats_minute` exactly once. Hour/day rollups pending; nothing calls the emitter until the proxy engine lands (T-S07) |
| Server app factory (T-S01) | Done 2026-11-08: routing for `/repository/<repo>/` (case-sensitive names, normalised paths) and `/v2/` (repo prefix → upstream alias → default docker repo), request context (request id, client address, JSON access log), native vs problem+json errors, `HEAD` for API routes, `/healthz`, `/readyz` (MongoDB + Valkey), `/metrics` (Prometheus, CIDR-restricted), start-up `check_schema`, in-process repository catalog (5 s refresh) |
| Local authentication (T-S02) | M0 slice 2026-11-08: bootstrap admin via `init`, argon2id, sessions with idle/absolute timeouts, CSRF, login rate limit and lockout, `/api/v1/config`, `/api/v1/auth/{providers,login,logout,me,me/preferences}`. Password change, session list/revoke and profile endpoints pending |
| RBAC (T-S03) | M0 slice 2026-11-08: one decision point (`security.access`), built-in roles, scopes incl. `type:`, hidden = 404, every route declares its rule (enumeration test), CSRF enforced app-wide for session requests. Custom roles, groups and the grant cache pending |
| Repositories (T-S05) | M0 slice 2026-11-08: proxy repositories of docker, npm, apt and raw: REST CRUD, validation with field errors, write-only encrypted upstream secrets, test upstream, "Set me up" snippets, per-format options in OpenAPI. Hosted/group, clone and audit pending |
| Real-client harness (T-P2-HARNESS) | M0 slice 2026-11-08: `make clients-test FORMAT=<f> MODE=replay\|record\|live` runs client containers against a per-format compose project with a record/replay fake upstream (impersonates public upstream hosts via `extra_hosts` + throwaway CA; replay fails on any miss; record scrubs and stores fixtures under `tests/fixtures/upstreams/<format>/`); `make clients-check` (in `make check`) rejects non-allowlisted hosts, non-documentation IPs, keys/tokens and oversize fixtures. Only the `echo` self-test format exists so far; nightly live smoke workflow stub |
| M0 API contract + mocks (T-V01) | Done 2026-11-08: OpenAPI 3.1 contract (`Application/Web/openapi/waystation-m0.yaml`), generated TS types + `openapi-fetch` client, MSW handlers with seeded fixtures (11 proxy repos: Docker Hub, GHCR, Quay, registry.k8s.io, npmjs, Debian, Debian security, Ubuntu, Raspberry Pi OS, two raw) and a live-event generator (bursty docker pulls, npm installs, apt runs, raw downloads, coalescing, failures); `npm run dev:mock` / `make frontend-mock`. Server-side contract test pending (server lane) |
| Web shell (T-F10) | Done 2026-11-08: Quasar 2 / Vue 3 / TS strict SPA under `/ui/` (build output `Application/Web/dist/spa`), light/dark/system themes, self-hosted Inter + JetBrains Mono, SVG Material Symbols (no icon font), collapsible rail, top bar with live indicator, router + auth guard, local sign-in, shared live store (one stream, topic ref-counting, reconnect + Last-Event-ID, 4 Hz flush), `make frontend-build` with bundle budget (initial route 158 kB gzip of 250) |
| Design system (T-U01 min) | M0 slice 2026-11-08: tokens, themes, format icon sprite, base components, SVG charts. Density toggle and showcase route still to do |
| Dashboard (T-U03 min) | M0 slice 2026-11-08 on mocks: KPIs, live transfers, traffic chart, storage donut, upstream health, activity feed, all live from the event stream. Top artifacts / recently added still to do |
| Repositories UI (T-U04 min) | M0 slice 2026-11-08 on mocks: list + filters, create/edit/delete docker/npm/apt/raw proxies with test upstream and field errors, detail page (Overview, Browse, Settings), Set me up panel. Snippets shown with a `<your-api-token>` placeholder (tokens are shown once at creation, T-S04) |
| Browse UI (T-U05 min) | M0 slice 2026-11-08 on mocks: docker images/tags/manifest, npm packages/versions, apt suites/packages, raw folder listing. Screenshot tour (both themes, desktop + mobile) in `Documents/Notes/screenshots/m0/` |
| Everything else | Not started |

## 15. Open decisions (owner)
| # | Question | Proposal |
|---|---|---|
| D1 | Licence | **Decided 2026-11-08 (the owner):** GPL-3.0-or-later (same as DeviceVault). |
| D2 | Bundled MongoDB vs an existing shared MongoDB | Bundled single-node replica set by default; external URI supported. |
| D3 | Format priority after Phase 2; do PyPI/Maven gate cutover? | **Decided 2026-11-08 (the owner):** cutover needs only today's Nexus formats (docker, apt, yum, npm, raw); PyPI and Maven come after, then Helm, Go, Terraform, Galaxy, Swift, then Phase 6 formats. |
| D4 | Vulnerability scanning (Trivy/Grype on cached images and packages) | Post-v1 (Phase 7) as an optional `scanner` worker queue. |
| D5 | S3/MinIO blob storage driver | Post-v1; the storage API is designed for it (ARCHITECTURE §5). |
| D6 | Docker Hub default route name | Setting `default_docker_repo`, initially the Docker Hub proxy. |
| D7 | Product name/brand | "Waystation", logo/icon designed in T-U02. |

