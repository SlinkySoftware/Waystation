# Waystation administration guide

Day-2 operation of a running Waystation in one place: who can sign in and what they may do, repositories and their
storage, background tasks, notifications, monitoring, backups, upgrades and troubleshooting. Each section says what
to do and links to the detailed reference instead of repeating it. Installing Waystation is covered in
[DEPLOYMENT.md](DEPLOYMENT.md); the product's full behaviour is specified in
[SPECIFICATION.md](../SPECIFICATION.md).

Examples use `https://registry.example.com` as the base URL. Everything below can be done in the web UI
(`/ui/`) or through the REST API ([../API/README.md](../API/README.md)); most of it can also be declared in a
provisioning file ([PROVISIONING.md](PROVISIONING.md)).

## 1. Administrator access

- **First administrator**: `init` creates the local user `admin` on the first start, with
  `WAYSTATION_BOOTSTRAP_ADMIN_PASSWORD(_FILE)` or a one-time password printed once in `docker logs waystation-init`
  (DEPLOYMENT.md section 6). A one-time password must be changed at the first sign-in.
- **Two-factor sign-in**: every local account can turn on TOTP on its profile page (authenticator app plus ten
  single-use recovery codes). A setting (Settings page, "require two-factor sign-in for local administrators") forces it for every
  local account holding `admin`: until they set it up, their sessions may only reach their profile.
- **Lost second factor**: a user holding `user-admin` resets another user's TOTP from the user's actions on
  Administration → Users. Nobody can reset their own, or an account that holds more than they do.
- **Locked out**: after `WAYSTATION_LOGIN_LOCKOUT_FAILURES` wrong passwords a local account is locked for
  `WAYSTATION_LOGIN_LOCKOUT_MINUTES`; Administration → Users → Unlock ends it early. The last enabled administrator
  can never be disabled, deleted or demoted.

## 2. Users, groups, roles and access

Permissions are `(action, scope)` pairs (SPECIFICATION §4.10). Actions: `browse`, `read`, `write`, `delete`,
`manage` on repositories, plus the global `admin`, `user-admin`, `audit-read`, `stats-read`, `settings-admin`.
Scopes: `*`, a repository name, a glob (`docker-*`), a format (`format:npm`) or a type (`type:proxy`).

| Task | Where |
|---|---|
| Create local users, reset passwords, disable, unlock, sign a user out everywhere | Administration → Users |
| See exactly what someone may do (roles and where they come from, per-repository actions) | Administration → Users → the user's page (effective permissions) |
| Bundle users into local groups and give the group roles | Administration → Groups |
| Define custom roles (1-100 permissions) | Administration → Roles |
| Decide what anonymous clients may do | Administration → Roles → Anonymous access |
| Review every security-relevant change | Administration → Audit log (filters, change diff, CSV/NDJSON export) |

- **Built-in roles**: `admin`, `reader` (browse + read everything), `deployer` (read + write), `viewer` (browse +
  statistics) and `anonymous` (browse + read on proxy repositories). Built-in roles cannot be edited; copy one into a
  custom role instead.
- **Anonymous access** defaults to browse + read on proxy repositories. Clear it to require sign-in everywhere. Signed-in
  users always receive the anonymous grants as well, so signing in never reduces access.
- **Delegation**: a `user-admin` can only hand out what it holds itself, and only an administrator can grant
  `admin`.
- **Hidden means 404**: a repository a principal may not see answers 404 everywhere, exactly like a missing one
  (unauthenticated requests on `/repository/...` get the same 401 challenge for both).

## 3. Sign-in providers (LDAP, Active Directory, OIDC)

Administration → Authentication providers (`settings-admin`). Several providers can be active; they are tried in
order.

- **LDAP / Active Directory**: servers (`ldaps://`, StartTLS or, for tests only, clear text) with failover, a service
  bind, the user search base and filter, attribute mapping, groups from `memberOf` or a group search (optionally
  nested), an optional "require group" gate and a CA bundle. **Test connection** checks every server; **Test user**
  shows the entry, the groups, the gate and the resulting roles (and checks a password when given).
