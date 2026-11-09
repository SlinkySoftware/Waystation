# Waystation formats

Protocol notes per format: what clients call, what upstreams provide, what to rewrite, what to validate, and the
gotchas that real-world clients hit. Read the section before touching a format. Verify details against the
cited specs while implementing; when this file is wrong, fix it in the same change.

## Definition of done for a format
1. Plugin under `waystation_server/formats/<format>/` implementing the `Format` contract (ARCHITECTURE §11).
2. Proxy (and hosted/group where SPEC §4.5 says so) work with **every listed client** in `tests/clients/<format>/`
   using recorded upstream fixtures; nightly live-upstream smoke passes.
3. Checksums/digests validated (SPEC §4.1); metadata URLs rewritten; conditional requests supported.
4. Components extracted for browse/search with correct `version_sort`.
5. "Set me up" snippets for each client; UI browse renderer for the format (delivered in the format's own task).
5a. Client authentication works for every listed client (SPEC §4.9) and is covered by the client tests.
6. Docs: `Documents/Operations/clients/<format>.md` (client config, auth, troubleshooting).
7. Perf budgets hold for the format's hot paths.

Common rules: all format routes live under `/repository/<repo>/` (Nexus-compatible). Paths are normalised by
`waystation_common.paths.normalise` before any lookup. Group repos merge metadata only where noted.

---

## Raw (generic) — Phase 2
- `GET/HEAD /repository/<repo>/<path>`; hosted `PUT` (stream) and `DELETE`; directory listing as HTML/JSON
  (`Accept: application/json`) for browse.
- Proxy: plain HTTP(S) upstream base URL; cache by path (`<repo>/<path>` → `<upstream_url>/<path>`, the path
  percent-escaped, `:` included, so a segment is never read as a scheme, query or fragment; the query string is
  ignored); `metadata` kind for paths matching the per-repo `metadata_path_regex`, else `content`. The regex is
  *searched* in `/<path>` (leading slash added), so `(^|/)latest$`, `.*/(index\.json|latest)$` and `^/dists/` all work
  and a root-level `latest` matches `.*/latest$`. Metadata is revalidated after `metadata_max_age_s`; content is kept
  for `content_max_age_s` (default forever).
- Nothing to verify: a plain web server publishes no digests, so raw files are trusted as fetched (size-bounded,
  the stored sha256 is the ETag). Upstream 404/410 → native 404 (negative-cached); other failures → 502/504.
- Content-Type: by extension from Python's built-in table (plus `.deb`, `.rpm`, `.iso`, `.xz`, `.zst`, `.sh`, ...)
  so a mislabelled upstream cannot change how a known file is served; otherwise the upstream's type when well formed
  (`charset` kept), else `application/octet-stream`. File answers carry `Content-Disposition` (`content_disposition`
  option, `attachment` by default) and every answer `X-Content-Type-Options: nosniff`; with `inline`, HTML,
  XHTML, SVG and XML are served as `text/plain` so upstream content never runs on the Waystation origin.
- Uses: installers, ISOs, release binaries, `get.docker.com`-style scripts.
- Hosted (`formats/raw/hosted.py` on `waystation_server.hosted.store`): `PUT /repository/<repo>/<path>` streams the
  body into the blob store as a live upload transfer (bounded by `WAYSTATION_MAX_UPLOAD_GB` and `Content-Length`,
  free-space guard) and records the asset `<path>`; `X-Checksum-Sha256` / `-Sha1` / `-Md5` (hex, the Nexus and
  Artifactory convention) are verified before anything is committed (mismatch or malformed → 400, nothing stored);
  the stored Content-Type is by extension only (never the client's), else `application/octet-stream`; `201` with
  `ETag: "sha256:<hex>"` and `X-Checksum-Sha256`. A URL ending in `/` cannot be uploaded to (400). Write policy:
  `allow_redeploy` replaces, `disable_redeploy` refuses an existing path (409, re-checked after the upload so a
  concurrent upload cannot slip through; delete stays allowed), `read_only` refuses uploads and deletes (403).
  A hard repository quota (T-O03, `HostedStore.admit`) refuses an upload before any byte is read (507).
  `DELETE` → 204 (404 when missing); blob GC frees the bytes. `GET`/`HEAD` serve by X-Accel with the same headers as
  the proxy. Uploads and deletes are audited (`content.upload` with path, size, sha256 and any verified sha1/md5,
  `replaced`; `content.delete`) and appear in the activity feed (`asset.created` / `asset.deleted`).
  `metadata_path_regex` is a proxy-only option (refused on hosted and group repositories).
- Group (`formats/raw/group.py`): members in order (raw proxy and hosted, no nested groups, offline members skipped);
  the first member that has the file answers as that member would (a proxy member fetches from its upstream); a
  member answering 404 is skipped, one that fails is skipped while a later member can answer, else its failure is
  returned. `X-Ws-Group-Member` names the member (passed through the X-Accel location). `PUT`/`DELETE` go to the
  `writable_member` (hosted raw), which needs the action on the group (router) and on the member
  (`req.allows_member`); without one → 405. Reading needs `read` on the group only.
- Directory listings (`formats/raw/listing.py`), every type: `GET`/`HEAD` on the repository root or any path ending in
  `/`, from the asset tree (hosted: uploads; proxy: what is cached, never an upstream listing; group: the members'
  first pages merged, folders united, a file name from the first member). JSON with `Accept: application/json` in the
  browse API's `FolderListing` shape (`path`, `entries`, `next_cursor`; `?cursor=`, 1000 per page), else an HTML index
  (escaped names, `Content-Security-Policy: default-src 'none'`, `nosniff`). Needs `browse` besides `read`. A folder
  with nothing under it → 404; the root answers an empty listing.
- Built (T-P2-RAW): proxy `GET`/`HEAD` (M0) with curl and wget; hosted and group as above, with curl upload (with a
  checksum), download, conditional request, wget download (bearer header), JSON listing, group reads in order,
  upload through the group and delete in the real-client harness. Browse (T-S09 min): the generic folder listing of
  assets (`/api/v1/repositories/{name}/browse`), which shows uploads too. Nexus import creates raw hosted and group
  repositories (T-M01). Not yet: components/search indexing beyond the asset tree, a basic-auth-only client test
  matrix beyond curl/wget, the nightly live smoke.

## Docker / OCI — Phase 2
Specs: OCI Distribution Spec v1.1, OCI Image Spec v1.1, Docker Registry HTTP API v2, Docker token auth spec.
- Endpoints: `GET /v2/` (version check; `401` + `WWW-Authenticate: Bearer realm="<base>/auth/token",service="<host>"`
  when auth is required, including for anonymous-allowed repos if the client may need push); manifests
  `HEAD/GET/PUT/DELETE /v2/<name>/manifests/<ref>`; blobs `HEAD/GET/DELETE /v2/<name>/blobs/<digest>`; uploads
  `POST /v2/<name>/blobs/uploads/` (+ `?digest=` monolithic, `?mount=<digest>&from=<repo>` cross-mount),
  `PATCH` chunks with `Content-Range`, `PUT ...?digest=` finalise, `GET` upload status; `GET /v2/<name>/tags/list`
  (`n`, `last` pagination + `Link`); `GET /v2/<name>/referrers/<digest>` (+ `artifactType` filter);
  `GET /v2/_catalog` (filtered by RBAC).
- Name routing (SPEC §4.4): first segment = repo name → else upstream alias (`docker.io`, `ghcr.io`, ...) →
  else `default_docker_repo`. Also served under `/repository/<repo>/v2/...` for Nexus-path compatibility.
- Headers: always `Docker-Content-Digest`, correct `Content-Type` = manifest media type, `Docker-Distribution-API-Version: registry/2.0`,
  `OCI-Subject` on referrer-bearing PUTs. HEAD on manifests must not count against Docker Hub limits.
- Accept handling: honour client `Accept` list across `application/vnd.oci.image.index.v1+json`,
  `application/vnd.oci.image.manifest.v1+json`, `application/vnd.docker.distribution.manifest.list.v2+json`,
  `application/vnd.docker.distribution.manifest.v2+json`; never convert between formats (digest must stay stable);
  request upstream with the client's Accept list.
- Proxy specifics:
  - Docker Hub: upstream `https://registry-1.docker.io`, token from `https://auth.docker.io/token`
    (`service=registry.docker.io`, `scope=repository:<name>:pull`), optional Hub credentials for higher limits.
    Single-segment names get `library/` prepended. Track `ratelimit-limit/remaining` headers per repo.
  - Generic OCI (GHCR, Quay, gcr, ECR public, Gitea registries): follow the upstream's `WWW-Authenticate` challenge,
    anonymous token or configured credentials. Ping the upstream's `GET /v2/` once to learn the challenge, so every
    request carries its token up front (a first anonymous request per image would make record/replay and upstream
    request counts depend on which server process answers); re-learn on a later `401`.
  - Tag → digest resolution is metadata (`metadata_max_age`); digest-addressed manifests and blobs are content.
  - Blob redirects: upstreams often 307 to a CDN; follow (SSRF-checked) without forwarding Authorization to other hosts.
    Docker Hub uses more than one CDN host (`production.cloudflare.docker.com`, `production.cloudfront.docker.com`),
    GHCR uses `pkg-containers.githubusercontent.com`: outbound allowlists must include them.
  - Docker Hub answers `401` (not `404`) for image names that do not exist; map an upstream 401/403 after token
    negotiation to `NAME_UNKNOWN`. Proxies without a referrers API answer `404`, and clients fall back to the
    `sha256-<hex>` tag schema (an extra tag lookup per pull).
  - Mirror mode: Docker daemon `registry-mirrors` sends Hub names (`library/nginx`) to `/v2/` → default repo.
- Hosted: tag immutability patterns, overwrite policy, manifest validation (all referenced blobs/manifests exist
  in the repo or are mountable), size limit, `subject` field for referrers (cosign/notation signatures, SBOMs).
  Built (T-P2-DOCKERH, `formats/docker/hosted.py` on `waystation_server.hosted.store`):
  - Layout: blobs are assets `blobs/<digest>` per repository (as in a proxy cache, so browse and cleanup work
    unchanged); manifests `<image>/manifests/<digest>` plus a `docker_manifests` record (references, `subject`,
    effective `artifactType` = `artifactType` else the config media type, size, annotations); tags in `docker_tags`
    (`pushed_by`). Tags resolve through `docker_tags`; there are no tag assets.
  - Uploads: `POST` starts a session (`upload_sessions`, a resumable file `uploads/<id>` in the blob store, 12 h
    idle expiry); `PATCH` appends (with `Content-Range` it must start at the current offset, else `416` with the
    current `Range`; without it the chunk is appended); `PUT ?digest=` appends the rest and commits after the sha256
    matches; `GET` answers `204` + `Range`; `DELETE` cancels. One request appends at a time (session lease);
    bytes a crashed request wrote past the acknowledged offset are cut off by the next one. `POST ?digest=` with the
    body is a monolithic upload. Only sha256 uploads (`DIGEST_INVALID` otherwise). The sha256 continues
    incrementally across requests in one process; another process re-hashes the file in a thread at commit.
  - Mounts: `POST ?mount=<digest>&from=<name>`: `from` is resolved like any `/v2/` name (repository prefix, alias,
    default), must be a docker repository the principal may `read` (the registry token must carry its `pull`
    scope), and must hold the blob; the blob is then linked (claimed against GC), `201`. Otherwise an upload session
    starts (`202`). A blob already in the target repository mounts without `from`.
  - Quotas (T-O03): with a hard quota reached, `POST` (also mounts; refused early when its `Content-Length` would go
    over), `PATCH`, `PUT` and manifest pushes (refused when the manifest would go over) answer `403 DENIED` with
    "repository <name> is over its quota: ..."; pulls and deletes still work. The low-disk guard answers `507`.
  - Manifest `PUT`: `Content-Type` (or the document's `mediaType` when the header is generic) must be a manifest or
    index type and agree with `mediaType`; `schemaVersion` 2; descriptors well-formed; ≤ 4 MiB (`413`); by digest the
    bytes must match it (sha256 or sha512). Blobs referenced by a manifest (config, layers; not
    non-distributable/foreign layers) must be in the repository with the stated size (`MANIFEST_BLOB_UNKNOWN`,
    `MANIFEST_INVALID`); manifests referenced by an index must be in the image (`MANIFEST_UNKNOWN`). The OCI empty
    JSON blob (`{}`) is created when a manifest uses it. `201` with `Location`, `Docker-Content-Digest` and
    `OCI-Subject` when the manifest has a subject (which need not exist yet).
  - Write policy: `read_only` → `403 DENIED` for every push and delete; `disable_redeploy` → no existing tag moves
    or is deleted; tags fully matching an `immutable_tag_patterns` regex never move or go away (`403 DENIED`).
    Re-pushing the same digest to an immutable tag is fine.
  - `DELETE /manifests/<tag>` removes the tag; `/manifests/<digest>` removes the manifest and its tags (not while an
    immutable tag points at it); `/blobs/<digest>` removes a blob unless a manifest of the repository uses it (`405`,
    so registries and the conformance suite treat blob delete as unsupported for referenced blobs). `202`.
  - `GET /v2/<name>/referrers/<digest>`: an OCI index of the image's manifests whose subject is `<digest>` (always
    `200`; ≤ 1000 entries), `artifactType` filter with `OCI-Filters-Applied: artifactType`.
  - `tags/list`: lexical order, `n`/`last` with `Link`; no `n` = every tag (≤ 10 000); `404 NAME_UNKNOWN` when the
    image has no tags. `GET /v2/_catalog` (any registry token carries `registry:catalog:*`): `<repo>/<image>` for every
    tagged image of the docker repositories (proxy and hosted) the principal can see, by name, `n`/`last`/`Link`.
  - A tag that cannot exist (fails the tag grammar, e.g. `.INVALID`) is `404 MANIFEST_UNKNOWN` when read or deleted
    and `400 TAG_INVALID` when pushed (proxies too).
  - Registry tokens: `push` and `delete` grants also carry `browse`, so a token with only `delete` (as conformance
    clients request) sees the repository instead of getting the hidden-repository 404.
  - Audit `content.upload` per pushed manifest and `content.delete` per delete (tag, manifest, blob); activity
    `asset.created`/`asset.deleted`; blob uploads are live transfers with `direction: upload` (counted as
    `bytes_in_upload`, neither hits nor misses).
  - Tested: unit + integration (`Application/Server/tests/test_docker_hosted_*.py`), real clients in
    `tests/clients/docker/hosted.sh` (docker push, buildx multi-platform with attestations, pulls per platform,
    immutability, crane copy with mounts from the proxy, skopeo copy --all, oras push/pull/attach/discover, cosign 3
    sign/verify offline via the referrers API, crane delete, browse and audit) and the OCI distribution-spec
    conformance suite v1.1.1 (pull, push, content discovery, content management: all specs pass).
- Group: read through members; tag lists merged; push goes to `writable_member`.
  Built (T-P2-DOCKERG, `formats/docker/group.py`): members are docker proxy or hosted repositories by name, in order
  (offline or missing ones skipped). Manifests (tag or digest): the first member that answers; a 404 moves on, an
  upstream failure (5xx, 401/403/429) moves on and is returned only when no member answers. Blobs: every member's
  stored/cached `blobs/<digest>` first (served by X-Accel, no upstream), then a `GET` asks members in order; a `HEAD`
  never goes upstream (it is how pushes check for existing layers, and pushed names must not reach public
  registries). `tags/list`: union of hosted members' tags and proxy members' upstream first page (cached as their
  `tags/list`), sorted, `n`/`last`/`Link`; `404 NAME_UNKNOWN` when no member knows the name. Referrers: union over hosted
  members. Writes (manifest `PUT`/`DELETE`, uploads incl. status and cancel, blob `DELETE`) go to the writable member
  with the request otherwise unchanged (so `Location` keeps the group's name and a push continues through the group);
  the principal needs the action on the group (router) and on the member (`req.allows_member`, RBAC without the
  registry token's per-repository grants); none configured → `405 UNSUPPORTED`. Answers from a member carry
  `X-Ws-Group-Member`. A mount `from` a group looks at its members' copies. Statistics count on the member that
  served. Note: a manifest `HEAD`/`GET` for a name only a hosted member has still asks the proxy members' upstreams
  when it misses there (as Nexus groups do); put hosted members first.
- Clients to test: docker (BuildKit), podman, containerd/nerdctl (`hosts.toml` mirror with and without
  `override_path`), skopeo, crane, oras (artifacts), helm (OCI charts), cosign (signatures via referrers).
- Browse (T-S09 min): every cached manifest is parsed in the background (thread, bounded) into `docker_manifests`
  (index entries with platforms, or config + layers); a manifest cached by tag also sets `docker_tags` (only the
  full-`Accept` variant moves an existing tag). Image configs are read from the cache at view time (platform,
  `created`, labels, history). Index entries with platform `unknown/unknown` (attestations) are not platforms.

## npm — Phase 2 (proxy), Phase 5 (hosted)
Spec: npm registry API (registry.npmjs.org behaviour; CouchDB-derived).
- `GET /<pkg>` and `GET /@scope%2f<pkg>` (also accept unencoded `@scope/pkg`) → packument. `Accept:
  application/vnd.npm.install-v1+json` = abbreviated metadata ("corgi"); cache both representations separately.
- Tarballs: `GET /<pkg>/-/<pkg>-<ver>.tgz` and `/@scope/<pkg>/-/<pkg>-<ver>.tgz`. Rewrite every
  `versions.*.dist.tarball` to `<base>/repository/<repo>/...`. Validate tarballs against `dist.integrity`
  (sha512 SRI) or `dist.shasum` (sha1).
- Pass-through or local answers: `/-/v1/search` (proxy, short cache), `/-/npm/v1/security/advisories/bulk` and
  `/-/npm/v1/security/audits/quick` (proxy to upstream audit, no cache; hosted/group = merge or 404 configurable),
  `/-/ping`, `/-/whoami`, `/-/user/org.couchdb.user:<name>` (login → issues API token), dist-tags endpoints.
- Hosted publish: `PUT /<pkg>` with JSON body including base64 `_attachments`; `npm unpublish`, `npm deprecate`
  (packument updates), `npm dist-tag add/rm`.
- Group: merge packuments (versions union; first member wins per version; `dist-tags` from first member that has
  the tag), rewrite tarball URLs to the group.
- Clients: npm 9/10/11, pnpm, yarn v1, yarn berry (`npmRegistryServer`), bun.
- Browse (T-S09 min): a stored packument (either representation) is summarised in the background into
  `npm_packages` (latest, dist-tags, version count; description and licence only from the full packument); versions
  are read from the cached packument (full preferred: publish times) and sorted by semver precedence.

## apt (Debian, Ubuntu, Raspberry Pi OS) — Phase 2 (proxy), Phase 5 (hosted)
- Layout under the upstream base: `dists/<suite>/{InRelease,Release,Release.gpg}`,
  `dists/<suite>/<component>/binary-<arch>/Packages{,.gz,.xz}`, `.../i18n/Translation-*`, `Contents-*`,
  `dists/<suite>/<component>/dep11/*`, **`by-hash/SHA256/<hash>`** paths, `pool/...` package files.
- Proxy, three kinds of file:
  1. `InRelease` / `Release` / `Release.gpg` = metadata (short TTL); bytes passed through untouched (clients verify
     the signature with their own keyring).
  2. **Index files** (`Packages*`, `Sources*`, `Translation-*`, `Contents-*`, dep11...) at their plain paths are
     **mutable**: never cache them by path as immutable content. Store them as blobs keyed by the hash the currently
     served `Release` lists for that path, and serve the plain path by looking up that expected hash (so the index
     set always matches the `Release` the client got). `by-hash/SHA256/<h>` paths are immutable and self-validating.
     If a client requests an index whose expected hash isn't cached, fetch it and verify against the `Release`;
     on mismatch, revalidate `Release` once, then fail.
  3. **Package files** under `pool/` are not listed in `Release`. Validate them against the `SHA256` field of their
     entry in a cached `Packages` file: when a `Packages` file is cached, index `Filename → (SHA256, Size)` for that
     repo (MongoDB, per suite/component/arch). If a pool file is requested before any `Packages` that lists it has
     been cached, fetch it and record its computed hash, mark it `unverified`, and verify on the next index refresh.
- Multiple suites (`trixie`, `trixie-updates`, `trixie-backports`, `trixie-security`) through one proxy repo is
  normal; the repo's `distribution` field is informational.
- Hosted: upload `.deb` (`PUT /repository/<repo>/pool/...` or `POST` multipart); worker regenerates
  `Packages`/`Release`/`InRelease` per suite/component/arch, signs with the repo key (`InRelease` clearsigned +
  `Release.gpg`), publishes atomically (write new index blobs, then swap metadata assets).
- Clients: apt (deb822 `.sources` and one-line), with `Signed-By`; Debian 12/13, Ubuntu 24.04/26.04, Raspberry Pi OS (arm64/armhf).
- Built (M0, T-P2-APT min): proxy `GET`/`HEAD` (`formats/apt/plugin.py`, parsers in `deb822.py`, `apt_pool_index`
  access in `db/apt.py`). Indexes at plain paths are stored under their `by-hash/SHA256/<h>` key from the
  `InRelease` (else `Release`) currently cached for the suite and fetched from the upstream `by-hash` path when the
  `Release` says `Acquire-By-Hash: yes`; files the `Release` does not list answer 404. `by-hash/SHA256` and
  `by-hash/SHA512` are accepted (other algorithms 404). `Packages` files (any of `.xz`, `.gz`, `.bz2`, `.lzma`,
  `.zst`, plain) are parsed in a background task after they are cached (decompression bounded to 512 MiB, at most
  1 M entries; stanzas with unsafe `Filename`s are skipped) and written to `apt_pool_index` as `verified`; an
  unverified pool file whose recorded hash disagrees with a new listing is dropped from the cache. Parsed `Release`
  files are kept in a small in-process LRU keyed by blob digest. Real clients: apt on Debian 13 and Ubuntu 26.04
  (`tests/clients/apt/`). Not yet: hosted, `Release` signature checks by Waystation (clients verify), proactive
  `Packages` refresh by a worker, Raspberry Pi OS / arm64 clients, components (`components` collection). Browse
  (T-S09 min): the same background pass writes each `Packages` stanza (name, version, architecture, section) to
  `apt_packages`, replacing the previous generation of that index; suites come from the cached `InRelease`/`Release`
  (origin, codename, date, components, architectures).

## yum / dnf (RHEL family, EPEL, Oracle, Fedora) — Phase 2 (proxy), Phase 5 (hosted)
- `repodata/repomd.xml` (+ `.asc`), `repodata/<hash>-primary.xml.{gz,zst}`, `filelists`, `other`, `updateinfo`,
  `modules.yaml`, `comps`; packages anywhere under the base.
- Proxy: `repomd.xml(.asc)` = metadata; files referenced by `repomd.xml` = content validated against its
  `<checksum>`; RPMs = content (verified against `primary.xml` checksums when cached). Mirrorlist/metalink
  upstreams are not supported: configure a concrete base URL. Paths keep `$releasever/$basearch` structure.
- Hosted: upload RPMs; worker builds `repodata` (createrepo-equivalent: primary/filelists/other, zstd), signs
  `repomd.xml` (`repomd.xml.asc`), atomic swap. Optional package signing is out of scope.
- Clients: dnf 4/5, yum, zypper (smoke only).
- Built (T-P2-YUM min, proxy): `formats/yum/plugin.py`, defused bounded parsers in `repodata.py`, `yum_packages`
  access in `db/yum.py`. A *repository directory* is any directory holding `repodata/`; one Waystation repository
  serves every directory under its upstream URL. `repodata/repomd.xml` is metadata (revalidated, passed through,
  refused with 502 unless it parses); `repomd.xml.asc`/`.key` are small metadata and a newly stored `repomd.xml`
  drops the cached `.asc`. Files the currently cached `repomd.xml` lists are served only by its checksum
  (SHA-1/256/512) and size, buffered and verified before the first byte; by-checksum names are cached forever under
  their own path, plain names (`comps.xml`) under `repodata/<hex>-<name>`; on a mismatch `repomd.xml` is revalidated
  once, then 502. Unlisted by-checksum names (a client with an older `repomd.xml`) are self-validating by the hex
  length of their prefix; other unlisted files 404. Other checksum types (sha224/384, md5) are not served. XML is
  parsed with expat refusing any DTD, entity or notation declaration (no entity expansion, no external entities),
  text values bounded to 1 KiB, `repomd.xml` to 8 MiB / 1000 entries. A listed `primary` is decompressed in 1 MiB
  chunks (`.gz`, `.xz`, `.zst`, `.bz2`, plain; at most 2 GiB decompressed, 1 M packages) and parsed in a thread in
  the background into `yum_packages` (entries with unsafe `href`s or an `xml:base` are skipped; a new generation of
  a directory's `primary` replaces the old one). Packages (`*.rpm`, `*.drpm`) are validated against their entry; a
  package with no entry looks up the closest enclosing directory with a cached `repomd.xml`, ensures and indexes its
  `primary` (waiting for it), then validates; otherwise it is served and recorded `unverified` and dropped if a later
  listing disagrees (or uses another algorithm). Other paths (`.treeinfo`, keys) are metadata by path, so files
  over 128 MiB outside `repodata/` and packages (installer images) are not supported. Real clients: dnf 4 on Rocky
  Linux 10 and AlmaLinux 10 against their `extras` repositories (`tests/clients/yum/`). Browse: the generic folder
  tree only (no yum package view yet). Not yet: hosted, group, Oracle Linux 9 / EPEL / Fedora / zypper in the
  harness, dnf 5, the `components` collection, a worker-side refresh of `primary` and re-verification of unverified
  packages.

## PyPI — Phase 5
Specs: PEP 503 (simple HTML), PEP 691 (JSON simple API, `application/vnd.pypi.simple.v1+json`), PEP 658/714
(`.metadata` files, `data-core-metadata`), PEP 700 (versions, upload-time), legacy JSON API (`/pypi/<pkg>/json`).
- `GET /repository/<repo>/simple/` (index), `/simple/<normalised-name>/` (PEP 503 normalisation, redirect
  non-normalised to normalised), content negotiation HTML vs JSON. Rewrite file URLs (upstream
  `files.pythonhosted.org`) to `/repository/<repo>/packages/...`, keep `#sha256=` fragments and `hashes`, keep
  `data-requires-python`, `data-yanked`, `data-dist-info-metadata`/`core-metadata`.
- Validate wheel/sdist sha256. Serve `.metadata` sidecars.
- Hosted: twine/`uv publish` legacy upload API (`POST /repository/<repo>/` multipart, `:action=file_upload`), with
  metadata extraction from the wheel/sdist; yank support.
- Group: merge simple pages (union of files by filename; first member wins).
- Clients: pip, uv, poetry, pdm, twine.

## Maven — Phase 5
- Path layout `<group/as/path>/<artifactId>/<version>/<artifactId>-<version>[-classifier].<ext>`, plus checksums
  `.md5 .sha1 .sha256 .sha512` and `.asc`. `maven-metadata.xml` at artifact and version (SNAPSHOT) level = metadata.
- Proxy: upstream `https://repo.maven.apache.org/maven2/` (Central) or others; validate against upstream `.sha1`/
  `.sha256` when present; generate missing checksum files from stored digests.
- Hosted: `PUT` deploy (mvn deploy, gradle publish), version policy `release`/`snapshot`/`mixed`, layout policy
  strict/permissive, regenerate `maven-metadata.xml` on deploy (server-side, serialised per GA).
- Group: merge `maven-metadata.xml` (versions union, latest/release recomputed).
- Clients: mvn 3.9/4, gradle 8/9, sbt (smoke).

## Go modules — Phase 5
Spec: GOPROXY protocol (`go help goproxy`), checksum database (`sum.golang.org`).
- `/<module>/@v/list`, `/<module>/@v/<ver>.info|.mod|.zip`, `/<module>/@latest`; module paths case-encoded
  (`!` + lower-case). `list` and `@latest` = metadata; `.info/.mod/.zip` = content.
- Proxy upstream `https://proxy.golang.org`. Also proxy the checksum database at
  `/repository/<repo>/sumdb/sum.golang.org/...` (`supported`, `lookup`, `tile`) so clients can verify modules without
  direct access to sum.golang.org (the go command asks the proxy's `/sumdb/<name>/supported` first). Private modules:
  `GOPRIVATE` / `GONOSUMDB` for those paths (documented).
- Clients: go 1.24+.

## Helm (classic repositories) — Phase 5
- `index.yaml` (metadata) + chart `.tgz` (content) + optional `.prov`. Proxy rewrites chart URLs in `index.yaml`
  (absolute and relative) to the repo. Hosted: `PUT`/`POST` chart upload (ChartMuseum-compatible `POST /api/charts`
  optional), worker regenerates `index.yaml`. Group merges `index.yaml`.
- OCI charts are served by docker repos (`helm push oci://...`). Classic repo auth is HTTP basic
  (`helm repo add --username/--password`).
- Clients: helm 3/4 (`helm repo add`, `helm pull`, `helm cm-push` optional).

## Terraform / OpenTofu — Phase 5
Specs: Provider Network Mirror Protocol, Provider Registry Protocol, Module Registry Protocol, Remote Service
Discovery (`/.well-known/terraform.json`).
- **Providers (proxy)**: implement the *network mirror protocol* at `/repository/<repo>/<hostname>/<namespace>/<type>/index.json`
  and `/<version>.json` (archives per platform with `hashes` (`h1:`/`zh:`)). The mirror fetches from the origin
  registry via the provider registry protocol (service discovery on `<hostname>`, `providers.v1`,
  `/v1/providers/<ns>/<type>/versions`, `/<version>/download/<os>/<arch>`), downloads the zip, verifies against
  `SHA256SUMS` and its GPG signature (`signing_keys`). Clients: `provider_installation { network_mirror { url = ".../repository/<repo>/" } }`.
- **Modules**: module registry protocol (`/v1/modules/<ns>/<name>/<system>/versions`, `/download` returning
  `X-Terraform-Get`); proxy rewrites `X-Terraform-Get` to an archive cached by Waystation when the source is an
  HTTP(S) archive or a GitHub tag archive; git-only sources are passed through unchanged (documented limitation).
- **Hosted**: private module registry (upload `.tar.gz` per version) and private providers (zip + SHA256SUMS +
  signature, served via the provider registry protocol). Service discovery (`/.well-known/terraform.json`) is per
  host, so an instance advertises **one** hosted Terraform repository (setting `terraform_hosted_repo`); its
  `modules.v1`/`providers.v1` URLs point into that repo. Proxy (network mirror) repos need no discovery and any
  number can exist.
- Works for registry.terraform.io and registry.opentofu.org upstreams. Clients: terraform 1.9+, tofu 1.8+.

## Ansible Galaxy — Phase 5
Spec: Galaxy NG / Pulp Ansible v3 API (what `ansible-galaxy` speaks to galaxy.ansible.com).
- Discovery `GET /repository/<repo>/api/` → `available_versions: {v3: "v3/"}`.
- Collections: `/api/v3/collections/<ns>/<name>/`, `/versions/`, `/versions/<ver>/` (with `download_url`,
  `artifact.sha256`), artifact download; upstream galaxy.ansible.com uses
  `/api/v3/plugin/ansible/content/published/collections/index/...` paths: map them. Rewrite `download_url` to the
  repo; validate sha256. Hosted: `ansible-galaxy collection publish` (multipart upload to
  `/api/v3/artifacts/collections/`, returns a task URL; the client then polls
  `/api/v3/imports/collections/<task_id>/` until `state` is `completed`/`failed`, so implement that endpoint backed
  by the worker import job that validates `MANIFEST.json`/`FILES.json`). Auth header is `Authorization: Token <t>`.
  Group: merge version lists.
- Roles: legacy v1 API is git-sourced; proxy role metadata read-only, archives from GitHub tag tarballs cached as raw
  content (documented limitation).
- Clients: ansible-galaxy (ansible-core 2.17+), `ansible-galaxy collection install -s <url>` / `ansible.cfg` server list.

## Swift Package Registry — Phase 5
Spec: Swift Package Registry Service (SE-0292, `swift package-registry`). `Accept: application/vnd.swift.registry.v1+json`, responses carry `Content-Version: 1`.
- Endpoints: `GET /{scope}/{name}` (releases), `GET /{scope}/{name}/{version}` (metadata, `resources[{name:"source-archive", checksum}]`),
  `GET /{scope}/{name}/{version}/Package.swift` (+ `?swift-version=`), `GET /{scope}/{name}/{version}.zip`,
  `GET /identifiers?url=<repo-url>` (SCM URL → package ids), `PUT /{scope}/{name}/{version}` (publish, multipart:
  source-archive, metadata, optional signature), `POST /login`.
- There is no universal public upstream registry. **Proxy mode** = "SCM-backed": a mapping of scopes to Git hosts
  (e.g. scope `apple` → `https://github.com/apple/`); releases = Git tags (semver); source archives built from the
  tag's archive tarball, repackaged as zip with the `<name>/` prefix the spec requires. The repackaging must be
  **byte-deterministic** (sorted entries, fixed timestamps/permissions, fixed compression level) and the archive and
  its checksum are pinned on first fetch: these archives are excluded from cleanup policies by default, because a
  re-built archive with a different checksum would break clients' `Package.resolved`. `/identifiers` derives ids
  from URLs. Optionally proxy another registry when configured.
- Hosted: publish via `swift package-registry publish`; signature verification optional (record signer).
- Clients: Swift 6 toolchain on Linux in tests (`swift package-registry set`, `--replace-scm-with-registry`).

## NuGet v3 — Phase 6
Service index `index.json` (resources: `PackageBaseAddress/3.0.0` flat container, `RegistrationsBaseUrl`,
`SearchQueryService`, `PackagePublish/2.0.0`). Rewrite every resource URL; registration pages contain absolute
URLs (rewrite); `.nupkg` content validated against catalog/registration hash when available. Hosted push with
`X-NuGet-ApiKey` = API token. Clients: dotnet CLI, nuget.exe (smoke).

## Cargo (sparse index) — Phase 6
`config.json` (`dl`, `api`), sparse index files at `/<prefix>/<crate>` (metadata), downloads `.crate` validated
against `cksum` in the index. Hosted publish API (`PUT /api/v1/crates/new`), yank/unyank, owners minimal.
Client: cargo with `sparse+https://.../repository/<repo>/index/`.

## RubyGems — Phase 6
Compact index (`/versions`, `/info/<gem>`, `/names`) + `/gems/<name>.gem`, `/quick/Marshal.4.8/*.gemspec.rz`;
hosted `POST /api/v1/gems`. Clients: gem, bundler.

## Composer — Phase 6
`packages.json` with `metadata-url` (`/p2/%package%.json`); dist URLs rewritten; proxy only. Client: composer 2.

## Conda — Phase 6
Channel `channeldata.json`, `<subdir>/repodata.json(.zst)` (metadata), `.conda`/`.tar.bz2` (content, sha256 from
repodata). Proxy only. Clients: conda, mamba, pixi.

## Alpine apk — Phase 6
`<branch>/<repo>/<arch>/APKINDEX.tar.gz` (signed by Alpine keys; pass through) + `.apk` files. Proxy only. Client: apk.

## Hugging Face — Phase 6
Hub HTTP API subset used by `huggingface_hub` (`/api/models/<id>/revision/<rev>`, `/<id>/resolve/<rev>/<file>`
with `X-Repo-Commit`, `ETag`, LFS redirects). Proxy only; large files. Client: `HF_ENDPOINT=<repo url>`.
