# Waystation architecture

**Enforced rules.** Code that violates a rule here is a bug, even if tests pass. Changing a rule needs an ADR
(`adr/`) approved by the owner. Section numbers are referenced from code comments and tasks (`ARCH §6.3`).

## 1. Components and responsibilities

```
                 ┌──────────────────────── web (nginx) ────────────────────────┐
 clients ──TLS──►│ /            SPA (static, immutable hashed assets)           │
 (docker, npm,   │ /api/, /v2/, /repository/, /auth/, /.well-known/ ──► server  │
  apt, browser)  │ internal location /_blobs/ ──► WAYSTATION_DATA_DIR (ro)      │
                 └──────────────▲───────────────────────────┬──────────────────┘
                                │ X-Accel-Redirect          │ proxy_pass (no buffering)
                 ┌──────────────┴────────── server (N) ─────▼──────────────────┐
                 │ FastAPI/uvicorn (uvloop, httptools). Format plugins.        │
                 │ authn/authz · proxy engine · hosted writes · REST · SSE     │
                 └───┬──────────────┬───────────────┬──────────────┬───────────┘
                     │              │               │              │ httpx (HTTP/2)
                  MongoDB        Valkey      DATA_DIR (rw)     upstream registries
                     ▲              ▲               ▲
                 ┌───┴──────────────┴───────────────┴──── worker (N) ──────────┐
                 │ job runner: index/sign, cleanup, GC, rollups, prefetch, import│
                 └──────────────────────────────────────────────────────────────┘
```

- **web**: the only published port. Serves the SPA, proxies dynamic paths to `server`, and serves file bodies
  for cache hits from the blob store via `X-Accel-Redirect` (sendfile, HTTP ranges, `ETag`). Holds no state, no
  credentials, no business logic. The blob store is mounted **read-only**.
- **server**: stateless. Speaks every format protocol, the REST API, SSE, the Docker token service. Makes every
  authorization decision. Reads/writes MongoDB and Valkey; writes blobs. Never runs long batch work: it enqueues jobs.
- **worker**: stateless job runner. Owns all batch and scheduled work. No HTTP listener except health.
- **MongoDB**: system of record for configuration, metadata, users, stats (time-series), audit, job history.
- **Valkey**: coordination only (`noeviction`, AOF on). Losing it must never lose data or corrupt the cache:
  coalescing degrades to per-request fetches, live stats pause, scheduled jobs are re-derived and event-driven jobs
  are re-dispatched from the MongoDB outbox (§10).

## 2. Technology (see adr/0001)
- Python 3.14, FastAPI + pydantic v2, uvicorn with uvloop/httptools, `httpx` (HTTP/2) for upstream calls,
  PyMongo async API (`pymongo.AsyncMongoClient`), `redis-py` asyncio client against Valkey, `argon2-cffi`,
  `ldap3`, `authlib` (OIDC), `cryptography`. OpenPGP signing for apt/yum hosted repos sits behind
  `waystation_common.signing`; the implementation (a Python OpenPGP library or `gpg` inside the worker image only)
  is chosen in task T-P5-APTH with an ADR.
- Frontend: Quasar 2, Vue 3, TypeScript strict, Pinia, vue-router, `openapi-fetch` + `openapi-typescript`.
  Charts are in-house SVG components (no heavyweight chart library), following DeviceVault's `components/charts`.
- nginx (official image, unprivileged variant) for `web`.

## 3. Package boundaries (import-linter contracts, CI-enforced)
- `waystation_common` imports nothing from server/worker; no FastAPI imports; no network or DB clients at import time.
- `waystation_server` and `waystation_worker` may import `waystation_common`; **never each other**.
- Inside the server: `formats/*` may import `proxy`, `storage` (via `waystation_common.storage`), `security.access`
  (decision API only), `events`; formats never import each other or `api/*`. `api/*` (REST) never imports `formats/*`
  internals, only their registered descriptors.
- Only modules under `*.db` / `*.repositories` packages touch MongoDB collections; everything else uses them.
- The web SPA calls only `/api/v1/*` and the SSE endpoint, through the generated client in `src/api/`.

## 4. Request lifecycle (server)
1. **Route**: a single router maps the URL to `(repository, format handler, operation)`; Docker `/v2/` names
   are resolved by repo prefix, then upstream alias, then the default docker repo (SPEC §4.4).
