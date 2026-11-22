# Deploying Waystation

This guide describes how to run Waystation on one Docker host, or split across hosts (section 9), **as the code works
today** (release 0.2.8).
Anything not built yet is marked **Planned** with its task ID. For a five-minute evaluation run,
see [TRY_IT.md](TRY_IT.md); for putting a reverse proxy in front, see [REVERSE_PROXIES.md](REVERSE_PROXIES.md); for
client configuration per format, see [clients/](clients/).

Examples use `registry.example.com` as the public host name and `/srv/waystation` as the install directory.

## 1. What runs

| Container | Image | Role | Publishes a port |
|---|---|---|---|
| `waystation-web` | `waystation-web` (nginx, non-root) | Serves the UI under `/ui/`, proxies dynamic paths to the server, serves cached files from the blob store via `X-Accel-Redirect` | yes: `WAYSTATION_HTTP_PORT` (default 8080), plain HTTP |
| `waystation-server` | `waystation-server` | Format protocols, REST API, live event stream, Docker token service | no (port 8000 on the compose network) |
| `waystation-worker` | `waystation-worker` | Background jobs (stats rollups, indexing; more job types arrive with later tasks) | no (no HTTP listener at all) |
| `waystation-init` | `waystation-server` (one-shot) | Fixes data-dir ownership and modes, initiates the MongoDB replica set, applies schema and migrations, creates the first administrator | no; exits 0 |
| `waystation-mongodb` | `mongo:8` | System of record: configuration, metadata, users, stats | no |
| `waystation-valkey` | `valkey/valkey:8-alpine` | Coordination: coalescing locks, live counters, job queues (`noeviction`, AOF) | no |

Networks: `frontend` (web, server, worker; has egress so the server and worker can reach upstream registries) and
`backend` (`internal: true`, no egress: MongoDB, Valkey, server, worker, init). Only `web` publishes a port.

