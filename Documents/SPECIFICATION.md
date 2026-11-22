# Waystation specification

The product specification of Waystation as built: what it does, how it is deployed and how it is verified.
Architecture rules are in `Architecture/ARCHITECTURE.md`, per-format protocol detail in `Architecture/FORMATS.md`,
the UI in `UI_DESIGN.md`, day-2 operation in `Operations/ADMIN_GUIDE.md` and the REST API in `API/README.md`.
How the product was built (task breakdown, build status, notes) is kept under `History/`.

## 1. Purpose

Waystation is a self-hosted artifact repository for a home lab, team or small organisation:

1. **Cache**: a pull-through proxy for public package ecosystems, so every machine pulls packages and images
   once over the internet and then at LAN speed, keeps working through upstream outages and rate limits, and is
   shielded from upstream disappearance of versions.
2. **Registry**: a private registry for your own container images and packages (Artifactory-style), used
   by CI runners and deployments.
3. **Window**: a fast, live, professional web UI that shows what is being fetched right now, what is cached,
   how much bandwidth it saves and how healthy each upstream is.

It is an open-source alternative to Sonatype Nexus Repository and JFrog Artifactory for small and medium
deployments, with no component or request caps and no telemetry.

## 2. Goals and non-goals

Goals:
- Broad format coverage (§4.5): Docker/OCI, npm, PyPI, Maven, apt, yum, raw, Go, Helm, Terraform/OpenTofu, Ansible
  Galaxy, Swift, NuGet, Cargo, RubyGems, Composer, Conda, Alpine apk and Hugging Face.
- A straightforward migration from Nexus: repository configuration is imported from a running Nexus (§4.12).
- Drop-in URLs: Nexus-compatible path layout `/repository/<repo-name>/...`, so clients switch with no changes.
- Fast: cache hits served by nginx `sendfile` from disk; the API and UI feel instant (§5.1 budgets).
- Clean separation: web / server / workers / database / cache as separate containers; workers scale
  independently; the server is stateless.
- One storage path (`WAYSTATION_DATA_DIR`) holds every artifact; it can live on local disk or a NAS share.
- Local, LDAP and OIDC sign-in; RBAC down to repository and action; API tokens for CI and clients.
- Zero telemetry, no usage caps, no phone-home.

Out of scope: clustering across sites and replication, vulnerability scanning, S3/object storage for blobs (the
storage interface is designed to allow a driver), build tooling, a CLI beyond what format clients already provide,
and Windows containers for the server.

## 3. Concepts

| Term | Meaning |
|---|---|
| **Format** | A package ecosystem protocol (docker, npm, pypi, maven, apt, yum, go, helm, terraform, galaxy, swift, nuget, cargo, rubygems, composer, conda, apk, huggingface, raw). Implemented as a server plugin. |
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
  owner goes away (its replica dies or shuts down) a follower takes the fetch over.
- **Fills finish without their client**: once a content fill has started it runs to completion (or upstream
  failure) even when the client that triggered it disconnects (an aborted `docker pull` or `apt` run, a client
  timeout on a multi-GB layer): the file is verified, cached and indexed, coalesced followers are served from it and
  the next request is a cache hit. Bounded by the upstream timeouts, a stall limit, the size limit and the low-disk
  guard; server shutdown cancels such fills (nothing partial is kept).
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
- Upstream credentials per repo (basic, bearer, Docker Hub token login, GHCR PAT), stored encrypted. Tokens an
  upstream registry issues (Docker token realms) are shared by every server replica through Valkey, encrypted with
  the secret keys and kept no longer than they are valid, so one replica's token serves all.
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
  sets `allow_private_upstream: true`. IPv6 forms that stand for an IPv4 address (IPv4-mapped, IPv4-compatible,
  NAT64 `64:ff9b::/96`) are judged by that IPv4 address; 6to4, Teredo and site-local addresses are refused.