2. **Authenticate**: session cookie, bearer API token, Docker registry token, basic auth; else `anonymous`.
3. **Authorize**: `access.check(principal, action, repo)`; failure on an unreadable repo = 404 (or the format's
   native "not found"), failure on a readable repo for write = 403/401 per protocol (docker needs 401 +
   `WWW-Authenticate` to trigger login).
4. **Resolve**: format handler resolves to an asset (cached, needs revalidation, missing).
5. **Serve**: cached content → `X-Accel-Redirect: /_blobs/<sha256 path>`; never stream cached bytes through Python.
   Metadata generated per request (merged groups, rewritten URLs) is served from Valkey/Mongo cache directly.
   X-Accel rules (nginx drops most upstream headers on an internal redirect and computes its own ETag):
   - The **server answers conditional requests itself** (`If-None-Match` against the digest ETag → 304) before
     issuing X-Accel; nginx only handles byte ranges.
   - The server sends `X-Ws-Content-Type`, `X-Ws-Etag`, `X-Ws-Digest` (and any format headers such as
     `Docker-Content-Digest`); the `/_blobs/` location sets `etag off`, an empty `types {}` map, and
     `add_header ETag $upstream_http_x_ws_etag always` /
     `add_header Docker-Content-Digest $upstream_http_docker_content_digest always` (etc.), `internal`, `sendfile on`.
     The server also sets its normal `Content-Type` on the X-Accel response: nginx keeps it across the internal
     redirect (verified in T-F05; `default_type` cannot take a variable, so `X-Ws-Content-Type` is informational).
     Implemented by `waystation_server.dataplane.xaccel.serve_blob` (ETag `"sha256:<hex>"`; `HEAD` is answered by
     the server with `Content-Length`; `Content-Disposition` passes through nginx unchanged).
   - File access: server/worker and web share a GID (`waystation`, default 10001); blob dirs are setgid (2750),
     files 0640, processes run with umask 0027; the unprivileged nginx user is a member of that group. The `init`
     container fixes ownership/modes on the data dir at start (idempotent, only when wrong).
6. **Record**: emit a stats/event record (non-blocking, batched, fire-and-forget into Valkey).

## 5. Storage
- `waystation_common.storage.BlobStore` is the only API to blobs: `open_writer() -> BlobWriter` (streaming,
  hashing sha256 incrementally, size limit), `commit(writer) -> BlobRef` (fsync file, atomic rename into place,
  fsync dir), `exists`, `stat`, `path_for_xaccel`, `delete` (GC only). Interface designed so an S3 driver can be
  added (D5) with presigned-redirect serving instead of X-Accel.
- Layout under `WAYSTATION_DATA_DIR`:
  - `blobs/sha256/<aa>/<bb>/<full-hex>`: immutable content. Created 0640, dirs 0750.
  - `uploads/<uuid>`: in-progress downloads (cache fill) and uploads (hosted, OCI chunked). Same filesystem as
    `blobs/`. Orphans older than 24 h removed by GC.
  - `keys/`: per-repo signing keys, encrypted with `SECRET_KEYS`.
- A blob is written once; identical content from any repo or format is stored once (refcounted via assets, GC'd
  by mark-and-sweep, SPEC §4.8). Never mutate a blob; never hard-link.
- Free-space guard checked before opening writers (SPEC §4.8).

As implemented (T-O02): mark-and-sweep runs as the worker job `blob.gc` (`waystation_worker.gc`) over the `blobs`
collection. To make a cache fill racing the sweep safe, `BlobStore.commit` **claims** the blob through
`BlobStore.guard` (`waystation_common.db.blobs.MongoBlobGuard`) before the rename, also when identical content is
already in place: the claim sets `claimed_at` and clears the mark, and cannot happen while GC holds the blob in state
`deleting` (it waits, then writes the file again). GC deletes only after a compare-and-set to `deleting` that needs
both the mark and the last claim to be older than the grace period, then re-checks that no asset references the
blob. Code that makes a new reference to an existing blob without a commit must claim it first. nginx may be sending
a blob GC unlinks: the open file stays readable on POSIX, and a blob served moments ago was referenced, so it is not
deleted before the grace period ends.

## 6. Proxy engine
### 6.1 Upstream client
One shared `httpx.AsyncClient` per upstream host (HTTP/2, connection pooling, limits per host), per-repo auth
plug-ins (basic, bearer, docker token negotiation with cached scoped tokens, GHCR), SSRF-checked resolver,
timeouts (connect 10 s, read 60 s idle), retries only on idempotent requests and only before first byte.