Every long-running container runs as a non-root user with a read-only root filesystem (MongoDB's excepted),
`cap_drop: [ALL]` and `no-new-privileges`. `init` alone starts as root with `CHOWN`, `FOWNER`, `DAC_OVERRIDE`,
`SETUID` and `SETGID` to fix the data directory, then drops to `WAYSTATION_UID:GID`.

## 2. Requirements

- Linux on amd64 or arm64 with Docker Engine 24 or later and Docker Compose v2.20 or later (`depends_on.restart`
  and `docker compose up --wait` are used).
- A host name for Waystation (`registry.example.com`). Waystation must own the **root** of its host name: Docker
  clients always talk to `https://<host>/v2/`, the token realm is `<base URL>/auth/token`, and the UI and API use
  absolute paths (`/ui/`, `/api/v1/`). Mounting Waystation under a path such as `https://example.com/waystation/`
  is not supported.
- TLS in front of it for anything beyond a lab test (section 8).
- Outbound HTTPS from the host to the upstream registries you proxy (directly or via `WAYSTATION_HTTP_PROXY`).

### Sizing

These are starting points for a home lab or small team, not measured capacity limits (the k6 suite, `make perf`,
checks latency budgets on a CI-sized stack, not host sizing).

| Resource | Small (a few machines, CI runner) | Medium (tens of machines, busy CI) | Notes |
|---|---|---|---|
| vCPU | 2 | 4 | Cache hits are served by nginx `sendfile`; CPU goes to cache fills (sha256 of every byte) and the API |
| RAM | 4 GiB | 8 GiB | MongoDB's WiredTiger cache takes about half of (RAM - 1 GiB) by default; Valkey uses tens of MiB |
| Blob store | 100 GiB | 500 GiB+ | Everything you pull is cached here once (identical content is stored once across repositories). New writes are refused below `WAYSTATION_MIN_FREE_GB` (default 5) |
| MongoDB disk | 5 GiB | 20 GiB | Metadata, per-minute stats (time series), users; grows with the number of cached assets |
| Network | 1 Gbit/s | 1-10 Gbit/s | Throughput is bound by disk and network, not CPU |

On a shared host, cap MongoDB's cache with a compose override (`command: ["mongod", "--replSet", "rs0",
"--bind_ip_all", "--quiet", "--wiredTigerCacheSizeGB", "1"]`).

Raspberry Pi 5 (8 GB, arm64, Debian 13) runs the whole stack (tested with the 0.2.8 images). Raspberry Pi kernels
ship without the memory cgroup, so `mem_limit` in an override is ignored ("Your kernel does not support memory limit
capabilities") unless `cgroup_enable=memory` is added to the kernel command line; cap MongoDB's cache as above.

### MongoDB connections

Every server process (`WAYSTATION_SERVER_WORKERS`) and the worker keep a connection pool of up to 100 MongoDB
connections, plus two monitoring connections each; under heavy concurrent load the pools fill up. mongod refuses
connections past its limit, which it derives from its open-file limit: with Docker's default soft limit of 1024 it
accepts only about 400 (WiredTiger keeps a file open per collection and index), and the refused connections show up
as `AutoReconnect: ... Connection is closed` in the server log and 500 answers. The bundled `mongodb` service
therefore sets `ulimits: nofile: 64000` (MongoDB's recommended value). For an external MongoDB, make sure its
open-file limit (and `net.maxIncomingConnections`) leave room for `(server processes + 1) x 102` connections per
Waystation host. A read that meets a transient connection error is retried once after a short random pause.

## 3. Images

Images are named `${WAYSTATION_IMAGE_PREFIX}/waystation-{web,server,worker}:${WAYSTATION_IMAGE_TAG}`. Both variables
are required; there is no `:latest`.

- **Release images** (recommended) are public on GHCR: `ghcr.io/slinkysoftware/waystation-{server,worker,web}:X.Y.Z`,
  multi-arch (linux/amd64, linux/arm64) with an SBOM and SLSA provenance, published by
  `.github/workflows/release.yml` for every `vX.Y.Z` tag (`Documents/Operations/CI.md`). Set
  `WAYSTATION_IMAGE_PREFIX=ghcr.io/slinkysoftware` and pin an exact release; `latest` exists but upgrades should be
  deliberate.
- **Build them yourself** from a checkout (Docker only, no host toolchain): `make images IMAGE_PREFIX=waystation
  IMAGE_TAG=dev`. To run them on another host, push them to your own registry or `docker save | docker load`.

## 4. Prepare the host

```bash
sudo install -d -m 0755 /srv/waystation
# Secret files (section 5) are read by the containers as WAYSTATION_GID (default 10001), so that group owns the directory
sudo install -d -m 0750 -o root -g 10001 /srv/waystation/secrets
# Copy Build/Containers/compose.yaml from the release (or the checkout) into /srv/waystation/
sudo cp compose.yaml /srv/waystation/compose.yaml
```

The compose project name is `waystation` (top-level `name:`); containers are named `waystation-<role>` and every
service has a unique network alias (`waystation-server`, never a bare `server`) so the stack can join a shared
reverse-proxy network without name clashes. Because container names are fixed, run one Waystation stack per Docker
host.

### Storage

| Data | Default | Mount | Override |
|---|---|---|---|
| Blob store (`blobs/`, `uploads/`, `keys/`) | named volume `waystation_data` | `/var/lib/waystation`, read-write in server, worker and init, **read-only** in web | `WAYSTATION_DATA_MOUNT_TYPE=bind` and `WAYSTATION_DATA_SOURCE=/srv/waystation/data` |
| MongoDB | named volume `waystation_mongodb` | `/data/db` | `WAYSTATION_MONGO_MOUNT_TYPE=bind` and `WAYSTATION_MONGO_DIR=/srv/waystation/mongodb` |
| Valkey (AOF) | named volume `waystation_valkey` | `/data` | none (Valkey holds no system-of-record data) |

- `uploads/` must be on the same filesystem as `blobs/` (cache fills commit with an atomic link or rename). Keep the
  whole data directory on one mount.
- The blob store works on local disk, NFSv4 or SMB (commits use hard links where the filesystem has them, else an
  atomic rename; CI runs the blob store tests on an NFSv4.1 mount). On a network filesystem, coalesced followers
  wait for the commit instead of tailing the in-progress file (`WAYSTATION_COALESCE_TAIL=auto`, ARCHITECTURE §6.3).
- `init` creates the layout and fixes ownership (`WAYSTATION_UID:WAYSTATION_GID`, default `10001:10001`, setgid
  directories 2750, files 0640) on every start, idempotently. The web container joins the same group
  (`group_add`) to read blobs. For a bind mount, create the directory and let `init` fix it.
- A MongoDB bind mount must be writable by UID 999 (`sudo install -d -o 999 -g 999 -m 0700 /srv/waystation/mongodb`).
- Keep MongoDB off the blob path (and ideally on local SSD).

## 5. The env file

The env file lives **outside** the repository and outside the compose directory's version control, readable only
by root and the docker group (`root:docker 0640`). It serves two purposes: compose variable interpolation (image,
port, mounts, subnets) and the application settings, which compose passes through to the containers (variables
listed without a value under `x-app-env` in `compose.yaml`).

Minimal `/srv/waystation/waystation.env`:

```bash
# Compose
WAYSTATION_IMAGE_PREFIX=ghcr.io/slinkysoftware
WAYSTATION_IMAGE_TAG=0.2.8
WAYSTATION_HTTP_BIND=0.0.0.0
WAYSTATION_HTTP_PORT=8080

