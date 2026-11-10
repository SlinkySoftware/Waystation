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
- **Request coalescing**: concurrent requests for the same uncached (or stale) asset cause one upstream fetch, across
  every server replica; followers stream from the partially written file as it grows (same host, local filesystem)
  or are served from the cache once it is committed (ARCHITECTURE §6.3). Followers are reported as `COALESCED` and
  the owner's live transfer shows how many follow it. A failed or mismatched fetch fails its followers too; if the
  owner goes away (its client disconnects, its replica dies) a follower takes the fetch over.
- **Freshness**: `metadata_max_age` (default 30 min, per repo) and `content_max_age` (default: forever for
  immutable assets). Revalidate with `If-None-Match` / `If-Modified-Since` when the upstream supports it.
- **Negative cache** of 404s (default 10 min, per repo).
- **Serve stale on error**: if the upstream errors or times out and a cached copy exists, serve it and flag the
  repo "degraded" (default on). **Offline mode** per repo: never contact the upstream.
- **Auto-block** (per repo `auto_block`: on by default, 5 failures, 30 s doubling to 30 min): after N consecutive
  upstream failures (connection or DNS failure, timeout, reset mid-body, 5xx, 429; 4xx, digest mismatches and SSRF
  refusals do not count) stop calling it for an exponential backoff period: requests are answered from the cache
  (stale-on-error) or with 503 + `Retry-After`. When the period ends, one request (or the server's background prober,
  using the format's probe path) probes the upstream: success lifts the block, failure doubles the period up to the
  maximum. Shown in upstream health (status `blocked` with `blocked_until`, a countdown in the UI), `upstream.state`
  events, activity and metrics; saving the repository clears a block.
- Upstream credentials per repo (basic, bearer, Docker Hub token login, GHCR PAT), stored encrypted.
- Upstream TLS: verify by default; per-repo custom CA bundle (`ca_pem`, added to the system CAs and
  `WAYSTATION_EXTRA_CA_FILE` for that repo only); optional HTTP proxy per repo (`http_proxy`, no credentials) or
  global (`WAYSTATION_HTTP_PROXY` / `WAYSTATION_NO_PROXY`, credentials allowed). Through a proxy the SSRF guard
  checks every hop's target before the request; a repo with `allow_private_upstream` may use names only the proxy
  resolves.
- Respect and surface upstream rate limits (Docker Hub `ratelimit-remaining`, `Retry-After`); HEAD where the
  protocol allows to avoid consuming quota.
- Content validation: verify digests/checksums the protocol provides (OCI digests, npm `integrity`, PyPI
  `#sha256=`, Maven `.sha1/.sha256`, apt `Packages` hashes, yum `primary.xml` checksums, Go sumdb) before an asset
  is committed to the cache. Mismatch = not cached, client gets 502, event logged. Content streamed to the client
  during the fetch (tee) has its status already sent: the last chunk is held back until the bytes are verified and a
  mismatch cuts the connection, so the client never receives a complete wrong file (an incomplete transfer instead
  of a 502). A `HEAD` for content that is not cached is answered from an upstream `HEAD` without downloading.
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
  Now (T-P2-DOCKERH): docker hosted repositories accept `docker push`/`buildx --push` (multi-platform indexes with
  attestations), skopeo, crane, oras and cosign: chunked, monolithic and resumable blob uploads verified by sha256,
  cross-repository mounts from any docker repository the client may read (proxy caches included), manifest validation
  (media types, referenced blobs/manifests present with the stated sizes, 4 MiB), write policy (`allow_redeploy`
  default, `disable_redeploy`, `read_only`) and immutable tag patterns, delete by tag, digest and unreferenced blob,
  the referrers API (with `artifactType` filter), `tags/list` and `/v2/_catalog` (filtered by RBAC). Pushes need
  `write`, deletes `delete`; pushed manifests and deletes are audited. The OCI distribution-spec conformance suite
  passes (pull, push, content discovery, content management). A hard quota (§4.8) refuses blob uploads, mounts and
  manifest pushes with `403 DENIED` and a message naming the quota; the low-disk guard answers `507`.
- PyPI (T-P5-PYPI): hosted repositories accept twine and `uv publish` (the legacy upload API, `POST` to the repository
  URL, multipart streamed into the blob store): wheels and sdists whose core metadata (read from the archive in a
  thread, bounded, zip/tar-slip checked) agrees with the file name and form, `sha256_digest` verified; a wheel's
  `METADATA` is also served as its PEP 658 `.metadata` file. Write policy as for docker (`disable_redeploy` accepts
  the same bytes again and refuses other content with "File already exists"). Project pages (PEP 503 HTML and PEP 691
  JSON) are rendered from the stored files per request; releases can be yanked (PEP 592) from Browse or
  `POST /api/v1/repositories/{name}/pypi/yank` (`write`). Uploads need `write`; uploads and yanks are audited.
- npm (T-P5-NPMH): hosted repositories accept `npm publish` (one version and its base64 tarball per request, ≤ 100
  MiB; `dist.integrity`/`shasum` and the tarball's own `package.json` checked against the version; the tarball read
  in a thread without extraction, bounded), and generate their packuments (full and abbreviated, `ETag`/`304`) from
  the stored versions after every change. `npm deprecate`, `npm unpublish <pkg>@<version>` and `--force` (needs
  `delete`), `npm dist-tag add/rm/ls` (`write`; `latest` cannot be removed) and `npm search` work. Write policy as for
  docker; `disable_redeploy` answers npm's "You cannot publish over the previously published versions". Publishes
  are audited `content.upload`, deprecations and tag changes `content.update`, unpublishes `content.delete`.
- Raw: `curl --upload-file` (`PUT`) and `DELETE` on any path; `X-Checksum-Sha256`/`-Sha1`/`-Md5` headers verified
  before anything is stored; write policy as above (`disable_redeploy`: a path is written once, 409; deletes stay
  allowed); directory listings (HTML, or JSON for scripts) at the root and any path ending in `/`.
  Now (T-P2-RAW): built as described; uploads need `write`, deletes `delete`; both are audited and live in the
  activity feed.
- Helm: `curl --upload-file` (`PUT`) and the ChartMuseum API (`POST api/charts`, as `helm cm-push` sends it);
  provenance files; delete by file or `api/charts/<name>/<version>`.
  Now (T-P5-HELM): chart packages are checked before they are stored (single top-level directory, no links or
  unsafe names, size limits, `Chart.yaml` with a valid name and SemVer version) and stored as
  `<name>-<version>.tgz`; write policy as above; the worker rebuilds `index.yaml` from the stored charts through the
  outbox (seconds after an upload or delete) and publishes it atomically. Uploads and deletes are audited.
- Ansible Galaxy: `ansible-galaxy collection publish` (multipart to `api/v3/artifacts/collections/`, answered with an
  import task the client polls at `api/v3/imports/collections/<task>/`); delete a version or a whole collection.
  Now (T-P5-GALAXY): the upload (`<namespace>-<name>-<version>.tar.gz`, ≤ 256 MiB) streams into the blob store and
  must match its `sha256` field; the import task and its `import.galaxy` job are written in one transaction
  (outbox); the worker validates the archive without extracting it (tar-slip guards, `MANIFEST.json` naming the
  same collection version, `FILES.json` matching the manifest checksum, every file's sha256, no unlisted files) and
  only then lists the version. Versions are immutable (409; delete first); `read_only` refuses (403). Uploads and
  deletes are audited.
- Index generation for apt and yum hosted repos by workers, signed with a per-repo GPG key (key generated or
  imported; public key downloadable from the repo root and shown in "Set me up").
- Quotas per repo (soft warn / hard block) on stored bytes (§4.8).
  Now (T-P5-MAVEN): Maven hosted repositories accept `mvn deploy`, Gradle `maven-publish` and curl `PUT` (live upload
  transfers after the hard-quota check), with a version policy (`release` / `snapshot` / `mixed`), a layout policy
  (`strict` Maven paths / `permissive`) and the write policy (`disable_redeploy` never replaces a release file;
  SNAPSHOT versions always redeploy); refusals answer `400`, `read_only` `403`. Uploaded checksum files are compared
  with the stored file and not stored (checksums are generated on request); `maven-metadata.xml` is regenerated by
  the server after every deploy and delete, serialised per groupId:artifactId, so concurrent deploys lose nothing.
  Files and version folders can be deleted (`delete`). Uploads and deletes are audited.

### 4.3 Group repositories
- Ordered members of the same format (proxy and hosted mixed). Reads resolve first match in order; metadata that
  must be merged is merged (npm packuments, PyPI simple pages, Maven `maven-metadata.xml`, Helm `index.yaml`,
  Go `@v/list`, docker tag lists). apt/yum groups are not supported (index merging + signing is out of scope).
- Docker group may have a **writable member** so `docker push` to the group lands in that hosted repo.
  Now (T-P2-DOCKERG): docker groups (up to 50 docker proxy/hosted members, no nested groups) answer manifests from the
  first member that has them (a proxy member asks its upstream), blobs from any member's stored or cached copy first
  (a blob `HEAD` never goes upstream), merged `tags/list` and referrers; pushes, upload sessions and deletes go to the
  writable member (a hosted repository), which the client must be allowed to write too; without one the group is
  read-only. Reading through a group needs `read` on the group only (its members' content is served through it,
  as in Nexus). A repository that is a group member cannot be deleted until it is removed from the group.
  Now (T-P5-MAVEN): Maven groups (proxy and hosted members, in order) answer files from the first member that has
  them (404 and failing members skipped; members whose version policy excludes the version not asked) and merge
  `maven-metadata.xml` per request (versions union, `latest`/`release` recomputed in Maven order, newest SNAPSHOT
  build, plugin prefixes; checksum files from the same bytes); they are read-only (405).
  Now (T-P2-RAW): raw groups (raw proxy/hosted members) answer each file from the first member that has it, list
  folders as the members' listings merged, and send uploads and deletes to the writable member (a hosted raw
  repository the client may also write to); without one the group is read-only.
  Now (T-P5-HELM): Helm groups serve one `index.yaml` merged from their members' indexes (the first member that
  lists a chart version wins it), charts from the first member that has them, and send uploads and deletes to the
  writable member.
  Now (T-P5-GALAXY): Ansible Galaxy groups merge their members' collection version lists (the first member that has
  a version wins it), answer version documents and artifacts from the first member that has the version and roles
  from the first proxy member, and send publishing (and import-task polling) to the writable member.
  Now (T-P5-PYPI): PyPI groups merge their members' project pages per request (union by file name, the first member
  wins; a proxy member's cached page, refreshed as needed), serve files from the first member holding them (else the
  proxy members in order) and send uploads to a hosted writable member.
  Now (T-P5-GO): Go groups (Go proxy members) merge `@v/list` per request (union, newest first, `ETag`/`304`), answer
  `.info`/`.mod`/`.zip` from any member's cached copy first, then `@latest`, queries and misses from the members in
  order, and the checksum database from the first member that proxies it.
  Now (T-P5-NPMH): npm groups (npm proxy and hosted members) merge packuments in both representations (versions
  union, the first member that has a version wins it; dist-tags from the first member that has the tag; tarball URLs
  rewritten to the group), kept in the blob store and served with `ETag`/`304` until a member changes; tarballs come
  from the members in order (so the member that won a version serves it); `npm publish`, `deprecate`, `unpublish`
  and `dist-tag` through the group go to the hosted writable member (`?write=true` reads answer that member's own
  packument); without one the group is read-only.

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
  scraper's network) or an API token with the `metrics` scope whose owner holds `stats-read`
  (`Authorization: Bearer`); everyone else gets 404. The client address
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
| npm | yes | yes (P5, built) | yes | 2 (proxy) |
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
  Now (M0, T-S09 min): `GET /api/v1/repositories/{name}/browse?path=` lists the cached assets of any repository as
  folders (first) and files with size, type, sha256 and last-modified, paged by cursor; npm `packages`/`versions`,
  apt `suites`/`packages` (per suite, architecture, component; cached-only filter; `verified` = the cached file
  matches its listing), docker `images`/`tags`/`manifest` (platforms, layers with cached flag and history,
  labels, pull commands) and Maven `artifacts`/`versions` (T-P5-MAVEN: groupId:artifactId with the highest version,
  versions in Maven order with file count and size; the files are the version folder's listing) show what is held.
  Browsing needs `browse` on the repository (anonymous: proxy repositories); download counts are not recorded yet
  (0/null). Helm (T-P5-HELM): `helm/charts` and `helm/versions?chart=` list charts (latest version, app version,
  description) and their versions newest first (created, digest, size, cached) for proxy, hosted and group
  repositories. Ansible Galaxy (T-P5-GALAXY): `galaxy/namespaces`, `galaxy/collections?namespace=` and
  `galaxy/versions?namespace=&collection=` (records in `galaxy_collections`: hosted imports, a proxy's cached version
  lists) show namespaces, collections (latest version, description, `requires_ansible`) and versions newest first by
  SemVer (sha256, size, dependencies, publisher, cached) for proxy, hosted and group repositories. PyPI (T-P5-PYPI): `pypi/projects` (summaries in
  `pypi_packages`: latest final release by PEP 440, version count, files held) and `pypi/versions?project=` (versions
  newest first, each with its wheels and sdist: size, sha256, requires-python, yanked, PEP 658 metadata, held or not,
  uploader for hosted files) for proxy and hosted repositories; groups are browsed through their members. Go
  (T-P5-GO): `go/modules` (summaries in `go_modules`: latest by Go semver, tagged versions, cached versions, zip bytes)
  and `go/versions?module=` (cached list and cached files merged, newest first in semver order with pseudo-version,
  pre-release and `+incompatible` flags, commit time from the `.info`, the `.info`/`.mod`/`.zip` files held) for
  proxy repositories; search finds modules by path. Terraform (T-P5-TFP): `terraform/providers` (summaries in
  `terraform_providers`: provider address, latest release, versions listed, archives cached and their size) and
  `terraform/versions?provider=` (registry-listed and cached versions newest first in SemVer order, with protocols,
  platforms and the archives held: platform, size, sha256, `h1:`, downloads); search finds providers by address
  (and by type, the last segment).
- Global search across repositories the principal can read: name (prefix and substring), version, format,
  repository, digest/checksum lookup ("where is this sha256 used?"), with facets (format, repo, type).
  Target: results in < 150 ms p95 for 1M assets.
  Now (T-U06, 2026-11-09): `GET /api/v1/search?q=&format=&repository=&kind=&type=&cached=&cursor=&limit=` searches
  only the repositories the principal may `browse` (`access.filter_repos`; a hidden repository never appears in a
  hit, a facet value or a count, and naming it as a filter yields an empty result). `q` is
  `<name prefix>[ <version prefix>]`, case-insensitive, matched on the full name or its last `/` segment (`nginx`
  finds `library/nginx`, `node` finds `@types/node`); empty lists everything. Sources: docker tags (kind `image`),
  npm packages, PyPI projects and apt/yum index listings (kind `package`; `cached` says whether the listed file is in the cache),
  and cached files of every format but docker (kind `file`). `cached=true` leaves out the apt/yum listings. A digest
  (`sha256:<8-64 hex>` or 64 hex digits) finds every cached file with that blob and the docker tags pointing at it,
  in any visible repository. Results come straight from per-collection `(name_lc, repo_id, _id)` indexes, merged
  with exact keyset cursor paging; facet counts (format, repository, kind, repository type) are disjunctive (each
  ignores its own filter), on the first page only, and lower bounds when `capped`. Every hit carries a Browse deep
  link. Substring (non-prefix) matching and the 1M-asset p95 measurement are still to do.
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
- **Cleanup policies** (named, reusable, attached to repos in run order; a policy is for one format or `*`):
  criteria `last_downloaded_older_than_days` (never downloaded = since cached), `published_older_than_days`,
  `keep_latest_n_versions` (per component, in the format's version order: semver for npm, dpkg for apt, rpm for yum,
  PEP 440 for PyPI (per project; every file of a release, wheels, sdist and `.metadata`, shares its rank); raw: newest
  cached files per folder), name/version regex include/exclude, `prerelease_only`, docker
  `untagged_older_than_days`. Every criterion that is set must match. Docker policies select tags (image = name, tag =
  version, newest by recency); removing a tag also removes the manifests only it reached and the layers no remaining
  manifest uses. Only content is removed, never metadata (indexes, packuments). Every policy has a **dry-run
  preview** (counts, bytes, per repository, paged candidates with the reason) computed by the same code a run uses.
- Policies run on a schedule (cron per policy, or manually) as worker jobs with progress in the job history; they
  delete asset/tag/manifest records, never blobs. Downloads are recorded in Valkey on the request path and flushed to
  the assets in bulk (≤ 10 s), feeding "last downloaded" and download counts.
- **Blob GC**: mark-and-sweep in workers (every 6 h by default, or on demand); a blob is deleted only if unreferenced
  and unclaimed for a grace period (default 24 h), so in-flight uploads/fetches are never broken: a cache fill claims
  its blob before putting it in place, and a claim either stops the deletion or waits for it and writes the file
  again. Orphan upload files older than 24 h are removed; untracked blob files are adopted; referenced blobs whose
  file is missing are counted (proxies re-fetch them; the `blob.verify` task, §8.5, names the affected assets and
  reports hosted losses). Freed bytes show in the activity feed and `/metrics`.
- Docker: untagged manifest cleanup through cleanup policies; layer GC via blob GC.
- Quotas per repo (§4.2) and a global low-disk guard (refuse new cache writes below a free-space threshold,
  keep serving hits, alert).
  Now (T-O03):
  - **Quota** per proxy or hosted repository (`storage{quota_bytes, quota_mode}` in the REST API and the repository
    form; groups store nothing, so a quota on a group is a 422 field error). **Usage** is the sum of the sizes of the
    repository's assets: a blob shared with other repositories counts in **each** of them (de-duplication saves disk,
    not quota). It is counted incrementally on every asset write and delete (one `$inc` on `repo_usage`) and
    recounted from `assets` every hour by the worker job `quota.reconcile`, which repairs drift. Repository views
    carry `quota{quota_bytes, mode, used_bytes, used_ratio, state (ok / warning from 80 % / exceeded at 100 %)}`;
    the repository list and detail show usage against the quota.
  - Both modes announce reaching 80 % (`quota.warning`) and 100 % (`quota.exceeded`) once in the activity feed (kind
    `storage`) and on `/metrics`; falling back below a threshold re-arms it. `soft` only warns. `hard` also refuses
    new writes once the quota is reached (or when an upload's announced size would exceed it): hosted uploads get the
    format's native error (docker: `403 DENIED`, "repository ... is over its quota"); proxy cache fills of content
    are **served to the client without being stored** (verified against the expected digests and size like a normal
    fill; response header `X-Ws-Not-Stored: quota`), so a client request never fails because of a quota. Metadata
    (indexes and other small documents a repository needs to work) is still cached. Cached content is always
    served, and deletes always work.
  - **Low-disk guard**: `GET/PUT /api/v1/storage/guard` (`settings-admin` to change; audited `settings.update`):
    `min_free_bytes` and `min_free_percent` (0-90) of the data filesystem; the floor is the larger of the two
    (default `WAYSTATION_MIN_FREE_GB`). Below it the blob store refuses new writers: proxy fills (content and
    metadata) are passed through uncached (`X-Ws-Not-Stored: low_disk`), uploads get `507`. Every server replica
    picks up a change within 5 s. The worker job `storage.check` (every minute) announces `storage.low` /
    `storage.ok` once per change; the UI shows a banner while the guard is tripped and the Storage page shows and
    edits the guard.

### 4.9 Authentication
- **Local** users (argon2id), optional TOTP 2FA.
  Now (T-O08): a local account with a password can turn on two-factor sign-in (RFC 6238 TOTP: HMAC-SHA1, 6
  digits, 30 s steps, one step of clock drift either way). Set-up (profile page; needs an interactive session and
  the current password): `POST /auth/me/totp/setup` returns a new 160-bit secret once, as base32 and as an
  `otpauth://totp/<instance>:<username>` URI that the UI draws as a QR code in the browser; it stays pending
  (encrypted) for 15 minutes; `POST /auth/me/totp/confirm` with a code from the app turns it on and returns 10
  single-use recovery codes (`xxxxx-xxxxx`, about 50 bits each) once. The secret is stored AES-GCM encrypted
  (`SECRET_KEYS`), recovery codes only as keyed hashes. `POST /auth/me/totp/recovery-codes` (a TOTP code) replaces
  the recovery codes; `POST /auth/me/totp/disable` (current password and a TOTP or recovery code) turns it off;
  `GET /auth/me/totp` shows the state. Sign-in becomes two steps: a correct password answers `202` with
  `second_factor: totp` and a pending sign-in (not a session: `login_challenges`, 5 minutes, bound to the browser
  by the HttpOnly, SameSite=Strict cookie `ws_mfa`); `POST /auth/login/totp` with a code or a recovery code
  then creates the session. A TOTP code is accepted once per step per user (Valkey, and `users.totp_last_step` so
  that MongoDB alone still refuses replays when Valkey is down; an earlier step after a later one is refused too),
  a recovery code once. Wrong codes count towards the account lockout, and the password step does not reset the
  failure count while the second factor is pending; after 5 wrong codes the pending sign-in ends (problem code
  `second_factor_expired`); the second step is rate limited like sign-in. The password of an account with
  two-factor sign-in is refused outside the UI (`docker login`, audit reason `totp_required`): package clients
  use API tokens. LDAP and OIDC accounts are never asked (their provider owns their sign-in). An administrator
  holding `user-admin` resets a user's second factor (lost phone) with `DELETE /users/{id}/totp` (not their own,
  not an account holding more than they do). Settings → "Require two-factor sign-in for local administrators"
  (`security.require_totp_for_admins`): a local administrator without it gets sessions that may only read
  `GET /auth/me`, set it up and sign out (403 `totp_setup_required` otherwise; `Session.totp_setup_required`;
  the UI keeps the user on the profile page), and `docker login` with the password is refused
  (`totp_setup_required`). A pending one-time password is changed first.