### 6.2 Freshness
Each cached asset stores `fetched_at`, `etag`, `last_modified`, `upstream_status`. Metadata older than
`metadata_max_age` is revalidated (conditional request); on 304 only timestamps change. Content assets are
immutable unless the format says otherwise. Negative cache entries live in Valkey with TTL.
Stale-on-error and offline mode as SPEC §4.1.

### 6.3 Coalescing (single-flight across replicas)
- Key `fill:<repo>:<sha1(asset-key)>` in Valkey: a hash created atomically by a Lua claim script (the equivalent of
  `SET NX PX`: created with a lease TTL only when no pending/streaming fill exists; otherwise the caller is counted as
  a follower in the same call), holding the owner token and host id, the owner's upload file id, progress and the
  outcome. Implemented by `waystation_server.proxy.coalesce`; field list in DATA_MODEL §2.
- The owner streams upstream → `BlobWriter` and → its client simultaneously (tee). Progress (bytes flushed to the
  upload file) is published by a background task, never on the tee path: every 20 ms while followers are attached,
  every 250 ms otherwise; the follower count shows on the owner's live transfer.
- Followers either (a) **tail** the in-progress upload file using the progress counter, streaming to their client
  as bytes land (holding back the last piece until the owner has verified and committed, like the owner), or (b)
  wait for completion, then serve via X-Accel. Metadata fetches and revalidations are coalesced the same way
  (followers always wait). A failed or mismatched fetch fails its followers with the same error (stale-on-error
  still applies per follower); nothing unverified is ever completed.
- Owner failure = lease expiry (owner died) or an `aborted` outcome (its client went away, internal error):
  followers claim again and one becomes owner. A follower that already sent bytes continues only after checking
  that they are a prefix of the new fill (sha256 of the sent bytes vs the new file / new upstream bytes); a
  different upstream answer aborts that client's transfer.