# Application
WAYSTATION_BASE_URL=https://registry.example.com
WAYSTATION_SECRET_KEYS=<output of: head -c 32 /dev/urandom | base64>
WAYSTATION_SESSION_SECRET=<output of: head -c 32 /dev/urandom | base64>
# Behind a reverse proxy (REVERSE_PROXIES.md):
# WAYSTATION_TRUST_FORWARDED=true
# WAYSTATION_TRUSTED_PROXIES=192.0.2.10/32
```

```bash
sudo chown root:docker /srv/waystation/waystation.env && sudo chmod 0640 /srv/waystation/waystation.env
```

Every `docker compose` command in this guide passes `--env-file waystation.env`: without it compose cannot
interpolate the image names and refuses to run (`required variable WAYSTATION_IMAGE_PREFIX is missing`). To drop the
flag, name the file `/srv/waystation/.env` instead; compose reads `.env` from the project directory by itself.

Start from the minimal file above. If you copy [`.env.example`](../../.env.example) instead, it lists every setting
with the application's default, and two differ from what the bundled stack needs: set
`WAYSTATION_MONGO_REPLSET_INIT=true` (the bundled MongoDB's replica set is initiated by `init`; `false` there means an
external MongoDB) and remove or fill in every line you do not need.

`WAYSTATION_SECRET_KEYS` encrypts stored upstream credentials and the registry token signing key. **Back it up with
the database; losing it makes those secrets unreadable.** To rotate, prepend a new key (`new,old`): the first key
encrypts, all keys decrypt; the `secrets.rotate` task re-encrypts what is stored, then the old key can go
([SECRET_KEY_ROTATION.md](SECRET_KEY_ROTATION.md)).

### Secrets from files

Every secret (`MONGO_URI`, `VALKEY_URL`, `SECRET_KEYS`, `SESSION_SECRET`, `BOOTSTRAP_ADMIN_PASSWORD`) also accepts
`<NAME>_FILE` pointing at a file inside the container (setting both is an error). `compose.yaml` passes the `_FILE`
variables through but mounts no secret files, so add them with an override next to `compose.yaml`:

```yaml
# /srv/waystation/compose.override.yaml (picked up automatically by docker compose in that directory)
x-secrets: &secrets
  - {type: bind, source: /srv/waystation/secrets, target: /run/waystation-secrets, read_only: true}
services:
  server: {volumes: *secrets}
  worker: {volumes: *secrets}
  init: {volumes: *secrets}
