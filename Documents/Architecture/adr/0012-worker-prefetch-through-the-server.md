# ADR 0012: Worker prefetch through the server, authorised by a signed system grant

Status: **Proposed** (2026-11-22, T-O12). Needs the owner's approval before merging.

## Context
T-O12 adds prefetch jobs (SPECIFICATION §4.1 "Prefetch"): keep the popular metadata of proxy repositories fresh, and
warm configured lists (docker images, npm packages, Maven artifacts, Go modules, raw files). Batch and scheduled work
belongs to the worker (ARCHITECTURE §1, §10), but everything that makes a proxy fetch correct lives in the server:
the format plugins (a docker tag → index → platform manifests → config and layers; an npm packument → the version's
tarball and its `dist.integrity`; Go checksum-database checks; Maven checksum files), the proxy engine (validation,
digests, SSRF guard, breaker, coalescing with clients, fills that outlive their client, indexing for browse). The
worker may not import the server (ARCHITECTURE §3). The precedent, `apt.refresh` (`waystation_worker.apt`,
`waystation_worker.upstream`), re-implements one format's refresh in the worker; doing that for every format would
duplicate the protocol code and skip coalescing (FORMATS.md says so for apt: "Not coalesced with server fills").

Options:

1. **Re-implement prefetch per format in the worker** (like `apt.refresh`). Duplicates every format's protocol and
   validation, no coalescing with clients, two places to fix every format bug. Rejected.
2. **Move the format plugins and the proxy engine to `waystation_common`**, so the worker runs them. A large move of
   request-shaped code (Starlette responses, X-Accel serving, FastAPI-adjacent types) into the package that must not
   import a web framework (ARCHITECTURE §3). Rejected.
3. **The worker acts as a client of the server's format routes** (`GET /repository/<repo>/<path>`), authenticated
   by a signed grant. The worker still has to know each format's paths and parse its metadata to expand an item
   (a docker index to its manifests and layers), so format knowledge leaks into the worker again; it also needs a
   "refresh" hint on public routes.
4. **The worker asks the server to do it, through one internal route.** The worker decides *what* (which
   repositories, which popular metadata, which warm items) and *when* (schedules, run now, job runs and logs); a
   server handles each item by running the format plugin's own handler on synthetic `GET`s of its own paths, so the
   format expands its items and the engine fetches exactly as for a client.

## Decision
Option 4.

- **Route.** `POST /_internal/prefetch/<repo id>` (`waystation_server.prefetch`; not in the OpenAPI document, not
  routed by the web tier, whose `location /` answers 404). Body `{"op": "warm" | "refresh", "items": [...]}` (at most
  200). Answer: NDJSON, one line per file and per item, a ping every 10 s while it works, a final `done`. Bounded: 4
  items at a time per call, 8 per server process, 5000 requests per item.
- **Who may call it.** Only a request carrying a valid **system grant** in `X-Ws-System-Grant`:
  `<expiry>.<mac>`, HMAC-SHA256 under a key derived from each `WAYSTATION_SECRET_KEYS` entry (label
  `waystation system grant v1`; the first key signs, all verify; `waystation_common.system_grants`) over the purpose
  `prefetch`, the repository id and the expiry (5 minutes, 60 s clock tolerance). The worker signs one per call. The
  decision is in `security.access.system_grant` (the single decision point): a grant for this repository, which is
  an online proxy repository, yields the principal `Principal(kind=system, name='prefetch',
  method=system_grant)` whose grants are `browse` + `read` on that repository by name, nothing else. Everything else
  (no grant, another repository's, another purpose, expired, forged, a hosted or group or offline repository, no
  secret keys) answers 404, like a missing repository. The format routes, `/v2/` and the REST API never read the
  header, so a grant opens nothing there.
- **What the server does.** For `warm`, the descriptor's `warm_item` checks the item and the plugin's
  `warm(ctx, item)` expands it with `ctx.get(path, headers)` (a `FormatRequest` built for that repository, the
  `prefetch` principal and `prefetch='warm'`, passed to `plugin.handle`) and `ctx.read` (the body of a metadata
  answer, from the blob an X-Accel answer points at). For `refresh`, the descriptor's `refresh_request` maps a cached
  metadata asset key to the client request (`npm` packument → `GET <package>` with the matching `Accept`; docker tag
  manifest → `GET v2/<name>/manifests/<tag>`; raw, Maven, Go: the same path) and the engine revalidates it even before
  it is stale (`FormatRequest.prefetch == 'refresh'`, metadata only). Streamed content fills are read and dropped by
  the server (the fill is what matters); content never passes to the worker.
- **Not downloads.** For a `FormatRequest` with `prefetch` set the engine and the hosted store record no
  `asset_downloaded` (no `download_count`/`last_downloaded_at`, so prefetch never makes anything popular) and no
  client request counters (traffic charts, hit ratio). Upstream counters and live transfers stay (principal
  `prefetch`).
- **Where workers reach a server.** `WAYSTATION_INTERNAL_URL` (default `http://waystation-server:8000`, the bundled
  stack's network alias). Split hosts: a server host's published server port (`WAYSTATION_SERVER_BIND`), which is
  firewalled to the web and worker hosts. It is operator configuration, not user input, so the worker calls it
  without the SSRF guard; redirects are not followed.

## Consequences
- Formats own prefetch: a new format opts in with `warm_item`/`refresh_request` on its descriptor and `warm` on its
  plugin (FORMATS.md "Prefetch"); the worker never learns a format. Docker, npm, raw, Maven and Go do now.
- Prefetch shares fills with clients (coalescing): a client pulling a layer the warm job is fetching follows that
  fill instead of starting a second upstream download, and vice versa.
- ARCHITECTURE §1 gains one interaction (worker → server over HTTP, only for prefetch) and §7 one more way a request
  is authorised (the system grant), still decided by `security.access`. The worker needs `WAYSTATION_SECRET_KEYS`
  and a reachable server for prefetch; without them the prefetch jobs skip with that reason, nothing else changes.
- A server busy with prefetch serves fewer clients: the per-process bound (8 items) and the worker's bound (2
  repositories at a time for refresh) keep it a background load; the jobs run on the `prefetch` queue, which an
  operator can move to dedicated workers (`WAYSTATION_WORKER_QUEUES`).
- A leaked grant lets its holder make a server fetch into that one proxy repository's cache for up to 5 minutes
  (what anonymous reads of a proxy can usually do anyway); it cannot read anything back (the answer carries only
  paths, statuses and counts), write, or reach another repository.
- `apt.refresh` stays as it is (it also re-verifies the pool index, which is apt-specific worker work); apt could
  move to this route later.