- Tailing is only safe where the follower sees the owner's writes immediately: same host and a local filesystem.
  On NFS/SMB (close-to-open consistency, attribute caching) a follower on another host can read stale or short
  data. Rules: while followers are attached the owner `fdatasync`s before publishing the `synced` offset (followers
  on another host never read past it; followers on the owner's host, sharing its page cache, read up to `written`);
  the progress record carries the owner's host id (the kernel boot id, shared by containers on one machine);
  `WAYSTATION_COALESCE_TAIL=auto` (default) tails only when the follower is on the owner's host and the data dir is
  not a network filesystem (detected from `/proc/mounts`), otherwise waits; `local` / `wait` force a mode.
  Followers that wait still get bytes as soon as the commit lands. T-F07/T-S08 CI covers both modes; followers on a
  second host against an NFS export are not covered yet.
- Lock lease (`WAYSTATION_COALESCE_LEASE_S`, default 15 s) renewed every third of it while the fill makes progress
  (a fill with no progress for the upstream timeouts plus a lease stops renewing and expires); abort cleans the
  upload file. Valkey unavailable = no coalescing (each request fetches), never an error.

### 6.4 Validation
Bytes are committed to `blobs/` only after the expected digest/size (when the protocol supplies one) matches.
Formats without upstream checksums record the computed sha256 and serve consistently.

## 7. Security model
- One decision point: `waystation_server.security.access` exposes `check`, `filter_repos`, `filter_query`.
  Every route declares its required action (decorator); a test enumerates all routes and fails if one lacks it.
- Grants are materialised per principal (roles ∪ group roles ∪ anonymous) into a compact table cached in Valkey
  keyed by `grant_version` (incremented on any RBAC change; change stream invalidation across replicas).
- Token revocation: revoking an API token (or changing/disabling/deleting a service account) is announced on the
  Valkey pub/sub channel `revocations` (a token id, or `*` = drop everything); every replica's listener drops the
  cached entry at once (< 1 s), and clears its whole cache whenever it (re)subscribes. MongoDB stays
  authoritative: resolved tokens are cached ≤ 5 s per process, so a lost announcement is bounded by that.
- Tokens: API tokens are `wst_` + 32 random bytes, stored as HMAC-SHA256 under a key derived from each
  `SECRET_KEYS` entry (first key hashes, all keys are tried on lookup; unkeyed sha256 without secret keys) + a
  display prefix. A token narrows its owner's grants (scopes → actions, repository patterns → repositories);
  `access.check` applies the narrowing, so no route can forget it. Docker registry tokens are short-lived (5 min)
  JWT (EdDSA) signed with a key in `keys/`, scoped per the Docker token spec; one issued for an API token carries
  its id (`tid`) and the API token is re-checked on every `/v2/` request.
- Credentials for upstreams and LDAP/OIDC secrets are AES-GCM encrypted with `SECRET_KEYS` (first key encrypts,
  all keys decrypt; rotation job re-encrypts).
- Input hardening: path normalisation in one function (`waystation_common.paths`), size limits on every body and
  every parsed metadata document, XML parsing with `defusedxml`, archive extraction with zip/tar-slip checks,
  no `eval`/`pickle`/YAML unsafe load.
- Audit records written for SPEC §4.11 actions via `audit.record(...)` only (`waystation_server.audit`; one call per
  action, request context bound by the middleware). Records are queued to a bounded in-process `AuditWriter`
  (`waystation_common.audit`) and inserted in batches off the request path: auditing never blocks or fails a
  request; overflow is counted and logged at ERROR. Secret values never reach a record (`sanitize`, `diff`).
  Workers use the same writer (actor `system`).

## 8. Events, stats and live activity
- The server emits typed events (`waystation_common.events`): `transfer.started|progress|finished|failed`,
  `cache.hit|miss|stale`, `upstream.state`, `asset.created|deleted`, `job.*`, `worker.*`.
- Two channels with different rates and guarantees:
  - **Live (high rate, no history)**: transfer progress goes to the `transfers` hash (current state, one field per
    transfer, removed on finish + swept by TTL) and is published on pub/sub channel `live` at ≤ 4 Hz per transfer.
    A reconnecting client gets current state by reading the hash, not by replay.
  - **Activity (lower rate, durable-ish)**: started/finished/failed, uploads, deletes, upstream state changes, jobs
    go to the `activity` stream (`XADD`, MAXLEN ~200k ≈ hours-days of history). `Last-Event-ID` resumes on this stream.
- Hot path writes are O(1) Valkey ops (`HSET`, `PUBLISH`, `XADD`, `HINCRBY` per-minute counters, latency histogram
  buckets). **Never write MongoDB per request**: session `last_seen_at`, token `last_used_at` and asset downloads
  (`last_downloaded_at`, `download_count`) are recorded in Valkey and flushed in bulk by the rollup job (≤ 60 s
  staleness acceptable).
- **Rollups** (worker, every 10 s) write only **closed** minutes into `stats_minute` (time-series collections cannot
  upsert, so a minute is written exactly once, after it ends plus a 5 s grace); hour and day rollups run on closed
  hours/days. Latency is stored as fixed log-scale histogram buckets (counts), so percentiles can be recomputed
  for any range at query time; never store or average precomputed percentiles. Download counters per component are
  `$inc`-ed in bulk. `storage_snapshots` are taken hourly by the same job.
- SSE endpoint: each server replica runs one subscriber loop (pub/sub for live, one blocking `XREAD` for activity)
  and fans out to its connected clients, filtering per subscriber (RBAC + topic), with heartbeats every 15 s. The web
  tier's SSE location uses `proxy_buffering off`, `proxy_cache off`, `proxy_read_timeout 1h`, HTTP/1.1 keep-alive.

## 9. Performance rules
- No synchronous I/O in request handlers; hashing of large streams happens incrementally on chunks; CPU-bound
  work > 5 ms goes to `asyncio.to_thread` or a job.
- Cached content never passes through Python (X-Accel). Upstream streaming uses ≥ 256 KiB chunks.
- Every MongoDB query on a request path is covered by an index (CI test runs `explain()` on registered queries).
- Responses for metadata assets carry `ETag` and support conditional GET.
- SPA: route-level code splitting, virtual scroll for long lists, no blocking third-party scripts, hashed
  immutable assets with long cache lifetime, HTTP/2.
- Budgets in `Build/ci/budgets.json` (SPEC §5.1) enforced by `make perf` and the bundle check; `make budgets`
  reports them in CI (warnings until M2).

## 10. Workers and jobs
- Queue transport: Valkey Streams with consumer groups (`jobs:<queue>`), at-least-once delivery, visibility
  timeout via pending-entry reclaim, max attempts then dead-letter (`jobs:dead`). Jobs are **idempotent**.
- Job protocol (`waystation_common.jobs`): typed pydantic payloads, `queue`, `dedupe_key`, `run_after`.
- **Dedupe semantics ("dirty flag")**: if a job with the same key is *pending*, the new request is merged into it; if
  one is *running*, a `rerun` flag is set and the job runs again once after it finishes. So an upload that lands
  during an index rebuild is always picked up.
- **Durable outbox for event-driven jobs**: jobs triggered by a write (e.g. rebuild an apt/yum/helm index after an
  upload) are first written to MongoDB `job_outbox` in the same transaction as the triggering change; a dispatcher
  moves them to Valkey and marks them sent. If Valkey is lost, unsent and in-flight outbox entries are re-dispatched.
  Scheduled jobs are re-derived from `job_definitions`.
- Scheduler: a leader (Valkey lease) per worker deployment enqueues scheduled jobs from MongoDB definitions.
- Queues: `index` (apt/yum/helm index + signing), `maintenance` (cleanup, GC, verify), `stats` (rollups),
  `prefetch`, `import`. `WORKER_QUEUES` selects which a worker consumes.
- Job runs, progress and logs are recorded in MongoDB (`job_runs`) and streamed to the UI via events.

As implemented (T-F08): job types are frozen pydantic models subclassing `waystation_common.jobs.JobPayload`
(`kind`, `queue`, `max_attempts`, `dedupe_key()`); the server enqueues through `JobClient` (`Services.jobs`):
`enqueue_durable(payload, session=s)` writes the outbox inside its transaction, `enqueue(payload)` goes straight to
Valkey. Every queue transition (enqueue/merge/rerun, start, finish/retry/dead, heartbeat) is one Lua script in
`waystation_common.jobs.queue`. The worker (`waystation_worker.jobs.JobSystem`) runs a consumer (`XREADGROUP`,
heartbeat via `XCLAIM JUSTID` every visibility/3, `XAUTOCLAIM` of entries idle > 60 s; attempt = failed attempts +
delivery count, so a lost worker counts; backoff retries via `jobs:<queue>:delayed`) and competes for
`lease:scheduler`, whose holder dispatches the outbox, fires `job_definitions` (`next_run_at` compare-and-set, so a
fire is enqueued once even if two leaders overlap) and promotes delayed jobs. Valkey data loss is detected with the
`jobs:epoch` token. Handlers (`@handles(Payload)`) get a `JobContext` (`progress`, `log`, db, valkey). The stats
rollup (§8) runs as the scheduled job `stats.rollup` (`@every 10s`, queue `stats`). Graceful stop lets running jobs
finish for 8 s; abandoned jobs are reclaimed. Liveness: the heartbeat file is withheld when the consumer loop stalls.

## 11. Format plugin contract
`waystation_server.formats.base.Format` (protocol) with: `name`, `routes()` (mounted under `/repository/<repo>/`
and any format-specific roots such as `/v2/`), `resolve(request, repo) -> Resolution`, `proxy_fetch(...)`,
`hosted_put(...)`, `group_merge(...)` where applicable, `component_from_asset(path, meta) -> ComponentKey | None`,
`setup_snippets(repo, user) -> list[Snippet]`, `config_schema` (pydantic model for format-specific repo options),
`ui_descriptor` (icon, labels, browse renderer hints). Shared helpers for metadata rewriting, checksum
validation and URL building live in `formats/common/`.

As implemented (M0): a format is a `FormatDescriptor` (name, supported types, `options_model` = `config_schema`,
probe path, "Set me up" snippets, UI hints) plus a plugin with `handle(req: FormatRequest) -> Response`, registered in
`waystation_server.plugins`. The router has already resolved the repository, normalised the path and authorised the
request. Proxy formats describe each file as a `proxy.engine.AssetSpec` and return `await req.proxy.serve(req, spec)`
(`ensure`/`read` give them cached metadata to parse); the engine implements §5, §6.1, §6.2 and §6.4 for them.

## 12. Errors and logging
Format endpoints return the format's native error shape (OCI JSON errors, npm JSON errors, plain 404 for file
formats). REST API errors use RFC 9457 problem+json. Logs are JSON lines with `request_id`, `principal`,
`repo`, `format`, `op`, `cache`, `status`, `duration_ms`, `bytes`; never secrets, never tokens, never full
authorization headers.