```

Compose merges `volumes` lists, so the data mount stays. Then set, for example,
`WAYSTATION_SECRET_KEYS_FILE=/run/waystation-secrets/secret_keys` in the env file (and leave
`WAYSTATION_SECRET_KEYS` unset). The directory and the files must be readable by `WAYSTATION_GID` (directory
`root:10001 0750` as created in section 4, files `root:10001 0640`); otherwise the server stops with
`WAYSTATION_SECRET_KEYS_FILE: cannot read ...: Permission denied`.

### Environment reference

The authoritative list of application settings, with defaults and descriptions, is
[`.env.example`](../../.env.example) at the repository root. It is generated from `waystation_common.settings` by
`make env-example`, and CI fails if it is stale. Summary:

| Variable | Default | Purpose |
|---|---|---|
| `WAYSTATION_BASE_URL` | `https://registry.example.com` | Public URL clients use (scheme, host, optional port, no trailing slash). Drives every generated URL, the Docker token realm and the `Secure` cookie flag (on for `https://`) |
| `WAYSTATION_INSTANCE_NAME` | `Waystation` | Name in the UI top bar and sign-in page |
| `WAYSTATION_LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | Logging (`console` for humans) |
| `WAYSTATION_DATA_DIR` | `/var/lib/waystation` | Blob store path inside the containers (leave as is; change the mount instead) |
| `WAYSTATION_UID` / `GID` | `10001` / `10001` | Owner and shared group of the data dir |
| `WAYSTATION_MAX_UPLOAD_GB` | `64` | Largest single cached file or upload |
| `WAYSTATION_MIN_FREE_GB` | `5` | New cache writes are refused below this free space |
| `WAYSTATION_COALESCE_TAIL` / `COALESCE_LEASE_S` | `auto` / `15` | Request coalescing (ARCHITECTURE §6.3) |
| `WAYSTATION_MONGO_URI` [secret] | `mongodb://waystation-mongodb:27017/?replicaSet=rs0` | MongoDB; must be a replica set (transactions) |
| `WAYSTATION_MONGO_DB` | `waystation` | Database name |
| `WAYSTATION_MONGO_REPLSET_INIT` | `true` in compose | `init` initiates the bundled single-node replica set; set `false` for an external MongoDB |
| `WAYSTATION_VALKEY_URL` [secret] | `redis://waystation-valkey:6379/0` | Valkey |
| `WAYSTATION_SECRET_KEYS` [secret] | none (required) | Comma list of base64 AES-256 keys |
| `WAYSTATION_SESSION_SECRET` [secret] | none (required) | Session signing secret |
| `WAYSTATION_BOOTSTRAP_ADMIN_PASSWORD` [secret] | unset | First admin password; unset = `init` prints a one-time password |
| `WAYSTATION_PROVISION_FILE` | unset | Declarative provisioning file (YAML) applied by `init` at every start; mount it (and the secret files it references) into `init` and `server` at the same path ([PROVISIONING.md](PROVISIONING.md)) |
| `WAYSTATION_SESSION_IDLE_MINUTES` / `SESSION_MAX_HOURS` | `480` / `168` | UI session lifetimes |
| `WAYSTATION_LOGIN_RATE_LIMIT`, `LOGIN_LOCKOUT_FAILURES`, `LOGIN_LOCKOUT_MINUTES` | `20`, `10`, `15` | Sign-in throttling and lockout |
| `WAYSTATION_TOKEN_FAILURE_LIMIT` | `60` | Rejected API tokens allowed per client address per 5 minutes (then 429) |
| `WAYSTATION_AUDIT_RETENTION_DAYS` | `400` | Days an audit record is kept (MongoDB TTL); applies to records written from then on |
| `WAYSTATION_TRUST_FORWARDED` / `TRUSTED_PROXIES` | `false` / empty | Trust `X-Forwarded-For` from these CIDRs (REVERSE_PROXIES.md) |
| `WAYSTATION_METRICS_ALLOW_CIDRS` | `127.0.0.0/8,::1/128` | Who may scrape `/metrics` |
| `WAYSTATION_SSRF_ALLOW_CIDRS` | empty | Private ranges upstreams may resolve to (e.g. an internal registry) |
| `WAYSTATION_HTTP_PROXY` / `NO_PROXY` | empty | Outbound proxy for upstream fetches (`NO_PROXY`: comma-separated host names, a leading dot matches subdomains). A repository can set its own proxy (`proxy.http_proxy`, no credentials), which then wins. The SSRF guard still checks every target, so the server must be able to resolve upstream names (unless the repository allows private upstreams) |
| `WAYSTATION_EXTRA_CA_FILE` | empty | Extra PEM bundle trusted for upstream TLS (mount it like the secrets above) |
| `WAYSTATION_SERVER_WORKERS` | `1` | uvicorn processes in the server container |
| `WAYSTATION_WORKER_QUEUES` / `WORKER_CONCURRENCY` | all queues / `4` | Which job queues a worker consumes, and how many jobs at once |

