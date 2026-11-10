# Changelog

All notable changes, newest first. Each entry: date, task ID, summary.

## 2026-11-10
- T-P5-YUMH: hosted yum repositories. RPMs are uploaded with `PUT <path>.rpm` or `POST` to a directory (streamed,
  quota-checked; the header is read safely in a thread and the package's repodata entries rendered then), with
  write policy, refusal of the same package at another path and `DELETE`. The worker job `index.yum` (outbox, dirty
  flag) builds gzip `primary`/`filelists`/`other` and `repomd.xml`, signs it with the repository's own key and
  publishes atomically (previous generation kept); the public key is served at `RPM-GPG-KEY`. Signing sits in
  `waystation_common.signing` (RSA 3072 per repository, encrypted with `WAYSTATION_SECRET_KEYS` under `keys/`) on the
  in-house `waystation_common.openpgp`: ADR 0007 drafted, **Proposed** (needs the owner's approval; gpg is the
  fallback). Browse shows uploads, "Set me up" gives the `.repo` file (`repo_gpgcheck=1`, gpgkey, credentials) and
  curl commands, the create dialog offers hosted yum. Harness: rpmbuild in the Rocky client, upload, dnf
  install/upgrade with `repo_gpgcheck=1`, delete.
- T-P2-YUM (done): the yum proxy can verify `repomd.xml` itself. New option `repomd_keys` (the upstream's armored
  public keys, validated when saved): `repomd.xml.asc` is fetched with every new `repomd.xml`, which is cached only
  when the signature verifies (else 502; stale-on-error applies); the verified `.asc` is served with it. In-house
  OpenPGP verification `waystation_common.openpgp` on `cryptography` (v4 RSA/Ed25519 keys, SHA-2 signatures; SHA-1,
  expired and unknown-critical signatures refused), tested against real Rocky Linux and AlmaLinux signatures. RPM
  version order (`rpmvercmp` with `~`/`^`, epoch:version-release) as `yum_packages.version_sort` with index
  `repo_name_sort`. Browse: `GET /api/v1/repositories/{name}/yum/packages` and `/yum/versions` and a yum renderer
  (packages → files, plus the folder tree). yum is offered in the create dialog (proxy) with Rocky, AlmaLinux, Oracle
  Linux and EPEL presets and its options (repository path, gpgkey, repomd keys). Harness: Oracle Linux 9 (`addons`),
  EPEL 10, dnf 5 (Fedora 43) and zypper (openSUSE Leap 16) clients, an API token in the `.repo` (refused once
  revoked), Rocky with `repomd_keys`; fixture budget 16 MiB (EPEL metadata).
- T-O09: declarative provisioning. A YAML file (`WAYSTATION_PROVISION_FILE`, `Documents/Operations/PROVISIONING.md`,
  full example `provisioning.example.yaml`, JSON Schema `provisioning.schema.json` from the pydantic models) declares
  repositories (every format, type and option; cleanup attachments; groups whose members the same file creates),
  cleanup policies, custom roles, local groups, service accounts, LDAP/OIDC providers with their group → role
  mapping, webhooks and settings (instance name, 2FA policy, anonymous access, default docker repository, low-disk
  guard). Secrets are only `{file: ...}`/`{env: ...}` references (inline values and Waystation's own settings
  variables refused, never echoed), stored encrypted and shown as redacted. Plans list create / update (field
  diffs) / unchanged / prune; a file with any validation error (the API's own rules, run as dry builds) applies
  nothing; applying an unchanged file changes nothing. `init` applies it at every start and fails when it is
  invalid (init now also waits for Valkey); Administration → Provisioning and `GET /api/v1/provisioning`,
  `POST /api/v1/provisioning/plan|apply` (admin) plan and apply the configured file (never an uploaded one);
  `python -m waystation_server.provisioning schema|check|plan|apply`, `make provision-check|plan|schema`. Managed
  objects carry `managed_by: provisioning` (new field on repositories, roles, groups, service accounts, providers,
  cleanup policies and webhooks, and on their API views), existing objects are adopted, left-out ones released or,
  with `prune` per section, deleted (API guards apply). Every change is audited with `details.source=provisioning`;
  cleanup policy changes are now audited everywhere (`cleanup_policy.create|update|delete`). UI: "Managed" label
  and read-only forms on managed objects, the Provisioning page (status, plan diff, apply), MSW mocks.
- T-S08: `EventEmitter.flush` runs one flush at a time. A flush that started while another was in flight (the
  background loop and an explicit `flush`) could land first, so an older transfer state written last stayed in
  `transfers` (a stalled transfer never corrects it), and `flush()` could return before the earlier events were in
  Valkey. This was the cause of the intermittent `test_owner_gone_mid_transfer_a_follower_takes_over` timeout
  (followers' live transfers stuck at 0 bytes); reproduced with an injected pipeline delay and fixed. The test's
  timeout now reports the bytes each follower had sent.
- T-F08: a job run's `expires_at` is computed from the same clock read as its `started_at` (it read the clock twice,
  so a run could be kept retention + 1 ms; `test_stats_rollup_runs_as_a_job_exactly_once` failed now and then).
- T-P5-HELM / T-P5-MAVEN: deleting a repository now also drops its `helm_charts` records and its Maven `components`
  (they were left behind). A test fails if a `repo_id`-keyed collection is missing from the delete list.
- T-P5-GALAXY: Ansible Galaxy repositories (`waystation_server.formats.galaxy`, FORMATS.md "Ansible Galaxy").
  Proxy: discovery (`api/`, v1 only where roles are proxied), collection, version list and version documents cached
  as metadata (every upstream page, revalidated, validated, bounded; large fields dropped) and served with this
  repository's URLs (`href`s, pagination `links`, `download_url`); upstream paths in the Galaxy NG layout by default
  (`collections_path` option), Galaxy NG paths also accepted from clients; artifacts verified against the version's
  sha256 and size while they stream. Hosted: `ansible-galaxy collection publish` (multipart streamed into the blob
  store, `sha256` field checked, 256 MiB limit, quota), the import task and the new `import.galaxy` worker job
  written in one transaction (outbox); the worker reads the archive without extracting it (tar-slip guards,
  `MANIFEST.json`, `FILES.json` against the manifest checksum, every file's sha256, no unlisted files) and then lists
  the version; `GET api/v3/imports/collections/<task>/` for polling; versions immutable; delete a version or a whole
  collection; audited. Group: merged version lists (first member wins), documents and artifacts from the first
  member that has the version, publishing and task polling through the writable member. Legacy v1 roles proxied
  read-only (search, versions, archives cached unverified). New collections `galaxy_collections`, `galaxy_imports`
  (TTL 30 days); repository deletion drops them. Browse `galaxy/namespaces`, `galaxy/collections`, `galaxy/versions`
  (contract + UI renderer), create flow (galaxy.ansible.com preset, collections path, roles toggle), "Set me up"
  (`ansible.cfg` server list with a token, install, requirements, publish, delete, roles). Cleanup groups artifacts
  per collection in SemVer order. `formats.pypi.multipart` moved to `formats.common.multipart` (shared). Real-client
  harness `tests/clients/galaxy/` (new `client-ansible`: ansible-core 2.22; galaxy.ansible.com, its S3 redirect and a
  GitHub role archive recorded, 75 KB) and the fixture scrubber now redacts Pulp's `validate_token` query value.
  `Documents/Operations/clients/galaxy.md`.
- T-S07, T-S12, T-S05, T-S09: fixes for the k6 suite's findings (full profile, before -> after). 500 concurrent
  clients: 1-5 failed downloads per run -> 0. The cause was mongod refusing connections past its open-file limit
  (Docker's default soft limit 1024 leaves about 400 connections; four server processes with 100-connection pools
  exceeded it), which pymongo reported as `AutoReconnect: Connection is closed`; `compose.yaml` now sets
  `ulimits: nofile: 64000` on `mongodb` (DEPLOYMENT.md "MongoDB connections"), and hot-path reads (`assets.get`,
  `assets.sizes`, the repository catalog load) retry once after 20-100 ms on `AutoReconnect`
  (`waystation_common.db.retry`; writes never). Docker tokens: a server process whose repository catalog predated a
  new repository issued tokens granting nothing on it (and refused its `/v2/` requests) for up to 5 s; the catalog
  now follows a change stream on `repositories` and reloads once when a lookup by name misses (at most once a second
  and once per name and snapshot; Docker resolution and `/repository/<name>/`). Repository list at 1M assets: p95
  179 ms / p99 1112 ms -> 8 ms / 10 ms: every process recounted `assets` (a 1M-document aggregation) every 30 s on
  the request path; the storage figures now read `repo_usage` (new `artifacts` counter of content assets, kept by
  every asset write, reconciled hourly, filled by migration `0003_usage_artifacts`) and an expired snapshot is
  refreshed in the background. Folder browse: p95 40 ms -> 7 ms: the walk reads several small sub-folders per query
  (batches grow while they hold several children) and the first page walks the folder once instead of twice.
- T-P5-NPMH: npm hosted and group repositories. Hosted (`formats/npm/hosted.py`, `publish.py`): `npm publish` bodies
  (≤ 100 MiB) parsed in a thread, one version and its base64 tarball, `length`/`dist.integrity`/`dist.shasum` checked,
  the tarball read without extraction (bounded, safe entry names) and its `package.json` matched to the version;
  stored as a live upload transfer with the manifest's `dist` computed by the server; write policy (`disable_redeploy`
  answers npm's "cannot publish over"), quotas. Packuments (full and abbreviated) generated from the new
  `npm_versions` collection and the hosted `npm_packages` document (compare-and-set on `rev`; a regeneration that
  raced runs again) and stored as metadata assets (X-Accel, `ETag`/`304`). `npm deprecate` and
  `npm unpublish <pkg>@<v>` (packument updates; removing versions needs `delete`), `unpublish --force`, tarball
  deletes (also behind the `repository/<repo>/` path npm sends), dist-tags `GET/PUT/DELETE` (`dist-tag rm` = `write`),
  hosted search; a version whose tarball a cleanup removed is dropped when a client asks for it. `npm login
  --auth-type=legacy` / `npm logout` on every npm repository: new descriptor hooks `client_login` and
  `request_action`, `FormatRequest.sign_in`/`sign_out`, `waystation_server.client_login` (password checked like
  `docker login`, API token with the user's repository scopes, hidden repositories answer 404 for any failure,
  audited); `verify_login(via=...)`. Groups (`formats/npm/group.py`): packuments merged in a thread (first member wins
  per version, dist-tags from the first member with the tag, tarball URLs to the group), kept in the blob store per
  members' digests, tarballs from the members in order, `?write=true` from the writable member, writes to the
  writable member; proxies answer `dist-tags` too. UI: npm hosted and group in the create flow, hosted browse shows
  version counts instead of cache state, "Set me up" publish snippets (`.npmrc` `_authToken`, `npm login`,
  `publishConfig`, publish/dist-tag/deprecate/unpublish, pnpm). Harness `tests/clients/npm/`: npm 11 login, whoami,
  publish to the group and the hosted repository, republish refused, install of a hosted and a proxied package
  through the group, dist-tag add/ls/rm, deprecate, unpublish of a version and of the package, logout (fixtures
  re-recorded). Client guide `Documents/Operations/clients/npm.md` (publishing, groups, `npm login`).
- T-O07: backup and restore. `Build/Containers/backup/waystation-backup.sh` (bash + Docker only; `make backup`,
  `make restore`, `make verify-blobs`) works in a throwaway `mongo:8` container that borrows the worker's data mount,
  networks and MongoDB settings (URI never on a command line). Backup: `mongodump --archive --gzip` first, then the
  blob files, `keys/` and `manifest.json` (mode, image, MongoDB version, migrations, collection counts, blob
  files/bytes, archive sha256) into `waystation-<UTC time>/` (`.partial` until complete, `latest` link, lock), blob
  files hard-linked from the previous backup (incremental snapshots, copies where the target cannot link),
  `--keep N`, `--hosted-only` (only blobs hosted repositories reference), `--user` for NAS targets that squash root.
  Restore: archive checked against the manifest, database replaced (renamed if needed), blobs and keys copied back
  without overwriting, owner and modes set, Valkey emptied. New worker job `blob.verify` (built-in, Sunday 03:30 UTC,
  `deep`/`repair` parameters): missing or damaged (size, or sha256 when deep) blob files; damaged files moved out of
  `blobs/`; `assets.blob_missing` marks (partial index): proxied assets re-fetched on the next request (fills clear
  the mark), hosted assets reported lost in the run's log and result, a `storage.blobs_missing` activity event and
  `waystation_assets_blob_missing{repository_type}`; `waystation_verify_*_total` counters; Grafana panels and Zabbix
  items with a "hosted content lost" trigger; Tasks page names the task and summarises its runs.
  `python -m waystation_worker.run_task <task> --wait` runs any scheduled task from a worker container.
  `make backup-test`: seeded stack (real uploads and a proxy fill), full and incremental backups, hosted-only backup
  with retention, wipe, restore into an empty stack, verify (proxied asset marked and re-fetched, hosted files
  intact), a deleted hosted blob reported lost. `Documents/Operations/BACKUP.md` (procedures, NAS targets, cron and
  systemd timer); DEPLOYMENT.md §10 points to it.
- T-P2-PERF: k6 performance suite, `make perf [PROFILE=ci|full] [STRICT=1]` (`tests/perf/README.md`). Each run
  starts a throwaway stack as its own compose project (production images built from the checkout, CPU and memory
  limits per profile) with a synthetic nginx upstream generated per profile (raw files, npm, PyPI, Maven, apt, yum,
  Go, Helm and OCI content whose digests the proxies verify), creates one proxy repository per format, warms the cache,
  bulk-seeds cached-file records for the API (100 000 in CI, 1 000 000 in the full profile), runs one k6 scenario
  per SPECIFICATION §5.1 budget (content hits incl. ranges and a large file, metadata hits, docker manifests, miss
  overhead against the origin, API list/search, 500 concurrent downloads in the full profile) plus a live-event
  probe on the SSE stream, and always tears the stack down. It holds the machine-wide integration-test lock for the
  whole run. `tests/perf/collect.py` turns the results into `tests/perf/.results/measured.json`, now the `source` of
  the k6 budgets in `Build/ci/budgets.json`, and a Markdown report with per-scenario figures and container CPU and
  memory. `make budgets STRICT=1` / `budgets.py --strict` fail on every budget over its limit (warn-only until M2
  otherwise). Gitea workflow `perf.yml`: CI profile after every push to `main`, full profile nightly, results as an
  artifact. First results (dev box): every measured budget within its limit in the CI profile (content hit p95 TTFB
  4 ms, metadata 3.7 ms, docker manifests 3.8 ms, miss +3.6 ms TTFB and no throughput loss at 100 MiB/s, API p95
  21 ms at 100 000 assets, live events 245 ms, 0 errors with 100 clients); full profile: API p95 35 ms at 1M assets,
  1-2 failed downloads per run in about 115 000 with 500 concurrent clients (warning: a cache hit's asset lookup
  got `pymongo AutoReconnect: Connection is closed` and answered 500 without a retry; to be investigated). A cached 10 GiB file is served at about
  1.3 GiB/s with the server at 5% of one core, which completes T-S06's perf acceptance.
- T-O05: signed webhooks. Definitions in `webhooks` (name, URL, events with categories and `*`, repository globs, enabled, secret and custom headers write-only and AES-GCM encrypted, private-target exception, timeout, attempts) through `/api/v1/webhooks` (admin; CRUD, `POST /{id}/test`, `GET /{id}/deliveries[/{delivery}]`, `POST .../redeliver`), audited `webhook.create|update|delete|test|redeliver`. Events are the successful audit records of `content.upload|delete|update` and `repository.create|update|delete`: `audit.record` hands them to a server-side notifier that writes a `webhook.event` job to the durable outbox when an enabled webhook wants it. Worker jobs `webhook.event` (fan-out into `webhook_deliveries`, attempted at once), `webhook.deliver` (test events, redeliveries) and the scheduled `webhook.sweep` (every 15 s): at-least-once with leases, HMAC-SHA256 `Waystation-Signature: t=<unix>,v1=<hex>` over `<t>.` + body, `Idempotency-Key`, exponential retries to a dead delivery, 1 KiB answer snippets, 30-day log. The SSRF guard moved to `waystation_common.ssrf` (re-exported by `waystation_server.proxy.ssrf`) so worker deliveries use it; common now depends on `httpcore`, the worker on `httpx`. Metrics `waystation_webhook_deliveries_total{outcome}` (Valkey `webhooks:totals`) and `waystation_webhook_deliveries_pending`. UI Administration → Webhooks (table, editor with a browser-generated secret, delivery drawer with attempts, request and redeliver), MSW mocks, screenshot tour entries. `Documents/Operations/WEBHOOKS.md` with Python and Node verification snippets. Test helper `FakeUpstream` now records request bodies.
- T-P5-HELM: Helm classic chart repositories (`formats/helm/`). Proxy: `index.yaml` cached as metadata (revalidated),
  parsed with a safe, bounded YAML loader in a thread (only `null` resolved implicitly, no tags or aliases), chart
  URLs rewritten to the repository (relative paths kept, charts hosted elsewhere served at
  `_ext/<scheme>/<host>/<path>`), served as JSON (valid YAML); a chart package is fetched only when the cached index
  lists it and verified against its sha256 `digest` while streaming; `.prov` pass-through. Hosted: `PUT` uploads and
  the ChartMuseum API (`api/charts`, multipart `chart`/`prov`, deletes by `api/charts/<name>/<version>`), chart
  packages read without extraction (one top-level directory, no links or unsafe names, member and size limits,
  `Chart.yaml` validated: apiVersion, name, SemVer version), stored as `<name>-<version>.tgz`, write policy, deletes,
  quotas, audit. The chart record and an `index.helm` job are written in one transaction (outbox, dedupe per
  repository); the worker rebuilds `index.yaml` from the records and publishes it in one asset write; cleanup runs
  that remove hosted charts trigger it too. Group: one merged `index.yaml` (first member wins per chart version)
  committed to the blob store and served by X-Accel, charts from the first member that has them, uploads to the
  writable member. New `helm_charts` collection (SemVer `version_sort`), browse API `helm/charts` and
  `helm/versions`. UI: Helm tile in the create flow (proxy presets, hosted, group), browse renderer (charts →
  versions), "Set me up" (repo add/update/search/install/pull, upload with curl or `helm cm-push`), mocks.
  Real-client harness `tests/clients/helm/` (helm 3 and helm 4: repo add, search, pull, template through a proxy of
  the metrics-server chart repository and a group; curl and ChartMuseum uploads to a hosted repository, index rebuild,
  delete), recorded and replayed. Client guide `Documents/Operations/clients/helm.md`. OCI charts stay in docker
  repositories.
- T-O08: two-factor sign-in (TOTP, RFC 6238) for local accounts. `security.totp` (standard library: HMAC-SHA1, 6 digits, 30 s, ±1 step, constant time), `security.second_factor` (replay protection in Valkey `totp:used:<user>:<step>` and `users.totp_last_step`, recovery codes as keyed hashes consumed atomically, pending sign-ins in the new `login_challenges` collection, the admin policy). API: `POST /auth/login` answers `202` (`SecondFactorRequired`, HttpOnly SameSite=Strict `ws_mfa` cookie) for enrolled accounts, `POST /auth/login/totp`, `GET /auth/me/totp`, `POST /auth/me/totp/{setup,confirm,recovery-codes,disable}`, `DELETE /users/{id}/totp`, `Session.totp_setup_required`, `UserSummary.totp_enabled`, `InstanceSettings.security.require_totp_for_admins` (`settings` document `security`), problem+json `code`. Wrong codes count towards the lockout; the password step of an enrolled account no longer resets the failure count. `docker login` with the password of an enrolled account (or of an administrator who must enrol) is refused. Audit actions `auth.totp_enable`, `auth.totp_disable`, `auth.totp_recovery_codes`. UI: second sign-in step (code or recovery code), profile card (set-up dialog with a QR code from an in-house SVG encoder, recovery codes dialog with copy/download, new codes, turn off), admin "Reset two-factor sign-in", Settings toggle, router guard; MSW mocks (`sam` signs in with a code).
- T-P5-GO: Go module proxy and group repositories (`formats/go/`). Proxy: the GOPROXY protocol (`@v/list`, `@latest`,
  `.info`, `.mod`, `.zip`) with module paths and versions case-decoded, validated and re-encoded, lists and `@latest`
  revalidated as metadata, version files kept as content, size limits, upstream `410` answered as `404`. `.mod` and
  `.zip` files are verified against the checksum database's `go.sum` lines (`h1:` dirhash) before they are cached
  (option `verify_checksums`; private modules skipped with `nosumdb` patterns). The checksum database is proxied at
  `/repository/<repo>/sumdb/sum.golang.org/` (`supported`, `latest`, `lookup`, tiles), so the go command verifies
  modules without reaching sum.golang.org. Groups merge `@v/list` and serve files from the first member that has them.
  Browse (`go/modules`, `go/versions`) and search on `go_modules`; Go semver order for cleanup; UI create flow,
  options, Go browser and "Set me up" (GOPROXY, GONOSUMDB, `.netrc`). Real-client harness `tests/clients/go/` (go
  1.25 through a proxy and a group, recorded against proxy.golang.org and sum.golang.org); client guide
  `Documents/Operations/clients/go.md`.
- T-M01 (follow-up to T-P5-PYPI and T-P5-GO): the Nexus import maps PyPI and Go proxies' content max age to
  "forever" like docker, npm, apt and yum (PyPI files are verified against the project page's hashes, Go module
  versions are immutable and checked against the checksum database), reporting the difference.
- T-P5-MAVEN: Maven proxy, hosted and group repositories (`formats/maven`). Options `version_policy`
  (release/snapshot/mixed) and `layout_policy` (strict/permissive). Proxy: `maven-metadata.xml` cached as revalidated
  metadata and refused (502) unless it parses (`defusedxml`, new server dependency; no DTDs, bounded); artifacts
  verified while streaming against the upstream `.sha1` (else `.sha256`); checksum files of cached files generated
  from the stored digests; `.asc` passed through; SNAPSHOT paths of a release proxy answer 404 without an upstream
  request. Hosted: `PUT` deploys as live uploads after the hard-quota check, version/layout/write policies
  (SNAPSHOTs always redeploy), uploaded checksums compared with the stored file, `maven-metadata.xml` (artifact and
  SNAPSHOT version level) regenerated by the server after each deploy and delete under a per-artifact Valkey lease
  (`lock:maven:*`), groupId-level plugin documents merged, file and version-folder deletes, audited. Group: first
  member wins, merged `maven-metadata.xml` (versions union, latest/release recomputed), read-only. Components with
  Maven ComparableVersion `version_sort` in `components` (new partial unique index `maven_gav_unique`). Browse API
  `GET /repositories/{name}/maven/artifacts` and `/maven/versions` (contract, generated client, MSW mocks) and a
  Maven browser in the UI; Maven in the create flow (Central, Google and Gradle-plugin presets; hosted and group)
  with the two policies in the repository form; Set me up for Maven (settings.xml mirror, servers, SNAPSHOT profile,
  preemptive auth, distributionManagement), Gradle and sbt. Real-client harness `tests/clients/maven/` (mvn 3.9,
  build plugins pre-seeded in the client image; 128 KiB of fixtures). Harness fix: fixture file names keep the request
  key for names with dots (`x.pom` and `x.jar` no longer share a file); older names are still read when their
  recorded request matches. Draft ADR 0005 (401 challenge instead of 404 for anonymous requests to hidden
  repositories on format routes; proposed, not implemented). The Nexus import now creates `maven2` repositories
  (`maven.versionPolicy`/`layoutPolicy` mapped). `Documents/Operations/clients/maven.md`.
- T-P5-PYPI: PyPI proxy, hosted and group repositories (`formats/pypi/`). Proxy: `simple/` and `simple/<project>/`
  in PEP 503 HTML and PEP 691 JSON (negotiated by `Accept`, cached per representation, `301` to the normalised name and
  trailing slash), rendered from the upstream's JSON or HTML page with relative file URLs into the repository and
  hashes, requires-python, yanked and core-metadata kept (PEP 700 versions, sizes, upload times); files and PEP 658
  `.metadata` files looked up in the cached page and verified by hash before they are cached; legacy JSON API
  pass-through (`json_api` option). Hosted: twine / `uv publish` upload API streamed through an in-house multipart
  reader into the blob store, core metadata read from wheels and sdists in a thread with zip/tar-slip and size checks
  and matched against the file name and form, `sha256_digest` verified, wheel `METADATA` served as `.metadata`, write
  policy (`disable_redeploy`: same bytes accepted, other content "File already exists"), pages rendered per request
  (`ETag`/`304`), yank / un-yank (`POST /api/v1/repositories/{name}/pypi/yank`, audited as the new `content.update`;
  uploads audited `content.upload`). Group: merged pages (first member wins per file), files from any member's copy
  then the proxy members, uploads to the writable member. Browse API `pypi/projects` and `pypi/versions`, new
  `pypi_packages` collection (search source, kind `package`), PEP 440 ordering (`waystation_common.cleanup.versions`:
  tuple key and an index-sortable string) used by cleanup (files of one release share a rank). UI: PyPI tile in the
  create flow (proxy with a pypi.org preset, hosted, group with writable member), `json_api` option, browse renderer
  (projects, versions with wheels/sdists, yank dialog), MSW mocks, "Set me up" for pip, uv, Poetry, PDM, twine and
  uv publish. Real-client harness `tests/clients/pypi/` (new `client-python` image: pip 26, uv 0.13, twine 7,
  Poetry 2.5, PDM 2.29) against recorded pypi.org fixtures (~60 KB); replay and live pass.
  `Documents/Operations/clients/pypi.md`.
- T-O03: quota and low-disk monitoring. Grafana dashboard row "Quotas and disk" (low-disk guard state; repositories
  near their quota, over a soft quota and at a hard quota; downloads not cached and uploads refused by a quota;
  quota used per repository; a per-repository quota table). Zabbix template: items for the low-disk guard, downloads
  not cached (quota, low disk) and uploads refused; "Repository quotas" discovery with quota, mode, stored bytes,
  quota used and per-repository refusals; triggers "reached its hard quota" (High), "over its soft quota" (Warning)
  and "near its quota" (Information, macro `{$WAYSTATION.QUOTA.WARN}`, default 80). New unit test
  `test_monitoring_assets.py` checks the dashboard and template against the metrics the server exports. The PromQL
  was checked with promtool; neither file has been re-imported into Grafana or Zabbix yet.
- T-F03: Gitea check workflow `.gitea/workflows/check.yml`. Every push to `main` and every pull request runs the
  parts of `make check` except the integration tests as separate steps (forbidden strings, workflow lint, format
  check, lint, typecheck, unit tests, client-harness fixtures, frontend checks), the generated API client diff and
  the budget report, with the dev image built once per job. `make test-integration` (about 6 GB of memory) runs in a
  second job after it, only on push to `main` and manual runs, with a 60 minute timeout; the organisation variable
  `WAYSTATION_CI_INTEGRATION=false` skips it. Checked with actionlint; not yet run on the Gitea runners. CI.md
  documents the runner needs (host-mode jobs, Docker, memory) and the variable.
- T-M02: Nexus migration. `Documents/Operations/MIGRATING_FROM_NEXUS.md`, step by step: inventory, import,
  re-entering upstream credentials and access (roles, sign-in, API tokens), copying hosted content (skopeo/crane for
  docker, a Nexus assets API + curl loop with checksum for raw), warming the cache, side-by-side validation with the
  expected differences, switching clients per format, cutover through the reverse proxy (per repository, docker
  connector ports), rollback (uploads since the switch from the audit log) and decommissioning.
  `Build/migration/nexus_compare.py` (standard library only) sends the same read-only requests from a TOML checks
  file to a Nexus and a Waystation and compares status, size, sha256 (optionally after base-URL and JSON
  normalisation), `Docker-Content-Digest`, media type and JSON Pointer values; answers docker Bearer challenges, never
  sends credentials across redirects, takes credentials only from the environment; table, Markdown or JSON report and
  exit code 0/1/2. `checks.example.toml` covers docker, npm, apt, yum, raw and pypi; `make nexus-compare`. Tested
  against two fake servers in `make test`.
- T-P2-RAW: raw hosted and group repositories, directory listings. Hosted (`formats/raw/hosted.py`): streaming
  `PUT /repository/<repo>/<path>` (live upload transfer, `WAYSTATION_MAX_UPLOAD_GB` and `Content-Length` bound,
  free-space guard), `X-Checksum-Sha256`/`-Sha1`/`-Md5` verified before anything is committed (400 on a mismatch or a
  malformed header), Content-Type by extension, `201` with the digest `ETag`; write policy (`allow_redeploy` replaces,
  `disable_redeploy` → 409, `read_only` → 403); `DELETE` → 204; audited `content.upload` / `content.delete`,
  `asset.created` / `asset.deleted` in the activity feed. Group (`formats/raw/group.py`): members in order, failing
  members skipped while a later one answers, uploads and deletes to the writable member (RBAC on the member too),
  `X-Ws-Group-Member` (now also passed through the web tier's X-Accel location). Directory listings
  (`formats/raw/listing.py`) for every raw repository at the root and paths ending in `/`: HTML (escaped, `default-src
  'none'`) or JSON (`Accept: application/json`, the browse API's `FolderListing` with `?cursor=` paging), `browse`
  required. `metadata_path_regex` is proxy-only. "Set me up": curl upload (with a checksum), download, delete and
  listing; wget with a bearer header. UI: the repository form offers Hosted and Group for raw (writable member,
  no mutable-path regex outside proxies); mock snippets. Real-client harness: hosted upload/download/delete,
  conditional request, JSON listing, group reads in member order and upload through the group (replay and live
  pass). T-M01: Nexus raw hosted and group repositories are imported (new fixture `raw-group`).
- T-O03: repository quotas and the low-disk guard. `repositories.storage{quota_bytes, quota_mode}` (soft / hard;
  not on groups) in the model, REST, contract and repository form; repository views carry `quota{quota_bytes, mode,
  used_bytes, used_ratio, state}`. Usage per repository in the new `repo_usage` collection, counted with one `$inc`
  per asset write or delete (proxy fills, hosted uploads and deletes, cleanup) and recounted hourly by the worker job
  `quota.reconcile` (repairs drift); a blob shared by several repositories counts in each. Thresholds 80 % and 100 %
  are announced once (`quota.warning` / `quota.exceeded`, activity kind `storage`) and re-armed when usage falls
  back. Hard quotas refuse hosted docker pushes with `403 DENIED` (message names the quota) and serve proxy content
  fills to the client verified but uncached (`X-Ws-Not-Stored: quota`; coalesced followers fetch on their own);
  metadata, cache hits and deletes are unaffected. Low-disk guard settings (`settings` document `storage`:
  `min_free_bytes`, `min_free_percent`; `GET/PUT /api/v1/storage/guard`, `settings-admin`, audited) applied by every
  replica within 5 s; below the floor fills pass through uncached (`low_disk`) and uploads get `507`; the worker job
  `storage.check` (every minute) announces `storage.low` / `storage.ok` (Valkey `storage:low`). Metrics:
  `waystation_quota_refusals_total`, `waystation_fills_not_stored_total`, `waystation_quota_alerts_total`,
  `waystation_storage_low`, `waystation_repository_{quota_bytes,stored_bytes,quota_used_ratio}`; the
  `waystation_blobstore_min_free_bytes` gauge is now the effective floor. UI: quota section in the repository form,
  usage meters in the repository list and detail, low-disk banner in the shell, Low-disk guard card with an edit
  dialog on the Storage page, `storage` activity kind, MSW mocks. Tests: unit, integration (hard quota blocks writes
  and still serves hits, soft only warns, reconcile fixes drift, low-disk pass-through, guard API), Vitest, and a
  client-harness step pushing to a hard-quota docker hosted repository.

## 2026-11-09
- T-M01: Import from Nexus. `waystation_server.migration` reads a Nexus Repository 3 configuration over its REST API
  (`/service/rest/v1/repositories` + per-format detail endpoints, list-entry fallback; basic auth per request, never
  stored or logged; SSRF guard with `allow_private`, optional CA bundle, no redirects, bounded sizes, count and time),
  maps each repository (name, format, type, upstream, metadata/content ages, negative cache, auto-block, blocked →
  offline, docker Hub + upstream-host aliases, default docker repository from the request, a connector or the only
  Hub proxy, apt distribution, raw disposition, hosted write policy and group members where supported) and reports
  create / exists / unsupported / invalid with field-level differences. REST `POST /api/v1/import/nexus/preview`
  (dry run) and `/apply` (selection, members before groups, idempotent, `repository.create` audit records with
  `details.source=nexus_import`), contract + regenerated client, `repos.service.check` (dry-run validation). UI:
  Repositories → "Import from Nexus" page (connect, plan table with selection and differences, apply), MSW mocks,
  screenshot-tour entries. Tests on hand-written Nexus 3 REST fixtures served by the fake upstream (unit + API
  integration); no live Nexus.
- T-U08: Administration UI. Users (list with source badge, roles, last sign-in and status; filters in the URL;
  keyset paging; create a local user with or without a password, edit, reset password, unlock, disable/enable, sign
  out everywhere, delete) and a page per user (linked identities; local, mapped and provider-reported groups;
  effective permissions with a repository × action matrix; sessions; API tokens). Groups (local CRUD and members,
  mapped groups' roles), Roles (permission matrix editor with live scope matches, built-ins read-only, duplicate,
  "held by") with an Anonymous access card, API tokens (every token, revoke) with Service accounts (tokens shown once,
  permissions), Settings and System pages; 403/409 refusals shown with the server's reason; rail items by capability
  (Auth providers now needs `settings.admin`, like its API). New API: `GET/DELETE /users/{id}/sessions` (audited
  `session.revoke` with `details.by_admin`), `GET/PUT /settings` (instance name in the `settings` collection, served
  by `/config`, audited `settings.update`; environment limits read-only), `GET /system` (version, MongoDB/Valkey
  health, data volume). MSW mocks for every administration endpoint (`mocks/admin.ts`; sign in as `helpdesk` for a
  user-admin who is not an administrator). T-A03 done with the Users/Groups pages.
- T-P2-DOCKERG: docker group repositories (`formats/docker/group.py`). Model/REST/contract `group{members,
  writable_member}` (validated: existing docker members, no nesting, no duplicates, writable member hosted; the
  descriptor's `writable_group_member`); a repository in a group cannot be deleted (409). Reads try members in order,
  blobs come from any member's copy first and blob `HEAD`s never go upstream, `tags/list` and referrers are merged;
  pushes, upload sessions and deletes go to the writable member (RBAC on the member via `req.allows_member`), a group
  without one answers `405`. Cancelling an upload (`DELETE /blobs/uploads/<id>`) now needs push, not delete
  (`registry_auth.request_action`). A mount `from` a group uses its members' copies. UI: "Group" tile with ordered
  members and writable member; mocks; "Set me up". Tests: integration and client harness (push through
  `docker-group`, member order, merged tags), and the OCI conformance suite also runs through the group.
- T-P2-DOCKERH: docker hosted repositories. Push side of the registry API (`formats/docker/hosted.py`): monolithic,
  chunked and resumable blob uploads (`upload_sessions`, append lease, `Range`/`416`, upload status and cancel) into
  resumable uploads of the blob store (`BlobStore.start_upload`/`resume_writer`, sha256 continued across requests or
  re-hashed in a thread), cross-repository mounts (`?mount=&from=`, RBAC `read` on the source, proxy caches included),
  manifest validation (media types, referenced blobs with sizes, index children, 4 MiB, digest), write policy
  (`allow_redeploy`/`disable_redeploy`/`read_only`) and immutable tag patterns, deletes (tag, manifest, unreferenced
  blob), the referrers API (`artifactType` filter, `OCI-Subject`), `tags/list` paging and `/v2/_catalog` (proxy and
  hosted, filtered by RBAC; every caller may ask for `registry:catalog:*`). Registry `push`/`delete` grants also carry
  `browse`; tags that cannot exist answer `404 MANIFEST_UNKNOWN` (read) / `400 TAG_INVALID` (push). New
  `waystation_server.hosted.store` (`req.hosted`: receive as a live upload transfer, record, link with a GC claim,
  serve by X-Accel, remove). Repositories: `hosted{write_policy, immutable_tag_patterns}` in the model, REST and
  contract, descriptor `check_options` (docker aliases/Docker Hub only on proxies); UI create flow "Hosted" tile and
  settings, live "UPLOAD" badge, MSW mocks. Audit `content.upload`/`content.delete` (no longer pending), activity
  entries for pushes and deletes, upload transfers counted as `bytes_in_upload` (neither hits nor misses). "Set me up"
  for docker, buildx, podman, crane, skopeo, oras and cosign. Client harness: `tests/clients/docker/hosted.sh` (docker
  push, buildx multi-platform, crane, skopeo, oras, cosign 3 offline) and the OCI distribution-spec conformance suite
  v1.1.1 (`client-conformance`), all passing in replay; client-docker image adds crane, oras and cosign (pinned).
- T-O04: Tasks and workers. REST `/api/v1/tasks/*` (all `admin`): `GET /tasks/scheduled` (job definitions with
  schedule, enabled, last run, next run, owning cleanup policy), `PATCH /tasks/scheduled/{id}` (schedule validated by
  `jobs.schedule.validate_schedule`: cron, macro or `@every` ≥ 10 s; a changed schedule restarts the timer; a cleanup
  policy's schedule answers 409), `POST /tasks/scheduled/{id}/run`, `GET /tasks/runs` (status/kind/queue/task/job
  filters, keyset cursor, routine jobs hidden unless `include_routine`), `GET /tasks/runs/{id}` (with the log),
  `GET /tasks/workers` (heartbeat health, running jobs, leader, queue figures, dead-letter count),
  `GET /tasks/dead-letters`, `POST /tasks/dead-letters/{id}/retry` (one Lua step: queue a fresh job, delete the dead
  letter, remember it in `jobs:dead:retried` so a repeat answers with the same job) and `DELETE` (idempotent). Audit
  actions `task.update|run|retry|discard`. Job envelopes and runs carry `trigger` (schedule/manual/retry); worker
  presence lists run ids and concurrency; `job_runs` index `status_started`. Live: the worker publishes progress and
  log lines on `live:jobs`; the SSE topic `tasks` (admin only, 403 otherwise) carries `job.progress`, `job.log`,
  `job.update` and `worker.update`. UI: the Tasks page replaces the placeholder (summary strip; Scheduled, Runs,
  Workers and Dead letters tabs; schedule dialog; run drawer with a live log viewer), MSW mock job system with live
  events, screenshot-tour entries, Vitest; integration tests for the API, dead letters, RBAC, audit and the topic.
- T-S03: RBAC administration and the grant cache. Custom roles (`roles` collection; `GET/POST /roles`,
  `GET/PUT/DELETE /roles/{id}`; built-ins read-only; permissions validated; in-use roles cannot be deleted), local
  groups and members (`/groups`, `/groups/{id}/members`; mapped groups read-only except roles), users (`/users`:
  list with filters and keyset paging, detail with source/identities/groups/effective roles, create local, update,
  delete, password reset, unlock), effective permissions (`/users/{id}/permissions`,
  `/service-accounts/{id}/permissions`, `/auth/me/permissions`), `GET /service-accounts/{id}`, configurable
  anonymous access (`/anonymous-access`, `rbac_state.anonymous_roles`). `security.rbac.RbacState` materialises
  `Principal.grants` per `grant_version` (in-process + Valkey `grants:<principal>:<version>`), learns new versions
  from its own writes, a change stream on `rbac_state` and a 5 s poll; every RBAC change bumps the version.
  Delegation limits in `security.access` (`may_assign`, `may_manage`) now also apply to service accounts and to
  provider group mappings (a `settings-admin` can no longer map a group to `admin`), plus a last-administrator
  guard. `access.filter_query`. New index `users.groups`. Fixed: the route-enumeration test had passed vacuously
  since FastAPI started keeping included routers as `_IncludedRouter` (`ws_testing.iter_routes`); a route × principal
  matrix now covers every REST route. Audit: `user.update|delete`, `group.*`, `role.*` no longer pending.
- T-S02: done. One-time passwords enforced (sessions may only use `/auth/me`, `POST /auth/me/password` and sign-out;
  `docker login` refused, audit reason `password_change_required`), own password change (current password, policy:
  12+ characters, not the username; other sessions end), `PATCH /auth/me` (local profile), own sessions
  (`GET/DELETE /auth/sessions`, `DELETE /auth/sessions/{id}`); `Session.must_change_password`; UI: password card
  on the profile page and a router guard that keeps a one-time-password user there. Audit:
  `auth.password_change`, `session.revoke` no longer pending.
- T-S07: upstream auto-block, per-repository CA bundle and outbound HTTP proxy. Auto-block (`proxy.breaker`): after
  `auto_block.failures` (default 5) consecutive connection/DNS failures, timeouts, mid-body resets, 5xx or 429
  answers the upstream is blocked for `initial_s` (30 s), doubling after each failed probe up to `max_s` (30 min);
  blocked requests get 503 + `Retry-After` or the stale copy, one probe per ended period across the cluster (a client
  request or the per-process prober), a success resumes; state shared in `upstream:<id>:health` via Lua; saving the
  repository clears it. Visible in `/stats/upstreams` (`blocked`, `blocked_until`), repository status, the
  `upstream.state` SSE event, activity, `waystation_upstream_state`, `waystation_upstream_blocked_until_timestamp_seconds`
  and `waystation_upstream_autoblock_total`. Repositories gain `proxy.ca_pem` (validated PEM, trusted for that
  repository only) and `proxy.http_proxy` (else the server-wide `WAYSTATION_HTTP_PROXY`/`NO_PROXY`), applied to
  fetches, Docker token negotiation and "test upstream"; the SSRF guard checks every hop's target through a proxy.
  Contract `ProxyConfig` + `AutoBlockConfig`; repository form sections "Caching and resilience" (auto-block) and
  "Network and TLS"; the blocked countdown on the dashboard and repository overview uses real data. Fault-injection
  tests with a private CA and a local forward proxy.
- T-V02: M0 feedback build tagged `v0.1.0-m0` (images build on Gitea; GitHub Actions run only for major releases).
- CI: GitHub Actions run only for major releases (`vX.0.0` tags) or by hand (ci, release); the live-client run is
  manual only (no nightly schedule). Gitea keeps building images on every push to `main`.
- T-A02: OIDC sign-in providers (kind `oidc` in `auth_providers`). `security.oidc` (protocol side): discovery
  (cached per issuer, issuer must match exactly), authorization code + PKCE S256 with `state` and `nonce`, token
  request (`client_secret_basic`/`client_secret_post`/public), ID token verification with `joserfc` (asymmetric alg
  allowlist, `none`/HMAC refused, key type/alg match, `crit` refused; `iss`, `aud`/`azp`, `exp`, `iat`, `nbf`,
  `nonce`, `sub`), JWKS cache with refetch on an unknown `kid`, optional userinfo, claim mapping with dotted paths,
  group claim, require-group gate, RP-initiated logout URL. All IdP HTTP on the pooled upstream client with the
  SSRF guard (per-provider "allow private addresses"), no redirects, size bounds and timeouts. `oidc_auth` (browser
  side): `GET /auth/oidc/<id>/login` and `/callback`, flow record in Valkey (`oidc:state:*`, single use, 10 min),
  browser binding cookie `ws_oidc`, RFC 9207 `iss` check, rate-limited starts, `?error=oidc_<reason>` on failure.
  Sessions record `provider_id` and the encrypted ID token; `POST /api/v1/auth/logout` now returns
  `{end_session_url}`. Write-only encrypted client secret; admin "Test discovery". UI: OIDC section in the provider
  dialog (redirect URIs to register, claims, scopes, algorithms), "Sign in with <provider>" buttons, sign-out
  follows the end-session URL; MSW mocks. Tests: unit (wrong aud/iss, expired, bad nonce, alg confusion, unknown
  kid → JWKS refresh, PKCE, state/binding) and integration against a Keycloak realm (`make test-integration`) and
  authentik (opt-in `make test-integration-authentik`; `Build/Containers/test/keycloak`, `.../authentik`). New
  dependency `joserfc`; ADR 0004 (accepted 2026-11-09) replaces `authlib` in ARCHITECTURE §2.
- T-A03 (OIDC half): OIDC sign-ins go through `security.external.link_identity` (`oidc:<provider>`, auto-provision
  toggle, never linked to an account with a local password) and sync the group claim into `external_groups`, so
  provider group mappings apply; OIDC provider admin form. Left: Users/Groups admin pages (T-U08).
- T-V02: M0 feedback build (no tag yet). Walked every M0 page against the real server and fixed what did not match:
  Docker tags resolved by an upstream HEAD (how the Docker daemon pulls) are now recorded, so they show in browse;
  activity entries name artifacts the way live transfers do (`image@digest`); a duplicate repository name is a 409
  with a field error on `name`; the UI makes no session check without a session; the live-transfer format filter
  lists only formats in use; routine jobs (the 10 s `stats.rollup`) no longer emit `job.started`/`job.finished`
  activity events, which flooded the dashboard's recent activity (failures still do); "Set me up" shows the API-token
  note only for snippets that use a token; "1 artifact" singular on Browse. `make e2e-m0` (`tests/e2e/m0/run.sh`):
  docker pull nginx, npm install, `apt update && apt install` in a Debian 13 container and a raw download through a
  running stack, checked in the live event stream, stats, activity, upstream health and browse; creates the demo
  repositories when missing. `Documents/Operations/TRY_IT.md` (fresh host: build images, env file, start, first
  sign-in, the four repositories, clients, stop/reset). `scripts/screenshots.mjs` gains `REAL=1` (signs in, tours
  real cached content); the M0 tour is now taken against the real server (`Documents/Notes/UI_TOUR_M0.md`, 88 shots
  including Activity, Storage, Auth providers and Audit log, pngquant). Feedback log `Documents/Notes/UI_FEEDBACK.md`.
- T-O06: metrics gaps filled. The server now exports, besides the format traffic metrics: route latency for every
  HTTP request (`waystation_http_request_duration_seconds{route,method,status}`, route template and status class
  only), free-space guard refusals (`waystation_storage_refusals_total`), connected event-stream clients
  (`waystation_sse_clients`), and cluster figures read from Valkey, MongoDB and the data filesystem when `/metrics` is
  scraped (10 s cache, 2 s per source, `waystation_metrics_source_up{source}` when one fails): upstream state per
  proxy repository (`waystation_upstream_state`, now set, the UI's rule) and consecutive failures, the latest
  upstream rate limit (Docker Hub limit, remaining, window, when), jobs waiting, running and delayed and the oldest
  waiting age per queue, dead letters, live workers and scheduler leaders, blob store used bytes and blobs, free and
  total bytes of the data filesystem and the free-space guard. With `WAYSTATION_SERVER_WORKERS > 1` the processes'
  metrics are merged (`PROMETHEUS_MULTIPROC_DIR` on the server tmpfs, prepared by `python -m waystation_server`), so
  every scrape is complete. Grafana dashboard: new Upstreams, Jobs and workers, Storage and HTTP rows. Zabbix
  template: items for all of them, upstream and job-queue discovery, and triggers for blob store full (below the
  guard), low disk space, refused writes, too few workers, no scheduler leader, old waiting jobs, dead letters,
  blocked/failing/offline upstreams, upstream rate limit and API latency. Both re-checked in Grafana 12.2 /
  Prometheus 3.7 and Zabbix 7.0. `Documents/Operations/MONITORING.md` and SPECIFICATION §10 describe the metrics.
  After merging T-O01/T-O02: the cleanup and blob GC totals (`waystation_cleanup_*_total`, `waystation_gc_*_total`,
  from Valkey `maintenance:totals`) moved from a collector on the process registry into the cluster figures, so
  they are also exported with several server processes; new dashboard panels (bytes freed, cleanup/GC activity),
  Zabbix items (GC runs, bytes freed by GC and removed by cleanup) and a "blob GC has not run for a day" trigger.
- T-O02: blob garbage collection. Worker job `blob.gc` (`waystation_worker.gc`; built-in schedule `0 */6 * * *`,
  `grace_s` and `upload_age_s` 24 h): removes orphan upload files, adopts blob files that have no `blobs` document,
  then marks unreferenced blobs and deletes those unreferenced and unclaimed for the grace period. Writers and GC
  coordinate through the `blobs` document (`waystation_common.db.blobs`): `BlobStore.commit` claims its blob through
  the new `BlobStore.guard` (`MongoBlobGuard`, wired in the server's `Services`) before the rename, also when the
  file is already there; GC deletes only after a compare-and-set to `deleting` and a re-check of references, and a
  claim during a deletion waits and writes the file again (stale deletions are taken over after 10 min). The claim
  replaces the separate `record_blob` write after a fill. Referenced blobs without a file are counted and logged
  (proxies re-fetch them). `GET /api/v1/maintenance/gc`, `POST /api/v1/maintenance/gc/run` (admin) and a Blob GC
  card on the Cleanup and Storage pages; freed bytes as an activity event and on `/metrics` (`waystation_gc_freed_bytes_total`,
  `..._freed_blobs_total`, `..._orphan_uploads_removed_total`, `..._runs_total`, plus `waystation_cleanup_*_total`,
  from the Valkey hash `maintenance:totals`). `BlobStore` gained `prefixes`/`scan`/`existing` and a `keep` set for
  `sweep_uploads`. Tests: a Hypothesis property test (never deletes a referenced, recently claimed or recently
  marked blob; always deletes an eligible one) and a real cache fill through the proxy engine racing the sweep in
  every interleaving (`Application/Worker/tests/test_gc_integration.py`). ARCHITECTURE §5 documents the protocol.
- T-O01: cleanup policies. `waystation_common.cleanup` holds the criteria (`last_downloaded_older_than_days`,
  `published_older_than_days`, `keep_latest_n_versions`, name/version include/exclude regexes, `prerelease_only`,
  docker `untagged_older_than_days`; ANDed), version order per format (semver precedence, dpkg, rpm `rpmvercmp`,
  natural order otherwise), coordinates from asset paths (npm tarballs, apt pool `.deb`, yum `.rpm`, others by path
  per folder) and `plan_repository`, which the server's dry run and the worker's run both call. Docker policies
  select tags and cascade to the manifests only those tags reached and the layers no remaining manifest uses;
  untagged manifests go by age. Only records are removed (assets, docker tags/manifests), never metadata or blobs.
  `waystation_common.db.cleanup` (policies, `job_definitions` sync for schedules, attachment, loading, idempotent
  removal that skips assets changed since planning). Worker job `cleanup.run` (progress and log in `job_runs`,
  one activity event per repository, totals for `/metrics`). REST (contract + MSW + generated client):
  `/cleanup-policies` CRUD (admin; readable with `manage` on any repository), `/cleanup-policies/preview` (paged
  candidates with reasons, per-repository totals, `limit: 0` for counts; capped at 100k assets per repository with
  `truncated`), `/cleanup-policies/{id}/run` and `/runs`, `/repositories/{name}/cleanup` (attach in run order;
  format-checked). New collection `cleanup_policies`; `repositories.cleanup_policy_ids` kept across updates;
  `job_runs` indexes `dedupe_started`, `kind_started`. Downloads are now recorded off the hot path: the proxy engine
  calls `EventEmitter.asset_downloaded` (Valkey `seen:assets`/`dl:assets`) and `stats.rollup` flushes them to
  `assets.last_downloaded_at`/`download_count`. UI: the `/cleanup` page (policy table, editor with criteria builder,
  schedule presets and a live impact panel, paged preview, run history) and a Cleanup tab on the repository page.
  "Preview equals execution" is tested on npm and docker fixtures for five criteria sets.
- T-S08: fix a second upstream fetch under load. The owner's publisher task ran `fdatasync` on the upload file
  (while followers are attached) before renewing its lease; on a busy disk one sync took 3.2 s, longer than the
  test's 2 s lease, so the lease expired, a follower on the other replica claimed the fill and fetched again. The
  sync now runs in the background on the thread pool (one at a time, `synced` rises when it completes), the
  renewal never waits on it, and the upload descriptor is closed only after an in-flight sync has returned (also
  when the fill ends first). Regression test with a stuck
  `fdatasync`. The 200-client test now runs with the production 15 s lease (a Valkey or event-loop stall longer
  than the lease legitimately costs a takeover, ARCHITECTURE §6.3), the owner-gone test with a read timeout that
  cannot end the first fetch before its client goes away, and every wait in both suites is bounded with a message.
- Tooling: `make test-integration` queues on a machine-wide `flock` (`TEST_LOCK`), so parallel worktrees run one
  integration container stack at a time instead of exhausting the dev box's memory.
- T-S10: audit service. Catalog of audited actions (`waystation_common.audit.AuditAction`, mapped to every SPEC §4.11
  phrase), typed records (actor with auth method, provider and API token id; target; outcome and failure reason;
  client address via the trusted-proxy rules; request id; details; field-level before/after changes with secret
  values redacted and URL credentials masked) and a non-blocking batched writer (bounded queue, retries, drops
  counted and logged at ERROR). Recorded: sign-in success and failure (UI; `docker login` with a password at most
  hourly per user and address), lockout, sign-out, API token create/revoke/use-refused, service account changes,
  bootstrap and provisioned users, identity linking and group sync, sign-in provider changes and tests, repository
  create/update/delete/test-upstream. Retention via `WAYSTATION_AUDIT_RETENTION_DAYS` (default 400, MongoDB TTL on
  `expires_at`; new indexes `principal_name_ts`, `target_name_ts`, `target_id_ts`, `expires_at_ttl`, created by
  `init`). `GET /api/v1/audit` (time range, actor, action incl. `<category>.*`, target, outcome; cursor paging) and
  streamed CSV/NDJSON `GET /api/v1/audit/export`, both `audit-read`; capability `audit.read`. Contract, MSW mocks and
  the Audit log page (filters in the URL, detail drawer with the change diff, export). Acceptance test enumerates the
  catalog; actions of features not built yet are listed as pending with their task. The real-directory LDAP tests
  check the sign-in, provisioning, link, group sync and failure-reason (`not_in_group`, `directory_unavailable`)
  records.
- T-U06: global search and the command palette. `GET /api/v1/search` searches the repositories the principal may
  browse (hidden repositories never leak, not even in facet counts): `<name prefix>[ <version prefix>]` matched
  case-insensitively on the full name or its last `/` segment across docker tags, npm packages, apt/yum index
  listings and cached files, or a `sha256:` digest ("where is this blob used?"). Filters by format, repository,
  kind, repository type and cached-only, with disjunctive facet counts, exact keyset cursor paging and a Browse deep
  link per hit. New search keys `name_lc`/`base_lc` (written by the format index writers, backfilled by migration
  `0002_search_keys`) and partial `search_name`/`search_base` indexes; an integration test explains every query the
  search issues. UI: Search page (facet sidebar, format icons, keyboard navigation, URL state, empty states) and the
  `Ctrl/⌘ K` command palette (quick search, recent items, repositories, pages, actions; ARIA combobox/listbox,
  loaded on first use). Contract, generated client and MSW mocks updated.
- T-O06: monitoring. Grafana dashboard `Build/monitoring/grafana/waystation-dashboard.json` (Prometheus data
  source as a variable; job, instance and repository filters; stat tiles for request rate, cache hit ratio, bytes
  served from cache and fetched from upstream, upstream error ratio and transfers in flight; requests by cache state,
  throughput by direction, hit ratio and upstream errors per repository, upstream latency p95, coalescing, and a
  per-repository table). Zabbix 7.0 template `Build/monitoring/zabbix/waystation_by_http.yaml` ("Waystation by
  HTTP": HTTP agent items on `/healthz`, `/readyz` and `/metrics`, dependent Prometheus-pattern items, repository
  discovery with per-repository rates, ratios and average upstream latency; triggers for server down, unreachable,
  MongoDB/Valkey unavailable, no metrics, overall upstream error ratio and per-repository upstream down/degraded).
  Both use only metrics the server exports today; the dashboard was loaded into Grafana 12.2 against Prometheus 3.7
  scraping real `Metrics` output, and the template imported into Zabbix 7.0 with its items and triggers exercised.
  `Documents/Operations/MONITORING.md` lists every exported metric and the useful ones still missing (upstream
  state, Docker Hub rate limit, job queues, worker health, disk space). SPECIFICATION §10 now lists the metrics as
  exported.
- T-M03: deployment and reverse-proxy guides. `Documents/Operations/DEPLOYMENT.md` describes the single-host
  `compose.yaml` as it works today: containers and networks, requirements (Waystation owns the root of its host
  name), sizing, images, host layout and storage, the env file (with a compose override for `*_FILE` secrets), the
  environment reference (application settings from `.env.example` plus compose-only variables), start-up and first
  sign-in, health checks, TLS options, scaling and split-deployment status, manual backup/restore commands, upgrades
  and troubleshooting; planned pieces are marked with their task IDs. `Documents/Operations/REVERSE_PROXIES.md`:
  requirements for any proxy (no body limit, no request/response buffering, unbuffered event stream, long idle
  timeouts, keep-alive), `X-Forwarded-For` trust, and example configurations for nginx, Traefik v3 (static config,
  file provider, Docker labels in the new `Build/Containers/examples/traefik.override.example.yaml`), Caddy and
  HAProxy under `Build/Containers/examples/reverse-proxies/`, plus Synology DSM notes. `make proxies-check` runs each
  example in containers (`nginx -t`, `caddy validate`, `haproxy -c`, Traefik start-up, `docker compose config`), then
  against a stand-in web tier over TLS (redirect, unbuffered SSE, 1 GiB download and upload, forwarded headers;
  `SLOW=1` adds a 75 s idle response and an 80 s upload) and checks the doc blocks equal the files. `compose.yaml`
  now passes `WAYSTATION_INSTANCE_NAME`, `WAYSTATION_BOOTSTRAP_ADMIN_PASSWORD[_FILE]`, the session lifetimes and the
  sign-in limits through to the containers (they were silently ignored before).
- T-U07: Activity and Storage pages. Activity (`/activity`): the live event log, following the `activity` SSE topic
  (applied in 4 Hz batches; holds new events while paused or scrolled down, with an "N new events" pill), filters for
  event kind, repository, format, user or address, cache state, outcome and time (presets or a custom window), all in
  the URL; a virtualised list that pages back through history; a detail drawer (every field, copy path/id, narrow the
  log to this repository/user/kind or to the 10 minutes around the event); CSV export of the current filter. Server:
  `GET /activity` gains `format`, `principal`, `cache`, `outcome`, `since`, `until` (time windows are stream-id
  ranges) and `ActivityEvent.path`; new `GET /activity/export` streams CSV (≤ 100,000 rows, formula-looking cells
  prefixed `'`, `stats-read`, visible repositories only). Storage (`/storage`): stored/free/de-duplication/blob KPIs
  (stored bytes live from stats ticks), data volume bar, storage by format, largest repositories (new `BarList`
  chart), a sortable, filterable repository table (sort and filter in the URL), and a placeholder for growth until
  storage snapshots exist. Contract, generated client and MSW mocks updated (mock activity: stream-id cursors, the
  same filters, a day of seeded history, CSV export).
- T-A01: LDAP sign-in provider. `security.ldap` (ldap3 in a dedicated thread pool, connect/response timeouts and an
  overall ceiling): ordered server list with failover (unreachable or TLS-failing servers skipped for 30 s), LDAPS,
  StartTLS or clear text, custom CA with host-name verification by Python `ssl`, service bind with a write-only
  encrypted password, user search (base + filter, escaped username), user bind on its own connection (empty passwords
  refused), attribute mapping, groups by `memberOf` or group search with optional nested walk, require-group gate,
  and a bind cache (HMAC of provider, config version, username, password; per-process key) used by `docker login`.
  `security.login.verify_login` now takes the services and an optional provider: local accounts with a password are
  only checked locally; everyone else goes to the enabled LDAP providers in order. Admin API `/api/v1/auth-providers`
  (CRUD with 422 field errors and 409 on a taken name, test-connection per server, test-user with groups, gate and
  mapped roles; `settings-admin`) and `GET /api/v1/roles`; `/auth/providers` lists enabled providers. New collection
  `auth_providers`. UI: "Authentication providers" admin page (FreeIPA / AD / OpenLDAP presets, connection, users,
  groups, caching sections, test buttons, group-mapping table) with MSW mocks, and a provider select on the sign-in
  page. `make test-integration` now also builds and starts an OpenLDAP server with a FreeIPA-style tree and a Samba
  AD DC with a throwaway CA (`Build/Containers/test/`); integration tests cover TLS modes, wrong CA/host name,
  failover, wrong password, locked/disabled accounts, require-group, nested groups, group sync, the same-username
  rules, the docker token service with an LDAP password and the admin API. New dependencies: `ldap3`, `types-ldap3`.
- T-A03 (LDAP half): provider-neutral identity linking and group sync (`security.external.link_identity`):
  provisioning, same-username rules (never link to an account with a local password; password-less local accounts
  and other providers' users are linked; `users.identities[]`), `users.external_groups[]` replaced per provider at each
  sign-in. T-S03 groups: `groups` documents hold roles; a provider's group mapping is stored as groups; effective roles
  (own ∪ local groups ∪ mapped external groups) resolved per request by `security.groups.GroupDirectory`, also for API tokens of users.
- T-P2-YUM (proxy min): yum/dnf proxy repositories (`formats/yum`: descriptor, `repodata.py` parsers, plugin;
  `db/yum.py`; registered in `plugins.py`). The upstream layout is kept, so one repository serves every `repodata/`
  directory below its upstream URL. `repomd.xml` is metadata, passed through untouched after a defused parse (expat
  with no DTD, entity or notation declarations; bounded sizes, entries and text) and refused with 502 when invalid;
  a newly stored `repomd.xml` drops the cached `repomd.xml.asc`. Files `repomd.xml` lists are served only by its
  SHA-1/256/512 checksum and size (by-checksum names cached forever, plain names keyed by checksum), with one
  `repomd.xml` revalidation on a mismatch, then 502; unlisted by-checksum names are self-validating, other unlisted
  files 404. Cached `primary` files are decompressed in bounded chunks and parsed in a thread into the new
  `yum_packages` collection; packages are validated against their entry (a package requested before its `primary`
  makes Waystation fetch and index the enclosing directory's `primary` first), else served `unverified` and dropped
  when a later listing disagrees. Options `repo_path`/`gpg_key` (`YumOptions` in the contract and generated client;
  the repository form keeps them on save); "Set me up" `.repo` files for Rocky Linux, AlmaLinux, Oracle Linux, EPEL,
  Fedora and generic upstreams. Mirrorlist/metalink upstreams are not proxied. Browse uses the generic folder tree
  (the UI's "coming" card for yum is gone). Real-client harness `tests/clients/yum/`: dnf on Rocky Linux 10 and
  AlmaLinux 10 against their `extras` repositories with `repo_gpgcheck=1` (makecache, install, cache hit + `rpm -K`,
  reinstall from an empty dnf cache, browse API), 52 KB of fixtures. `Documents/Operations/clients/yum.md`.
  `linux.duke.edu` (the repodata XML namespace) added to the host allowlist.
- T-U02: brand. An original mark (a waypoint marker with a hexagonal head holding a stack: slab and two layers) and a
  monoline geometric "waystation" wordmark, both defined once in `Application/Web/src/assets/brand/geometry.ts`.
  `BrandMark` draws from it (theme colours, `mono` variant with the glyph cut out, a 16 px-tuned glyph below 20 px,
  optional accessible label). `make brand` (`scripts/gen-brand.mjs`, `@resvg/resvg-js` dev dependency, runs in the dev
  image) writes the master SVGs (mark light/dark/mono/small, wordmark, lockups, app tile) and the favicon set in
  `public/` (theme-following `favicon.svg`, PNG 16/32/48, `favicon.ico`, `apple-touch-icon.png`, `icon-192/512.png`,
  `icon-maskable-512.png`, `manifest.webmanifest`), wired into `index.html` with `theme-color`; `make brand-check` runs
  in `make frontend-check`. Empty-state illustrations (`EmptyIllustration.vue`: repositories, cache, search,
  activity, error, notFound, soon), painted only through theme variables, replace the icon tiles in every
  `EmptyState` use; the repository page distinguishes a load failure (error illustration, retry) from a missing
  repository. `Application/Web/branding/` is gone (UI_DESIGN §8 now names `src/assets/brand/`).

## 2026-11-08
- T-S04: API tokens and service accounts. `security.tokens.TokenService`: `wst_` tokens shown once, stored as
  HMAC-SHA256 under a `SECRET_KEYS`-derived key (all keys tried, so key rotation keeps tokens), per-process cache
  ≤ 5 s, revocation announced on the Valkey `revocations` channel (every replica drops the token at once; tested
  across two app instances < 1 s), rejected tokens rate-limited per client address (`WAYSTATION_TOKEN_FAILURE_LIMIT`).
  Scopes (`read`, `write`, `delete`, `manage`, `stats-read`, `metrics`, `admin`) and repository patterns only narrow
  the owner's RBAC, applied inside `access.check`; a token can only get scopes its owner holds. Authentication:
  `Bearer`/`Token` API tokens and basic auth with the token as the password on `/repository/*` and the API; invalid
  credentials are refused instead of falling back to anonymous; `docker login` with a token (the registry token
  carries `tid`, re-checked on every `/v2/` request); `/metrics` with a `metrics` token. Last used recorded in
  Valkey (`seen:tokens`) and flushed in bulk by the `stats.rollup` job together with session last-seen
  (`waystation_common.db.seen`). New collection `service_accounts`. REST: `/auth/me/tokens` (list, create with an
  interactive session only, revoke), `/tokens` (all, revoke), `/service-accounts` (CRUD, tokens) in the server
  OpenAPI, the M0 contract, generated client and MSW mocks. UI: profile page with an "API tokens" card (create
  dialog with scopes, repositories and expiry; copy-once secret dialog; list with status and last used; revoke).
  Real-client harness steps for raw (curl basic and bearer, narrowed, revoked), npm (`.npmrc` `_authToken`), apt
  (`auth.conf`) and docker (`docker login`); client docs' authentication sections updated.
- T-F08: job system on Valkey Streams (ARCHITECTURE §10). `waystation_common.jobs`: typed payloads (`JobPayload`
  with `kind`, `queue`, `max_attempts`, `dedupe_key()`, `routine`; `JOB_TYPES` registry), `JobEnvelope`, the queue
  state machine as atomic Lua scripts (`JobQueue`: enqueue with dirty-flag dedupe, start, finish/retry/dead, heartbeat,
  reclaim via XAUTOCLAIM, delayed set, epoch), cron/`@every` schedules, and `JobClient` for the server
  (`Services.jobs`: `enqueue_durable` into `job_outbox` inside the caller's transaction, `enqueue` straight to Valkey).
  `waystation_common.db.jobs` for `job_outbox`, `job_runs`, `job_definitions`, `workers` (new collections and indexes
  in the registry); `waystation_common.leases` (shared lease helpers). Worker: `JobSystem` = runner (concurrency,
  heartbeats, retries with backoff, dead letters, `job_runs` with progress/log/result, `job.*` activity events),
  coordinator under `lease:scheduler` (outbox dispatcher with re-dispatch when Valkey lost its data, scheduler with
  compare-and-set fires, delayed promotion, consumer pruning), presence in `workers`, liveness watchdog for the
  healthcheck; graceful stop lets jobs finish for 8 s. The stats rollup now runs as the scheduled job `stats.rollup`
  (`@every 10s`, queue `stats`), still under `lease:rollup`, so a minute is written exactly once. Integration tests
  cover a killed worker (exactly one re-run), a flushed Valkey (outbox re-dispatched), uploads during a running
  rebuild (exactly one rerun that sees them all), one fire per schedule with three workers, retries and dead letters.
- T-F03: CI. `.github/workflows/ci.yml` runs `make check`, the stale-generated-client check (`make gen-api` +
  `git diff --exit-code`), the budget report (job summary) and, in a parallel job, `make clients-test FORMAT=all
  MODE=replay`; the dev image is built once per job with the GitHub Actions layer cache (`SKIP_DEV_IMAGE_BUILD=1`
  stops make rebuilding it). `release.yml` builds server/worker/web for amd64 and arm64 on `v*` tags (checked against
  `VERSION`) and pushes them to GHCR with SBOM, `mode=max` provenance and a GitHub artifact attestation, using only
  `GITHUB_TOKEN`; the web image now builds its SPA stage on the build platform. `.gitea/workflows/images.yml` pushes
  images to the Gitea registry on every push to `main` (`vars.REGISTRY_HOST`, `secrets.REGISTRY_USER`/
  `REGISTRY_TOKEN`). Actions pinned by commit SHA; `make workflow-lint` (actionlint + shellcheck). Repository-wide
  forbidden-strings check `make forbidden-strings` (`Build/ci/forbidden_strings.sh`): the `clients-check` scanner now
  also scans every tracked/untracked file, with the base allowlist moved to `Build/ci/allowed_hosts.txt` (adds `url`
  and `not-a-host` entries), CIDR definitions of the special ranges accepted, lockfiles host-checked on download
  lines only, SVG path data ignored, and a `# forbidden-strings: allow` line marker used by the SSRF and client-IP
  tests. `Build/ci/budgets.json` (SPEC §5.1) + `make budgets` report; the SPA bundle budget reads its limit there and
  records the measured size. Both `forbidden-strings`, `workflow-lint` and `budgets` are part of `make check`.
  `Documents/Operations/CI.md`.
- T-S08: request coalescing across replicas (ARCHITECTURE §6.3). New `waystation_server.proxy.coalesce`: one
  Valkey hash per asset (`fill:<repo_id>:<sha1(key)>`) claimed by a Lua script (owner or counted follower in one
  round trip), lease renewed by a background publisher (`WAYSTATION_COALESCE_LEASE_S`, default 15 s) that also
  publishes progress (every 20 ms while followed, 250 ms otherwise) and `fdatasync`s for a `synced` offset while
  followers are attached; one shared poller per key and process for followers of other replicas, in-process
  followers read the owner's lease directly. Engine: `serve`/`ensure` claim before contacting the upstream for a
  missing or stale asset; followers of a streaming content fill tail the upload file (`COALESCED`, last piece held
  back until the owner committed), others wait and are served by X-Accel (metadata, `HEAD`, `COALESCE_TAIL=wait`,
  other host or network filesystem from `/proc/mounts`, host id = kernel boot id); failures and mismatches fail
  followers with the same error (stale-on-error per follower); an aborted owner (client gone) or a dead one (lease
  expiry) is replaced by a follower, which resumes after checking the bytes it already sent are a prefix of the new
  fill; Valkey down disables coalescing only. The tee moved into `_tee` (unchanged behaviour for a single client).
  Live transfers carry `followers` (`Transfer.followers`, `EventEmitter.transfer_followers`), new metric
  `waystation_coalesce{repo,event}`; `BlobWriter.flushed`/`fileno()`. No format plugin changes. Tests: two uvicorn
  replicas with 200 clients for one 64 MiB blob (1 upstream fetch, identical bytes; `WAYSTATION_TEST_COALESCE_MIB`),
  owner replica SIGKILLed mid-transfer, and in-process two-instance tests (mismatch, takeover, metadata, 404, wait
  mode, Valkey down). The fake upstream can pause mid-body (`gate`/`pause_at`) or stall (`hang_after`).
- T-S09 (M0 slice): browse API to the M0 contract (`api/browse.py`, contract test extended):
  `GET /api/v1/repositories/{name}/browse` (generic folder tree of cached assets, every format: folders first, keyset
  cursors, one index seek per sub-folder, 404 for a missing folder), npm `packages`/`versions`, apt
  `suites`/`packages`, docker `images`/`tags`/`manifest`; `browse` permission, hidden repositories 404, format views
  404 on other formats. Format-neutral `waystation_server.browse` package (response models, `BrowseContext`, the
  `NpmBrowser`/`AptBrowser`/`DockerBrowser` protocols a plugin exposes as `browser`, cursors). Background indexing
  after cache fills (`formats/common/background.py`): `npm_packages` (packument summaries), `apt_packages` (each
  cached `Packages` stanza; `deb822` now also reads Package/Version/Architecture/Section and the descriptive
  `Release` fields), `docker_tags`/`docker_manifests` (every cached manifest; `repo_image_updated` index). Repository
  delete also drops these records and `apt_pool_index`. Integration tests populate repositories through a fake
  upstream and explain every browse query (no collection scans); the raw, npm, apt and docker harness scripts check
  the browse API after their installs and pulls. The UI's browse renderers now work against the real server.
- T-P2-DOCKERP (M0 slice): Docker / OCI proxy plugin (`formats/docker/plugin.py`, `protocol.py`, `upstream_auth.py`,
  registered in `plugins.py`). Pull side of the registry API at `/v2/<repo|alias|default>/...` and
  `/repository/<repo>/v2/...`: tag manifests as metadata (one asset per Accept variant, checked against the upstream
  `Docker-Content-Digest`, never converted), stale tags re-checked with an upstream `HEAD` first; digest manifests and
  blobs as content verified against their digest (blobs keyed by digest, CDN redirects followed without
  Authorization); tags/list with paging; `HEAD` misses answered by an upstream `HEAD`; OCI error codes. Upstream auth
  pings `/v2/` once per repository and process to learn the challenge, fetches anonymous or credentialed pull tokens
  (Docker Hub realm built in) and caches them per scope; Docker Hub one-segment names get `library/` and
  `ratelimit-*` headers are recorded for upstream health. Engine: `AssetSpec.revalidate_head` (keep a stale asset
  that an upstream `HEAD` confirms unchanged) and `AssetSpec.on_upstream` (see each upstream response's headers).
  Real-client harness format `docker` (dockerd mirror mode, prefix and alias pulls from Docker Hub and GHCR; skopeo
  inspect, list-tags and copy) with 2.6 MB of fixtures; the `client-docker` service is now `docker:29-dind` plus curl
  and skopeo with its entrypoint cleared. Client guide `Documents/Operations/clients/docker.md`.
- T-P2-APT (M0 slice): apt proxy plugin (`formats/apt/plugin.py`, bounded `Release`/`Packages` parsers and
  decompression in `formats/apt/deb822.py`, `apt_pool_index` access in `db/apt.py`, registered in `plugins.py`):
  `InRelease`/`Release`/`Release.gpg` passed through untouched as metadata; index files served only by the SHA256 the
  cached `Release` lists (stored under their `by-hash` key, fetched by-hash when offered, one `Release` revalidation
  on mismatch, then 502; unlisted indexes 404); `by-hash/SHA256|SHA512` immutable and self-validating; cached
  `Packages` files indexed in the background into `apt_pool_index`, pool files validated against their listing or
  served `unverified` and dropped when a later listing disagrees. New `apt_pool_index` index `repo_id,verified`. "Set
  me up" snippets use the Ubuntu keyring for Ubuntu upstreams and no `-updates` suffix for `-security` suites.
  Real-client harness format `apt` (Debian 13 + Ubuntu 26.04, four repositories, `*-updates`/`*-security` suites,
  2.3 MB of fixtures, 32 MiB budget per the owner's fixture decision); `CLIENT` in `format.env` may list several
  clients (run in turn, `HARNESS_CLIENT` set); `lib.sh`'s `wait_for`/`ws_login`/`ws_api` work without curl (bash
  `/dev/tcp`). Client guide `Documents/Operations/clients/apt.md`.
- T-P2-NPM (M0 slice): npm proxy plugin. Packuments full and abbreviated (`application/vnd.npm.install-v1+json`) cached
  separately as metadata and revalidated, scoped names as `@scope%2fname` or `@scope/name`, every
  `versions.*.dist.tarball` rewritten to `<base>/repository/<repo>/` in a thread; tarballs cached as content and
  verified while streaming against `dist.integrity` (SRI) or `dist.shasum`, looked up in the cached abbreviated
  packument (fetched on a miss, refreshed once for an unlisted version, otherwise 404). `/-/ping` answered locally,
  `/-/v1/search` cached 5 minutes per query, audit POSTs relayed uncached when `audit_passthrough` is on. Engine:
  `AssetSpec.expect` (expected digests resolved only on an upstream fetch) and `ProxyEngine.relay` (bounded, uncached
  pass-through); `FormatDescriptor.read_posts` lets a format declare query-only POST paths that the router authorises as
  `read`. Real-client harness: `tests/clients/npm/` (npm 11, lockfile-pinned and lockfile-free installs incl. a scoped
  package, `npm audit`) with recorded registry.npmjs.org fixtures; `setup.sh` scripts can create repositories as a
  random per-run bootstrap admin; new `DROP_JSON_KEYS` format setting removes personal and registry-internal fields
  (readmes, maintainer e-mails, `_npmOperationalInternal`) from recorded JSON. Client guide
  `Documents/Operations/clients/npm.md`.
- T-P2-RAW (M0 slice): raw proxy plugin (`formats/raw/plugin.py`, registered in `plugins.py`): `GET`/`HEAD
  /repository/<repo>/<path>` cached by path; `metadata_path_regex` (searched in `/<path>`) selects revalidated
  metadata, everything else is immutable content; Content-Type by extension from Python's built-in table, else the
  sanitised upstream type; `Content-Disposition` (`attachment` default) and `X-Content-Type-Options: nosniff` on every
  answer, and `inline` never renders HTML/SVG/XML; upstream paths percent-escaped (`:` too); upstream 404/410 →
  native 404 (negative cache), other methods 405, directories 404 until listings land. Real-client harness format
  `raw` (curl + wget, fixtures from `deb.debian.org`); the harness now creates a per-run bootstrap admin
  (`HARNESS_ADMIN_PASSWORD`), `lib.sh` gains `ws_login`/`ws_api` for `setup.sh` scripts, and `make clients-check`
  scans a format's own files (`tests/clients/<format>/`) with its `allowed_hosts.txt`. import-linter: the
  engine's typing-only import of `formats.base` is ignored in the "formats never import each other" contract.
  Client guide `Documents/Operations/clients/raw.md`.
- T-V01: npm `overrides` pin `minimatch` ≥ 11.0.1 and `glob` ≥ 14.0.2 (dev-only transitive dependencies of eslint,
  `@vue/test-utils` and `openapi-typescript`) for two moderate advisories; `npm audit` clean.
- T-U03: mock data mirrors the owner's repository set: 13 proxies (`dockerhub-proxy` default/mirror + `docker.io`
  alias with the Hub rate-limit gauge, `ghcr-proxy` with the `ghcr.io` alias, apt `debian`, `debian-security`,
  `ubuntu` (resolute), `ubuntu-security`, `raspberrypi` (arm64/armhf), yum `rocky`, `almalinux`, `epel` (auto-blocked
  with a rolling retry countdown), `oracle` (degraded: slow, intermittent 5xx, stale repomd.xml), `npm` and one example
  raw repo `raw-releases`), public canonical upstreams only, per-repo freshness (docker metadata 6 h / negative 60 min,
  apt and yum 60 min with security pockets at 30 min, npm 30 min, content 24 h outside docker). The live generator adds
  dnf makecache/install runs and per-suite apt updates; storage-by-format and the traffic split include yum. yum
  displays everywhere (Rocky/Alma/Fedora marks by upstream, list and live format filters, settings edit without
  format options), gets a dnf **Set me up** (`/etc/yum.repos.d/<repo>.repo`, `gpgcheck=1`) and a "coming with
  T-P2-YUM" browse state; creating yum repos stays unavailable. Docker Set me up leads with the upstream-name pull for
  aliased non-default repos (`ghcr.io/...`). The settings form now keeps `content_max_age_s` instead of resetting it.
  Screenshot tour regenerated (new shots: `repo-ghcr-proxy-setup`, `repo-rocky-setup`, `browse-yum`).
- T-S12 (M0 slice): Docker token service `GET /auth/token` (`security.registry_token`, `registry_auth`): 5-minute
  EdDSA JWTs signed with an Ed25519 key kept encrypted in `keys/registry-token.key`; Docker scope grammar; grants are
  what RBAC allows on the repository each name resolves to (prefix, alias, default repo); anonymous tokens and
  `docker login` with a local password. `/v2/` now requires a registry token and otherwise answers the Bearer
  challenge (`realm`, `service`, `scope`, `error="insufficient_scope"`); token principals are limited to token
  grants ∩ current RBAC (`access.check`). Verified with a real `docker login` against the dev stack.
- T-S11 (M0 slice): stats and live APIs per the M0 contract: `GET /api/v1/stats/summary`, `/stats/traffic`,
  `/stats/storage`, `/stats/upstreams`, `/repositories/{name}/stats/traffic`, `/transfers`, `/activity`, and the
  repository list's 24 h `stats` and `degraded` status. Series merge rolled-up minutes with the live Valkey counters
  (a minute present in both counts once). `GET /api/v1/events/stream`: one hub per process (pub/sub `live`, blocking
  `XREAD` on `activity`, 1 Hz `stats.tick`), per-subscriber topic and RBAC filtering, bounded queues,
  `transfers.snapshot` on connect, `Last-Event-ID` resume without gaps or duplicates, 15 s heartbeats. OpenAPI now
  documents the SSE payloads (`LiveEvent`); the contract test covers every M0 operation the server implements.
- T-S07 (M0 slice): proxy engine `waystation_server.proxy.engine`. Formats describe files as `AssetSpec` (kind,
  upstream path/URL/headers, `Expected` digests incl. npm SRI, stored/response headers, size and age limits,
  `validate`/`transform` hooks run in a thread, `on_stored`, pluggable `UpstreamAuthenticator`) and call
  `serve`/`ensure`/`read`. Hits go out by X-Accel; content misses are teed to the client and the blob store with the
  last chunk held until verified and committed; metadata is fetched bounded, verified, transformed and committed.
  Conditional revalidation (`REVALIDATED`), stale-on-error (`STALE`), offline mode and a Valkey negative cache.
  Redirects followed manually with the SSRF guard on every hop and credentials only for the upstream origin.
  Records `assets`/`blobs`, live transfers (new `Transfer` fields `artifact`, `component`, `client_agent`, `error`),
  activity (`cache.hit`, transfers), per-minute counters (new `upstream_requests`, `upstream_errors`), the upstream
  health hash and Prometheus metrics. Fault-injection integration tests (timeouts, slow body, reset, 5xx, digest
  and size mismatch, validation failure, SSRF on redirect, credential stripping) and a dev-stack run against a
  public Debian mirror.
- T-S06: data plane `waystation_server.dataplane.xaccel.serve_blob`: `If-None-Match`/`If-Modified-Since` → 304 by
  the server, HEAD answered by the server, GET → empty response with `X-Accel-Redirect` to `/_blobs/...`, digest
  ETag and `X-Ws-*` headers, format headers replayed. nginx `/_blobs/` also re-adds `Docker-Distribution-API-Version`,
  `X-Ws-Cache` and `X-Request-Id`. Verified through the dev stack's nginx (exact bytes, 206 ranges, 304, HEAD).
- T-S05 (M0 slice): repositories. Format descriptors with option models and "Set me up" snippets for docker
  (aliases, Docker Hub, default repo), npm, apt (distribution, flat) and raw (metadata regex) proxies, registered by
  `waystation_server.plugins`. `repos.service` validates names (reserved words, docker aliases, case-insensitive
  uniqueness → 409), formats/types, upstream URLs and options (422 field errors), encrypts upstream secrets
  (write-only, kept on update) and keeps one default docker repository. REST: list (filters, cursor paging,
  RBAC-filtered), create, read, replace, delete, test upstream, setup guide, per the M0 contract; OpenAPI
  `FormatOptions` is `oneOf` the registered option models. Upstream HTTP clients (`proxy.http`: HTTP/2, pooled per
  host, no automatic redirects) with the SSRF guard enforced at connect time (`proxy.ssrf`). Contract test compares
  the generated OpenAPI with `Application/Web/openapi/waystation-m0.yaml`. DATA_MODEL `repositories.proxy` fields
  aligned with the API.
- T-S03 (M0 slice): RBAC decision point `waystation_server.security.access`: `check`, `can_see`, `filter_repos`,
  `require`, `require_repo`, `capabilities`; built-in roles (`admin`, `reader`, `deployer`, `viewer`,
  `anonymous` = browse + read on proxy repositories); scopes `*`, name, glob, `format:`, `type:` (new, SPEC §4.10).
  Hidden repositories answer 404; visible-but-denied answers 401 (anonymous) or 403. Every route declares its rule
  with a decorator, applied by the app-wide `enforce` dependency (fails closed; CSRF for session requests on unsafe
  methods); a test enumerates all routes and fails on a missing rule.
- T-S02 (M0 slice): local authentication. argon2id passwords (hashing in a thread; unknown users cost the same),
  `init` creates the first `admin` (configured or one-time password printed once, idempotent), sessions
  (`ws_session` cookie, sha256-hashed ids, fixation-safe, idle and absolute timeouts, last-seen kept in Valkey),
  double-submit CSRF, per-address login rate limit (429 + `Retry-After`) and account lockout. REST:
  `GET /api/v1/config`, `GET /auth/providers`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`,
  `PUT /auth/me/preferences` (contract shapes). New settings: `INSTANCE_NAME`, `SESSION_IDLE_MINUTES`,
  `SESSION_MAX_HOURS`, `LOGIN_RATE_LIMIT`, `LOGIN_LOCKOUT_FAILURES`, `LOGIN_LOCKOUT_MINUTES`. The integration-test
  MongoDB gets a higher open-files limit (every test creates a database).
- T-S01: server app factory. `Services` (MongoDB, Valkey, event emitter, blob store, keyring, repository catalog,
  format registry, metrics) created and started in the lifespan; start-up refuses an unknown or unmigrated schema.
  Routing for `/repository/<repo>/<path>` and `/v2/<name>/...` (SPEC §4.4 name resolution) hands requests to format
  plugins (`formats.base.FormatPlugin`, `formats.registry`); unknown repositories get the native 404. Pure ASGI
  middleware for request id, client address (`X-Real-IP` from the web tier, `X-Forwarded-For` only through trusted
  proxies) and the access log; `HEAD` for API routes. Errors: problem+json for the API, OCI JSON under `/v2/`, plain
  text under `/repository/`. `/readyz` checks MongoDB and Valkey; `/metrics` serves Prometheus metrics to
  `WAYSTATION_METRICS_ALLOW_CIDRS` only (SPEC §4.4 default corrected to loopback). New `repositories.name_ci_unique`
  index (case-insensitive uniqueness, SPEC §3). Dependencies: `prometheus-client`; dev: `pyyaml`.
- T-P2-HARNESS (M0 slice): real-client harness in `tests/clients/`. `make clients-test FORMAT=<f|all>
  MODE=replay|record|live` (default replay) runs each format as its own compose project (own container names, no
  published port) on top of `compose.yaml` + `compose.dev.yaml`. A stdlib fake upstream impersonates the public
  upstream hosts (`extra_hosts` + per-run throwaway CA trusted via `WAYSTATION_EXTRA_CA_FILE`), replays fixtures
  from `tests/fixtures/upstreams/<format>/` and fails the run on any miss; record mode scrubs (response header
  allowlist, signed-URL query values, JSON credentials; the auth *scheme* only is keyed) and installs fixtures only
  after the client passed and `make clients-check` is clean. `make clients-check` (now part of `make check`) rejects
  non-allowlisted hosts, internal suffixes, non-documentation IPs, private keys/tokens, local deny-list strings and
  oversize fixtures. Pinned client services: curl, node, debian, ubuntu, skopeo, docker (dind). `echo` self-test
  format with a test-only origin passes in record, replay and live mode (also with the stack up). Nightly
  `clients-live.yml` workflow stub. Harness Python is linted, type-checked and unit-tested with the workspace.
- T-F09 (M0 slice): `waystation_common.events`: SPEC §4.7 cache states, `Transfer`/`ActivityEvent`/`LiveMessage`
  models, Valkey key helpers and fixed log-scale latency buckets with query-time percentiles, and a non-blocking
  `EventEmitter` (synchronous O(1) producer calls, bounded queue, one pipeline per 250 ms flush, progress coalesced to
  ≤ 4 Hz per transfer, Valkey outages drop stats and never affect serving). Worker `rollup` loop (leader lease)
  writes closed minutes (+5 s grace) to `stats_minute` exactly once and sweeps stale live transfers. DATA_MODEL:
  documented `stats:pending`, counter fields and bucket names. Usage is in the `waystation_common.events` docstring.
- T-F07 (M0 slice): `waystation_common.storage.BlobStore` per ARCHITECTURE §5: streaming `BlobWriter` (incremental
  sha256, writes and hashing in a worker thread in 1 MiB chunks, size limit, expected size/digest), `commit`
  (fsync, atomic rename into `blobs/sha256/aa/bb/<hex>`, fsync dirs; 0640 files, 2750 dirs), `exists`, `stat`,
  `path_for_xaccel`, `delete`, `sweep_uploads`, free-space guard. Tests cover kill -9 mid-write, concurrent
  identical commits (placed once) and event-loop responsiveness during a 64 MiB write. NFS CI scenario pending.
- T-F06 (M0 slice): MongoDB layer in `waystation_common.db`: `create_client` (PyMongo `AsyncMongoClient`,
  tz-aware), collection registry with named indexes for the M0 collections (new: `apt_pool_index` for apt pool
  validation, added to DATA_MODEL), `ensure_schema` (creates only what is missing, reports index conflicts),
  forward-only migrations with `check_schema` for newer/pending schemas, replica-set initiation. `init` now runs data
  dir → replica set → schema → migrations; a re-run changes nothing. `make test-integration` starts throwaway
  MongoDB/Valkey containers (project name unique per checkout) and runs `pytest -m integration`; it is part of
  `make check`.
- T-F05 (M0 slice): containers and dev stack. Server/worker images (uv, non-editable venv, UID/GID 10001), web image
  (nginx-unprivileged, SPA build stage with placeholder fallback, `nginx.conf.template`: `/` → `/ui/`, SPA with
  immutable assets, unbuffered proxy of the dynamic paths with no body limit, SSE location, internal `/_blobs/`).
  `compose.yaml` (hardening anchor, healthchecks, `service_healthy` ordering, frontend/backend networks, only web
  publishes a port), `compose.dev.yaml` (hot reload), `make dev-up/dev-down/dev-clean/dev-logs/dev-ps/images`.
  Minimal `waystation_server.app:create_app()` and worker main with a heartbeat-file healthcheck; `init` prepares
  the data dir (ownership/modes, then drops root) and initiates the replica set. Third-party loggers (pymongo,
  httpx...) are clamped to WARNING because their debug output includes commands and URLs.
  Doc fix: ARCHITECTURE §4.5 no longer says `default_type` comes from `$upstream_http_x_ws_content_type` (nginx
  cannot do that); the server's own `Content-Type` survives the internal redirect, verified with a real probe.
- T-F04: `waystation_common` core: settings (pydantic-settings, `WAYSTATION_` prefix, `<NAME>_FILE` for every secret
  including the Mongo/Valkey URIs, `make env-example` regenerates `.env.example` and a test fails when it is stale),
  JSON logging with contextvars request context and secret redaction, ULIDs, error types, `paths.normalise`
  (hypothesis property tests), AES-256-GCM keyring (first key encrypts, all decrypt, `rotate`). `make test` now
  excludes `integration` tests and enforces 100% branch coverage on `paths` and `crypto`.
- T-U05 (min): browse renderers (docker images → tags → manifest with platforms and layers, npm packages →
  versions, apt suites → packages, raw folder listing), Browse page, and the M0 screenshot tour
  (`scripts/screenshots.mjs`, PNGs in `Documents/Notes/screenshots/m0/`).
- T-U04 (min): repositories. List with URL-backed filters, create flow (format tiles → type → form with upstream
  presets and "Test upstream"), problem+json field errors on the form, detail page with KPI strip and Overview /
  Browse / Settings tabs, delete with typed confirmation, Set me up side panel (docker, containerd, podman, npm,
  pnpm, yarn, bun, apt, curl, wget). Secrets stay write-only in the form.
- T-U03 (min): dashboard. KPI tiles, the live transfers panel (also in the top-bar live drawer and per repository),
  traffic chart kept current from stats ticks without polling, storage-by-format donut, upstream health with the
  Docker Hub pull gauge and auto-block countdown, recent activity.
- T-U01 (min): design tokens (brand teal `#0f766e` family, status and cache colours, chart slots validated for
  colour-blind separation in both themes), format icon sprite (`src/assets/formats.svg`, regenerated by
  `scripts/gen-format-sprite.mjs`), base components (`PageCard`, `PageHeader`, `FormDialog`, `StatusPill`,
  `CacheBadge`, `RepoChip`, `FormatIcon`, `CopyField`, `EmptyState`, `TimeCell`, `SizeCell`), SVG charts
  (`Sparkline`, `StackedAreaChart`, `DonutChart`), formatters (binary sizes, SI rates, relative times).
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