- **LDAP / LDAPS / StartTLS**: multiple servers with failover, service bind, user search base + filter, attribute
  mapping (uid, name, email), group discovery (memberOf or group search), nested groups optional,
  "require group" gate, custom CA file. Tested against FreeIPA and Active Directory schemas.
  Now: providers are configured under Administration → Authentication providers (`/api/v1/auth-providers`, needs
  `settings-admin`), several allowed, tried in `order`. Servers are `host[:port]` or `ldap(s)://` URLs, tried in
  order; one that cannot be reached or fails TLS is skipped for 30 s. Security `ldaps`, `starttls` or `none` (clear
  text, testing only); certificates and host names are always verified (custom CA bundle in PEM, else the system
  store). The service-bind password is write-only and encrypted (`bind_password_set` on reads). The user is found
  with `(&<filter>(<username attribute>=<escaped name>))` under the user base (exactly one entry), then bound as
  itself on a separate connection (empty passwords refused); the directory's own lock/disable decides (or the user
  filter). Groups come from `memberOf` (any attribute name) or a group search (`member`/`uniqueMember` by DN, or
  `memberUid` by username), optionally nested (walked breadth first, ≤ 500 groups, ≤ 10 levels); "require group"
  matches a group DN or name, case-insensitively. "Test connection" checks every server (connect, TLS, bind, search
  bases); "Test user" shows the entry, groups, the gate and the mapped roles, and checks a password when given. All
  LDAP I/O runs in a thread pool with connect/response timeouts. LDAP passwords work wherever local passwords do:
  the UI sign-in (the sign-in page shows a provider choice when there is more than one) and `docker login`; other
  format clients use API tokens. Successful checks are reused for `cache_ttl_s` (default 60 s; never for the UI
  sign-in) under an HMAC of provider, configuration version, username and password (the password is never stored).
- **OIDC**: authorization code + PKCE, discovery URL, client secret, scopes, claim mapping (username, email,
  groups), group claim → Waystation groups, auto-provision on first login (configurable), RP-initiated logout.
  Multiple providers allowed; login page shows a button per provider.
  Now: OIDC providers sit next to LDAP under Administration → Authentication providers (kind `oidc`): issuer
  (discovery at `<issuer>/.well-known/openid-configuration`, which must name exactly that issuer; cached an hour),
  client id, write-only encrypted client secret (`client_secret_basic`, `client_secret_post` or a public client),
  scopes (default `openid profile email`), username / display name / email / groups claims (dotted paths), an
  optional require-group gate, auto-provision, optional userinfo, RP-initiated logout, "allow private addresses"
  (SSRF guard), signing algorithm allowlist (asymmetric only: RS/PS/ES/EdDSA; `none` and HMAC refused) and timeout.
  The form shows the redirect URI (`<base>/auth/oidc/<provider id>/callback`) and post-logout URI to register, and
  "Test discovery" checks the issuer, endpoints, JWKS and (for confidential clients) the client credentials. The
  sign-in page shows "Sign in with <provider>" per enabled OIDC provider. `GET /auth/oidc/<id>/login` sends the
  browser to the provider with a fresh `state`, `nonce` and PKCE S256 challenge (flow record in Valkey for 10 min,
  single use, bound to the browser by an HttpOnly `ws_oidc` cookie; starts rate limited per address). The callback
  checks the binding and RFC 9207 `iss`, exchanges the code, and verifies the ID token: signature with a JWKS key
  (cached an hour, refetched when an unknown `kid` appears, at most every 10 s), `alg` in the allowlist and matching
  the key, `iss`, `aud`/`azp`, `exp`, `iat`, `nbf`, `nonce`, `sub` (60 s leeway). Claims become an external identity
  (`oidc:<provider>`), linked with the same rules as LDAP; the groups claim feeds group sync and the group mapping.
  Failures go back to the sign-in page with `?error=oidc_<reason>` (details only in the log). The session remembers
  the provider and (encrypted) the ID token, so sign-out returns the provider's end-session URL with
  `id_token_hint` when RP-initiated logout is on. All IdP calls use the pooled upstream client, the SSRF guard, no
  redirects, size bounds and the provider timeout. OIDC users use API tokens for package clients. Tested against
  Keycloak (`make test-integration`) and authentik (opt-in, `make test-integration-authentik`).
- Identity linking: one Waystation user per username across providers (never link to an account that has a
  usable local password). Now: a local account with a password is checked only against that password, whatever
  provider the client picks; a successful external sign-in creates the user when there is none (`source`
  `ldap:<provider>` / `oidc:<provider>`; OIDC only when its auto-provision toggle is on), links a password-less local account (which takes the
  provider as source) or a user of another provider (`identities[]` lists every linked external account), and
  refuses disabled users. Group sync: the groups the provider reported replace that provider's entries in
  `external_groups` on every sign-in through it.
- **API tokens**: per user and per service account. A token is `wst_` + 43 random characters, shown once at
  creation; only a keyed hash (HMAC-SHA256 under a key derived from `SECRET_KEYS`, every configured key tried on
  lookup) and a display prefix (`wst_` + 8) are stored. Scopes `read`, `write`, `delete`, `manage`, `stats-read`,
  `metrics`, `admin` (a token can only carry scopes its owner holds) and optional repository patterns (RBAC scope
  syntax: name, glob, `format:`, `type:`) only ever **narrow** the owner's current RBAC grants. Optional expiry
  (UI presets 7/30/90/365 days or none). Last used time and client address are recorded in Valkey, at most once a
  minute per token, and flushed to MongoDB by the worker. Revoking a token (or disabling/deleting a service
  account, or changing its roles) is announced on the Valkey `revocations` channel and refused by every replica
  within a second; resolved tokens are cached per process for at most 5 s. Rejected tokens are counted per client
  address (`TOKEN_FAILURE_LIMIT` per 5 min, then 429). Presented but invalid credentials are refused (401), never
  downgraded to anonymous. Creating a token needs an interactive session (a token cannot mint tokens).