Compose-only variables (used for interpolation in `compose.yaml`, not read by the application):

| Variable | Default | Purpose |
|---|---|---|
| `WAYSTATION_IMAGE_PREFIX`, `WAYSTATION_IMAGE_TAG` | required | Image names |
| `WAYSTATION_HTTP_BIND`, `WAYSTATION_HTTP_PORT` | `0.0.0.0`, `8080` | Published address and port of `web` (bind `127.0.0.1` when a proxy on the same host is the only client) |
| `WAYSTATION_DATA_MOUNT_TYPE`, `WAYSTATION_DATA_SOURCE` | `volume`, `data` | Blob store mount |
| `WAYSTATION_MONGO_MOUNT_TYPE`, `WAYSTATION_MONGO_DIR` | `volume`, `mongodb` | MongoDB mount |
| `WAYSTATION_SUBNET`, `WAYSTATION_BACKEND_SUBNET` | Docker picks | Explicit subnets for `frontend` / `backend` when Docker's default pools collide with routed networks; only read through the override `compose.subnets.yaml` (`COMPOSE_FILE=compose.yaml:compose.subnets.yaml`) |

The web image reads `WAYSTATION_SERVER_ADDR` (default `waystation-server:8000`) and `WAYSTATION_WEB_PORT` (default
`8080`) from its own environment; the defaults match `compose.yaml`.

## 6. Start

```bash
cd /srv/waystation
docker compose --env-file waystation.env config --quiet     # validates interpolation
docker compose --env-file waystation.env up -d --wait
docker compose --env-file waystation.env ps
```

Start-up order: MongoDB and Valkey become healthy, `init` runs to completion, then server and worker start, then
web. If `init` fails, nothing else starts: `docker logs waystation-init`.

### First sign-in

Unless `WAYSTATION_BOOTSTRAP_ADMIN_PASSWORD(_FILE)` was set, `init` prints a one-time password for the user `admin`
**once**, on the run that creates the first user:

```bash
docker logs waystation-init 2>&1 | grep -A4 'first administrator'
```

Save it now: recreating `init` (any upgrade or configuration change) discards these logs, and the password is never
shown again. To choose it yourself instead, set `WAYSTATION_BOOTSTRAP_ADMIN_PASSWORD_FILE` before the first start.

Open `https://registry.example.com/ui/`, sign in as `admin`, and create repositories (Repositories → Create).
Before TLS is in place, use `http://<host>:8080/ui/` **with** `WAYSTATION_BASE_URL=http://<host>:8080` in the env
file: an `https://` base URL makes the session cookie `Secure`, so a plain-HTTP sign-in cannot keep its session
(section 8, "No TLS at all"; switch the base URL when the proxy is in place). Change the password on your profile (avatar menu → Profile), or set
`WAYSTATION_BOOTSTRAP_ADMIN_PASSWORD_FILE` before the first start to choose it. Each repository's "Set me up" panel
shows client configuration; the per-format guides are in [clients/](clients/). For scripted installs, the REST API
is described by `GET /api/v1/openapi.json` on the running server; [AGENT_DEPLOYMENT.md](AGENT_DEPLOYMENT.md) section 8
shows repository creation through it.

To keep the configuration in version control instead, declare repositories, roles, providers, policies, webhooks and
settings in a provisioning file ([PROVISIONING.md](PROVISIONING.md)); `init` applies it at every start.

## 7. Health checks