- **Prefetch** (per proxy repository `proxy.prefetch`; adr/0012): formats docker, npm, raw, Maven, Go, NuGet,
  Composer, Cargo, RubyGems, Conda, Hugging Face and Alpine apk (other formats refuse the settings).
  - *Keep popular metadata fresh* (`refresh_popular`, off by default): the built-in task `prefetch.refresh` (every
    15 minutes, queue `prefetch`) takes the repository's metadata files downloaded in the last 7 days, most downloaded
    first (at most 200), and revalidates those that are stale or go stale before the next run (conditional request;
    a `304` only moves the timestamp), so clients get fresh cache hits instead of waiting for the upstream. Content is
    never refreshed. Docker tags are re-checked with an upstream `HEAD` first (no Docker Hub pull counted).
  - *Warm list* (`warm`, at most 200 items in the format's syntax, FORMATS.md "Prefetch"; `schedule` cron/macro/
    `@every`, at least 15 minutes apart, or none = "Warm now" only): each item is fetched the way its client would
    (a docker image's index, the chosen platforms' manifests, config and layers; an npm package's packument and
    tarball; a Maven POM and artifact; a Go module's `.info`/`.mod`/`.zip`; a NuGet package's version list, `.nupkg`
    and `.nuspec`; a Composer package's `p2` document and archive; a gem's info file, `.gem` and gemspec; a raw path). Items already cached cost
    no upstream request. The repository page shows the last run and starts one ("Warm now"); the schedule is the
    task `prefetch:<repository id>` on the Tasks page (edited in the repository's settings).
  - Prefetch goes through the same proxy path as clients (validation, digests, coalescing: a client asking for the
    same file at the same moment shares the fetch), skips offline and auto-blocked repositories, and is never counted
    as a download or as client traffic (download counts, "last downloaded", traffic charts), so it cannot make
    anything look popular. Live transfers show it with the principal `prefetch`. Needs `WAYSTATION_SECRET_KEYS`
    (the worker proves itself to the server with a signed grant) and `WAYSTATION_INTERNAL_URL` (where workers reach
    a server; default the bundled stack's `http://waystation-server:8000`).

### 4.2 Hosted repositories
- Upload via each format's native client (docker push, npm publish, twine upload, mvn deploy, helm push,
  curl PUT for raw/apt/yum, swift package-registry publish, ...).
- **Write policy** per repo: `allow_redeploy` / `disable_redeploy` (default for release repos) / `read_only`.
  SNAPSHOT-style repos (Maven) allow redeploy for snapshot versions only.
- **Docker**: hosted repositories accept `docker push`/`buildx --push` (multi-platform indexes with
  attestations), skopeo, crane, oras and cosign: chunked, monolithic and resumable blob uploads verified by sha256,
  cross-repository mounts from any docker repository the client may read (proxy caches included), manifest validation
  (media types, referenced blobs/manifests present with the stated sizes, 4 MiB), write policy (`allow_redeploy`
  default, `disable_redeploy`, `read_only`) and immutable tag patterns, delete by tag, digest and unreferenced blob,
  the referrers API (with `artifactType` filter; proxies: the upstream's referrers API, else its
  `sha256-<hex>` tag schema, cached as metadata; groups merge every member's), `tags/list` and `/v2/_catalog`
  (filtered by RBAC). Pushes need
  `write`, deletes `delete`; pushed manifests and deletes are audited. The OCI distribution-spec conformance suite
  passes (pull, push, content discovery, content management). A hard quota (§4.8) refuses blob uploads, mounts and
  manifest pushes with `403 DENIED` and a message naming the quota; the low-disk guard answers `507`.
- **PyPI**: hosted repositories accept twine and `uv publish` (the legacy upload API, `POST` to the repository
  URL, multipart streamed into the blob store): wheels and sdists whose core metadata (read from the archive in a
  thread, bounded, zip/tar-slip checked) agrees with the file name and form, `sha256_digest` verified; a wheel's
  `METADATA` is also served as its PEP 658 `.metadata` file. Write policy as for docker (`disable_redeploy` accepts
  the same bytes again and refuses other content with "File already exists"). Project pages (PEP 503 HTML and PEP 691
  JSON) are rendered from the stored files per request; releases can be yanked (PEP 592) from Browse or
  `POST /api/v1/repositories/{name}/pypi/yank` (`write`). Uploads need `write`; uploads and yanks are audited.
- **npm**: hosted repositories accept `npm publish` (one version and its base64 tarball per request, ≤ 100
  MiB; `dist.integrity`/`shasum` and the tarball's own `package.json` checked against the version; the tarball read
  in a thread without extraction, bounded), and generate their packuments (full and abbreviated, `ETag`/`304`) from
  the stored versions after every change. `npm deprecate`, `npm unpublish <pkg>@<version>` and `--force` (needs
  `delete`), `npm dist-tag add/rm/ls` (`write`; `latest` cannot be removed) and `npm search` work. Write policy as for
  docker; `disable_redeploy` answers npm's "You cannot publish over the previously published versions". Publishes
  are audited `content.upload`, deprecations and tag changes `content.update`, unpublishes `content.delete`.
- **Raw**: `curl --upload-file` (`PUT`) and `DELETE` on any path; `X-Checksum-Sha256`/`-Sha1`/`-Md5` headers verified
  before anything is stored; write policy as above (`disable_redeploy`: a path is written once, 409; deletes stay
  allowed); directory listings (HTML, or JSON for scripts) at the root and any path ending in `/`. Uploads need
  `write`, deletes `delete`; both are audited and appear in the activity feed.
- **Helm**: `curl --upload-file` (`PUT`) and the ChartMuseum API (`POST api/charts`, as `helm cm-push` sends it);
  provenance files; delete by file or `api/charts/<name>/<version>`. Chart packages are checked before they are stored (single top-level directory, no links or
  unsafe names, size limits, `Chart.yaml` with a valid name and SemVer version) and stored as
  `<name>-<version>.tgz`; write policy as above; the worker rebuilds `index.yaml` from the stored charts through the
  outbox (seconds after an upload or delete) and publishes it atomically. Uploads and deletes are audited.
- **Ansible Galaxy**: `ansible-galaxy collection publish` (multipart to `api/v3/artifacts/collections/`, answered
  with an import task the client polls at `api/v3/imports/collections/<task>/`); delete a version or a whole
  collection. The upload (`<namespace>-<name>-<version>.tar.gz`, ≤ 256 MiB) streams into the blob store and
  must match its `sha256` field; the import task and its `import.galaxy` job are written in one transaction
  (outbox); the worker validates the archive without extracting it (tar-slip guards, `MANIFEST.json` naming the
  same collection version, `FILES.json` matching the manifest checksum, every file's sha256, no unlisted files) and
  only then lists the version. Versions are immutable (409; delete first); `read_only` refuses (403). Uploads and
  deletes are audited.
- **Swift Package Registry**: `swift package-registry publish` (SE-0391: `PUT <scope>/<name>/<version>`, multipart
  with the source archive, optional release metadata and signature), answered synchronously (`201`); the archive
  (≤ 512 MiB, hard quota first) is checked in a thread before it is stored (one top-level folder, safe entry names,
  no links leaving the package, bounded size and count, `Package.swift` with a tools version: else `422`); the
  signer's certificate subject is recorded, the signature passed to clients (not verified by Waystation). Releases are
  immutable: a removal (`DELETE`) keeps the version taken (`410` to clients, `409` to a new publish). `read_only`
  refuses (403). Publishing and removals are audited.
- **apt and yum**: workers generate the indexes of hosted repositories and sign them with a per-repository key
  (generated on first use; the public key is downloadable from the repository root and shown in "Set me up").
  Hosted yum repositories accept RPMs by `PUT <path>.rpm` (stored at that path) or `POST` to a
  directory (stored as `<name>-<version>-<release>.<arch>.rpm`), streamed into the blob store after the hard-quota
  check; the header is read in a thread (bounded, offsets checked, header SHA-256 verified) and the package's
  `primary`/`filelists`/`other` entries are rendered then. Write policy: `allow_redeploy` replaces a path,
  `disable_redeploy` refuses an existing one (409), `read_only` refuses (403); the same
  name-epoch-version-release-architecture at another path is refused (409); nothing may be stored under `repodata/`.
  `DELETE <path>.rpm` removes a package. The record and an `index.yum` job are written in one transaction (outbox);
  the worker rebuilds `repodata/` (gzip `primary`/`filelists`/`other`, `repomd.xml` with checksums), signs
  `repomd.xml` with the repository's key (generated by the worker on first use, RSA 3072, stored encrypted under
  `keys/`; adr/0007, accepted) and publishes it atomically (new files first, then `repomd.xml`, `.asc` and the public
  key switched in one transaction; the previous generation's files are kept for clients mid-update). The public key
  is served at `RPM-GPG-KEY` and `repodata/repomd.xml.key`. Before the first build `repomd.xml` answers 404. Key
  import/rotation and RPM package signing are not offered.
  Hosted apt repositories publish one distribution (`distribution`, with `components`, default
  `main`, and `architectures`, default `amd64 arm64`). `.deb` packages are uploaded by `PUT` (`curl --upload-file`)
  or `POST` to the repository (raw body or a multipart file), `?component=` choosing a component; the body streams
  into the blob store after the hard-quota check and the package is read in a thread (ar layout, bounded
  control-archive decompression, tar names checked, one control paragraph with valid `Package`/`Version`/
  `Architecture`) and stored at its pool path `pool/<component>/<prefix>/<source>/<name>_<version>_<arch>.deb`.
  Write policy as for yum; the same package version and architecture in another component is refused (409).
  `DELETE <pool path>` removes a package. The record and an `index.apt` job are written in one transaction; the
  worker builds `Packages` (plain, `.gz`, `.xz`) per component and architecture (`Architecture: all` packages in
  each), publishes them at their `by-hash` paths, then switches `Release`, the clearsigned `InRelease`, `Release.gpg`
  (signed with the repository's key, as for yum), the indexes and the public key (`public.asc`, `public.gpg`) in one
  transaction, keeping the previous generation's `by-hash` files. Before the first upload `InRelease` answers 404.
  Uploads need `write`, deletes `delete`; both are audited. Key import/rotation, source packages and several
  distributions per repository are not offered.
- Quotas per repository (soft warn / hard block) on stored bytes (§4.8).
- **Maven**: hosted repositories accept `mvn deploy`, Gradle `maven-publish` and curl `PUT` (live upload
  transfers after the hard-quota check), with a version policy (`release` / `snapshot` / `mixed`), a layout policy
  (`strict` Maven paths / `permissive`) and the write policy (`disable_redeploy` never replaces a release file;
  SNAPSHOT versions always redeploy); refusals answer `400`, `read_only` `403`. Uploaded checksum files are compared
  with the stored file and not stored (checksums are generated on request); `maven-metadata.xml` is regenerated by
  the server after every deploy and delete, serialised per groupId:artifactId, so concurrent deploys lose nothing.
  Files and version folders can be deleted (`delete`). Uploads and deletes are audited.
- **Terraform**: an instance has **one** hosted Terraform repository (a second is refused), announced by
  `/.well-known/terraform.json`: a private module registry (`curl --upload-file module.tar.gz` to
  `v1/modules/<namespace>/<name>/<system>/<version>`; the archive's root must hold the module's `.tf` files; read
  without extracting, bounded) and a private provider registry (a multipart `POST` to
  `v1/providers/<namespace>/<type>/<version>` with the platform zips, `SHA256SUMS`, its detached signature and the
  public key it verifies with; checked as the clients will). Versions and releases are immutable (409; delete first);
  `read_only` refuses. Uploads need `write`, deletes `delete`; both are audited. The files a download answer points
  at carry short-lived signed grants for signed-in callers (adr/0010), because terraform and tofu do not
  send their token with file downloads.

### 4.3 Group repositories
- Ordered members of the same format (proxy and hosted mixed). Reads resolve first match in order; metadata that
  must be merged is merged (npm packuments, PyPI simple pages, Maven `maven-metadata.xml`, Helm `index.yaml`,
  Go `@v/list`, docker tag lists). apt/yum groups are not supported (index merging + signing is out of scope).
- **Docker**: a group may have a **writable member** so `docker push` to the group lands in that hosted repository.
  Docker groups (up to 50 docker proxy/hosted members, no nested groups) answer manifests from the
  first member that has them (a proxy member asks its upstream), blobs from any member's stored or cached copy first
  (a blob `HEAD` never goes upstream), merged `tags/list` and referrers; pushes, upload sessions and deletes go to the
  writable member (a hosted repository), which the client must be allowed to write too; without one the group is
  read-only. Reading through a group needs `read` on the group only (its members' content is served through it,
  as in Nexus). A repository that is a group member cannot be deleted until it is removed from the group.
- **Maven**: groups (proxy and hosted members, in order) answer files from the first member that has
  them (404 and failing members skipped; members whose version policy excludes the version not asked) and merge
  `maven-metadata.xml` per request (versions union, `latest`/`release` recomputed in Maven order, newest SNAPSHOT
  build, plugin prefixes; checksum files from the same bytes); they are read-only (405).
- **Raw**: groups (raw proxy/hosted members) answer each file from the first member that has it, list
  folders as the members' listings merged, and send uploads and deletes to the writable member (a hosted raw
  repository the client may also write to); without one the group is read-only.
- **Helm**: groups serve one `index.yaml` merged from their members' indexes (the first member that
  lists a chart version wins it), charts from the first member that has them, and send uploads and deletes to the
  writable member.
- **Ansible Galaxy**: groups merge their members' collection version lists (the first member that has
  a version wins it), answer version documents and artifacts from the first member that has the version and roles
  from the first proxy member, and send publishing (and import-task polling) to the writable member.
- **PyPI**: groups merge their members' project pages per request (union by file name, the first member
  wins; a proxy member's cached page, refreshed as needed), serve files from the first member holding them (else the
  proxy members in order) and send uploads to a hosted writable member.