- **Service accounts**: non-human principals holding built-in roles, managed by `user-admin`
  (`/api/v1/service-accounts`, tokens under `/service-accounts/{id}/tokens`); they authenticate only with API
  tokens. Only administrators may give a service account `admin`. `/api/v1/tokens` lists and revokes every token
  (incident response).
- **API token transport** for format clients on `/repository/*` and the API: `Authorization: Bearer <token>`,
  `Authorization: Token <token>`, or HTTP basic with the token as the password (any username). Basic credentials
  whose password is not an API token are refused: account passwords (local or LDAP) are accepted only by the UI sign-in,
  `docker login` and `npm login` (which exchanges them for an API token).
- **Format client auth**: docker (Bearer token service at `/auth/token` per Docker token spec; `docker login` with
  username + local password or an API token as the password, any username; a registry token issued for an API token
  carries its id and is re-checked on every request, so revocation is immediate. `/v2/` accepts only these
  5-minute EdDSA tokens and answers everything else with the `401` Bearer challenge naming the scope; a token's
  grants are re-checked against RBAC on every request), npm (`npm login --auth-type=legacy` and `_authToken` = API token;
  now (T-P5-NPMH): the legacy login exchanges the password, checked like `docker login`, for a new API token of the
  user with the repository scopes they hold, no repository pattern and no expiry, listed and revocable on the profile;
  it needs `read` on the repository and a repository the caller could not see answers any failure like a missing
  repository (404, or the 401 challenge without credentials);
  `npm logout` revokes it), PyPI/twine
  (basic: username + token), Maven (basic in `settings.xml`, username + API token; Maven, Gradle and sbt send it
  after the 401 challenge, ADR 0005; preemptive authentication is optional),
  Helm classic repos (basic: `helm repo add --username --password`), Terraform/OpenTofu (`credentials` block → `Authorization: Bearer`), Ansible Galaxy
  (`Authorization: Token <api-token>`), Swift (`swift package-registry login`: basic or token), NuGet
  (`X-NuGet-ApiKey` for push, basic for read), apt/yum (basic auth in URL or `auth.conf`/`.repo`), Go (`.netrc` basic;
  the go command sends credentials over HTTPS only).
  Each format's auth is part of its Definition of Done (FORMATS.md).
- **Challenge on format routes** (ADR 0005): on `/repository/<repo>/...`, a request that carries **no credentials**
  (no `Authorization` header, no valid session) for a repository anonymous cannot see answers `401` with
  `WWW-Authenticate: Basic realm="Waystation"` (plain-text body) instead of `404`, so clients that send credentials
  only after a challenge (Maven, Gradle, sbt, Helm, pip with a keyring) work with plain credentials, like with
  Nexus. A repository that does not exist answers exactly the same (status, headers, body), so anonymous callers
  cannot enumerate repository names; a visible repository the anonymous principal may not act on gives the same
  answer too. A request with credentials keeps `404` for a repository it cannot see. `/v2/` (Bearer challenge), the
  REST API, search and browse are unchanged.
- Anonymous access: a configurable `anonymous` principal with its own roles (default: read on proxy repos only).
  Now: `GET/PUT /api/v1/anonymous-access` (`user-admin`) sets its roles: the built-in `anonymous` role and/or roles
  holding repository permissions only (no global action); empty = no anonymous access. Applies to the REST API,
  format routes and Docker's anonymous registry tokens on every replica within moments.
- Sessions: HttpOnly, Secure (https base URL), SameSite=Lax cookie `ws_session`; a new session on every sign-in;
  stored as a sha256 of the token; CSRF on unsafe methods (double submit: readable cookie `ws_csrf`, header
  `X-CSRF-Token`; bearer requests exempt); idle (`SESSION_IDLE_MINUTES`, default 8 h) and absolute
  (`SESSION_MAX_HOURS`, default 7 days) timeouts; login rate limiting per client address (`LOGIN_RATE_LIMIT` per
  5 min, then 429 + `Retry-After`); account lockout after `LOGIN_LOCKOUT_FAILURES` failures for
  `LOGIN_LOCKOUT_MINUTES` (local only). Sign-in failures never say which part was wrong.
- First administrator: `init` creates local user `admin` (role `admin`) when there are no users, with
  `BOOTSTRAP_ADMIN_PASSWORD[_FILE]` or a one-time password printed once to the init container's stderr.
- **One-time passwords**: the generated bootstrap password and a password an administrator sets (by default) carry
  `must_change_password`. Such a session may only read `GET /auth/me` (which says so), change the password and sign
  out; everything else answers 403. `docker login` with a one-time password is refused. The UI keeps the user on the
  profile page until it is changed.
- **Own account** (any signed-in user): `POST /auth/me/password` (local accounts; needs an interactive session and
  the current password; rate limited like sign-in; at least 12 characters, not the username, not the current one;
  ends the user's other sessions), `PATCH /auth/me` (display name, e-mail of a local account; providers overwrite
  theirs at sign-in), `GET /auth/sessions` (newest first: created, last used, address, browser, OIDC provider,
  which one is current; ids are derived from the stored session id and never usable as cookies),
  `DELETE /auth/sessions/{id}` (also the current one, clearing its cookies) and `DELETE /auth/sessions` (every other
  session), `GET /auth/me/permissions` (effective permissions, narrowed by the API token used).

### 4.10 RBAC
- **Permissions** are `(action, scope)`: actions `browse`, `read` (download/pull), `write` (upload/push),
  `delete`, `manage` (repo settings, cleanup, run tasks), plus global `admin`, `user-admin`, `audit-read`,
  `stats-read`, `settings-admin`.
- **Scopes**: `*`, a repository name, a repository name glob (`docker-*`), a format (`format:npm`), a repository
  type (`type:proxy`). Global actions are granted only with scope `*`; `write`, `delete` and `manage` imply `browse`
  and `read` on the same scope. A repository the principal can neither browse nor read is hidden (404 everywhere;
  one exception, §4.9 "Challenge on format routes": a request without credentials on `/repository/<repo>/...` gets
  the 401 Basic challenge, for a missing repository too).
- **Roles** bundle permissions; built-ins: `admin`, `reader` (browse+read all), `deployer` (read+write on scope),
  `viewer` (browse + stats), `anonymous` (browse + read on `type:proxy`). Custom roles via UI/API.
  Now: built-in roles are read-only; custom roles (`/api/v1/roles`, `user-admin`) have a slug id (lower-case letters,
  digits, `-`), a name, a description and 1-100 permissions `(action, scope)` (global actions only on `*`). A role
  still held by a user, group or service account cannot be deleted (409); `GET /roles/{id}` shows who holds it.
  `GET /roles` (any signed-in principal) lists the grantable roles with their permissions.
- **Groups** (local, or mapped from LDAP groups / OIDC claims) hold roles; users hold roles directly or via groups.
  Now: a provider's group mapping (external group DN or name → roles) is stored as one group per row; a user gets
  the roles of the mapped groups among those reported at its last sign-in through that provider. Mapping changes
  apply to signed-in users within seconds; membership changes on the next sign-in. Local groups are managed under
  `/api/v1/groups` (`user-admin`: create, rename, describe, roles, delete; members added and removed with
  `/groups/{id}/members`); mapped groups are listed there read-only except for their roles (the same documents as
  the provider's group mapping). Administration → Groups manages both (T-U08).
- **Users** (`/api/v1/users`, `user-admin`): list (username prefix, source, disabled, local group, direct role;
  keyset paging), detail (source, linked identities, local and mapped groups, the groups a provider last reported,
  effective roles, lockout), create a local user (with a password, or without one so that an LDAP/OIDC sign-in of
  the same username links to it), update (display name, e-mail, disabled, roles, local groups), delete (sessions end,
  API tokens are revoked), reset a local password (sessions end; one-time by default), unlock, and the effective
  permissions (`/users/{id}/permissions`, also `/service-accounts/{id}/permissions`: roles with where they come from,
  every permission held, global actions, the actions on each repository). Disabling ends the user's sessions.
- **Delegation**: what a caller hands out is bounded by what it holds: roles (and groups, and custom role
  permissions, and provider group mappings) it adds may carry only global actions the caller holds, and `admin` only
  from an administrator; an account holding more than the caller (an administrator, for a `user-admin`) cannot be
  changed, deleted, given a password or (service accounts) given tokens by it (403). Nobody can disable or delete
  their own account, and the last enabled administrator (direct role or local group) cannot be disabled, deleted,
  demoted or removed from its group (409).
- Effective permissions are computed per request from a cached, versioned grant table (ARCHITECTURE §7) and
  checked in one place. Content listings and search are filtered, never just hidden.
  Now: every RBAC change bumps `grant_version`; the replica that made it applies it at once, the others within
  moments (MongoDB change stream; a 5 s poll if the stream is down).
- **Refusals**: a principal that is not signed in gets 401 where one is needed; a signed-in principal lacking a
  permission gets 403 with problem code `permission_required` (CSRF and pending sign-in steps keep their own codes);
  a hidden repository is 404. `GET /auth/me` lists coarse UI capabilities: `admin`, `repositories.create`,
  `repositories.manage` (manage on some repository), `repositories.browse` (browse or read on some repository),
  `stats.read`, `audit.read`, `users.admin`, `settings.admin`.
- **What a user without roles sees** (T-U10): signing in only needs the provider's required group, so a user may hold
  no role at all (no mapped group roles, no direct roles; signed-in users do not get the anonymous grants). Such a
  session answers consistently: no 401 anywhere (the session stays valid), every route needing a global action 403
  `permission_required` (statistics, activity, transfers, the event stream, cleanup policies, administration),
  listings and search filtered to empty pages, every repository 404, and the routes of any signed-in user (profile,
  own sessions and API tokens, preferences, `GET /roles`, the low-disk guard state) work. Its capabilities are empty;
  the UI shows only the dashboard (a "You are signed in, but have no access yet — ask an administrator" landing) and
  the profile, opens no event stream, and shows a "No access to this page" page for anything else. A reader (or a
  user with only the anonymous grants, browse + read on proxies) sees Repositories, Browse and Search and a
  dashboard listing its repositories, but no statistics, activity, live transfers or storage (`stats-read`), no
  Cleanup or repository settings (`manage`), and no administration.

### 4.11 Audit
Append-only audit log of security-relevant actions: sign-in/out/failure, token create/revoke/use-refused,
user/group/role/provider changes, repo create/update/delete, uploads and deletes on hosted repos, cleanup runs, task changes,
settings changes, webhook changes. Searchable in the UI, exported via API; retention configurable (default 400 days).
- **Record**: time, action (`<category>.<verb>`, catalog `waystation_common.audit.AuditAction`), outcome
  (`success`/`failure`) and for failures a reason code (`bad_credentials`, `unknown_user`, `locked`, `disabled`,
  `not_in_group`, `directory_unavailable`, `link_refused`, `invalid_token`, `too_many_failures`,
  `password_change_required`, `bad_second_factor`, `totp_required`, `totp_setup_required`), the actor (user,
  service account, anonymous or `system`; how it authenticated: session, API token, basic, registry token; its
  sign-in provider; the API token id when one was used), the target (kind, id, name), the client address (after the
  trusted-proxy rules of §8.4) and the request id, plain `details`, and field-level `changes` (before/after).
  **Secret values are never recorded**: a changed secret appears as `***` with `redacted: true`; credentials inside
  URLs are masked.
- **Recorded now**: `auth.login` (UI sign-in success and every failure, with the attempted username and `via`
  `ui`/`docker`; a successful `docker login` with a password at most once an hour per user and client address,
  because Docker clients re-send the password with every token request; API-token `docker login`s are token use, not
  sign-ins), `auth.lockout`, `auth.logout`, `token.create`, `token.revoke`, `token.use_refused` (an invalid, expired
  or revoked token, with its display prefix; not past the failure rate limit), `service_account.create|update|delete`,
  `user.create` (bootstrap admin, provider provisioning, the users API), `user.link` (an external account linked for
  the first time), `user.groups_sync` (the groups a provider reports changed), `user.update` (profile, status, roles,
  groups, unlock; own profile changes too), `user.delete`, `auth.password_change` (own change, success or a wrong
  current password; an administrator's reset with `details.by_admin`), `auth.totp_enable` (two-factor sign-in
  turned on), `auth.totp_disable` (turned off by the user, `details.with` the kind of code, or reset by an
  administrator, `details.by_admin`), `auth.totp_recovery_codes` (recovery codes replaced); a two-step sign-in is
  `auth.login` with `details.second_factor` (`totp`/`recovery_code`), a wrong code `auth.login` failure
  `bad_second_factor` with `details.step` `second_factor`; codes and secrets are never recorded; `session.revoke`,
  `group.create|update|delete` (membership changes as `group.update` with `details.members_added|members_removed`),
  `role.create|update|delete` (anonymous access changes as `role.update` on target `anonymous_access`),
  `auth_provider.create|update|delete|test`, `repository.create|update|delete|test_upstream`, `task.update|run|retry|discard`
  (schedule edits, run now, dead-letter retry and discard on the Tasks page), `content.upload` (a manifest pushed to
  a docker hosted repository, with image, tag, digest, media type and size; a file uploaded to a PyPI hosted
  repository, with project, version, file name, sha256, size and whether it replaced one) and `content.delete` (a
  docker tag, manifest or blob deleted), `content.update` (a PyPI release yanked or un-yanked, with the reason and the
  files changed), `settings.update` (the instance settings, with field changes), `webhook.create|update|delete`
  (field changes; a new secret or new custom headers show as redacted changes), `webhook.test` and
  `webhook.redeliver` (with the delivery ids), `cleanup_policy.create|update|delete` (field changes); changes made by
  the provisioning file (§9) carry `details.source = provisioning`; an administrator signing a user out everywhere is `session.revoke` on the user with `details.by_admin`. Not yet (the feature does
  not exist): cleanup runs (T-O01); `PENDING`
  in the catalog names them and the acceptance test fails when one is emitted without a test.
- **Writing never fails or slows a request**: records go to a bounded in-process queue (10 000) drained by one task
  with batched inserts (retried with backoff); on overflow or a lost batch the records are counted and logged at
  ERROR (`AUDIT RECORDS DROPPED`, at most every 10 s with the running total).
- **Retention**: each record expires `WAYSTATION_AUDIT_RETENTION_DAYS` (default 400) after it was written (MongoDB
  TTL); a changed setting applies to records written from then on.
- **API** (`audit-read`, which `admin` implies): `GET /api/v1/audit` with `from`/`to`, `actor` (name or id), `action`
  (repeatable, exact or `<category>.*`), `target` (name or id), `outcome`, cursor paging (newest first, ≤ 200 per
  page); `GET /api/v1/audit/export?format=csv|ndjson` streams the same selection (≤ 100 000 records, then a
  truncation marker; CSV cells that a spreadsheet would evaluate are prefixed with `'`).
- **UI**: Administration → Audit log: table (time, actor with sign-in method, action, target, outcome, client
  address) with the filters in the URL, a detail drawer with the change diff, and CSV/NDJSON export.

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
- **Tasks and workers** (`admin`; REST `/api/v1/tasks/*`, live on the event stream topic `tasks`):
  - Scheduled tasks = the `job_definitions` (built-ins `stats.rollup` every 10 s and `blob.gc`, and one per scheduled
    cleanup policy) with schedule, enabled, last run and next run. An admin can change a schedule (5-field UTC cron,
    macro, or `@every <n>s|m|h` of at least 10 s; validated, 422 with a field error otherwise) or pause/resume it;
    a changed schedule restarts its timer (missed fires do not catch up). A cleanup policy's schedule belongs to the
    policy (409; edited on the Cleanup page). "Run now" queues the task (a pending run absorbs it: `merged`).
  - Job runs: one row per attempt (status `running`, `succeeded`, `failed` (will retry), `dead`, `interrupted`;
    trigger `schedule`, `manual` or `retry`), newest first, filters (status, kind, queue, task, job), keyset cursor;
    routine jobs (the stats rollup) are hidden unless asked for. A run's detail adds its result and its log (the last
    500 lines). While a job runs, its progress (≤ 1/s) and new log lines are pushed live (`job.progress`, `job.log`),
    and starts/ends as `job.update`; worker start/stop as `worker.update`.
  - Workers: host, version, queues, concurrency, leader (scheduler lease), running jobs, heartbeat age (`late` after
    30 s, `lost` after 90 s); queue figures (waiting, running, delayed, oldest waiting) and the dead-letter count.
  - Dead letters (`jobs:dead`): retry queues the same work as a new job with fresh attempts (trigger `retry`; the
    usual dedupe applies) and removes the dead letter in one step; a repeated retry answers with the job the first
    one queued. Discard deletes it; discarding a missing one is a no-op. Malformed entries can only be discarded.
  - Schedule edits, run now, retry and discard are audited (§4.11).