| Check | Where | Meaning |
|---|---|---|
| `GET /healthz` | through web, or server port 8000 | Server process is alive (always `{"status":"ok"}`) |
| `GET /readyz` | through web, or server port 8000 | MongoDB and Valkey both answered within 2 s (`200 ok`), else `503 unavailable` |
| `GET /ui/` | web | The web container's own healthcheck |
| heartbeat file | worker | `python -m waystation_worker.health` (the container healthcheck): the job loop polled its queues within 45 s |
| `GET /metrics` | through web | Prometheus metrics, only for `WAYSTATION_METRICS_ALLOW_CIDRS` (404 for everyone else). The default allows only scrapes from inside the containers: a request from the host through the published port arrives from the Docker bridge gateway, so add the scraper's (or that gateway's) CIDR |

`docker compose --env-file waystation.env ps` shows the health of every container. `depends_on` restarts web when the server restarts.
Prometheus scraping, the Grafana dashboard and the Zabbix template: [MONITORING.md](MONITORING.md).

## 8. TLS

| Option | Status |
|---|---|
| External reverse proxy terminates TLS and forwards plain HTTP to `web` | **Supported.** Settings for nginx, Traefik, Caddy, HAProxy and Synology DSM: [REVERSE_PROXIES.md](REVERSE_PROXIES.md). Set `WAYSTATION_BASE_URL=https://...`, and `WAYSTATION_TRUST_FORWARDED=true` with `WAYSTATION_TRUSTED_PROXIES` so client addresses are logged and rate-limited correctly |
| `web` terminates TLS itself (certificate and key paths, HTTP to HTTPS redirect, HTTP/2) | **Supported**: `compose.tls.yaml`, below |
| No TLS at all | Works for a lab: set `WAYSTATION_BASE_URL=http://<host>:8080`. Docker needs the host in `insecure-registries`; session cookies are sent without `Secure` |

### TLS in the web container

Add the override `Build/Containers/compose.tls.yaml` (copy it next to `compose.yaml`). `web` then serves HTTPS with
HTTP/2 (TLS 1.2 and 1.3) on container port 8443, and its plain HTTP port answers every request with a `308` redirect
to `WAYSTATION_BASE_URL` (method and body kept), except the container healthcheck `/_web/healthz`.

```bash
# /srv/waystation/.env (or waystation.env): the override and its settings
COMPOSE_FILE=compose.yaml:compose.tls.yaml
WAYSTATION_BASE_URL=https://registry.example.com
WAYSTATION_TLS_DIR=/srv/waystation/tls          # holds tls.crt (full chain) and tls.key, PEM
WAYSTATION_HTTPS_PORT=443                       # published HTTPS port (default 8443)
WAYSTATION_HTTP_PORT=80                         # published plain port, now redirect only (default 8080)
# WAYSTATION_TLS_CERT_NAME / WAYSTATION_TLS_KEY_NAME: other file names inside WAYSTATION_TLS_DIR
```

```bash
sudo install -d -o root -g 10001 -m 0750 /srv/waystation/tls      # 10001 = WAYSTATION_GID
sudo install -o root -g 10001 -m 0640 fullchain.pem /srv/waystation/tls/tls.crt
sudo install -o root -g 10001 -m 0640 privkey.pem /srv/waystation/tls/tls.key
```

The web container (uid 101, group `WAYSTATION_GID`) must be able to read both files; it refuses to start, with the
reason in `docker logs waystation-web`, when only one is set or a file is unreadable. After renewing the certificate:
`docker exec waystation-web nginx -s reload`. For certbot, either copy the renewed files in a deploy hook (as above),
or set `WAYSTATION_TLS_DIR=/etc/letsencrypt` with `WAYSTATION_TLS_CERT_NAME=live/<name>/fullchain.pem` and
`WAYSTATION_TLS_KEY_NAME=live/<name>/privkey.pem` (the `live/` links point into `archive/`, inside the same mount)
and give group `WAYSTATION_GID` read access to the key. HSTS is not sent; add it at a proxy in front if you want it.

When the reverse proxy runs in Docker on the same host, the simplest setup is to attach `web` to the proxy's network
with an override and not publish a port at all; `Build/Containers/examples/traefik.override.example.yaml` shows
this for Traefik.

## 9. Scaling and split deployments