- **OIDC** (Keycloak, authentik, Entra ID, Google and any other compliant provider): issuer, client id and secret,
  scopes, claim mapping (username, display name, e-mail, groups), auto-provisioning, RP-initiated logout. Register
  the redirect URI shown on the form (`<base URL>/auth/oidc/<provider id>/callback`) with the provider. **Test
  discovery** checks the issuer, endpoints, keys and client credentials.
- **Group mapping**: map directory groups or OIDC group claims to Waystation roles on the provider form. Mapping
  changes apply to signed-in users within seconds; membership changes at their next sign-in.
- **Linking**: one Waystation user per username across providers. A local account with a password is never linked to
  an external one; create a local user without a password to pre-assign roles to someone who will sign in through a
  provider.
- LDAP passwords work in the UI and for `docker login`. OIDC users, and every other package client, use API tokens.

## 4. API tokens and service accounts

- **Personal tokens**: every user creates tokens on the profile page: scopes (`read`, `write`, `delete`, `manage`,
  `stats-read`, `metrics`, `admin`), optional repository patterns, optional expiry. A token never exceeds its
  owner's current permissions and is shown only once.
- **Service accounts** (Administration → API tokens → Service accounts, `user-admin`) are non-human principals for
  CI and automation. They hold roles and authenticate only with tokens. Disabling a service account or changing its
  roles takes effect on every replica within a second.
- **Incident response**: Administration → API tokens lists every token of every user and service account, with
  last use and client address; revoking one is immediate. Signing a user out everywhere also revokes the Docker
  registry tokens issued to them.
- How each client sends a token: the client guides in [clients/](clients/README.md).

## 5. Repositories

Repositories → Create (or `POST /api/v1/repositories`, or the provisioning file). Each repository has one format and
one type: **proxy** (caches an upstream), **hosted** (stores uploads) or **group** (an ordered union of members).
The formats and what each supports are listed in [clients/README.md](clients/README.md).

- **Test upstream** on a proxy checks DNS, the SSRF guard, TLS, HTTP and authentication against the upstream.
- **Upstream credentials** (basic, bearer, Docker Hub, GHCR) are write-only and encrypted with
  `WAYSTATION_SECRET_KEYS`.
- **Private upstreams** (an internal registry on a private address) need **Allow private upstream** on the
  repository, or the range in `WAYSTATION_SSRF_ALLOW_CIDRS`.
- **Freshness, negative cache, serve-stale and offline mode** are per proxy repository (SPECIFICATION §4.1).
- **Auto-block**: after repeated upstream failures a proxy stops calling the upstream for a growing back-off period
  and answers from the cache (or 503). The repository list shows `blocked` with a countdown; saving the repository
  clears the block.
- **Write policy** on hosted repositories: allow redeploy, disable redeploy (the default for releases) or read-only.
  Docker repositories also take immutable tag patterns.
- **Clone** copies a repository's configuration (no content) under a new name.
- **Set me up** on every repository page shows the client configuration for the signed-in user.
- **Docker routing**: the default docker repository (Settings) receives unprefixed image names and Docker daemon
  mirror requests; upstream aliases (`docker.io`, `ghcr.io`) route `registry.example.com/ghcr.io/...`; dedicated
  port connectors are described in DEPLOYMENT.md section 8.
- **Moving from Nexus**: Repositories → Import from Nexus creates equivalent repositories from a running Nexus 3
  ([MIGRATING_FROM_NEXUS.md](MIGRATING_FROM_NEXUS.md)).

## 6. Storage: cleanup, garbage collection, quotas and the low-disk guard

- **Cleanup policies** (Cleanup page): reusable rules (last downloaded, published age, keep the latest N versions,
  name/version patterns, pre-releases, untagged docker manifests) attached to repositories, run on a schedule or by
  hand. Always check the **dry-run preview** (counts, bytes and every candidate with its reason) before enabling a
  schedule. Policies remove records; the disk space comes back at the next blob GC.
- **Blob GC** (task `blob.gc`, every 6 hours) deletes blobs no asset references once they have been unreferenced for
  the grace period (24 hours by default). Its status and freed bytes are on the Storage and Cleanup pages.