- System: storage path usage/free, MongoDB/Valkey health, version, licence, support bundle download (redacted).
  Now: `GET /api/v1/system` (`admin`): version, Python, when the answering server process started, MongoDB and
  Valkey (ping round trip and server version, 2 s limit each; a failure reports only the error class) and the data
  directory (size, used, free, and `MIN_FREE_GB` below which writes stop). Support bundle: T-O10.
- Settings: `GET/PUT /api/v1/settings` (`settings-admin`): the instance name (top bar, sign-in page, browser title,
  `GET /config`; stored in the `settings` collection over `WAYSTATION_INSTANCE_NAME`, applied by every replica
  within 5 s; audited `settings.update`) and, read-only, the limits set in the environment (sessions, sign-in rate
  limit and lockout, token failures, audit and statistics retention, upload size, free-space floor). The
  Settings page also sets the default docker repository (a repository option, saved through the repositories API).
- Users: an administrator lists a user's sessions and signs the user out everywhere
  (`GET/DELETE /api/v1/users/{id}/sessions`, `user-admin`; not for a user holding more than the caller).
- UI: Administration → Users (and a page per user), Groups, Roles (with Anonymous access), Auth providers, API tokens
  (with Service accounts), Audit log, Webhooks, Settings, System; each rail item shows only with the matching capability.
- **Import from Nexus** (`admin`; REST `POST /api/v1/import/nexus/preview` and `/apply`; UI Repositories → "Import
  from Nexus"): reads a Nexus Repository 3 configuration through its REST API (`/service/rest/v1/repositories`, then
  `/service/rest/v1/repositories/<format>/<type>/<name>` per repository, `maven` for `maven2`; a repository without
  a detail endpoint is mapped from its list entry) and creates equivalent repositories; cached content is re-fetched
  on demand, hosted content can be migrated by a worker job (Phase 6).
  - **Connection**: the administrator gives the Nexus base URL (http(s), no credentials in it) and a Nexus
    administrator's username and password, sent as basic auth to that origin only, for that request only: never
    stored, logged, audited or returned. The SSRF guard applies (a Nexus on a private or loopback address needs
    `allow_private`), optional extra CA certificates (`ca_pem`), no redirects followed, bodies bounded (list 8 MiB,
    detail 1 MiB, 1000 repositories, 120 s overall). Unreachable Nexus, refused credentials (401/403), a redirect or
    a non-Nexus answer → 502 problem with the reason; a bad URL or CA bundle → 422 field errors.
  - **Mapping**: same name (ADR 0003), format (`maven2` → `maven`; formats Waystation does not serve, or types it
    does not offer yet, are `unsupported` with the reason), type, `online`; proxy `remoteUrl` → `upstream_url`,
    `metadataMaxAge` (minutes) → `metadata_max_age_s` (-1 keeps the default), `contentMaxAge` → `content_max_age_s`
    for raw (docker, npm, apt, yum, PyPI and Go content is checksum-verified and kept forever), negative cache (disabled = 0),
    `autoBlock` → `auto_block.enabled`, a blocked Nexus repository → `offline`. Docker: index `HUB` (or a Docker Hub
    remote) → `docker_hub`; the upstream host becomes an alias (`docker.io` for Docker Hub, `ghcr.io`, ...; option
    `docker_aliases`, default on; an alias already routed or equal to a repository name is dropped); connector ports
    and subdomains are reported, not imported. apt `distribution`/`flat`; raw `contentDisposition`; maven
    `versionPolicy`/`layoutPolicy` (T-P5-MAVEN); hosted
    `writePolicy` and group members/writable member when this server has hosted/group repositories of that format.
    Upstream credentials cannot be imported (Nexus never returns passwords): the repository is created without
    authentication and the report says so. Settings without an equivalent (cleanup policies, routing rules, blob
    stores, truststore use, connection tuning, signing keys, ...) are listed as differences.
  - **Default docker repository**: the one named in the request (`default_docker_repo`), else the Docker Hub proxy
    behind a Nexus connector (directly, or the first Docker Hub member of a connector group), else the only Docker
    Hub proxy; never changed when Waystation already has one.
  - **Preview** (dry run, writes nothing): per Nexus repository `create` (with the exact repository it creates,
    checked by the same validation as the repositories API), `exists` (a repository with that name exists,
    case-insensitively: never changed; field-level differences against it), `unsupported` or `invalid` (with the
    reason); field-level `differences` (setting, Nexus value, Waystation value, why) and notes on every item.
  - **Apply** reads Nexus again (nothing is kept between the two calls), creates the selected `create` items
    (all by default) members before groups, each audited as `repository.create` with `details.source`
    `nexus_import`, and reports each outcome (`created`, `exists`, `failed` with the field errors, `not_selected`,
    ...). Idempotent: a name that exists by then is reported `exists` and left alone.
  - **Cutover** (T-M02): `Documents/Operations/MIGRATING_FROM_NEXUS.md` (import, credentials, hosted content, client
    switch per format, reverse-proxy cutover, rollback) and `Build/migration/nexus_compare.py`, which sends the same
    read-only requests (a TOML list per format) to Nexus and Waystation and compares status, size, sha256, docker
    digest, media type and chosen JSON values (report table/Markdown/JSON; exit 0 = all match).
- **Webhooks** (`admin`; REST `/api/v1/webhooks`; UI Administration → Webhooks; operations guide
  `Documents/Operations/WEBHOOKS.md`): signed HTTP `POST`s of a JSON document when content is pushed, published,
  changed or deleted and when repositories change.
  - **Definition**: name (§3 pattern, unique case-insensitively), description, target URL (http(s), no credentials
    in it, a literal private address only with `allow_private`), events (`content.upload|delete|update`,
    `repository.create|update|delete`, a category `content.*` / `repository.*`, or `*`), repository scope (name
    globs, case-insensitive; empty = all), enabled, signing secret (required on create, ≥ 16 characters; write-only:
    AES-GCM under `SECRET_KEYS`, reads return `secret_set`; omitted on update = kept), optional custom headers
    (write-only values, encrypted; reads return `header_names`; Waystation's own and transport headers refused),
    `timeout_s` (1-30, default 10), `max_attempts` (1-20, default 8).
  - **Events** are the audit records of those actions (§4.11), handed by `audit.record` to an in-process notifier
    that matches them against the enabled webhooks (cached ≤ 5 s) and, when one wants it, writes a `webhook.event`
    job to the durable outbox. The worker records one delivery per matching webhook (id `<event id>-<webhook id>`,
    so a re-run creates nothing new) and attempts it at once.
  - **Request**: JSON `{id, type, occurred_at, repository, actor{kind,name}, target, details, changes?, source}`
    (≤ 256 KiB, else the details are dropped); headers `Waystation-Event`, `Waystation-Event-Id`,
    `Waystation-Delivery` = `Idempotency-Key` (same on every retry), `Waystation-Attempt`, `Waystation-Timestamp`
    and `Waystation-Signature: t=<unix>,v1=<HMAC-SHA256(secret, "<t>." + body) hex>`, re-signed per attempt;
    receivers refuse timestamps older than 5 minutes (replay window).
  - **Delivery**: at least once (a delivery is claimed with a lease for one attempt; a retry sweep every 15 s picks
    up due retries and lost attempts); through the SSRF guard (connect-time check; private targets only with
    `allow_private` or `WAYSTATION_SSRF_ALLOW_CIDRS`; a refused target is dead at once); the server-wide outbound
    proxy and extra CA file apply; no redirects; the webhook's timeout per attempt; 2xx = success, anything else is
    retried after 30 s doubling to at most 1 h (±10 %) until `max_attempts`, then the delivery is dead. A webhook
    that is off gets no new deliveries and its waiting retries end as dead.
  - **Log** (30 days): per delivery its status (`pending`, `succeeded`, `dead`), trigger (`event`, `test`,
    `redeliver`), the JSON sent and the headers (custom values masked), and the last 20 attempts (status, latency,
    error, first KiB of the answer). **Send test event** records a `webhook.ping` delivery (one attempt, even while the
    webhook is off); **Redeliver** sends a delivery's document again as a new delivery (same event id).
  - Audited `webhook.create|update|delete|test|redeliver`; metrics `waystation_webhook_deliveries_total{outcome}`
    and `waystation_webhook_deliveries_pending` (§10).

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

`make perf` (k6, `tests/perf/README.md`) measures every budget except the dashboard paint against a throwaway stack
with a synthetic upstream: a CI profile (a few minutes, 100 000 seeded assets, 100 concurrent clients) and a full
profile with the sizes above (1M assets, 500 concurrent downloads, server limited to 4 vCPUs). Latency budgets are
measured at a steady request rate, not at saturation. Budgets warn until milestone M2 (`STRICT=1` fails them now).

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
OpenTelemetry tracing hooks (off by default). Zabbix 7 template and Grafana dashboard in `Build/monitoring/`
(`Documents/Operations/MONITORING.md`).

## 6. User interface
See `UI_DESIGN.md`. Summary: dashboard with live transfers and KPIs, repositories, browse/search, component
pages with pull/install snippets, activity log, storage, cleanup policies, tasks/workers, administration
(users, groups, roles, auth providers, tokens, audit, settings).

## 7. API
- REST JSON under `/api/v1/`, described by OpenAPI 3.1 generated from the server (FastAPI + pydantic v2).
  The UI uses only this API. Pagination by opaque cursor; filtering and sorting params documented.
- **Event stream**: `GET /api/v1/events/stream` (Server-Sent Events) with topic filters (`transfers`, `stats`,
  `repo:<name>`, `tasks`, `workers`); RBAC-filtered per subscriber; heartbeat 15 s; resumable with `Last-Event-ID`.
  Now: topics `transfers`, `stats`, `activity`, `tasks` (jobs and workers; also needs `admin`), `repo:<name>` (comma-separated `topics`; needs `stats-read`); on
  connect `retry: 3000` and a `transfers.snapshot`; `activity` events carry the stream id as `id:` and are replayed
  after `Last-Event-ID` (header or `last_event_id` query parameter, up to 1000); `stats.tick` at 1 Hz (rates over the
  last 10 s from the live counters). Stats endpoints (`/stats/*`, `/transfers`, `/activity`) need `stats-read` and
  only cover repositories the principal can see; series buckets are 1 min (15m, 1h), 10 min (24h), 1 h (7d), 6 h (30d).
  `GET /activity` filters by `repo`, `kind`, `format`, `principal` (case-insensitive substring of the user name or
  client address), `cache`, `outcome` and an inclusive `since`/`until` window, newest first with a stream-id cursor (a
  sparse filter may return a short page with `next_cursor` set: keep paging). `GET /activity/export` streams the same
  selection as CSV (newest first, at most 100,000 rows; cells a spreadsheet would read as a formula are prefixed `'`).
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
  `stats.tick`, `upstream.state`, `activity`). Errors are RFC 9457 problem+json with field-level `errors[]` and the stable error `code` (`not_found`, `password_change_required`, `totp_setup_required`, `second_factor_expired`...).
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
| `mongodb` | `mongo:8` (official) | Metadata, users, stats (time series), audit, jobs history | single-node replica set (or external); open-file limit 64000 (mongod's connection limit follows it) |
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
Caddy, HAProxy and Synology DSM (`Documents/Operations/REVERSE_PROXIES.md`; the example files in
`Build/Containers/examples/reverse-proxies/` are checked in containers by `make proxies-check`). Today only the
external-proxy mode exists: `web` listens on plain HTTP; TLS termination in `web` is planned (T-F05). Deployment
guide: `Documents/Operations/DEPLOYMENT.md`.

### 8.5 Backup and restore
`Build/Containers/backup/waystation-backup.sh` (bash + Docker only; `make backup` / `make restore` /
`make verify-blobs` from a checkout; `Documents/Operations/BACKUP.md`) runs in a throwaway `mongo:8` container that
borrows the worker container's data mount (`--volumes-from`), networks and MongoDB settings (`WAYSTATION_MONGO_URI`
or `_FILE`, never on a command line). **Backup**: `mongodump --archive --gzip` first, then the blob files (immutable,
and GC keeps unreferenced blobs for the grace period, so everything the dump references is still there), `keys/` and
a `manifest.json` (mode, image, MongoDB version, applied migrations, collection counts, blob files/bytes, archive
size and sha256) into `<target>/waystation-<UTC time>/`, written as `.partial` and renamed when complete; blob files
the previous backup holds are hard-linked from it (incremental snapshots; copies where the target cannot link);
`--keep N` retention; `--hosted-only` copies only blobs referenced by hosted repositories' assets (computed from
MongoDB). **Restore** (server, worker and web stopped): checks the archive's sha256, replaces the database
(`mongorestore --drop`, renaming the database if needed), copies blobs and keys back without overwriting, sets their
owner and modes, empties Valkey; then start the stack and run the `blob.verify` task. **Blob verify** (worker job,
weekly and on demand: Tasks page, API, `python -m waystation_worker.run_task blob.verify --wait`): checks every
asset's blob file (missing; size different from the recorded one; with `deep`, the sha256), moves damaged files out
of `blobs/`, marks affected assets (`assets.blob_missing`): proxied ones are re-fetched on the next request (the fill
clears the mark), hosted ones are reported as lost (job log and result per repository, a `storage.blobs_missing`
activity event, `waystation_assets_blob_missing{repository_type}`) until re-uploaded or restored; marks whose blob is
back are cleared. `make backup-test` runs backup, wipe, restore and verify against a throwaway stack.