| Topology | Status |
|---|---|
| All-in-one on one host (`compose.yaml`) | **Supported** |
| More server processes on that host | **Supported**: `WAYSTATION_SERVER_WORKERS=N` (uvicorn processes in one container). `/metrics` merges every process's values (MONITORING.md) |
| More worker concurrency or queue split | **Supported** within one worker container (`WAYSTATION_WORKER_CONCURRENCY`, `WAYSTATION_WORKER_QUEUES`). Extra worker containers need an override with a different `container_name` |
| External MongoDB (replica set) | **Supported**: set `WAYSTATION_MONGO_URI` to the replica set, `WAYSTATION_MONGO_REPLSET_INIT=false`, and either leave the bundled `mongodb` service running unused or replace the `depends_on` of server, worker and init in an override (`depends_on: !reset {...}`). Server and worker reach it through the `frontend` network, but `init` is only on the `internal` backend network, so the override must also attach `init` to `frontend` |
| External Valkey | **Supported** if it runs with `maxmemory-policy noeviction` and AOF enabled (job streams and locks must never be evicted) |
| Split hosts: web, server and worker on separate hosts, several server or worker hosts | **Supported**: `compose.standalone.{web,server,worker}.yaml`, below |
| Blob store on S3 | **Planned** post-v1 (D5) |

### Split hosts

Each tier runs from its own file, with the same env file on every host (copy the files from `Build/Containers/`):

| Host | File | Runs | Publishes |
|---|---|---|---|
| web (one or more) | `compose.standalone.web.yaml` (+ `compose.tls.yaml`) | nginx: UI, routing, cached files | `WAYSTATION_HTTP_PORT` (and HTTPS) |
| server (one or more) | `compose.standalone.server.yaml` | `init` (each start), then the server | `WAYSTATION_SERVER_BIND`:`WAYSTATION_SERVER_PORT` (8000) |
| worker (one or more) | `compose.standalone.worker.yaml` | the worker | nothing |

Requirements:

- **Shared blob store.** Every web, server and worker host mounts the same filesystem (NFSv4 recommended; hard
  links are used where available, rename atomicity is required) and points `WAYSTATION_DATA_SOURCE` at it (a host
  path; `WAYSTATION_DATA_MOUNT_TYPE=bind` is the default in these files). The web hosts mount it read-only. The
  files are owned by `WAYSTATION_UID:GID` (set by `init`), so the export must not squash those ids.
- **External MongoDB and Valkey.** There is no bundled database in the standalone files: set `WAYSTATION_MONGO_URI`
  to a replica set (`WAYSTATION_MONGO_REPLSET_INIT` defaults to `false` here) and `WAYSTATION_VALKEY_URL` to a
  Valkey with `maxmemory-policy noeviction` and AOF. Use authentication, and let only the server and worker hosts
  reach them.
- **The server port is for the web hosts only.** `WAYSTATION_SERVER_BIND` is the address the port is published on
  (required). The server takes the `X-Real-IP` of any peer on a private address as the client address (that peer is
  normally the web tier), so firewall the port so that nothing but the web hosts reaches it.
- **The web hosts know the servers.** `WAYSTATION_SERVER_ADDR=server1.example.com:8000,server2.example.com:8000`
  (host:port, comma-separated). nginx balances requests over them and retries a request whose server cannot be
  reached on the next one; the names must resolve when `web` starts.
- **Order.** Start a server host first (its `init` creates the schema, applies migrations and the provisioning
  file; running it on several server hosts at once is safe), then the worker and web hosts.

```bash
# on each host, from the directory holding the file and waystation.env
docker compose -f compose.standalone.server.yaml --env-file waystation.env up -d --wait   # server hosts
docker compose -f compose.standalone.worker.yaml --env-file waystation.env up -d --wait   # worker hosts
docker compose -f compose.standalone.web.yaml -f compose.tls.yaml --env-file waystation.env up -d --wait  # web
```