- **Quotas** (repository form → Storage quota): `soft` only warns at 80 % and 100 %; `hard` also refuses uploads and
  stops caching new content once full (proxy downloads are still served, just not stored). Usage counts a shared
  blob in every repository that holds it.
- **Low-disk guard** (Storage page, `settings-admin`): below the free-space floor (the larger of a byte and a
  percentage threshold; default `WAYSTATION_MIN_FREE_GB`) new cache writes and uploads are refused, cached content is
  still served, and a banner appears in the UI.
- **Statistics retention**: minute data 7 days, hourly 90 days, daily 2 years; nothing to maintain.

## 7. Tasks and workers

Administration → Tasks (`admin`) shows the scheduled tasks, every job run with its progress and log, the workers
and the dead letters.

| Task | Default schedule | What it does |
|---|---|---|
| `stats.rollup` | every 10 s | Rolls live counters into the statistics collections |
| `blob.gc` | every 6 h | Blob garbage collection |
| `blob.verify` | weekly (Sunday 03:30 UTC) | Checks every asset's blob file; marks missing proxied files for re-fetch, reports hosted losses ([BACKUP.md](BACKUP.md) section 6) |
| `secrets.rotate` | daily (04:23 UTC) | Re-encrypts stored credentials with the active secret key ([SECRET_KEY_ROTATION.md](SECRET_KEY_ROTATION.md)) |
| `apt.refresh` | hourly | Refreshes apt proxy indexes |
| `prefetch.refresh` | every 15 min | Keeps popular proxy metadata fresh (repositories that opt in) |
| `webhook.sweep` | every 15 s | Sends due webhook retries |
| `cleanup:<policy>`, `prefetch:<repository>` | the policy's / repository's schedule | Cleanup runs and warm-list prefetch |

- **Run now**, pause and schedule edits (cron, macro or `@every`) are on the Scheduled tab; a cleanup policy's
  schedule is edited on the Cleanup page.
- **Dead letters** are jobs that failed every attempt: read the run's log, fix the cause, then **Retry** (or
  **Discard**). Their count is `waystation_jobs_dead_letters` on `/metrics`.
- **Workers** report a heartbeat; a worker is `late` after 30 s and `lost` after 90 s. One worker holds the
  scheduler lease at a time; add workers or raise `WAYSTATION_WORKER_CONCURRENCY` when jobs wait (DEPLOYMENT.md
  section 9).

## 8. Prefetch

Per proxy repository (docker, npm, raw, Maven, Go, NuGet, Composer, Cargo, RubyGems, Conda, Hugging Face, apk),
repository settings → Prefetch:

- **Keep popular metadata fresh** revalidates the most downloaded metadata of the last 7 days before it goes
  stale, so clients get cache hits instead of waiting on the upstream.