- **Go**: groups (Go proxy members) merge `@v/list` per request (union, newest first, `ETag`/`304`), answer
  `.info`/`.mod`/`.zip` from any member's cached copy first, then `@latest`, queries and misses from the members in
  order, and the checksum database from the first member that proxies it.
- **npm**: groups (npm proxy and hosted members) merge packuments in both representations (versions
  union, the first member that has a version wins it; dist-tags from the first member that has the tag; tarball URLs
  rewritten to the group), kept in the blob store and served with `ETag`/`304` until a member changes; tarballs come
  from the members in order (so the member that won a version serves it); `npm publish`, `deprecate`, `unpublish`
  and `dist-tag` through the group go to the hosted writable member (`?write=true` reads answer that member's own
  packument); without one the group is read-only.
- NuGet, Cargo, RubyGems and Composer groups are described in FORMATS.md. Conda, apk, Hugging Face, Swift and
  Terraform have no group repositories.

### 4.4 Routing and URLs
- One base URL (`WAYSTATION_BASE_URL`, e.g. `https://registry.example.com`). TLS is terminated by the web tier or
  by an external reverse proxy in front of it (both supported; `WAYSTATION_TRUST_FORWARDED`).
- **Nexus-compatible layout**: every format under `/repository/<repo>/...`.
- **Docker/OCI** additionally answers the registry API at `/v2/`:
  - `/v2/<repo>/<image>` addresses repository `<repo>` (path routing).
  - **Upstream aliases**: a docker proxy can declare aliases (e.g. `docker.io`, `ghcr.io`), so
    `registry.example.com/ghcr.io/org/app:1` works.
  - A **default docker repository** (setting) receives unprefixed names, so `registry.example.com/library/nginx`
    and the Docker daemon `registry-mirrors` setting work (mirror mode only ever asks for Docker Hub names).
  - **containerd mirrors**: containerd adds `?ns=<registry>` to requests sent to a mirror. An unprefixed name with
    `ns` goes to the docker repository with that upstream alias, else to the default repository if `ns` is its own
    upstream, else answers `404` (containerd then pulls from the registry itself): a name that also exists on Docker
    Hub is never answered with Docker Hub's image when another registry was asked for. So one
    `[host."https://registry.example.com"]` entry serves every aliased upstream, without `override_path`.
  - **Port connectors** (Nexus style, adr/0013) for clients that need a whole host:port: extra listeners on the
    **web** container (still the only container publishing ports), each mapped to one docker repository. A docker
    repository (proxy, hosted or group) may set `format_options.connector_port`
    (1024-65535, unique across repositories, not the base URL's port; a clone starts without it). The web container
    listens on every port in its `WAYSTATION_DOCKER_CONNECTORS` (comma/space list, checked at start: 1024-65535, not
    the web ports, no duplicates), HTTPS + HTTP/2 on the same certificate when TLS is on, else plain HTTP; those
    listeners answer only `/v2` and `/v2/` (everything else 404) and send `X-Waystation-Connector: <port>` (replacing
    a client's copy; the main listeners never pass one). The server honours the header only from an internal hop (the
    web tier) and then resolves every `/v2/<name>` to the repository with that connector port, whole name, no prefix,
    alias or `ns` (`docker pull registry.example.com:5001/library/nginx`); a port no repository has resolves nothing
    (404). `/v2/_catalog` there lists that repository's images unprefixed. The challenge's realm is
    `<base>/auth/token?connector=<port>`: the token service resolves scopes through that repository and names granted
    scopes `<repository>/<image>`. The connector only selects a repository (like a prefix): RBAC and grants are
    unchanged. Listing the ports and publishing them (same numbers on the host) is operator work
    (`Documents/Operations/DEPLOYMENT.md` §8 "Docker port connectors"); "Set me up" shows `<base host>:<port>` snippets.
- Reserved top-level paths: `/api/`, `/ui/`, `/v2/`, `/repository/`, `/healthz`, `/readyz`, `/metrics`,
  `/.well-known/` (Terraform service discovery), `/auth/` (OIDC callbacks, docker token service).
  `/.well-known/terraform.json` names the hosted Terraform repository's `modules.v1` and
  `providers.v1` to callers who can see it (public route; terraform and tofu send the host's credentials), `{}`
  otherwise.
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

### 4.5 Formats
Protocol detail, limitations and the tested clients per format are in FORMATS.md; client set-up guides are in
`Operations/clients/`.

| Format | Proxy | Hosted | Group |
|---|---|---|---|
| Raw / generic files | yes | yes | yes |
| Docker / OCI (images, Helm OCI, any OCI artifact) | yes (Docker Hub, GHCR, Quay, any OCI registry) | yes | yes |
| npm | yes | yes | yes |
| apt (Debian, Ubuntu, Raspberry Pi OS) | yes | yes, signed | no |
| yum / dnf (Rocky, Alma, Oracle, EPEL) | yes | yes, signed | no |
| PyPI | yes | yes | yes |
| Maven / Gradle | yes | yes (+snapshots) | yes |
| Go modules (+ sumdb proxy) | yes | no | yes |
| Helm (classic index.yaml) | yes | yes | yes |
| Terraform / OpenTofu providers (network mirror) and modules | yes | yes (modules, providers) | no |
| Ansible Galaxy (collections; roles read-only) | yes | yes (collections) | yes |
| Swift Package Registry (SE-0292) | yes (source-archive proxy from git hosts) | yes | no |
| NuGet v3 | yes | yes | yes |
| Cargo (sparse index) | yes | yes | yes |
| RubyGems | yes | yes | yes |
| Composer (PHP) | yes | yes | yes |
| Conda | yes | yes | no |
| Alpine apk | yes | yes | no |
| Hugging Face (models/datasets) | yes | no | no |

### 4.6 Browse and search
- Browse any repository as a tree (paths) or as components (format-aware: images → tags → platforms → layers;
  npm packages → versions; apt suites → packages).
  `GET /api/v1/repositories/{name}/browse?path=` lists the cached assets of any repository as
  folders (first) and files with size, type, sha256 and last-modified, paged by cursor; npm `packages`/`versions`,
  apt `suites`/`packages` (per suite, architecture, component; cached-only filter; `verified` = the cached file
  matches its listing), docker `images`/`tags`/`manifest` (platforms, layers with cached flag and history,
  labels, pull commands) and Maven `artifacts`/`versions` (groupId:artifactId with the highest version,
  versions in Maven order with file count and size; the files are the version folder's listing) show what is held.
  Browsing needs `browse` on the repository (anonymous: proxy repositories). Helm: `helm/charts` and `helm/versions?chart=` list charts (latest version, app version,
  description) and their versions newest first (created, digest, size, cached) for proxy, hosted and group
  repositories. Ansible Galaxy: `galaxy/namespaces`, `galaxy/collections?namespace=` and
  `galaxy/versions?namespace=&collection=` (records in `galaxy_collections`: hosted imports, a proxy's cached version
  lists) show namespaces, collections (latest version, description, `requires_ansible`) and versions newest first by
  SemVer (sha256, size, dependencies, publisher, cached) for proxy, hosted and group repositories. PyPI: `pypi/projects` (summaries in
  `pypi_packages`: latest final release by PEP 440, version count, files held) and `pypi/versions?project=` (versions
  newest first, each with its wheels and sdist: size, sha256, requires-python, yanked, PEP 658 metadata, held or not,
  uploader for hosted files) for proxy and hosted repositories; groups are browsed through their members. Go
 : `go/modules` (summaries in `go_modules`: latest by Go semver, tagged versions, cached versions, zip bytes)
  and `go/versions?module=` (cached list and cached files merged, newest first in semver order with pseudo-version,
  pre-release and `+incompatible` flags, commit time from the `.info`, the `.info`/`.mod`/`.zip` files held) for
  proxy repositories; search finds modules by path. Terraform: `terraform/providers` (summaries in
  `terraform_providers`: provider address, latest release, versions listed, archives cached and their size) and
  `terraform/versions?provider=` (registry-listed and cached versions newest first in SemVer order, with protocols,
  platforms and the archives held: platform, size, sha256, `h1:`, downloads); search finds providers by address
  (and by type, the last segment). Terraform modules: `terraform/modules` and
  `terraform/module-versions?module=` (summaries in `terraform_modules`; proxy: cached version list and the archives
  held with their pinned sha256 and source; hosted: published versions with their publisher) and the hosted
  repository's providers (`<namespace>/<type>`, releases with platforms, `h1:`, signing key ID); search finds modules
  by address.
  Swift: `swift/scopes`, `swift/packages?scope=` and
  `swift/releases?scope=&package=` (records in `swift_packages`/`swift_releases`: hosted releases, a proxy's pinned
  releases) show scopes, packages (latest version, release counts, description, source URL) and releases newest
  first by SemVer (state, tools version, checksum, size, signature and signer, tag and commit, publisher); search
  finds packages by `scope.name`.
  NuGet: `nuget/packages` (summaries in `nuget_packages`: id as spelled, latest stable version, version
  count, description, authors) and `nuget/versions?package=` (newest first in NuGet version order: listed or unlisted,
  prerelease, cached, size, published, pusher) for proxy, hosted and group repositories (a group shows its members',
  first member wins); `POST nuget/listing` unlists or relists a hosted version (`write`); search finds packages by id.
  Composer: `composer/packages` (summaries in `composer_packages`: name, newest stable version,
  version count, description, type) and `composer/versions?package=vendor/name` (newest first in Composer version
  order: version as published and normalised, stability, cached, size, published, uploader, source reference) for
  proxy, hosted and group repositories (a group shows its members', first member wins); search finds packages by name.
  RubyGems: `rubygems/gems` (summaries in `rubygems_gems`: name, newest release, version count, summary)
  and `rubygems/versions?gem=` (newest first in `Gem::Version` order, one row per platform: prerelease, checksum, Ruby
  requirement, runtime dependencies, cached, size, published, pusher) for proxy, hosted and group repositories (a
  group shows its members', first member wins); search finds gems by name.
  Alpine apk: `apk/packages` (from `apk_packages`: name, newest version, architectures, version count,
  description, origin) and `apk/versions?package=` (newest first in apk version order: branch, repository,
  architecture, `C:` checksum, verified, cached, size, installed size, license, origin, dependencies, build time,
  uploader) for proxy (the cached APKINDEX listings) and hosted repositories; search finds packages by name.
  Hugging Face: `huggingface/models` (summaries in `huggingface_repos`: model or dataset, the default
  branch's commit, revisions asked for, cached files and bytes, downloads), `huggingface/revisions?model=` (revisions
  and the commit each resolved to; commits with cached files) and `huggingface/files?model=&commit=` (cached files by
  path); search finds models and datasets by id.
- Global search across repositories the principal can read: name (prefix and substring), version, format,
  repository, digest/checksum lookup ("where is this sha256 used?"), with facets (format, repo, type).
  `GET /api/v1/search?q=&format=&repository=&kind=&type=&cached=&cursor=&limit=` searches
  only the repositories the principal may `browse` (`access.filter_repos`; a hidden repository never appears in a
  hit, a facet value or a count, and naming it as a filter yields an empty result). `q` is
  `<name>[ <version prefix>]`, case-insensitive, matched as a prefix of the full name or of its last `/` segment
  (`nginx` finds `library/nginx`, `node` finds `@types/node`) and, from three characters on, anywhere in the name
  (adr/0009: `setup` finds `nginx-setup.sh`); prefix matches come first (by name), then substring matches
  (by the name from the match on); empty lists everything. Sources: docker tags (kind `image`),
  npm packages, PyPI projects and apt/yum index listings (kind `package`; `cached` says whether the listed file is in the cache),
  Maven components (kind `package`, one per groupId:artifactId:version, matched on the artifactId and named
  `groupId:artifactId`) and cached files of every format but docker (kind `file`; Maven files link to their
  artifact version in Browse). `cached=true` leaves out the apt/yum listings. A digest
  (`sha256:<8-64 hex>` or 64 hex digits) finds every cached file with that blob and the docker tags pointing at it,
  in any visible repository. Results come straight from per-collection `(name_lc, repo_id, _id)` indexes (substring
  matches: the multikey `(name_sfx, repo_id, _id)` index of name suffixes), merged
  with exact keyset cursor paging; facet counts (format, repository, kind, repository type) are disjunctive (each
  ignores its own filter), on the first page only, and lower bounds when `capped`. Every hit carries a Browse deep
  link. The API list/search budget (§5.1) is measured at 1M assets.
- Component pages show: coordinates, versions/tags, size, digests, created/last downloaded/last modified,
  download count, which repositories hold it, upstream URL, uploader (hosted), install/pull snippet.

### 4.7 Stats and live activity
- **Live transfers**: every in-flight download and upload with repo, asset, client (user/IP), bytes done/total,
  throughput, cache status, upstream latency. Cache states (one vocabulary everywhere: API, events, metrics, UI):
  `HIT` (served from cache), `FETCHING` (miss; this request owns the upstream fetch), `COALESCED` (miss; waiting on
  or tailing another request's fetch), `STALE` (served from cache because the upstream failed), `REVALIDATED`
  (metadata checked upstream, unchanged), `ERROR`.
- **Counters** per repo/format/minute: requests, hits, misses, bytes served from cache, bytes fetched upstream,
  errors, p50/p95 latency. **Bandwidth saved** = bytes served from cache that would otherwise have been fetched.
- **Storage**: bytes per repo/format, blob count, de-duplication savings (also per repository: a blob shared by n
  repositories counts 1/n in each), growth over time (hourly storage snapshots).
- **Top N**: most downloaded components (all time or over 24 h / 7 d / 30 d), most bytes served, largest components,
  recently added, busiest repositories. Components are package versions, Maven GAVs, docker tags and raw files,
  indexed by the worker from the cached files (content cached before is indexed the same way). A "most active
  clients" ranking is not provided; per-client figures are available from the activity log and its export.
- **Where used**: the assets holding a blob and the docker manifests referencing a digest.
- **Upstream health**: per proxy repo status (ok/degraded/blocked/offline), error rate, latency, rate-limit headroom.
- Retention: minute resolution 7 days, hourly 90 days, daily 2 years, storage snapshots 2 years, daily download
  counters 400 days (MongoDB time-series collections and TTL indexes, §DATA_MODEL). Hours and days are rolled up by
  the worker once closed, exactly once.
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
- Quotas per repository and a global low-disk guard (refuse new cache writes below a free-space threshold, keep
  serving hits, alert):
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
- **Local** users (argon2id) with optional TOTP two-factor sign-in. A local account with a password can turn on two-factor sign-in (RFC 6238 TOTP: HMAC-SHA1, 6
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
  "require group" gate, custom CA file. Tested against FreeIPA-style (OpenLDAP) and Active Directory (Samba AD)
  directories. Providers are configured under Administration → Authentication providers (`/api/v1/auth-providers`, needs
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
  Multiple providers allowed; the sign-in page shows a button per provider. OIDC providers sit next to LDAP under Administration → Authentication providers (kind `oidc`): issuer
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
  usable local password). A local account with a password is checked only against that password, whatever
  provider the client picks; a successful external sign-in creates the user when there is none (`source`
  `ldap:<provider>` / `oidc:<provider>`; OIDC only when its auto-provision toggle is on), links a password-less local account (which takes the
  provider as source) or a user of another provider (`identities[]` lists every linked external account), and
  refuses disabled users. Group sync: the groups the provider reported replace that provider's entries in
  `external_groups` on every sign-in through it.
- **API tokens**: per user and per service account. A token is `wst_` + 43 random characters, shown once at
  creation; only a keyed hash (HMAC-SHA256 under a key derived from `SECRET_KEYS`, every configured key tried on
  lookup) and a display prefix (`wst_` + 8) are stored. Scopes `read`, `write`, `delete`, `manage`, `stats-read`,
  `metrics`, `admin` (a token can only carry scopes its owner holds) and optional repository patterns (RBAC scope
  syntax: name, glob, `format:`, `type:`) only ever **narrow** the owner's current RBAC grants (its own plus the
  anonymous grants every signed-in principal receives, §4.9; so a user without roles may create a `read` token while
  anonymous reads are allowed). Optional expiry
  (UI presets 7/30/90/365 days or none). Last used time and client address are recorded in Valkey, at most once a
  minute per token, and flushed to MongoDB by the worker. Revoking a token (or disabling/deleting a service
  account, or changing its roles) is announced on the Valkey `revocations` channel and refused by every replica
  within a second; resolved tokens are cached per process for at most 5 s. Rejected tokens are counted per client
  address (`TOKEN_FAILURE_LIMIT` per 5 min, then 429). Presented but invalid credentials are refused (401), never
  downgraded to anonymous: any `Authorization` header outside `/v2/` that does not resolve to a principal (a value
  not shaped like an API token, an unknown scheme, a password instead of a token) gets the 401 challenge, also on a
  repository anonymous may read. Creating a token needs an interactive session (a token cannot mint tokens).
- **Service accounts**: non-human principals holding built-in roles, managed by `user-admin`
  (`/api/v1/service-accounts`, tokens under `/service-accounts/{id}/tokens`); they authenticate only with API
  tokens. Only administrators may give a service account `admin`. `/api/v1/tokens` lists and revokes every token
  (incident response).
- **API token transport** for format clients on `/repository/*` and the API: `Authorization: Bearer <token>`,
  `Authorization: Token <token>`, a bare `Authorization: <token>` (no scheme; format routes `/repository/*` only:
  Cargo sends its registry token that way, and RubyGems its API key for `gem push`/`yank`),
  or HTTP basic with the token as the password (any username). Basic credentials
  whose password is not an API token are refused: account passwords (local or LDAP) are accepted only by the UI sign-in,
  `docker login` and `npm login` (which exchanges them for an API token).
- **Format client auth**: docker (Bearer token service at `/auth/token` per Docker token spec; `docker login` with
  username + local password or an API token as the password, any username; a registry token issued for an API token
  carries its id and is re-checked on every request, so revocation is immediate. `/v2/` accepts only these
  5-minute EdDSA tokens and answers everything else with the `401` Bearer challenge naming the scope; a token's
  grants are re-checked against RBAC on every request. `docker login` (`offline_token=true`) also gets a
  90-day refresh token, which the Docker CLI stores instead of the password; `POST /auth/token` is the token spec's
  OAuth2 endpoint (`grant_type=password`, with `access_type=offline` for a refresh token, as containerd tries first;
  `grant_type=refresh_token`, which re-checks the user or the API token). Issued tokens are revoked before they
  expire when the user is disabled or deleted, their password changes or their sessions are revoked (signing out
  everywhere else included): `/v2/` and the refresh grant refuse every registry token of the user issued until then,
  on every replica at once; tokens issued for an API token live as long as that API token), npm (`npm login --auth-type=legacy` and `_authToken` = API token;
  now: the legacy login exchanges the password, checked like `docker login`, for a new API token of the
  user with the repository scopes they hold, no repository pattern and no expiry, listed and revocable on the profile;
  it needs `read` on the repository and a repository the caller could not see answers any failure like a missing
  repository (404, or the 401 challenge without credentials);
  `npm logout` revokes it), PyPI/twine
  (basic: username + token), Maven (basic in `settings.xml`, username + API token; Maven, Gradle and sbt send it
  after the 401 challenge, ADR 0005; preemptive authentication is optional),
  Helm classic repos (basic: `helm repo add --username --password`), Terraform/OpenTofu (`credentials` block → `Authorization: Bearer`), Ansible Galaxy
  (`Authorization: Token <api-token>`), Swift (`swift package-registry login`: basic or token), NuGet
  (`X-NuGet-ApiKey` = an API token for push and delete, read only without an `Authorization` header; basic for read),
  Composer (`auth.json` `http-basic`: username + API token, also for uploads with `curl -u`), RubyGems (basic with an
  API token in the source URL or `bundle config`; `gem push`/`yank`: `GEM_HOST_API_KEY` = API token, sent bare),
  Cargo (`cargo login` / `CARGO_REGISTRIES_<NAME>_TOKEN` = API token, sent bare; `config.json` answers the
  401 challenge first and then says `auth-required`),
  conda (`~/.netrc` or `https://user:token@host/...` channel URLs: basic with an API token; pixi: `pixi auth login` bearer),
  apt/yum (basic auth in URL or `auth.conf`/`.repo`), Go (`.netrc` basic;
  the go command sends credentials over HTTPS only).
  Each format's auth is part of its Definition of Done (FORMATS.md).
- **Challenge on format routes** (ADR 0005): on `/repository/<repo>/...`, a request that carries **no credentials**
  (no `Authorization` header, no valid session) for a repository anonymous cannot see answers `401` with
  `WWW-Authenticate: Basic realm="Waystation"` (plain-text body) instead of `404`, so clients that send credentials
  only after a challenge (Maven, Gradle, sbt, Helm, pip with a keyring) work with plain credentials, like with
  Nexus. A repository that does not exist answers exactly the same (status, headers, body), so anonymous callers
  cannot enumerate repository names; a visible repository the anonymous principal may not act on gives the same
  answer too. An authenticated request keeps `404` for a repository it cannot see; a request whose credentials are
  not valid gets the same `401` challenge for every repository (hidden, missing or visible) before any lookup. `/v2/` (Bearer challenge), the
  REST API, search and browse are unchanged.
- Anonymous access: a configurable `anonymous` principal with its own roles (default: browse and read on proxy
  repositories only). `GET/PUT /api/v1/anonymous-access` (`user-admin`) sets its roles: the built-in `anonymous` role and/or roles
  holding repository permissions only (no global action); empty = no anonymous access. Applies to the REST API,
  format routes and Docker's anonymous registry tokens on every replica within moments.
  **Signing in never reduces access**: every authenticated principal (users of any source, service
  accounts, whatever the credential) also receives the anonymous grants on top of its own, so the same change
  applies to them at once too. The narrowing of the credential still bounds everything (an API token limited to
  repository X cannot use the anonymous grants on Y; a registry token only what it grants), and the anonymous grants
  never count as something the principal can hand out (§4.10 Delegation). Anonymous grants never carry a global
  action, even when a role they use was later edited to hold one.
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
  `viewer` (browse + stats), `anonymous` (browse + read on `type:proxy`). Built-in roles are read-only; custom roles (`/api/v1/roles`, `user-admin`) have a slug id (lower-case letters,
  digits, `-`), a name, a description and 1-100 permissions `(action, scope)` (global actions only on `*`). A role
  still held by a user, group or service account cannot be deleted (409); `GET /roles/{id}` shows who holds it.
  `GET /roles` (any signed-in principal) lists the grantable roles with their permissions.
- **Groups** (local, or mapped from LDAP groups / OIDC claims) hold roles; users hold roles directly or via groups.
  A provider's group mapping (external group DN or name → roles) is stored as one group per row; a user gets
  the roles of the mapped groups among those reported at its last sign-in through that provider. Mapping changes
  apply to signed-in users within seconds; membership changes on the next sign-in. Local groups are managed under
  `/api/v1/groups` (`user-admin`: create, rename, describe, roles, delete; members added and removed with
  `/groups/{id}/members`); mapped groups are listed there read-only except for their roles (the same documents as
  the provider's group mapping). Administration → Groups manages both.
- **Users** (`/api/v1/users`, `user-admin`): list (username prefix, source, disabled, local group, direct role;
  keyset paging), detail (source, linked identities, local and mapped groups, the groups a provider last reported,
  effective roles, lockout), create a local user (with a password, or without one so that an LDAP/OIDC sign-in of
  the same username links to it), update (display name, e-mail, disabled, roles, local groups), delete (sessions end,
  API tokens are revoked), reset a local password (sessions end; one-time by default), unlock, and the effective
  permissions (`/users/{id}/permissions`, also `/service-accounts/{id}/permissions`: roles with where they come from,
  every permission held, global actions, the actions on each repository; the anonymous-access roles are listed with
  `via: anonymous` and their permissions included, as every signed-in principal receives them). Disabling ends the user's sessions.
- **Delegation**: what a caller hands out is bounded by what it holds itself (never the anonymous grants it also
  receives): roles (and groups, and custom role
  permissions, and provider group mappings) it adds may carry only global actions the caller holds, and `admin` only
  from an administrator; an account holding more than the caller (an administrator, for a `user-admin`) cannot be
  changed, deleted, given a password or (service accounts) given tokens by it (403). Nobody can disable or delete
  their own account, and the last enabled administrator (direct role or local group) cannot be disabled, deleted,
  demoted or removed from its group (409).
- Effective permissions are computed per request from a cached, versioned grant table (ARCHITECTURE §7) and
  checked in one place. Content listings and search are filtered, never just hidden. Every RBAC change bumps `grant_version`; the replica that made it applies it at once, the others within
  moments (MongoDB change stream; a 5 s poll if the stream is down).
- **Refusals**: a principal that is not signed in gets 401 where one is needed; a signed-in principal lacking a
  permission gets 403 with problem code `permission_required` (CSRF and pending sign-in steps keep their own codes);
  a hidden repository is 404. `GET /auth/me` lists coarse UI capabilities: `admin`, `repositories.create`,
  `repositories.manage` (manage on some repository), `repositories.browse` (browse or read on some repository),
  `stats.read`, `audit.read`, `users.admin`, `settings.admin`.
- **What a user without roles sees**: signing in only needs the provider's required
  group, so a user may hold no role at all (no mapped group roles, no direct roles). It still receives the anonymous
  grants (§4.9: signing in never reduces access), so with the default anonymous access it sees exactly what anonymous
  may: proxies browsable and readable, capability `repositories.browse`, the dashboard listing those repositories,
  403 (not 401) for what anonymous may not do on a visible repository. When anonymous access is off (or grants
  nothing the user could see), such a session answers consistently: no 401 anywhere (the session stays valid), every route needing a global action 403
  `permission_required` (statistics, activity, transfers, the event stream, cleanup policies, administration),
  listings and search filtered to empty pages, every repository 404, and the routes of any signed-in user (profile,
  own sessions and API tokens, preferences, `GET /roles`, the low-disk guard state) work. Its capabilities are empty;
  the UI shows only the dashboard (a "You are signed in, but have no access yet — ask an administrator" landing) and
  the profile, opens no event stream, and shows a "No access to this page" page for anything else. A reader (or a
  user with no role but the default anonymous grants, browse + read on proxies) sees Repositories, Browse and Search and a
  dashboard listing its repositories, but no statistics, activity, live transfers or storage (`stats-read`), no
  Cleanup or repository settings (`manage`), and no administration.

### 4.11 Audit
Append-only audit log of security-relevant actions: sign-in/out/failure, token create/revoke/use-refused,
user/group/role/provider changes, repo create/update/delete, uploads and deletes on hosted repos, task changes,
settings changes, webhook changes, secret-key rotation, support bundle downloads. Searchable in the UI, exported via API; retention configurable (default 400 days).
- **Record**: time, action (`<category>.<verb>`, catalog `waystation_common.audit.AuditAction`), outcome
  (`success`/`failure`) and for failures a reason code (`bad_credentials`, `unknown_user`, `locked`, `disabled`,
  `not_in_group`, `directory_unavailable`, `link_refused`, `invalid_token`, `too_many_failures`,
  `password_change_required`, `bad_second_factor`, `totp_required`, `totp_setup_required`, `undecryptable`), the actor (user,
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
  files changed; a NuGet version unlisted or relisted, target `nuget_package`; Composer uploads and deletes are
  `content.upload` / `content.delete` with target `composer_package`, RubyGems pushes and yanks with target
  `rubygems_gem`, apk uploads and deletes with target `apk_package`), `settings.update` (the instance
  settings, with field changes), `webhook.create|update|delete`
  (field changes; a new secret or new custom headers show as redacted changes), `webhook.test` and
  `webhook.redeliver` (with the delivery ids), `cleanup_policy.create|update|delete` (field changes),
  `secrets.rotate` (worker, actor `system`: a secret-key rotation run that re-encrypted something or found values no
  configured key decrypts, with counts per store, never values; outcome `failure`, reason `undecryptable` for the
  latter; quiet runs are not recorded); changes made by
  the provisioning file (§9) carry `details.source = provisioning`; `system.support_bundle` (a support bundle downloaded,
  with its size and file count); an administrator signing a user out everywhere is `session.revoke` on the user
  with `details.by_admin`. Cleanup runs are not audit records: they are job runs with their result and log on the
  Tasks page (§4.12); the catalog keeps `cleanup.run` reserved.
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
  **Clone** (`POST /api/v1/repositories/{name}/clone` with `{name, description?, online?}`; `manage` on the source and
  `admin`): a new repository with the source's configuration and no content: type, format, upstream (the stored
  secret is decrypted and re-encrypted for the new repository server-side, never returned), freshness, auto-block,
  CA bundle and outbound proxy, quota, hosted write policy, group members, attached cleanup policies and format
  options, validated like a create (409 when the name is taken). Docker `aliases` and `default_repo` stay with the
  source (they name one repository); the clone is never `managed_by` the provisioning file. Audited as
  `repository.create` with `details.cloned_from`. UI: a "Clone" action on the repository page asks for the name
  (default `<name>-copy`) and opens the clone.
  Repository status (list filter, detail header) is `ok`, `degraded` (an upstream failure in the last 10 minutes),
  `blocked` (auto-blocked, §4.1) or `offline` (disabled or offline mode), from the upstream health hash.
- **Set me up** per repo per client: generated config snippets (docker daemon.json, `docker login`, `.npmrc`,
  `pip.conf`, `settings.xml`, `sources.list`/`.sources`, `.repo`, Go env, Helm repo add, Terraform CLI config,
  `ansible.cfg`, Swift registry commands) with the current user's token placeholder.
- **Tasks and workers** (`admin`; REST `/api/v1/tasks/*`, live on the event stream topic `tasks`):
  - Scheduled tasks = the `job_definitions` (built-ins such as `stats.rollup` every 10 s, `blob.gc`, `blob.verify`,
    `secrets.rotate` (DATA_MODEL lists them all), and one per scheduled cleanup policy) with schedule, enabled, last run and next run. An admin can change a schedule (5-field UTC cron,
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
- **System**: `GET /api/v1/system` (`admin`): version, Python, when the answering server process started, MongoDB and
  Valkey (ping round trip and server version, 2 s limit each; a failure reports only the error class) and the data
  directory (size, used, free, and `MIN_FREE_GB` below which writes stop). **Support bundle**:
  `GET /api/v1/system/support-bundle` (`admin`; audited `system.support_bundle`; the System page's "Download support
  bundle" button) answers a zip `waystation-support-<instance>-<UTC time>.zip` for a support request: `README.txt`
  (what is inside; review before sharing), `manifest.json`, `versions.json` (Waystation, Python, OS and kernel, MongoDB
  version and feature compatibility version, Valkey, every worker's version, applied and pending migrations),
  `settings.json` (every `WAYSTATION_*` setting with secrets as `<redacted>`, the MongoDB/Valkey URIs without
  credentials, credentials in other URLs masked, which secrets came from `*_FILE` files; the stored instance settings),
  `repositories.json` and `auth_providers.json` (configuration; encrypted secrets only as `*_set` flags, CA bundles as
  certificate count and SHA-256 fingerprints), `health.json` (MongoDB/Valkey as `GET /system`, the data volume and the
  low-disk guard, workers, queue figures and dead-letter count, each proxy repository's upstream health and auto-block
  state), `jobs.json` (the newest 100 failed, dead or interrupted job runs with the last 20 lines of their logs, the
  newest 50 dead letters and their total) and `errors.json` (the newest 500 WARNING/ERROR log records of every server
  and worker process). Every value is scrubbed (secret-looking keys, private keys, URL credentials) and bounded, every
  file is capped at 8 MiB, the zip is built off the event loop, and a section that cannot be read (MongoDB or Valkey
  down, 10 s limit) holds `{"error": "<reason>"}` instead. Recent log records: every server and worker process keeps
  its WARNING and ERROR records (the log line's fields, already redacted; URL credentials masked; message, traceback
  and fields bounded; more than 10 records of one message a minute are summarised as one) in a bounded buffer and
  appends them in the background to the Valkey stream `errors:recent` (about 1000 kept, for 7 days); this never
  blocks or fails the caller, and when Valkey is down the records are dropped.
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
  on demand; hosted content is copied with client tools (`Documents/Operations/MIGRATING_FROM_NEXUS.md`).
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
    `docker_aliases`, default on; an alias already routed or equal to a repository name is dropped); a connector port
    (`httpsPort`, else `httpPort`) → `connector_port` (dropped with a difference when another repository
    or an earlier import has it, or it is the base URL's port; the other port, ports below 1024 and subdomain
    connectors are differences), with a note to list it in the web container's `WAYSTATION_DOCKER_CONNECTORS`. apt `distribution`/`flat`; raw `contentDisposition`; maven
    `versionPolicy`/`layoutPolicy`; hosted
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
  - **Cutover**: `Documents/Operations/MIGRATING_FROM_NEXUS.md` (import, credentials, hosted content, client
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
measured at a steady request rate, not at saturation. A budget over its limit fails the build
(`Build/ci/budgets.json`).

### 5.2 Availability and scale
- Server and workers are stateless; run N replicas behind the web tier. Single-node MongoDB replica set by default;
  external replica set supported. Valkey loss degrades live stats and coalescing, never correctness of downloads.
- Graceful shutdown: in-flight requests finish within the container's stop grace period; OCI chunked uploads are
  resumable.

### 5.3 Security
Reviewed against OWASP ASVS 4.0.3 Level 2 (`Security/ASVS_L2.md`). CSP without inline scripts; strict cookies; dependency and image audit with
`make security-scan` (`pip-audit`, `npm audit`, Trivy; `Security/DEPENDENCIES.md`); containers run as non-root, read-only root filesystem where possible, `cap_drop: [ALL]`,
`no-new-privileges`; secrets via env or `*_FILE`; encryption at rest for stored credentials (AES-GCM, key rotation
with multiple keys: the first key encrypts, all decrypt, and the `secrets.rotate` task (daily, or run now) re-encrypts
every stored credential and `keys/` file made with an older key and reports the API tokens and recovery codes still
hashed with one, so the older key can be removed; Operations/SECRET_KEY_ROTATION.md). Zip-slip/tar-slip safe archive handling. Size limits on every parser.

### 5.4 UX quality
Professional and consistent (UI_DESIGN.md). Responsive from 360 px to 4K. Light/dark/system themes.
WCAG 2.2 AA. Keyboard navigable, command palette. No external CDNs or fonts (works fully offline).

### 5.5 Observability
Structured JSON logs (request id, principal, repo), Prometheus metrics, health/readiness endpoints per container,
Zabbix 7 template and Grafana dashboard in `Build/monitoring/`
(`Documents/Operations/MONITORING.md`).

## 6. User interface
See `UI_DESIGN.md`. Summary: dashboard with live transfers and KPIs, repositories, browse/search, component
pages with pull/install snippets, activity log, storage, cleanup policies, tasks/workers, administration
(users, groups, roles, auth providers, tokens, audit, settings).

## 7. API
- REST JSON under `/api/v1/`, described by OpenAPI 3.1 generated from the server (FastAPI + pydantic v2) and served
  at `/api/v1/openapi.json`. The UI uses only this API. Pagination by opaque cursor. The static reference is
  `Documents/API/` (`README.md`: authentication, errors, paging, events; `REFERENCE.md`: every operation and schema),
  generated by `make api-docs` and checked against the code by a unit test.
- **Event stream**: `GET /api/v1/events/stream` (Server-Sent Events), RBAC-filtered per subscriber, heartbeat 15 s,
  resumable with `Last-Event-ID`. Topics (comma-separated `topics`; the stream needs `stats-read`): `transfers`,
  `stats`, `activity`, `tasks` (jobs and workers; also needs `admin`) and `repo:<name>`. On connect `retry: 3000` and a `transfers.snapshot`; `activity` events carry the stream id as `id:` and are replayed
  after `Last-Event-ID` (header or `last_event_id` query parameter, up to 1000); `stats.tick` at 1 Hz (rates over the
  last 10 s from the live counters). Stats endpoints (`/stats/*`, `/transfers`, `/activity`) need `stats-read` and
  only cover repositories the principal can see; series buckets are 1 min (15m, 1h), 10 min (24h), 1 h (7d), 6 h (30d),
  1 day (90d, 1y); 7d and longer read the hourly and daily rollups for the periods already rolled up. 15m stays at
  one-minute buckets: the counters are per minute (finer buckets would multiply the live keys and minute documents
  sixfold) and the live edge of the chart comes from the 1 Hz `stats.tick`. `GET /stats/summary` adds p50/p95
  request latency, `/stats/upstreams` the upstream p95; `GET /stats/top/artifacts` (`by` downloads, bytes, size,
  recent; `window` 24h, 7d, 30d, all), `/stats/top/repositories`, `/stats/storage/history` (7d, 30d, 90d, 1y);
  `GET /digests/{digest}/usage` (browse permission; hidden repositories are left out).
  `GET /activity` filters by `repo`, `kind`, `format`, `principal` (case-insensitive substring of the user name or
  client address), `cache`, `outcome` and an inclusive `since`/`until` window, newest first with a stream-id cursor (a
  sparse filter may return a short page with `next_cursor` set: keep paging). `GET /activity/export` streams the same
  selection as CSV (newest first, at most 100,000 rows; cells a spreadsheet would read as a formula are prefixed `'`).
- Auth: session cookie (UI) or `Authorization: Bearer <api-token>`. CSRF for cookie sessions is a double-submit
  token: `POST /auth/login` and `GET /auth/me` return `csrf_token` (also set in the readable `ws_csrf` cookie);
  unsafe methods must echo it in `X-CSRF-Token`. Bearer requests are exempt.
- Versioned: breaking changes only in `/api/v2/`.
- **UI contract**: the UI's API client is typed from the contract document
  `Application/Web/openapi/waystation-m0.yaml` (OpenAPI 3.1; the file name is historical): config, auth/session,
  repositories, stats, live transfers, activity, browse, search, administration and the SSE event schemas
  (`LiveEvent`: `transfers.snapshot`, `transfer.started|progress|finished|failed`, `stats.tick`, `upstream.state`,
  `activity`, job and worker events). `make gen-api` regenerates the UI's TypeScript types (`src/api/schema.d.ts`);
  CI fails if they are stale. A contract test (`test_contract.py`) keeps the server's generated document compatible
  with the contract for every operation the server implements.
- **Errors** are RFC 9457 problem+json with field-level `errors[]` and a stable error `code` (`not_found`,
  `permission_required`, `password_change_required`, `totp_setup_required`, `second_factor_expired`, ...).
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
  commits are atomic links or renames), `keys/` (repo signing keys, encrypted) and nothing else. Everything else
  is in MongoDB. Works on local disk, NFSv4 or SMB (hard links used where available, not required; rename
  atomicity required).
- MongoDB data in its own volume (`WAYSTATION_MONGO_DIR`), not on the blob path.

### 8.3 Compose conventions
- One `compose.yaml` per deployable unit in `Build/Containers/`: `compose.yaml` (all-in-one reference stack) and
  `compose.standalone.{web,server,worker}.yaml` for split hosts, `compose.dev.yaml` (source bind-mounted).
  Split hosts: the server file runs `init` and the server (port published on `WAYSTATION_SERVER_BIND`, for the web
  hosts only), the worker file the worker, the web file nginx with `WAYSTATION_SERVER_ADDR` = the server hosts
  (`host:port`, comma-separated; nginx balances and retries unreachable ones); all mount one shared blob store
  (`WAYSTATION_DATA_SOURCE`, a host path such as an NFS mount; read-only on web) and use an external MongoDB replica
  set and Valkey. `compose.yaml` and the standalone server/worker files share one `x-app-env` list
  (`test_compose_files.py`). `make deploy-test` runs two server hosts, a worker host and a TLS web host in containers.
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
- Explicit subnets for the networks are configurable with the optional override `compose.subnets.yaml`
  (`WAYSTATION_SUBNET` for `frontend`, `WAYSTATION_BACKEND_SUBNET` for `backend`), because Docker's default pools can
  collide with routed networks. Without it Docker picks.
- Dev stack (`make dev-up`): `compose.yaml` + `compose.dev.yaml` as project `waystation-dev` (containers
  `waystation-dev-*`), images built locally, Python source bind-mounted read-only with hot reload, throwaway secrets
  generated once into the gitignored `.dev-data/dev.env`; `compose.dev-spa.yaml` is added automatically to serve a
  locally built SPA (`Application/Web/dist/spa`), otherwise the image's copy (or a placeholder page) is served.

### 8.4 TLS and reverse proxies
Either `web` terminates TLS (cert/key paths, HTTP→HTTPS redirect, HTTP/2) or it runs plain HTTP behind a trusted
proxy (`WAYSTATION_TRUST_FORWARDED=true`, trusted CIDRs). Large uploads/downloads must pass through untouched:
docs list the required proxy settings (no buffering, no body size limit, long timeouts) for nginx, Traefik,
Caddy, HAProxy and Synology DSM (`Documents/Operations/REVERSE_PROXIES.md`; the example files in
`Build/Containers/examples/reverse-proxies/` are checked in containers by `make proxies-check`). TLS in `web`: the
override `compose.tls.yaml` mounts `WAYSTATION_TLS_DIR` and sets `WAYSTATION_TLS_CERT`/`WAYSTATION_TLS_KEY`; the
image's start script (`/docker-entrypoint.d/15-waystation.sh`) then renders an HTTPS listener (container port 8443,
HTTP/2, TLS 1.2/1.3) and turns the plain port into a `308` redirect to `WAYSTATION_BASE_URL` (or `https://<host>`),
keeping only `/_web/healthz` (the container healthcheck, also on the HTTPS server). It refuses to start with one of
the two paths set or a file unreadable. Docker port connectors (`WAYSTATION_DOCKER_CONNECTORS`, §4.4) get one more
`server` each, HTTPS on the same certificate when TLS is on; the proxy settings and the `/_blobs/` location are shared
include files (`waystation-proxy.inc.template`, `waystation-blobs.inc.template`); `make web-config-test` renders every
combination in the official nginx image and checks the connector header and the security headers live. Security
headers (rendered by the start script into `waystation-headers.inc` / `waystation-ui-headers.inc`): every
answer carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin` and, with
TLS on, `Strict-Transport-Security: max-age=31536000`; the UI gets a strict CSP (`default-src 'self'`, scripts and
connections same-origin only, inline style attributes allowed for Quasar, `frame-ancestors 'none'`), everything else
(artifacts, `/v2/`, the REST API, cached blobs) `Content-Security-Policy: sandbox; frame-ancestors 'none'`, so HTML
among proxied or uploaded files can never run script on the UI's origin. The server adds `Cache-Control: no-store`
to `/api/` and `/auth/` answers that set no caching of their own. Deployment guide:
`Documents/Operations/DEPLOYMENT.md`.

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
active; rotation: Operations/SECRET_KEY_ROTATION.md), `SESSION_SECRET`, `TRUST_FORWARDED`, `TRUSTED_PROXIES`, `LOG_LEVEL`, `LOG_FORMAT`, `SERVER_WORKERS`,
`WORKER_QUEUES`, `WORKER_CONCURRENCY`, `MAX_UPLOAD_GB`, `MIN_FREE_GB`, `BOOTSTRAP_ADMIN_PASSWORD_FILE`,
`HTTP_PROXY`/`NO_PROXY`, `EXTRA_CA_FILE`, `SSRF_ALLOW_CIDRS`, `COALESCE_TAIL`, `COALESCE_LEASE_S`, `INTERNAL_URL`
(where workers reach a server for prefetch jobs; split hosts: a server host's published server port). Auth providers, repositories and policies are
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
rate limit, API latency triggers). Process CPU and memory are left to the container runtime's own metrics
(cAdvisor, node exporter); sign-in failures are in the audit log (§4.11), not in `/metrics`.

## 11. Testing strategy
- Unit tests next to code (pytest, Vitest). Property-based tests (hypothesis) for path normalisation, version
  ordering, metadata merging, range handling.
- **Real-client tests** (`tests/clients/<format>/`): containers run the actual clients (docker/podman/skopeo/crane,
  npm/pnpm/yarn, pip/uv/twine, mvn/gradle, apt, dnf, go, helm, terraform/tofu, ansible-galaxy, swift, dotnet nuget,
  cargo, gem, composer, conda) against a Waystation stack with **recorded upstream fixtures** (an offline fake
  upstream replays captured responses; a manual run goes against the live upstreams). `make clients-test
  FORMAT=<f> MODE=replay|record|live`; fixtures are scrubbed and pass `make clients-check` (forbidden strings,
  size budget). arm64 clients (Raspberry Pi OS) are opt-in (`ARM64=1`) where the host can run linux/arm64
  containers. Client authentication (LDAP and OIDC users, tokens, refusals) is one matrix, `tests/clients/auth/`.
  How a format plugs in: `tests/clients/README.md`.
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
  tests in replay mode (`make clients-test FORMAT=all`); live-upstream smoke (`clients-live.yml`, manual on GitHub);
  `release.yml` on `vX.Y.Z` tags (must match `VERSION`) builds multi-arch (amd64, arm64) server/worker/web images
  with SBOMs and provenance attestations and publishes them to GHCR using only `GITHUB_TOKEN`.
- Gitea Actions (secondary) `.gitea/workflows/images.yml` builds the images on every push to `main` and pushes them
  to the Gitea registry. Registry host and credentials only from org settings (`vars.REGISTRY_HOST`,
  `secrets.REGISTRY_USER` / `secrets.REGISTRY_TOKEN`). `.gitea/workflows/perf.yml` runs `make perf` (CI profile)
  after every push to `main` (the full profile by hand), uploading the report as an artifact; `clients-live.yml`
  runs the real-client harness against the live upstreams by hand (arm64 clients optional). No scheduled CI runs. Details:
  `Documents/Operations/CI.md`.
- The static API reference (`Documents/API/`) is regenerated by `make api-docs`; a unit test fails when it is stale.
- The forbidden-strings check (`make forbidden-strings`, in `make check`) keeps environment details out of the
  public repository: host names outside `Build/ci/allowed_hosts.txt`, non-documentation IPs, keys and tokens fail.
- `VERSION` file at the root is the single source; `make set-version V=X.Y.Z` updates everything.
- Semantic versioning; `Documents/CHANGELOG.md` records every change, dated.

## 13. Repository layout and source control

### 13.1 Source control
The source is at `https://github.com/SlinkySoftware/Waystation` (default branch `main`). Releases are `vX.Y.Z`
tags matching the `VERSION` file; each tag publishes multi-arch images to GHCR (§12). Commit messages lead with a
task or change identifier and use the imperative mood.

### 13.2 Layout
```
Waystation/
  README.md  CLAUDE.md  AGENTS.md  VERSION  LICENSE  Makefile  pyproject.toml (uv workspace)  uv.lock
  .env.example  .gitignore  .dockerignore  .github/workflows/  .gitea/workflows/
  Application/
    Common/   waystation_common/   settings, logging, ids, crypto, events schema, job protocol, storage API, errors
    Server/   waystation_server/   app factory, api/ (REST), security/ (authn, access), formats/<format>/,
                                    proxy/ (fetch, coalesce, freshness), dataplane/ (x-accel, ranges), stats/, sse/
    Worker/   waystation_worker/   runner, jobs/ (cleanup, gc, index_apt, index_yum, rollups, prefetch, import)
    Web/      Quasar SPA (src/api, components, composables, layouts, pages, stores, router, css, mocks/),
              openapi/ (the UI's API contract), src/assets/brand/ (brand geometry + master SVGs)
  Build/
    Containers/  compose*.yaml, web/ (Dockerfile, nginx templates), server/, worker/, dev/ (dev image), examples/,
                 backup/ (waystation-backup.sh: backup, restore, verify)
    githooks/    pre-commit hook (`make hooks`)
    ci/          budgets.json, forbidden-strings check, API reference generator, scripts used by workflows
    monitoring/  zabbix/ (Zabbix 7 template), grafana/ (dashboard)
    migration/   nexus_compare.py (side-by-side Nexus/Waystation checks, `make nexus-compare`), checks.example.toml
  tests/  clients/<format>/  fixtures/upstreams/<format>/  perf/  security/  e2e/  backup/  deploy/  web/
  Documents/  SPECIFICATION.md  CHANGELOG.md  UI_DESIGN.md
              Architecture/  ARCHITECTURE.md  DATA_MODEL.md  FORMATS.md  adr/
              API/           README.md  REFERENCE.md  openapi.json
              Operations/    ADMIN_GUIDE.md  DEPLOYMENT.md  BACKUP.md  MONITORING.md  ...  clients/<format>.md
              Security/      ASVS_L2.md  DEPENDENCIES.md
              History/       how the product was built (tasks, build status, notes)
```

## 14. Implementation history
The build-status table that tracked the implementation task by task is kept, unchanged, in
`History/IMPLEMENTATION_STATUS.md`. This specification describes the product as built.

## 15. Product decisions
| # | Question | Decision |
|---|---|---|
| D1 | Licence | GPL-3.0-or-later. |
| D2 | Bundled MongoDB vs an existing shared MongoDB | Bundled single-node replica set by default; an external replica set is supported. |
| D3 | Which formats a Nexus cutover needs | Docker, apt, yum, npm and raw; every other format is additional. |
| D4 | Vulnerability scanning (Trivy/Grype on cached images and packages) | Out of scope for v1; a candidate for an optional `scanner` worker queue. |
| D5 | S3/MinIO blob storage driver | Out of scope for v1; the storage API is designed for it (ARCHITECTURE §5). |
| D6 | Docker Hub default route name | Setting `default_docker_repo` (the default docker repository), usually the Docker Hub proxy. |
| D7 | Product name and brand | "Waystation", with its own logo and icon set (UI_DESIGN.md). |