Sessions, uploads in progress, coalescing and the live event stream are shared through MongoDB, Valkey and the blob
store, so any server can answer any request. `make deploy-test` runs this topology in containers on one machine
(two server hosts, a worker host, a TLS web host, external MongoDB and Valkey, one shared blob directory) and checks
uploads, downloads, a proxied file, a worker job and that the web tier keeps serving with one server host stopped.

## 10. Backups

Full procedures are in [BACKUP.md](BACKUP.md). In short:

1. Keep **the env file and secret files** (`WAYSTATION_SECRET_KEYS` above all) in your host backups.
2. Copy `Build/Containers/backup/waystation-backup.sh` next to `compose.yaml` and run it from cron or a systemd
   timer: `sudo ./waystation-backup.sh backup --target /mnt/backup/waystation --keep 14`. It dumps MongoDB
   (`mongodump --archive --gzip`), then copies the blob files (hard-linked from the previous backup when unchanged)
   and `keys/`, and writes a manifest. `--hosted-only` skips cached proxy content (re-fetched after a restore).
3. Restore: `docker compose --env-file waystation.env stop web server worker`,
   `sudo ./waystation-backup.sh restore --from <backup>`, `docker compose --env-file waystation.env up -d`, then `./waystation-backup.sh verify` (the blob verify task marks proxied assets whose
   content is missing for re-fetch and reports hosted losses).

Valkey needs no backup: losing it never loses data (job outbox and schedules are re-derived from MongoDB).

## 11. Upgrades

1. Read the CHANGELOG for the target version.
2. Back up (section 10). Migrations are forward-only; to go back you restore the backup.
3. Change `WAYSTATION_IMAGE_TAG` in the env file, then:
   ```bash
   docker compose --env-file waystation.env pull      # release images; skip when you built them locally
   docker compose --env-file waystation.env up -d --wait
   ```
   `init` runs again and applies pending migrations; the server refuses to start against a database with a pending
   migration or an unknown newer schema version (`docker logs waystation-server`).

The upgrade restarts the server: in-flight downloads are cut after Docker's stop timeout (10 s by default; the server
itself allows 30 s). Add `stop_grace_period: 30s` to `server` in an override to let long transfers finish. Zero-
downtime upgrades need two or more server hosts (split hosts, section 9): for a release whose notes list no
migration, upgrade the server hosts one at a time; the web tier sends requests to the others meanwhile.

## 12. Troubleshooting

| Symptom | Check |
|---|---|
| `init` exits non-zero | `docker logs waystation-init`: usually a missing `WAYSTATION_SECRET_KEYS`/`SESSION_SECRET`, an unreachable MongoDB, or a bind-mounted data dir on a filesystem that rejects `chown` |
| Server unhealthy, log says schema | A newer database than the image (downgrade) or a migration that did not run: restore, or run `init` again |
| UI loads but sign-in fails behind a proxy | `WAYSTATION_BASE_URL` must be the URL in the browser; an `https://` base URL makes cookies `Secure`, so plain-HTTP access to the same stack cannot keep a session |
| Docker: `unauthorized` or token errors | The token realm is `<BASE_URL>/auth/token`; the proxy must forward `/auth/` and `/v2/` at the root of the host |
| Large pulls stop at a fixed time or size | A proxy timeout or body/buffer limit in front of Waystation (REVERSE_PROXIES.md) |
| Live dashboard does not update | The proxy buffers `/api/v1/events/stream` (REVERSE_PROXIES.md) |
| Log shows the proxy's address as the client | `WAYSTATION_TRUST_FORWARDED` / `WAYSTATION_TRUSTED_PROXIES` (REVERSE_PROXIES.md) |
| `AutoReconnect: ... Connection is closed` in the server log under load | mongod refuses connections past its open-file limit: "MongoDB connections" in section 2 |
| `/metrics` returns 404 | The scraper's address is not in `WAYSTATION_METRICS_ALLOW_CIDRS` (from the Docker host itself, requests come from the bridge gateway, section 7) |
| `required variable WAYSTATION_IMAGE_PREFIX is missing` | `docker compose` ran without `--env-file waystation.env` (section 5) |
| Server stops with `..._FILE: cannot read ...: Permission denied` | The secrets directory or file is not readable by `WAYSTATION_GID` (section 5) |