### 8.6 Upgrades
Forward-only migrations run by `init`; `server` refuses to start on an unknown newer schema; release notes flag
breaking changes. Zero-downtime for patch releases with ≥2 server replicas.

## 9. Configuration reference (environment)
All variables are `WAYSTATION_*`; every secret also accepts `<NAME>_FILE` (setting both is an error; empty values mean "use the default"). The authoritative list is
`.env.example` (generated from `waystation_common.settings` by `make env-example`; CI checks it is current).
Key ones: `BASE_URL`, `DATA_DIR`, `MONGO_URI`, `MONGO_DB`, `VALKEY_URL`, `SECRET_KEYS` (comma list, first is
active), `SESSION_SECRET`, `TRUST_FORWARDED`, `TRUSTED_PROXIES`, `LOG_LEVEL`, `LOG_FORMAT`, `SERVER_WORKERS`,
`WORKER_QUEUES`, `WORKER_CONCURRENCY`, `MAX_UPLOAD_GB`, `MIN_FREE_GB`, `BOOTSTRAP_ADMIN_PASSWORD_FILE`,
`HTTP_PROXY`/`NO_PROXY`, `EXTRA_CA_FILE`, `SSRF_ALLOW_CIDRS`, `COALESCE_TAIL`, `COALESCE_LEASE_S`. Auth providers, repositories and policies are
configured in the UI/API (stored in MongoDB), or declared in a **provisioning file** (`PROVISION_FILE`, YAML;
`Documents/Operations/PROVISIONING.md`): repositories, cleanup policies, custom roles, local groups, service
accounts, sign-in providers with their group → role mapping, webhooks and settings (instance name, 2FA policy,
anonymous access, default docker repository, low-disk guard). `init` applies it idempotently at every start (an
invalid file or a failed change fails `init`); Administration → Provisioning (`admin`; `GET /api/v1/provisioning`,
`POST /api/v1/provisioning/plan|apply`) shows the plan (create / update with field diffs / unchanged / prune) and
applies the configured file on demand (never an uploaded one). Secrets are only `{file: ...}` / `{env: ...}`
references (never inline, never Waystation's own settings variables), stored encrypted like UI-entered ones and
shown in plans and audit records only as redacted. Objects the file manages carry `managed_by: provisioning` and
are labelled "Managed" and read-only in the UI; a managed object the file no longer declares is released, or
deleted only with `prune: {<section>: true}` (API guards still apply). Changes are audited with
`details.source = provisioning`.

## 10. Observability
See §5.5. `/metrics` on the server exports two kinds of metric (the full list with labels and meanings is in
`Documents/Operations/MONITORING.md`):
- **Process metrics**, updated where things happen (O(1) per update): `waystation_requests_total{repo,format,status,
  cache}`, `waystation_bytes_total{repo,direction}`, `waystation_upstream_latency_seconds{repo}` (histogram),
  `waystation_coalesce_total{repo,event}`, `waystation_http_request_duration_seconds{route,method,status}` (histogram
  of every HTTP request except the event stream; `route` is the matched route template, `status` the class `2xx`...),
  `waystation_storage_refusals_total` (writes refused by the low-disk guard), `waystation_quota_refusals_total{repo,
  kind}` (writes refused by a hard quota: `upload` or `fill`), `waystation_fills_not_stored_total{repo,reason}` (proxy
  downloads served without being cached: `quota`, `low_disk`), `waystation_quota_alerts_total{repo,level}`, and the gauges
  `waystation_inflight_transfers` and `waystation_sse_clients`. With `WAYSTATION_SERVER_WORKERS > 1` the processes
  share them through `PROMETHEUS_MULTIPROC_DIR` (a fresh directory on the server's tmpfs) and every scrape returns
  the sum over all processes.
- **Cluster figures**, read from Valkey, MongoDB and the data filesystem when `/metrics` is scraped (cached 10 s, each
  source bounded to 2 s; a source that fails is reported by `waystation_metrics_source_up{source}` = 0 and its
  metrics are left out): `waystation_upstream_state{repo}` (0 ok, 1 degraded, 2 blocked, 3 offline: the UI's rule),
  `waystation_upstream_consecutive_failures{repo}`, `waystation_upstream_ratelimit_{limit,remaining,window_seconds,
  updated_timestamp_seconds}{repo}`, `waystation_jobs_{waiting,running,delayed,oldest_waiting_seconds}{queue}`,
  `waystation_jobs_dead_letters`, `waystation_workers`, `waystation_scheduler_leaders`,
  `waystation_blobstore_{used_bytes,blobs,free_bytes,size_bytes,min_free_bytes}` (`min_free_bytes` = the low-disk
  guard's effective floor), `waystation_storage_low` (1 while the guard is tripped), per repository with a quota
  `waystation_repository_quota_bytes{repo,mode}`, `waystation_repository_stored_bytes{repo}` and
  `waystation_repository_quota_used_ratio{repo}`, and the cleanup and blob GC
  running totals `waystation_cleanup_removed_{items,bytes}_total`, `waystation_gc_{runs,freed_blobs,freed_bytes,
  orphan_uploads_removed}_total` (counters, from the Valkey hash `maintenance:totals` the workers write), webhook
  delivery attempts `waystation_webhook_deliveries_total{outcome=succeeded|failed|dead}` (Valkey `webhooks:totals`)
  and `waystation_webhook_deliveries_pending`. Every server
  process and replica reports the same values (aggregate with `max`).

The Grafana dashboard (`Build/monitoring/grafana/`) and the Zabbix 7 template (`Build/monitoring/zabbix/`) cover all
of them plus `/healthz` and `/readyz` (low-disk, guard, workers, scheduler, job age, dead letters, upstream state and
rate limit, API latency triggers). Not exported yet: process CPU/memory, sign-in failures.

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
- Integration tests against real MongoDB and Valkey containers, and against real directories for LDAP (`make
  test-integration` builds an OpenLDAP server with a FreeIPA-style tree and a Samba AD domain controller with a
  throwaway CA, `Build/Containers/test/`) and OIDC providers (a Keycloak realm in `make test-integration`; authentik
  with PostgreSQL, server and worker is opt-in, `make test-integration-authentik`, as it takes minutes to start).
  The OIDC tests drive the provider's login form / flow executor headlessly with a real HTTP client.
- Playwright E2E for the UI (login with each provider type, dashboard live updates, repo CRUD, set-me-up).
- Load/perf tests (k6, `make perf`, `tests/perf/`) measuring the §5.1 budgets against a throwaway stack (production
  images, synthetic nginx upstream, one scenario per budget, results compared by `Build/ci/budgets.py`, a Markdown
  report per run); bundle-size budget for the SPA.
- Security tests: path traversal corpus, SSRF corpus, auth bypass matrix (every route × principal type).

## 12. CI/CD and release
- GitHub Actions (primary, `.github/workflows/ci.yml`): on every PR and push to `main`, `make check` (forbidden
  strings, workflow lint, format, lint, typecheck, unit, integration, fixture scan, SPA checks and build, budget
  report), the stale-generated-client check and the budget table in the job summary, plus every format's real-client
  tests in replay mode (`make clients-test FORMAT=all`); nightly live-upstream smoke (`clients-live.yml`);
  `release.yml` on `vX.Y.Z` tags (must match `VERSION`) builds multi-arch (amd64, arm64) server/worker/web images
  with SBOMs and provenance attestations and publishes them to GHCR using only `GITHUB_TOKEN`.
- Gitea Actions (secondary) `.gitea/workflows/images.yml` builds the images on every push to `main` and pushes them
  to the Gitea registry. Registry host and credentials only from org settings (`vars.REGISTRY_HOST`,
  `secrets.REGISTRY_USER` / `secrets.REGISTRY_TOKEN`). `.gitea/workflows/perf.yml` runs `make perf` (CI profile)
  after every push to `main` and the full profile nightly, uploading the report as an artifact. Details:
  `Documents/Operations/CI.md`.
- The forbidden-strings check (`make forbidden-strings`, in `make check`) keeps environment details out of the
  public repository: host names outside `Build/ci/allowed_hosts.txt`, non-documentation IPs, keys and tokens fail.
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
  CLAUDE.md  README.md  VERSION  LICENSE  Makefile  pyproject.toml (uv workspace)  uv.lock
  .env.example  .gitignore  .dockerignore  .github/workflows/  .gitea/workflows/
  Application/
    Common/   waystation_common/   settings, logging, ids, crypto, events schema, job protocol, storage API, errors
    Server/   waystation_server/   app factory, api/ (REST), security/ (authn, access), formats/<format>/,
                                    proxy/ (fetch, coalesce, freshness), dataplane/ (x-accel, ranges), stats/, sse/
    Worker/   waystation_worker/   runner, jobs/ (cleanup, gc, index_apt, index_yum, rollups, prefetch, import)
    Web/      Quasar SPA (src/api, components, composables, layouts, pages, stores, router, css, mocks/),
              src/assets/brand/ (brand geometry + master SVGs; public/ holds the generated favicon set)
  Build/
    Containers/  compose*.yaml, web/ (Dockerfile, nginx.conf.template), server/, worker/, dev/ (dev image), examples/,
                 backup/ (waystation-backup.sh: backup, restore, verify)
    githooks/    pre-commit hook (`make hooks`)
    ci/          budgets.json, scripts used by workflows
    monitoring/  zabbix/ (Zabbix 7 template), grafana/ (dashboard)
    migration/   nexus_compare.py (side-by-side Nexus/Waystation checks, `make nexus-compare`), checks.example.toml
  tests/  clients/<format>/  fixtures/upstreams/<format>/  perf/  security/  e2e/  backup/ (`make backup-test`)
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
| CI (T-F03) | Implemented 2026-11-08, first hosted runs pending: GitHub `ci.yml` (`make check`, generated-client check, budget report, client tests in replay; major release tags and manual only), `release.yml` (tagged multi-arch images to GHCR with SBOM and provenance), Gitea `check.yml` (2026-11-10: `make check` without integration tests on every push to main and pull request; `make test-integration` in a second job on push to main and manual runs, skippable with the org variable `WAYSTATION_CI_INTEGRATION=false`), Gitea `images.yml` (images to the Gitea registry from org variables/secrets); repository-wide forbidden-strings check with `Build/ci/allowed_hosts.txt`; `Build/ci/budgets.json` and `make budgets` (warnings until M2); `make workflow-lint` (actionlint) |
| Performance suite (T-P2-PERF) | Done 2026-11-10: `make perf` (k6, `tests/perf/`): throwaway stack with production images and a synthetic upstream (raw, npm, PyPI, Maven, apt, yum, Go, Helm, OCI), one scenario per §5.1 budget plus an SSE probe, CI and full profiles, results in `tests/perf/.results/` (`measured.json` read by `make budgets`, `report.md`), `STRICT=1`, Gitea `perf.yml` (CI profile on push to main, full nightly). Measured on the dev box: CI profile within every budget (content hit p95 TTFB 4 ms, metadata 3.7 ms, manifests 3.8 ms, miss +3.6 ms and no throughput loss at 100 MiB/s, API 21 ms at 100 000 assets, live events 245 ms); full profile: API p95 35 ms at 1M assets, 1-2 failed downloads per run with 500 concurrent clients (a MongoDB connection closed under the asset lookup, not retried: warning). Dashboard paint not measured (browser) Findings fixed 2026-11-10: 500s under 500 concurrent clients (mongod refused connections past its open-file limit; compose raises it, hot-path reads retry once on `AutoReconnect`), Docker tokens granting nothing for up to 5 s after a repository was created on another process (catalog change stream and reload on a miss), the repository list p95/p99 at 1M assets (storage figures from `repo_usage` counters, refreshed in the background) and folder browse round trips. |
| Common core (T-F04) | Done 2026-11-08: `waystation_common.settings` (env `WAYSTATION_*`, `<NAME>_FILE` for secrets, generated `.env.example`), `logs` (JSON lines + contextvars request context, redaction), `ids` (ULID), `errors`, `paths.normalise`, `crypto` (AES-256-GCM keyring, rotation) |
| Containers and compose (T-F05) | M0 slice 2026-11-08: server/worker/web images, `compose.yaml` + dev override, `make dev-up` brings web, server, worker, mongodb (rs0), valkey and init up healthy; server answers only `/healthz` and `/readyz`; web serves a placeholder page until the SPA is built. Standalone compose files and TLS pending |
| MongoDB layer (T-F06) | M0 slice 2026-11-08: async client factory, collection registry and indexes for the M0 collections, declarative schema + forward-only migrations run by `init` (idempotent), `make test-integration` against real MongoDB/Valkey |
| Blob store (T-F07) | M0 slice 2026-11-08: local-filesystem `BlobStore` (streaming sha256, size limits, verified atomic commit, X-Accel paths, orphan sweeper, free-space guard); NFS CI scenario pending |
| Events and live stats (T-F09) | M0 slice 2026-11-08: event schema and non-blocking emitter (live transfers, activity stream, per-minute counters and latency histograms in Valkey); the worker rolls closed minutes into `stats_minute` exactly once (the scheduled job `stats.rollup`, every 10 s, since T-F08). Hour/day rollups pending; nothing calls the emitter until the proxy engine lands (T-S07) |
| Job system (T-F08) | Done 2026-11-08: typed jobs (`waystation_common.jobs`), `JobClient` for the server (`Services.jobs`: durable outbox enqueue inside a transaction, or direct), Valkey Streams queues with consumer groups, dirty-flag dedupe (pending merges, running → exactly one rerun), outbox dispatcher with re-dispatch after Valkey data loss, heartbeated jobs reclaimed from lost workers, retries with backoff, dead letters after `max_attempts`, scheduler leader lease + cron/`@every` schedules from `job_definitions`, `job_runs` progress/log/result, `workers` presence, `job.*`/`worker.*` activity events, liveness watchdog. The stats rollup runs as the scheduled job `stats.rollup`. Chaos tests: killed worker, flushed Valkey, uploads during a rebuild, one fire with three workers. Job API and Tasks page: T-O04 (2026-11-09) |
| Server app factory (T-S01) | Done 2026-11-08: routing for `/repository/<repo>/` (case-sensitive names, normalised paths) and `/v2/` (repo prefix → upstream alias → default docker repo), request context (request id, client address, JSON access log), native vs problem+json errors, `HEAD` for API routes, `/healthz`, `/readyz` (MongoDB + Valkey), `/metrics` (Prometheus, CIDR-restricted), start-up `check_schema`, in-process repository catalog (5 s refresh) |
| Local authentication (T-S02) | Done 2026-11-09: bootstrap admin via `init`, argon2id, sessions with idle/absolute timeouts, CSRF, login rate limit and lockout, `/api/v1/config`, `/api/v1/auth/{providers,login,logout,me,me/preferences}`; one-time passwords enforced (only `/auth/me`, the password change and sign-out until changed; `docker login` refused), own password change, profile update, own sessions list/revoke (one, or every other), effective permissions; UI password card on the profile page and a router guard for one-time passwords |
| Two-factor sign-in (T-O08) | Done 2026-11-10: TOTP (RFC 6238) for local accounts: enrolment with a QR code drawn in the browser, 10 recovery codes, two-step sign-in with replay protection (Valkey + MongoDB), lockout and rate limits, `docker login` with the password refused for such accounts (API tokens instead), turn off, administrator reset, "require for local administrators" setting enforced like one-time passwords; LDAP/OIDC unaffected; audited |
| Audit (T-S10) | Done 2026-11-09: audit catalog and records (actor, target, outcome/reason, client address, request id, details, redacted before/after changes), non-blocking batched writer, TTL retention (`WAYSTATION_AUDIT_RETENTION_DAYS`), `GET /api/v1/audit` (filters, cursor paging) and streamed CSV/NDJSON export, Audit log page. Actions whose features are pending (cleanup) are emitted by those tasks; password change, session revoke and user/group/role admin since 2026-11-09 (T-S02/T-S03); `content.upload`/`content.delete` since T-P2-DOCKERH (docker hosted); `settings.update` since T-U08 |
| RBAC (T-S03) | Done 2026-11-09: one decision point (`security.access`: `check`, `filter_repos`, `filter_query`), built-in and custom roles, scopes incl. `type:`, hidden = 404 (2026-11-10, ADR 0005: the 401 Basic challenge instead for requests without credentials on `/repository/<repo>/...`, identical for missing repositories; `access.hidden_repo`), every route declares its rule, CSRF enforced app-wide for session requests; groups (local and provider-mapped) holding roles; materialised grants cached per `grant_version` (in-process and Valkey) with change-stream invalidation across replicas; configurable anonymous access; delegation bounded by the caller's own powers, last-administrator guard. Admin APIs: users, groups and members, roles, anonymous access, effective permissions (UI: T-U08). Tests: route enumeration over included routers (it had silently seen none since a FastAPI upgrade), route × {anonymous, reader, deployer, admin} matrix for every REST route plus the format-route matrix, cross-replica role change |
| API tokens and service accounts (T-S04) | Done 2026-11-08: personal and service-account API tokens (keyed hash, shown once, scopes and repository patterns that only narrow RBAC, expiry, batched last used, revocation on every replica within 1 s, failure rate limit); Bearer, `Token` and basic-with-token auth for format clients and the API; `docker login` with a token; `/metrics` with a `metrics` token; REST for own tokens, all tokens and service accounts; profile page "API tokens" card (create with scopes/repositories/expiry, copy-once dialog, list, revoke). Audited (T-S10); the service-account admin UI (T-U08) pending |
| Repositories (T-S05) | M0 slice 2026-11-08: proxy repositories of docker, npm, apt and raw: REST CRUD, validation with field errors, write-only encrypted upstream secrets, test upstream, "Set me up" snippets, per-format options in OpenAPI. Audited since T-S10. Hosted repositories (`hosted{write_policy, immutable_tag_patterns}`, per-type option rules) since T-P2-DOCKERH and group repositories (`group{members, writable_member}`) since T-P2-DOCKERG, for docker. Clone pending |
| Real-client harness (T-P2-HARNESS) | M0 slice 2026-11-08: `make clients-test FORMAT=<f> MODE=replay\|record\|live` runs client containers against a per-format compose project with a record/replay fake upstream (impersonates public upstream hosts via `extra_hosts` + throwaway CA; replay fails on any miss; record scrubs and stores fixtures under `tests/fixtures/upstreams/<format>/`); `make clients-check` (in `make check`) rejects non-allowlisted hosts, non-documentation IPs, keys/tokens and oversize fixtures. Formats: `echo` (self-test), `raw`, `npm` and `apt` (Debian 13 + Ubuntu 26.04 clients in one run: `CLIENT` may list several; `lib.sh` falls back to bash `/dev/tcp` where the image has no curl; apt fixture budget 32 MiB); `setup.sh` creates repositories through the API as a per-run bootstrap admin (`ws_login`/`ws_api` in `lib.sh`); nightly live smoke workflow stub |
| M0 API contract + mocks (T-V01) | Done 2026-11-08: OpenAPI 3.1 contract (`Application/Web/openapi/waystation-m0.yaml`), generated TS types + `openapi-fetch` client, MSW handlers with seeded fixtures (11 proxy repos: Docker Hub, GHCR, Quay, registry.k8s.io, npmjs, Debian, Debian security, Ubuntu, Raspberry Pi OS, two raw) and a live-event generator (bursty docker pulls, npm installs, apt runs, raw downloads, coalescing, failures); `npm run dev:mock` / `make frontend-mock`. Server-side contract test: `Application/Server/tests/test_contract.py` compares the server's generated OpenAPI with it for every implemented operation (T-S05, T-S11) |
| Web shell (T-F10) | Done 2026-11-08: Quasar 2 / Vue 3 / TS strict SPA under `/ui/` (build output `Application/Web/dist/spa`), light/dark/system themes, self-hosted Inter + JetBrains Mono, SVG Material Symbols (no icon font), collapsible rail, top bar with live indicator, router + auth guard, local sign-in, shared live store (one stream, topic ref-counting, reconnect + Last-Event-ID, 4 Hz flush), `make frontend-build` with bundle budget (initial route 158 kB gzip of 250) |
| Brand (T-U02) | Done 2026-11-09: original mark (hexagon-headed waypoint marker holding a stack) and monoline wordmark, one geometry source (`src/assets/brand/geometry.ts`) for `BrandMark` and the master SVGs; `make brand` generates the favicon set (SVG that follows the browser theme, PNG 16/32/48/180/192/512, ICO, maskable icon, web manifest) with resvg in the dev image, `make brand-check` in CI; seven theme-aware empty-state illustrations used by `EmptyState` (no repositories, nothing cached, no results, no activity, error, not found, coming soon); the repository page now tells a load failure (retry) from a missing repository |
| Design system (T-U01 min) | M0 slice 2026-11-08: tokens, themes, format icon sprite, base components, SVG charts. Density toggle and showcase route still to do |
| Dashboard (T-U03 min) | M0 slice 2026-11-08 on mocks: KPIs, live transfers, traffic chart, storage donut, upstream health, activity feed, all live from the event stream. Top artifacts / recently added still to do |
| Repositories UI (T-U04 min) | M0 slice 2026-11-08 on mocks: list + filters, create/edit/delete docker/npm/apt/raw proxies with test upstream and field errors, detail page (Overview, Browse, Settings), Set me up panel. Snippets shown with a `<your-api-token>` placeholder (tokens are shown once at creation, T-S04). Docker hosted repositories (create flow "Hosted" tile, write policy and immutable tag patterns) since T-P2-DOCKERH and groups ("Group" tile, ordered members, writable member) since T-P2-DOCKERG; live transfers mark uploads |
| Browse UI (T-U05 min) | M0 slice 2026-11-08 (built on mocks, now on the real API, T-S09 min): docker images/tags/manifest, npm packages/versions, apt suites/packages, raw folder listing. Screenshot tour of the real server (both themes, desktop + mobile): `Documents/Notes/UI_TOUR_M0.md` |
| Browse API (T-S09 min) | M0 slice 2026-11-08: generic folder listing of cached assets for every format, npm packages/versions, apt suites/packages, docker images/tags/manifest, to the M0 contract; indexing in the background after cache fills (`npm_packages`, `apt_packages`, `docker_tags`/`docker_manifests`); every browse query index-backed (explain test); the raw, npm, apt and docker real-client harness runs check it. Components, `version_sort`, download counters and "where used" pending
| Data plane (T-S06) | Done 2026-11-10 (implemented 2026-11-08): cached blobs served by nginx via `X-Accel-Redirect` (sendfile, ranges), conditional requests and HEAD answered by the server, digest ETags and format headers preserved; verified through the dev stack's nginx. Perf acceptance (k6, T-P2-PERF): a cached 10 GiB file served at about 1.3 GiB/s (the dev box's Docker bridge) with the server at 5% of one core |
| Proxy engine (T-S07) | M0 slice 2026-11-08: fetch with tee streaming, digest/size validation before commit, metadata freshness and conditional revalidation, stale-on-error, offline mode, negative cache, SSRF-guarded pooled HTTP/2 upstream clients, live transfers/activity/counters/upstream health; verified against a public mirror through the dev stack. 2026-11-09: auto-block with exponential backoff, probes and automatic resume (shared across replicas), per-repo CA bundle and HTTP proxy (SSRF guard still applied through the proxy), all exposed in the repository form and upstream health. 2026-11-10 (T-O03): fills the low-disk guard or a hard quota refuses are passed through to the client verified and uncached (followers fetch on their own). Pending: continuing a fill after its client disconnects; used by every proxy format |
| Request coalescing (T-S08) | Done 2026-11-08: Valkey single-flight per asset (`proxy.coalesce`: Lua claim, renewed lease, progress, outcome, follower count on the owner's live transfer); followers tail the owner's upload file on the same host and a local filesystem (`fdatasync`-aware `synced` offset for other hosts in `local` mode), else wait and are served by X-Accel; metadata fetches and revalidations coalesced; failures and mismatches fail followers (nothing unverified completes); owner abort or death (lease expiry) → a follower takes over after a prefix check of what it already sent; Valkey down → no coalescing. Tests: 200 clients over two real replicas → one upstream fetch, identical bytes; owner replica SIGKILLed mid-transfer → takeover; mismatch; wait mode. Lease renewal independent of `fdatasync` (2026-11-09 fix for a second fetch under disk load). Followers on a second host against NFS not covered by CI yet |
| Stats and live APIs (T-S11) | M0 slice 2026-11-08: KPI summary with sparklines, traffic series (cache/format split), storage summary, upstream health, live transfers, activity feed, repository 24 h stats and degraded status; SSE event stream with snapshot, RBAC filtering, resume and 1 Hz stats ticks; verified through nginx. Hour/day series, top-N and latency percentiles pending |
| Activity and Storage pages (T-U07) | 2026-11-09: Activity page (live tail with pause and scroll-hold, filters in the URL incl. user, cache state, outcome and time window, history paging, detail drawer, CSV export through the new streaming `/activity/export`); Storage page (stored/free/de-duplication/blob KPIs, data volume bar, by-format donut, largest repositories, sortable repository table). Storage growth, largest artifacts, per-repository de-duplication and GC status pending |
| Docker token service (T-S12) | M0 slice 2026-11-08: `/auth/token` (anonymous and local-password tokens, scope grammar, RBAC grants), `/v2/` Bearer challenges with scopes and `insufficient_scope`; real `docker login` verified; `docker login` with an API token (T-S04); `registry:catalog:*` for everyone with a RBAC-filtered `/v2/_catalog`, `push`/`delete` grants carry `browse` (T-P2-DOCKERH) |
| Raw format (T-P2-RAW) | M0 slice 2026-11-08: raw proxy repositories serve `GET`/`HEAD /repository/<repo>/<path>` through the proxy engine (cache by path, metadata regex → revalidated metadata, extension-based Content-Type, `Content-Disposition` + `nosniff`, native 404 with negative cache). 2026-11-10: raw hosted (streaming `PUT` with size limit and `X-Checksum-Sha256/Sha1/Md5` verification, write policy, `DELETE`, audit + activity + live upload transfers) and group repositories (members in order, uploads to the writable member), directory listings (HTML/JSON) for every type, "Set me up" for curl/wget upload and download, the repository form's Hosted/Group tiles for raw; curl and wget pass the real-client harness in replay (proxy, hosted upload/download/delete, group reads and upload). Nexus import creates raw hosted and group repositories; the harness also passes in live mode |
| apt format (T-P2-APT) | M0 slice 2026-11-08: apt proxy repositories pass `InRelease`/`Release` through untouched, serve index files only by the SHA256 the cached `Release` lists (by-hash fetch, one `Release` revalidation on mismatch, then 502), accept self-validating `by-hash` paths, index cached `Packages` files into `apt_pool_index` and validate pool files against it (or record them `unverified`); several suites per repository. apt on Debian 13 and Ubuntu 26.04 passes the real-client harness in replay (`*-updates` and `*-security` suites). Hosted, Raspberry Pi OS / arm64 clients and worker-side re-verification pending |
| npm proxy (T-P2-NPM) | M0 slice 2026-11-08: packuments (full and abbreviated, cached separately, revalidated; scoped names in every spelling) with tarball URLs rewritten to the repository; tarballs verified against `dist.integrity`/`dist.shasum` before they are cached; `npm ping`, search (5-minute cache) and audit relayed uncached (`audit_passthrough`). Real npm 11 installs (lockfile-pinned and lockfile-free, scoped packages, `npm audit`) pass in the harness. Hosted and group: see T-P5-NPMH. pnpm/Yarn/Bun harness runs pending |
| npm hosted + group (T-P5-NPMH) | Done 2026-11-10: hosted publish (bounded, verified, tarball inspected in a thread), packuments generated from `npm_versions` and stored as metadata assets (compare-and-set on `npm_packages.rev`), deprecate, unpublish (version and package), dist-tags endpoints, search; `npm login --auth-type=legacy` issuing an API token and `npm logout` (`waystation_server.client_login`, descriptor `client_login`/`request_action`); groups merging packuments of proxy and hosted members with tarballs from the winning member and writes to the writable member; UI create flow for hosted/group, hosted browse, "Set me up" (`.npmrc` `_authToken`, `npm login`, `publishConfig`, publish/tag/deprecate/unpublish). Harness: npm 11 login, publish (group and hosted), install through the group with a proxied dependency, dist-tag, deprecate, unpublish, logout. pnpm/Yarn/Bun publish not in the harness |
| PyPI (T-P5-PYPI) | Done 2026-11-10: proxy (PEP 503 HTML and PEP 691 JSON pages negotiated by `Accept` and cached per representation, normalised-name and trailing-slash redirects, file URLs rewritten into the repository with hashes, requires-python, yanked and core-metadata kept; files and PEP 658 `.metadata` files verified against the page's hashes before they are cached; legacy JSON API pass-through), hosted (twine / `uv publish` upload API streamed into the blob store, metadata read from wheels and sdists in a thread with zip/tar-slip checks, write policy, `.metadata` for wheels, yank API), group (merged pages, files from members, uploads to the writable member); browse (`pypi/projects`, `pypi/versions`) and search on `pypi_packages`; PEP 440 version order for cleanup; UI create flow, options, browse with yank, "Set me up" (pip, uv, Poetry, PDM, twine, uv publish). Harness: pip 26, uv 0.13, twine 7 against recorded pypi.org fixtures. Poetry/PDM harness runs pending |
| Go modules (T-P5-GO) | Done 2026-11-10: proxy (GOPROXY protocol: `@v/list` and `@latest` as metadata, canonical `.info`/`.mod`/`.zip` as content, `.info` queries as metadata; module paths and versions case-decoded, validated and re-encoded; upstream `404`/`410` → `404`; size limits), `.mod`/`.zip` verified against the checksum database's `go.sum` lines (`h1:` dirhash in a thread, bounded) before they are cached (`verify_checksums`, `nosumdb` patterns), checksum database proxy at `sumdb/<name>/` (`supported`, `latest`, `lookup`, tiles: full tiles immutable, hash tiles size-checked), group (merged `@v/list`, files from the first member holding them, sumdb from the first proxying member); `go_modules` browse (`go/modules`, `go/versions`) and search; Go semver order for cleanup; UI create flow, options, Go browser, "Set me up" (GOPROXY, GONOSUMDB, `.netrc`). Harness: go 1.25 `go mod download`/`go run` through a proxy of proxy.golang.org with sum.golang.org verified through the proxy, and through a group with `.netrc` over TLS, recorded and replayed. Cached version lists and `@latest` are measured by the k6 metadata scenario (T-P2-PERF) |
| Docker hosted (T-P2-DOCKERH) | Done 2026-11-09: docker hosted repositories (REST/UI type `hosted` with write policy and immutable tag patterns): chunked/monolithic/resumable uploads into the blob store (sha256-verified, `upload_sessions`), cross-repository mounts with RBAC on the source, manifest validation, tags with immutability, deletes, referrers API, `tags/list`, `/v2/_catalog` for proxies and hosted (RBAC-filtered), audit `content.upload`/`content.delete`, live upload transfers, browse of pushed images, "Set me up" for push/buildx/crane/oras/cosign. docker push and buildx multi-platform, skopeo, crane, oras and cosign 3 (offline, signatures via referrers) pass the client harness, and so does the OCI distribution-spec conformance suite v1.1.1. Quotas (T-O03): hard quotas refuse pushes with `403 DENIED` |
| Docker group (T-P2-DOCKERG) | Done 2026-11-09: docker group repositories (REST/UI type `group`: ordered members, optional hosted writable member): reads in member order, blobs from any member's copy first, merged tags and referrers, pushes/uploads/deletes to the writable member with RBAC on it, read-only groups `405`; client harness push through a group, member order and merged tags; the OCI conformance suite passes through a group as well |
| Docker proxy (T-P2-DOCKERP) | M0 slice 2026-11-08: the pull side of the registry API for docker proxy repositories at `/v2/<repo\|alias\|default>/...` and `/repository/<repo>/v2/...`: tag manifests as metadata (one asset per Accept variant, stale tags re-checked with an upstream `HEAD`, bytes checked against the upstream `Docker-Content-Digest`, never converted), digest manifests and blobs as content verified against the digest (blobs shared per repository), tags/list with `n`/`last` and rewritten `Link` (5-minute cache), `HEAD` misses answered by an upstream `HEAD`; Docker Hub (`library/`, `auth.docker.io` tokens, `ratelimit-*` recorded for upstream health) and any `WWW-Authenticate` registry (GHCR) with tokens cached per repository; OCI error codes. Docker daemon (mirror mode, prefix, alias) and skopeo pass the real-client harness against Docker Hub and GHCR in replay. Hosted and `_catalog` since T-P2-DOCKERH, groups since T-P2-DOCKERG; referrers for proxies and podman/containerd harness runs pending |
| Deployment docs (T-M03) | 2026-11-09: `Operations/DEPLOYMENT.md` (single-host compose as it works today, env reference, TLS options, scaling status, backups, upgrades) and `Operations/REVERSE_PROXIES.md` (nginx, Traefik labels + file provider, Caddy, HAProxy, Synology DSM) with example files checked in containers by `make proxies-check`. 2026-11-10: `Operations/BACKUP.md` (T-O07) |
| Monitoring (T-O06) | 2026-11-09: Grafana dashboard (`Build/monitoring/grafana/waystation-dashboard.json`: request rate, cache hit ratio, bytes served from cache / fetched, upstream errors and latency, coalescing, per-repository table) and Zabbix 7 template (`Build/monitoring/zabbix/waystation_by_http.yaml`: HTTP agent items on `/healthz`, `/readyz`, `/metrics`, repository discovery, down/degraded upstream, error-ratio and availability triggers), both checked by loading them into Grafana 12.2 / Zabbix 7.0; `Operations/MONITORING.md`. 2026-11-09 (gaps): upstream state and consecutive failures, upstream rate limits, job queues (waiting, running, delayed, oldest waiting age), dead letters, live workers and scheduler leaders, blob store used/free/size and the free-space guard (cluster figures read on scrape, 10 s cache), free-space guard refusals, HTTP latency per route template, event-stream clients; multi-process aggregation for `SERVER_WORKERS > 1` (`PROMETHEUS_MULTIPROC_DIR`); dashboard rows and Zabbix items, discovery and triggers (low disk, guard, workers, scheduler, job age, dead letters, upstream state, rate limit, API latency) for all of them, re-validated in Grafana 12.2 / Zabbix 7.0; cleanup and blob GC totals exported as cluster figures (also with several server processes), with dashboard panels, Zabbix items and a GC-not-run trigger; 2026-11-10: quota and low-disk panels, items, discovery and triggers (T-O03); a unit test checks both files against the exported metric names |
| LDAP provider (T-A01) | Done 2026-11-09: several LDAP providers with server failover, LDAPS/StartTLS/clear, custom CA (host names verified), encrypted service bind, user search + attribute mapping, groups by memberOf or group search with optional nesting, require-group gate, bind cache for `docker login`; admin API + "Authentication providers" page with test-connection/test-user and group-mapping table; provider choice on the sign-in page. Tested against OpenLDAP (FreeIPA-style tree) and Samba AD containers |
| OIDC provider (T-A02) | Done 2026-11-09: several OIDC providers (discovery, code + PKCE S256, state + nonce bound to the browser, ID token checks with JWKS rotation and an asymmetric alg allowlist, optional userinfo, claim and group-claim mapping, require-group, auto-provision toggle, RP-initiated logout), write-only encrypted client secret, IdP calls on the pooled client + SSRF guard; admin form with test discovery; "Sign in with" buttons. Tested against Keycloak and (opt-in) authentik. JOSE library: `joserfc` (ADR 0004, accepted 2026-11-09) |
| Identity linking and group sync (T-A03) | Done 2026-11-09: provider-neutral linking (same-username rules), group sync on sign-in, group → role mapping as `groups` documents applied per request (`security.groups`), LDAP and OIDC provider admin forms; Users/Groups admin pages (T-U08) |
| yum format (T-P2-YUM) | Proxy slice 2026-11-09: yum proxy repositories keep the upstream layout (every `repodata/` directory below the upstream URL), pass `repomd.xml` (+ `.asc`) through untouched after a defused, bounded parse, serve repomd-listed files only by the checksum the cached `repomd.xml` lists (one revalidation on mismatch, then 502), accept self-validating by-checksum names, index cached `primary.xml` files into `yum_packages` and validate packages against them (or record them `unverified`); "Set me up" `.repo` files for Rocky, AlmaLinux, Oracle, EPEL, Fedora and generic upstreams; browse is the generic folder tree. dnf on Rocky Linux 10 and AlmaLinux 10 passes the real-client harness in replay (`extras`). Hosted, group, Oracle Linux 9 / EPEL clients and a yum package view pending |
| Cleanup policies (T-O01) | Done 2026-11-09: named policies (criteria per SPEC §4.8, per-format version order, docker tag cascade), dry-run preview API (same planning code as runs), `cleanup.run` worker job with progress, per-policy cron schedules, attachment per repository, REST + UI (`/cleanup` page with live impact count, candidate preview and run history; repository Cleanup tab); downloads recorded off the hot path into `assets.last_downloaded_at`/`download_count`. Audit records pending (T-S10) |
| Blob GC (T-O02) | Done 2026-11-09: `blob.gc` worker job (every 6 h, 24 h grace): orphan uploads, adoption of untracked files, mark-and-sweep with a claim protocol that makes a racing cache fill safe (property test + real fill-vs-sweep race test), missing referenced blobs reported; GC status/run API and UI card; freed bytes as activity events and Prometheus counters (`waystation_gc_*`, `waystation_cleanup_*`) |
| Backup and restore (T-O07) | Done 2026-11-10: `Build/Containers/backup/waystation-backup.sh` (bash + Docker; `make backup` / `restore` / `verify-blobs`): dump-then-blobs backups with manifest, hard-linked incremental snapshots, retention, `--hosted-only`; restore (archive check, database replace, blobs and keys, ownership, Valkey emptied); `blob.verify` worker job (weekly + on demand; `waystation_worker.run_task` CLI): missing/damaged blobs, proxied assets marked and re-fetched, hosted losses reported (log, result, activity event, `waystation_assets_blob_missing` gauge, `waystation_verify_*` counters), Tasks page labels; `make backup-test` end to end (seed with real clients, backup, wipe, restore, verify, re-fetch, hosted loss); `Operations/BACKUP.md` (procedures, NAS targets, cron and systemd timer). Grafana panels (assets missing their blob, verify findings) and Zabbix items with a "hosted content lost" trigger. Left: deep verify and report-only mode are reachable only through the task parameters (no UI editor yet, T-O04), panels not yet loaded into a live Grafana/Zabbix |
| Quotas and low-disk guard (T-O03) | Done 2026-11-10: per-repository quotas (soft warns, hard refuses new writes) on stored asset bytes (`repo_usage`, incremental + hourly `quota.reconcile`; shared blobs count in each repository), 80 %/100 % alerts in the activity feed and on `/metrics`, docker hosted pushes refused with `403 DENIED`, proxy fills over quota or below the low-disk floor served verified and uncached; low-disk guard settings API (`/api/v1/storage/guard`, bytes and percent), `storage.check` alerts, UI form section, usage meters, shell banner and Storage page card; client harness pushes to a hard-quota docker hosted repository. 2026-11-10: Grafana "Quotas and disk" row and Zabbix quota items, discovery and triggers (hard quota reached, over soft quota, near quota), not yet loaded into Grafana/Zabbix |
| Maven format (T-P5-MAVEN) | Proxy, hosted and group 2026-11-10: Maven proxies (Central or any Maven 2 layout; `maven-metadata.xml` revalidated and refused unless it parses, artifacts verified against the upstream `.sha1`/`.sha256` while streaming, checksum files generated from stored digests, `.asc` passed through), hosted repositories (`mvn deploy`/Gradle publish, version, layout and write policies, server-side `maven-metadata.xml` regeneration serialised per artifact, file and version deletes, audited) and groups (first member wins, merged `maven-metadata.xml`, read-only); components in Maven version order; browse API and UI (artifacts → versions → files); create flow and Set me up for Maven, Gradle and sbt. Real mvn 3.9 resolves through a proxy and a group and deploys releases and SNAPSHOTs in the harness. The Nexus import maps `maven2` repositories. Gradle/sbt/mvn 4 harness runs and Maven components in global search pending |
| Tasks and workers (T-O04) | Done 2026-11-09: REST `/api/v1/tasks/*` (admin): scheduled tasks with last/next run, schedule edit (validated cron/macro/`@every` ≥ 10 s; restarts the timer; cleanup-policy schedules stay with the policy) and pause/resume, run now; job runs with filters, keyset cursor and detail with the capped log (`trigger` schedule/manual/retry on runs); workers (heartbeat health, running jobs with run ids, leader) and queue figures; dead letters with atomic, idempotent retry (`jobs:dead:retried`) and discard; all changes audited (`task.*`). Live: the worker publishes `job.progress`/`job.log` on `live:jobs`; SSE topic `tasks` (admin only) carries those plus `job.update`/`worker.update`. UI `/tasks`: summary strip, tabs Scheduled / Runs / Workers / Dead letters, schedule dialog, run drawer with live log viewer, mobile cards; contract + MSW mock job system with live events; Vitest + API integration tests. Later: GC grace/parameter editor, retry-all, per-kind retry metrics (T-O06) |
| Webhooks (T-O05) | Done 2026-11-10: signed webhooks (REST `/api/v1/webhooks`, admin): definitions with event filter (content and repository events, categories, `*`), repository globs, write-only secret and custom headers (AES-GCM), timeout and attempt limits, private-target exception; events taken from the audit records of content/repository changes into the durable outbox; worker deliveries (`webhook.event`, `webhook.deliver`, `webhook.sweep` every 15 s) at least once with leases, HMAC-SHA256 `Waystation-Signature` over timestamp + body, idempotency key, exponential retries to a dead delivery, SSRF guard at connect time (guard moved to `waystation_common.ssrf`), no redirects, bounded answers; delivery log (30 days) with test event and redelivery; audited `webhook.*`; metrics by outcome and pending. UI Administration → Webhooks (table, editor with generated secret, delivery drawer with attempts, request and redeliver; phone cards); contract + MSW mocks; unit (signature, filters, payloads, backoff), integration (fake receiver: real uploads → signed delivery, retry → dead → redeliver, SSRF refusal, timeout, metrics, audit acceptance) and Vitest tests; `Operations/WEBHOOKS.md` with verification snippets. Later: events of worker-side deletes (cleanup runs), Grafana/Zabbix panels for the webhook metrics |
| Search and command palette (T-U06) | 2026-11-09: `GET /api/v1/search` (name/version prefix and digest search over docker tags, npm packages, apt/yum listings and cached files; disjunctive facets; keyset cursor paging; RBAC-filtered incl. counts; Browse deep links) on new partial `search_name`/`search_base` indexes with migration `0002_search_keys` backfilling existing content, and an integration test that explains every query (index used, no in-memory sort). UI: Search page (search box, facet sidebar, result rows with format icons, keyboard navigation, state in the URL, empty states) and the `Ctrl/⌘ K` command palette (ARIA combobox/listbox: quick search, recent items, repositories, pages, actions; lazy-loaded). Left: substring matching, the 1M-asset p95 benchmark |
| Import from Nexus (T-M01) | 2026-11-09: `POST /api/v1/import/nexus/preview` (dry run) and `/apply` (admin): reads a Nexus 3 configuration over its REST API (SSRF-guarded, optional CA bundle, bounded; credentials used per request, never stored or logged), maps names, formats, types, upstreams, ages, negative cache, auto-block, docker Hub/aliases/default repository, apt distribution, raw disposition, hosted write policy and group members (where Waystation has those types: docker and, since T-P2-RAW, raw hosted and group), reports create / exists / unsupported / invalid with field-level differences, applies the selection idempotently with a `repository.create` audit record per repository; UI page Repositories → "Import from Nexus" (connect, plan table with differences, selection, apply), MSW mocks. Tested against hand-written Nexus 3 REST fixtures (`Application/Server/tests/fixtures/nexus3`); not yet against a live Nexus 3 container |
| Declarative provisioning (T-O09) | Done 2026-11-10: YAML provisioning file (`WAYSTATION_PROVISION_FILE`; schema from pydantic models, JSON Schema committed and checked; safe bounded YAML without aliases or duplicate keys) declaring repositories (every format, type and option, cleanup attachments, group members created by the same file), cleanup policies, custom roles, local groups, service accounts, LDAP/OIDC providers with group → role mapping, webhooks and settings (instance name, 2FA policy, anonymous access, default docker repository, low-disk guard); secrets only as file/env references; plan (create / update with field diffs, secrets redacted / unchanged / prune) and apply with the API's own validation, all-or-nothing on validation, idempotent; applied by `init` at start (fails `init` when invalid), on demand from Administration → Provisioning (`/api/v1/provisioning`, admin) and `python -m waystation_server.provisioning`; `managed_by: provisioning` tags with adoption, release or guarded prune; audited with `details.source = provisioning` (cleanup policy changes are now audited too: `cleanup_policy.*`); "Managed" label and read-only forms in the UI; `make provision-check|plan|schema`; `Operations/PROVISIONING.md` with a full example |
| Nexus migration guide + validation (T-M02) | 2026-11-10: `Documents/Operations/MIGRATING_FROM_NEXUS.md` (inventory, import, credentials and access, hosted content copy with skopeo/crane and a raw asset loop, cache warm-up, side-by-side validation with expected differences, client switch per format, reverse-proxy cutover per repository and docker connector ports, rollback from the audit log, decommission); `Build/migration/nexus_compare.py` (standard library only; checks from TOML; docker Bearer challenge, redirects without credentials to other hosts, base-URL and JSON normalisation, JSON Pointer comparisons; table/Markdown/JSON report; exit 0/1/2), `checks.example.toml` (docker, npm, apt, yum, raw, pypi), `make nexus-compare`; tested against two fake servers in `make test` (no real Nexus) |
| Administration UI (T-U08) | Done 2026-11-09: Users (list, filters, keyset paging, create/edit, reset password, unlock, disable, sign out everywhere, delete) and a page per user (identities, groups, effective permissions, sessions, tokens); Groups (local + members, mapped roles); Roles (permission matrix editor, held by) and Anonymous access; API tokens (all tokens, revoke) and Service accounts (tokens shown once, permissions); Settings (instance name, docker default repository, environment limits); System (version, MongoDB/Valkey health, data volume). New API `GET/DELETE /users/{id}/sessions`, `GET/PUT /settings`, `GET /system`; MSW mocks for every admin endpoint. Editable retention and UI defaults, support bundle (T-O10) pending |
| Ansible Galaxy format (T-P5-GALAXY) | Done 2026-11-10: Galaxy v3 proxy, hosted and group repositories (FORMATS.md "Ansible Galaxy"): discovery, collection, version list (every upstream page) and version documents cached as metadata, validated and rendered with this repository's URLs (pagination links, `download_url`), Galaxy NG `plugin/ansible/content/.../collections/index` paths accepted (and used upstream: `collections_path` option), artifacts verified against `artifact.sha256` and size; hosted `ansible-galaxy collection publish` (multipart, `sha256` checked) with the `import.galaxy` worker job through the outbox (archive read safely: tar-slip guards, `MANIFEST.json`/`FILES.json`, per-file sha256) and import-task polling, immutable versions, delete; groups merge version lists (first member wins) and publish to the writable member; legacy v1 roles proxied read-only (archives cached unverified); `galaxy_collections`/`galaxy_imports` with SemVer `version_sort`; browse namespaces → collections → versions with a UI renderer, create flow and "Set me up" (`ansible.cfg` server list with a token, install, publish); cleanup by collection in SemVer order; real-client harness `tests/clients/galaxy/` (ansible-core 2.22: install from galaxy.ansible.com through the proxy, role install, build + publish to hosted and through the group, install through the group and with `ansible.cfg`, delete; replay passing); `Documents/Operations/clients/galaxy.md` |
| Helm format (T-P5-HELM) | Done 2026-11-10: Helm proxy, hosted and group repositories (FORMATS.md "Helm"): proxy `index.yaml` validated with a safe YAML loader and rewritten (relative chart URLs, `_ext/<scheme>/<host>/...` for charts hosted elsewhere, served as JSON), charts fetched only when the cached index lists them and verified against its sha256 `digest`, `.prov` pass-through; hosted uploads by `PUT` and the ChartMuseum API (`api/charts`, multipart), chart packages read safely (tar-slip guards, size limits, `Chart.yaml` validation), write policy, deletes, `index.yaml` regenerated by the worker job `index.helm` through the outbox and published atomically (cleanup runs trigger it too); group index merged (first member wins per version) and served by X-Accel; `helm_charts` with SemVer `version_sort`; browse `helm/charts` and `helm/versions` with a UI renderer, create flow (proxy presets, hosted, group) and "Set me up"; real-client harness `tests/clients/helm/` (helm 3.19 and 4.0: repo add/search/pull/template through proxy and group, curl and ChartMuseum uploads, index rebuild, delete; replay passing); `Documents/Operations/clients/helm.md` |
| Minimal-permission users (T-U10) | Done 2026-11-10: a signed-in user without roles (or with only a reader or the anonymous grants) gets consistent answers (403 `permission_required`, filtered listings, hidden = 404, never 401) and a calm UI: rail, routes and palette by capability (new `repositories.browse`), "No access to this page", the dashboard landing ("Your repositories" / "no access yet"), no event stream without `stats-read`. Tests: no-role/anonymous-grants/reader columns in the route matrix (unit + integration), Vitest for navigation, guard, live store, landing, mock rules; `make ui-smoke` (Playwright on the mock build, zero console errors) |
| Terraform provider mirror (T-P5-TFP) | Done 2026-11-10: provider network mirror (proxy) repositories (FORMATS.md "Terraform / OpenTofu"): `index.json` and `<version>.json` rendered per request from cached origin metadata (`zh:` per platform from the signed `SHA256SUMS`, `h1:` for cached archives), archives cached as content; the origin is reached through service discovery and the provider registry protocol, archives from wherever the registry points (redirects and SSRF guard per hop); `SHA256SUMS` cached only after its OpenPGP signature verified with the registry's signing keys (`waystation_common.openpgp`, adr/0006 proposed) and archives verified against it; origin allowlist per repository; `terraform_providers` browse (`terraform/providers`, `terraform/versions`) and search; SemVer cleanup; UI create flow, options, browse renderer, "Set me up" (CLI configuration, credentials block); real-client harness `tests/clients/terraform/` (terraform 1.17 and tofu 1.13 init + apply through a TLS front, both registries, bearer token; replay passing); `Documents/Operations/clients/terraform.md`. Modules and the hosted repository: T-P5-TFM |
| Everything else | Not started |
| M0 feedback build (T-V02) | Done 2026-11-09, tagged `v0.1.0-m0`: `make e2e-m0` runs real docker, npm, apt (Debian 13) and raw clients through a running stack and checks the live stream, stats, activity and browse; `Documents/Operations/TRY_IT.md` (fresh host from images and an env file); real-server screenshot tour `Documents/Notes/UI_TOUR_M0.md`; feedback log `Documents/Notes/UI_FEEDBACK.md` |

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