- **Warm list** (up to 200 items in the format's syntax, [FORMATS.md](../Architecture/FORMATS.md) "Prefetch")
  fetches named packages or images on a schedule or with **Warm now**.

Prefetch never counts as downloads, skips offline and blocked repositories, and needs `WAYSTATION_SECRET_KEYS` and
a reachable `WAYSTATION_INTERNAL_URL` on the workers.

## 9. Webhooks

Administration → Webhooks (`admin`): signed HTTP `POST`s when content is uploaded, changed or deleted and when
repositories change. Each webhook has a target URL, the events, a repository filter, a signing secret and optional
headers; deliveries are retried with back-off and logged for 30 days (**Send test event**, **Redeliver**).
Receiver-side signature verification and the payload: [WEBHOOKS.md](WEBHOOKS.md).

## 10. Monitoring and alerts

- **Health**: `GET /healthz` (process alive) and `GET /readyz` (MongoDB and Valkey reachable).
- **Metrics**: `GET /metrics` for Prometheus, allowed by address (`WAYSTATION_METRICS_ALLOW_CIDRS`) or with an API
  token carrying the `metrics` scope.
- **Dashboards and alerting**: the Grafana dashboard and the Zabbix 7 template in `Build/monitoring/` cover
  upstream health, job queues, dead letters, blob store space, quotas, the low-disk guard and API latency
  ([MONITORING.md](MONITORING.md)).
- **In the UI**: the dashboard (live transfers, traffic, hit ratio, bandwidth saved), the Activity page (filterable,
  exportable event log), Storage, and the repository status (`ok`, `degraded`, `blocked`, `offline`).
- **Logs**: JSON lines on stdout of every container (`docker logs waystation-server`), with request ids
  (`X-Request-Id`) that also appear in the audit log.

## 11. Backup and restore

Back up three things: the MongoDB database, the blob store (`blobs/`, `keys/`) and the env file with
`WAYSTATION_SECRET_KEYS` (without the keys, stored credentials and signing keys cannot be read). Valkey needs no
backup.

`waystation-backup.sh` (from `Build/Containers/backup/`) takes consistent, incremental backups (`backup --target
<dir> --keep N`, optionally `--hosted-only`), restores them (`restore --from <backup>`) and runs the blob verify
afterwards. Schedule it with cron or a systemd timer and test a restore regularly: [BACKUP.md](BACKUP.md).

## 12. Upgrades

1. Read `Documents/CHANGELOG.md` for every version between yours and the target.
2. Back up (section 11). Migrations are forward-only: going back means restoring the backup.
3. Set the new `WAYSTATION_IMAGE_TAG`, then `docker compose --env-file waystation.env pull` and
   `docker compose --env-file waystation.env up -d --wait`. `init` applies the migrations; the server refuses to
   start against a schema it does not know.

Zero-downtime upgrades need two or more server hosts: DEPLOYMENT.md section 11.

## 13. Secret-key rotation

`WAYSTATION_SECRET_KEYS` is a comma-separated list: the first key encrypts, every key decrypts. To rotate, put a new
key first, restart, let `secrets.rotate` re-encrypt everything (or run it now), check its result reports nothing
left on the old key, then remove the old key: [SECRET_KEY_ROTATION.md](SECRET_KEY_ROTATION.md).

## 14. Configuration as code

A provisioning file (`WAYSTATION_PROVISION_FILE`) declares repositories, cleanup policies, roles, groups, service
accounts, sign-in providers, webhooks and settings; `init` applies it at every start and Administration →
Provisioning shows the plan and applies it on demand. Objects it manages are read-only in the UI:
[PROVISIONING.md](PROVISIONING.md).

## 15. Support bundle

Administration → System → **Download support bundle** (`admin`) produces a zip with versions, redacted settings,
repository and provider configuration (secrets only as "set" flags), health, recent failed jobs and the latest
warnings and errors of every process. Review it before sharing; downloads are audited.

## 16. Troubleshooting

| Symptom | Where to look |
|---|---|
| A client gets `401` or `404` on a repository | The user's effective permissions (section 2); with no credentials, every hidden or missing repository answers the same `401` challenge. Package clients need an API token, not a password (except `docker login` and `npm login`) |
| `docker login` refused for a user with two-factor sign-in | Use an API token as the password |
| A proxy repository shows `degraded` or `blocked` | Test upstream on the repository page; the activity log filtered to the repository; `waystation_upstream_state` |
| Clients get `502` on a download | The upstream sent content that failed its checksum or digest: the activity log names it; the file was not cached |
| Uploads fail with `507` or `403 ... quota` | Low-disk guard (Storage page) or a hard quota (repository page) |
| Jobs pile up or dead letters appear | Administration → Tasks: the run log, the workers' heartbeat |
| Sign-in through LDAP or OIDC fails | The provider's **Test user** / **Test discovery**; the audit log shows the failure reason (`not_in_group`, `directory_unavailable`, ...) |
| Prefetch jobs fail | `WAYSTATION_INTERNAL_URL` must reach a server from the worker; `WAYSTATION_SECRET_KEYS` must be set |
| Large transfers stop, the live dashboard does not update | Reverse proxy limits and buffering: [REVERSE_PROXIES.md](REVERSE_PROXIES.md) |
| Content missing after a restore or disk problem | Run `blob.verify` (Tasks page): proxied files are re-fetched, hosted losses are listed |

Deployment-level problems (containers that do not start, TLS, MongoDB connections): DEPLOYMENT.md section 12.
Client-specific problems: the "Troubleshooting" section of each guide in [clients/](clients/README.md).
