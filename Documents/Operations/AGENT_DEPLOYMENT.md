# Deploying Waystation with an AI agent

This is a runbook for an AI agent (or a person) asked to "deploy Waystation from
https://github.com/SlinkySoftware/Waystation" into someone's environment: a home lab, a small office, a NAS. It
turns [DEPLOYMENT.md](DEPLOYMENT.md), [REVERSE_PROXIES.md](REVERSE_PROXIES.md),
[PROVISIONING.md](PROVISIONING.md), [MIGRATING_FROM_NEXUS.md](MIGRATING_FROM_NEXUS.md) and
[BACKUP.md](BACKUP.md) into one ordered procedure, with the questions to ask first, a check after every step, and
the problems found in real deployments. Those documents stay the reference for every setting; this one says what to
do in which order.

Every name and address here is an example (`registry.example.com`, `192.0.2.10`, `2001:db8::10`). Replace them with
the environment's own values, and keep those values out of this repository and out of any public place.

## Rules for the agent

- **Ask before you act** where the answer belongs to the owner (section 1). Never guess hosts, credentials, DNS
  servers or which machines may be changed.
- **Report findings first.** Make live changes only when the owner has asked for the deployment. Confirm before
  anything destructive or hard to undo: deleting data, containers or DNS records, replacing an existing registry,
  restarting shared services (CI runners, reverse proxies, DNS servers), changing firewalls.
- **Secrets**: generate them on the target or on the machine you work from, store them only where the owner keeps
  secrets, and never print them in chat, logs, files you commit or command lines (use files and `_FILE` settings).
- **Back up before you edit** anything that already exists (reverse proxy rules, client config files, DNS records).
  Keep a rollback note until the owner says the change is final.
- **Verify every step** with the check given. If a check fails, stop and report: do not move on with a half-working
  stack.
- At the end, report exactly what was done, where it lives, what was verified, what was skipped and what the owner
  still has to do (section 13).

## 1. Ask the owner first

Collect these answers before touching anything. Suggested defaults are in brackets.

| # | Question | Why |
|---|---|---|
| 1 | Which host runs Waystation, and how do you reach it (SSH user, sudo/root)? | Docker host, VM or NAS (Synology Container Manager works) |
| 2 | Which host name will clients use? [`registry.<their domain>`] | Becomes `WAYSTATION_BASE_URL`; one name for the UI and every format |
| 3 | How is TLS terminated? An existing reverse proxy (nginx, Traefik, Caddy, HAProxy, Synology DSM) or a new one? Where does the certificate come from? | The web tier speaks plain HTTP only (DEPLOYMENT.md section 8) |
| 4 | Who manages DNS for that name, and how do you change it? | Cutover is a DNS change |
| 5 | Where does the data live, and how much disk can it use? [local disk on the host, 100 GB+] | Blob store and MongoDB; SSD preferred for MongoDB |
| 6 | Sign-in: local accounts only, LDAP / Active Directory, OIDC (Keycloak, authentik, Entra ID, ...)? Which directory groups become administrators and readers? | Providers and group mapping (section 7) |
| 7 | May anonymous users pull from the proxies? [yes, proxies only] | Nexus' "anonymous read" equivalent |
| 8 | Which upstreams and formats are needed (Docker Hub, GHCR, npm, PyPI, Maven, apt, yum, ...)? Is there an existing Nexus to import from? | Repositories (section 8) |
| 9 | Which client machines should be switched, how do you reach them, and may you change their package-manager config? | Cutover (section 11) |
| 10 | Monitoring (Prometheus/Grafana, Zabbix, none) and where backups should go? | Section 12 |
| 11 | Is there an existing registry to replace? Keep its host name? Keep it running for how long? | Rollback plan |

## 2. Preflight checks on the target host

Run these and report the results before deciding anything else.

| Check | Command | Pass |
|---|---|---|
| Docker and Compose | `docker version --format '{{.Server.Version}}'`; `docker compose version` | Engine 24+, Compose v2.20+ |
| Architecture | `uname -m` | `x86_64` or `aarch64` |
| CPU for MongoDB 8 | x86-64: `grep -m1 -wo avx /proc/cpuinfo` | prints `avx` (MongoDB 8 needs AVX on x86-64 and ARMv8.2-A on arm64; a Raspberry Pi 4 is too old, a Pi 5 is fine) |
| Memory | `free -g` | 4 GiB free minimum, 8 GiB comfortable (sizing: DEPLOYMENT.md section 2) |
| Disk | `df -h <data path>` | room for the cache plus the free-space guard (default 5 GiB, set it higher on small disks) |
| Filesystem | `stat -f -c %T <data path>` | ext4/xfs fine; btrfs: disable copy-on-write for the MongoDB directory (section 4) |
| Port free | `ss -ltn` (or `netstat -tln`) | the port you will publish (8080 by default) is free |
| Outbound HTTPS | `curl -sI https://registry-1.docker.io/v2/` (and each upstream) | an HTTP answer (401 is fine for Docker Hub) |
| Docker address pools | `docker network inspect bridge -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}'` | the compose networks must not collide with routed LAN or VPN ranges; if they might, pick explicit subnets (`WAYSTATION_SUBNET`, `WAYSTATION_BACKEND_SUBNET`) |
| Kernel | `uname -r` | anything Docker 24 runs on; Synology DSM 7's 4.4 kernel works |

A NAS works well when its CPU and RAM allow it, but check what else it does: a busy NAS (VM disks, nightly
backups) makes cache misses slower, and an SSD write-back cache in front of disks wears faster under MongoDB's
small writes. Prefer a volume on SSDs or local disks with little other write load.

## 3. Get the images

Released images are public on GitHub Container Registry, for `linux/amd64` and `linux/arm64`, with an SBOM and
signed build provenance:

```sh
for role in server worker web; do docker pull ghcr.io/slinkysoftware/waystation-$role:0.2.2; done
```

Use them in the env file (`WAYSTATION_IMAGE_PREFIX=ghcr.io/slinkysoftware`, `WAYSTATION_IMAGE_TAG=0.2.2`). Pin an
exact release, never `latest`, so an upgrade is always an explicit change. To check the provenance:
`gh attestation verify oci://ghcr.io/slinkysoftware/waystation-server:0.2.2 --owner SlinkySoftware`.

You also need `Build/Containers/compose.yaml` (and for backups `Build/Containers/backup/waystation-backup.sh`) from
the same release: take them from the release's source archive or the tag
(`https://github.com/SlinkySoftware/Waystation/tree/v0.2.2`).

**Build from source instead** (an unreleased commit, a fork, or a host without access to GHCR). Build on the target
if it has the CPU and network for it, otherwise on any machine with Docker and copy the images over (about 650 MB):

```sh
git clone https://github.com/SlinkySoftware/Waystation.git && cd Waystation
TAG=sha-$(git rev-parse --short=12 HEAD)
for role in server worker web; do
  docker build -f Build/Containers/$role/Dockerfile -t waystation/waystation-$role:$TAG .
done
# with make available, the same: make images IMAGE_PREFIX=waystation IMAGE_TAG=$TAG
docker save waystation/waystation-{server,worker,web}:$TAG | gzip -1 | ssh <user>@<target> 'gunzip | docker load'
```

Then use `WAYSTATION_IMAGE_PREFIX=waystation` and `WAYSTATION_IMAGE_TAG=$TAG`. Check: `docker images | grep
waystation-` on the target lists the three images with the tag you set.

## 4. Lay out the deployment directory

One directory on the target holds everything (the examples use `/srv/waystation`; on Synology use a shared folder
such as `/volume1/docker/waystation`). None of it goes into version control.

```sh
D=/srv/waystation
sudo install -d -m 0755 $D $D/data
sudo install -d -m 0750 -o root -g 10001 $D/secrets      # 10001 = WAYSTATION_GID
sudo install -d -m 0700 -o 999 -g 999 $D/mongodb          # MongoDB runs as 999
sudo chattr +C $D/mongodb 2>/dev/null || true             # btrfs only: no copy-on-write for the database
sudo cp Build/Containers/compose.yaml $D/compose.yaml     # from the release (or the checkout you built)
```

### Secrets

Generate each secret into its own file. **Use `base64 -w0` (or `openssl rand -base64 32`)**: plain `base64` wraps
lines at 76 characters, and a wrapped value silently becomes two lines.

```sh
umask 077
head -c 32 /dev/urandom | base64 -w0 | sudo tee $D/secrets/secret_keys >/dev/null
head -c 32 /dev/urandom | base64 -w0 | sudo tee $D/secrets/session_secret >/dev/null
head -c 24 /dev/urandom | base64 -w0 | tr -d '/+=' | sudo tee $D/secrets/admin_password >/dev/null
# only if used: the LDAP bind password and the OIDC client secret, written the same way (no trailing newline)
sudo chown root:10001 $D/secrets/* && sudo chmod 0640 $D/secrets/*
```

Give the owner a copy of `secret_keys` for safekeeping: it encrypts stored credentials and the registry token key,
and a database backup is only partly usable without it (BACKUP.md).

### Env file

`$D/waystation.env` (mode 0600, root): compose interpolation plus application settings. No secrets in it.

```sh
WAYSTATION_IMAGE_PREFIX=ghcr.io/slinkysoftware   # waystation for images built from source
WAYSTATION_IMAGE_TAG=0.2.2
WAYSTATION_HTTP_BIND=127.0.0.1        # 127.0.0.1 when the reverse proxy runs on this host; else the host's address
WAYSTATION_HTTP_PORT=8080
WAYSTATION_DATA_MOUNT_TYPE=bind
WAYSTATION_DATA_SOURCE=/srv/waystation/data
WAYSTATION_MONGO_MOUNT_TYPE=bind
WAYSTATION_MONGO_DIR=/srv/waystation/mongodb
# WAYSTATION_SUBNET=<an unused /24>          # only when Docker's default pools could collide with routed networks
# WAYSTATION_BACKEND_SUBNET=<another unused /24>

WAYSTATION_BASE_URL=https://registry.example.com
WAYSTATION_SECRET_KEYS_FILE=/run/waystation-secrets/secret_keys
WAYSTATION_SESSION_SECRET_FILE=/run/waystation-secrets/session_secret
WAYSTATION_BOOTSTRAP_ADMIN_PASSWORD_FILE=/run/waystation-secrets/admin_password
WAYSTATION_PROVISION_FILE=/etc/waystation/provision.yaml
WAYSTATION_TRUST_FORWARDED=true
WAYSTATION_TRUSTED_PROXIES=<see section 6>
WAYSTATION_METRICS_ALLOW_CIDRS=127.0.0.0/8,::1/128     # add the monitoring server (both its IPv4 and IPv6 address)
WAYSTATION_MIN_FREE_GB=20
```

### Compose override

`$D/compose.override.yaml` mounts the secrets and the provisioning file, and (optional, recommended on a shared host
or a NAS) caps memory so Waystation never starves other services:

```yaml
x-mounts: &mounts
  - {type: bind, source: /srv/waystation/secrets, target: /run/waystation-secrets, read_only: true}
  - {type: bind, source: /srv/waystation/provision.yaml, target: /etc/waystation/provision.yaml, read_only: true}
services:
  server: {volumes: *mounts, mem_limit: 1536m}
  worker: {volumes: *mounts, mem_limit: 1g}
  init: {volumes: *mounts}
  web: {mem_limit: 256m}
  valkey: {mem_limit: 256m}
  mongodb:
    mem_limit: 2g
    command: ["mongod", "--replSet", "rs0", "--bind_ip_all", "--quiet", "--wiredTigerCacheSizeGB", "0.75"]
```

## 5. The provisioning file

`$D/provision.yaml` (root:10001, 0640) declares sign-in providers, policies and settings; `init` applies it at every
start ([PROVISIONING.md](PROVISIONING.md), full example in `provisioning.example.yaml`, schema in
`provisioning.schema.json`). Secrets are file references, never values. Start small:

```yaml
version: 1
cleanup_policies:
  - name: unused-90-days
    description: Remove cached content nobody downloaded for 90 days
    format: '*'
    criteria: {last_downloaded_older_than_days: 90}
    schedule: '30 3 * * *'
    enabled: true
settings:
  instance_name: Waystation
  anonymous_access: [anonymous]          # built-in role: browse + read on proxy repositories; [] = none
  storage_guard: {min_free_bytes: 21474836480}
# auth_providers: added in section 7
# repositories: either here, in the UI, or imported from Nexus (section 8)
```

Validate it before the first start: `make provision-check` from the checkout (or start the stack: `init` refuses an
invalid file and nothing else starts, so the error is in `docker logs waystation-init`).

## 6. Start, then put TLS in front

```sh
cd /srv/waystation
sudo docker compose --env-file waystation.env config --quiet          # interpolation check
sudo docker compose --env-file waystation.env up -d --wait
sudo docker compose --env-file waystation.env ps                      # every service healthy, init exited 0
sudo docker logs waystation-init 2>&1 | tail -5                       # "init complete"
curl -s http://127.0.0.1:8080/readyz                                  # {"status":"ok"}
```

Then configure the reverse proxy for `registry.example.com` → `http://127.0.0.1:8080` with the settings in
[REVERSE_PROXIES.md](REVERSE_PROXIES.md): no request body limit, no request or response buffering, read and send
timeouts of at least 900 s, HTTP/1.1 to the backend, `X-Forwarded-For` and `X-Forwarded-Proto` set.

**`WAYSTATION_TRUSTED_PROXIES` is the proxy's address as the web container sees it**, which is often not the
proxy's LAN address:

| Proxy | Address to trust |
|---|---|
| On the same host, connecting to the published port (DSM, host nginx, host Caddy) | the gateway of the `waystation_frontend` network: `docker network inspect waystation_frontend -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}'` |
| A container on a shared Docker network with `web` | that container's address on the network (or the network's subnet) |
| Another machine | that machine's address |

Restart after changing it (`up -d`). Check: through the proxy,
`curl -s https://registry.example.com/readyz` is `{"status":"ok"}` with a valid certificate, and
`curl -sI https://registry.example.com/v2/` answers `401` with
`WWW-Authenticate: Bearer realm="https://registry.example.com/auth/token"` (the realm must use the public name; if
not, `WAYSTATION_BASE_URL` is wrong).

**Synology DSM**: Control Panel → Login Portal → Advanced → Reverse Proxy (settings in REVERSE_PROXIES.md section 7;
DSM 7.4 needs no custom nginx changes). To script it, call `synowebapi --exec api=SYNO.Core.AppPortal.ReverseProxy
method=create version=1 entry=<json>` from a Python script run as root on the NAS (`ssh root@nas python3 -`): JSON
in `entry=` does not survive the NAS shell's quoting. Bind the certificate to the new rule in Control Panel →
Security → Certificate → Settings.

## 7. Sign-in

The bootstrap administrator is `admin` with the password from `secrets/admin_password`. Keep it as break-glass.

**LDAP / Active Directory**: add an `auth_providers` entry of `kind: ldap` (servers, `security: ldaps`, `ca_pem` for a
private CA, bind DN with `bind_password: {file: ...}`, user base and attribute, `groups.mode: member_of`,
`require_group` to limit who may sign in) and a `group_mapping` of directory group DNs to roles (`admin`, `reader`,
`deployer`, `viewer` or custom roles). Then start the stack again (`up -d` re-runs init).

**OIDC**: the callback URL contains the provider's id, which Waystation assigns when the provider is created:
`<base URL>/auth/oidc/<provider id>/callback`. Two ways to register it at the identity provider:

1. Register a **regex** redirect URI if the IdP supports it (authentik, Keycloak with wildcards):
   `^https://registry\.example\.com/auth/oidc/[A-Za-z0-9]+/callback$`. Then create the client at the IdP first,
   add the provider to the provisioning file (`issuer`, `client_id`, `client_secret: {file: ...}`,
   `scopes: [openid, email, profile]`, `username_claim`, `groups_claim`, `require_group`, `group_mapping`) and start
   again. Provisioning does not contact the issuer, so a wrong issuer only shows at sign-in: run the connection test
   below right after.
2. Otherwise create the provider in Waystation, read the exact callback from
   `GET /api/v1/auth-providers/<id>` (`oidc.redirect_uri`) and register that at the IdP.

Post-logout redirect: `<base URL>/ui/sign-in`.

Check both providers without a user's password: `POST /api/v1/auth-providers/test-connection` and
`.../test-user` (appendix A) must answer `ok: true` (LDAP: connect, TLS, bind, user found, groups; OIDC: discovery,
JWKS, algorithms, PKCE). The only step that needs a person is one interactive sign-in through the IdP: ask the owner
to do it and confirm.

## 8. Repositories

Three ways, mix as needed:

- **Import from Nexus 3** (MIGRATING_FROM_NEXUS.md): `POST /api/v1/import/nexus/preview`, review every item and
  difference with the owner, then `POST /api/v1/import/nexus/apply` with the selected `repositories` (appendix A). A
  Nexus on a private address needs `allow_private: true`; a private CA goes in `ca_pem`. Upstream credentials
  (Docker Hub accounts, tokens) are never imported: re-enter them per repository.
- **Provisioning file** `repositories:` section (keeps the configuration in a file the owner can version).
- **The UI**: Repositories → Create.

For Docker, set the **default docker repository** to the Docker Hub proxy (unprefixed pulls and the daemon's
`registry-mirrors` setting go there) and give proxies their upstream **alias** (`docker.io`, `ghcr.io`) so
`registry.example.com/ghcr.io/<owner>/<image>` works. Attach the cleanup policy to every repository
(`PUT /api/v1/repositories/<name>/cleanup`). Each repository's "Set me up" panel shows the client configuration.

## 9. Validate before switching any client

Test every format the owner uses with a real client in a throwaway container, so no host changes yet. The private CA
(if any) is mounted read-only into each container.

```sh
R=registry.example.com
docker pull $R/library/alpine:3.22 && docker pull $R/ghcr.io/<owner>/<image>:<tag>      # aliases + default repo
docker run --rm debian:trixie bash -c "apt-get -qq update && apt-get -qq install -y ca-certificates >/dev/null &&
  rm -f /etc/apt/sources.list.d/* && printf 'Types: deb\nURIs: https://$R/repository/debian\nSuites: trixie trixie-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' > /etc/apt/sources.list.d/d.sources &&
  apt-get update && apt-get download hello"
docker run --rm rockylinux/rockylinux:10 bash -c "rm -f /etc/yum.repos.d/*.repo &&
  printf '[b]\nname=b\nbaseurl=https://$R/repository/rocky/\$releasever/BaseOS/\$basearch/os/\ngpgcheck=1\n' > /etc/yum.repos.d/b.repo &&
  dnf -q makecache && dnf -y -q install tree"
docker run --rm node:22 bash -c "cd /tmp && npm init -y >/dev/null && npm install --registry=https://$R/repository/npm/ lodash &&
  npm view --registry=https://$R/repository/npm/ lodash@4.17.21 version"
```

Also:

- **One large pull** through the reverse proxy (an image of 1 GB or more): a stall or `413` means the proxy buffers or
  limits bodies.
- **Side by side with the old registry** when replacing one: `Build/migration/nexus_compare.py` with a checks file
  of the paths clients really request (MIGRATING_FROM_NEXUS.md section 6, expected differences listed there).
- **arm64 clients** (Raspberry Pi OS) and other architectures the owner runs: an apt update on one of them with a
  throwaway state directory (`-o Dir::State::Lists=<tmp> -o Dir::Cache=<tmp> -o Dir::Etc::SourceParts=<tmp>`).

## 10. Keep the host name when replacing a registry

If Waystation replaces Nexus (or another registry) at the same host name and paths (`/repository/<name>/...`), clients
need no change at all: the cutover is moving the name.

1. Make the new front door answer for the old name first (reverse proxy rule and certificate on the new host), and
   test it with `curl --resolve registry.example.com:443:192.0.2.10 https://registry.example.com/readyz`.
2. Move DNS. Prefer A and AAAA records for the new host over a CNAME when the new host's own name has no AAAA: clients
   that reached the old registry over IPv6 keep doing so.
3. Check every resolver clients use (primary, secondaries, the router's cache) and flush caching resolvers; record
   TTLs can be a day.
4. Point the **old** front door at the new host for at least one TTL, so clients with a cached answer keep working,
   then stop the old registry (stop, do not delete) and keep it until the owner decides.

## 11. Switch and verify the clients

Per-format client configuration is in [clients/](clients/) (Docker daemon `registry-mirrors`, containerd
`hosts.toml`, apt `.sources`, dnf `.repo`, npm/yarn/pnpm, pip, Maven, Go, Helm, Terraform, Galaxy). Back up each
file before changing it, change one machine first, then the rest. On every machine verify with the client itself:

| Format | Check on the client |
|---|---|
| apt | `sudo apt-get update` shows `Get:`/`Hit:` lines from the registry and no `Err:` |
| dnf/yum | `sudo dnf makecache --refresh` succeeds |
| Docker | `docker pull busybox:1.37`, then the request appears in `docker logs waystation-web` (otherwise the daemon fell back to Docker Hub) |
| npm | `npm view lodash version` |

Machines that are off during the cutover follow DNS when they come back; list them for the owner.

## 12. Monitoring, backups, upgrades

- **Monitoring** ([MONITORING.md](MONITORING.md)): Prometheus scrape or the Zabbix template
  `Build/monitoring/zabbix/waystation_by_http.yaml` with `{$WAYSTATION.URL}`. Add the scraper to
  `WAYSTATION_METRICS_ALLOW_CIDRS` with **both** its IPv4 and IPv6 addresses (once the registry name has an AAAA
  record, the scraper usually connects over IPv6), or use a `metrics` API token. Check: the scraper gets `200` from
  `/metrics`.
- **Backups** ([BACKUP.md](BACKUP.md)): copy `Build/Containers/backup/waystation-backup.sh` to the host and run
  `waystation-backup.sh backup --target <dir> --hosted-only --keep 14` once by hand (proxy caches are re-fetched after
  a restore; hosted content and the full database are kept). Then schedule it daily (systemd timer, cron, or the NAS
  task scheduler), with the owner's agreement, and suggest an off-host copy.
- **Upgrades**: read the release notes, set `WAYSTATION_IMAGE_TAG` to the new release (or build the new commit), `docker compose --env-file waystation.env up
  -d --wait`. `init` migrates the schema; check `/readyz` and one client per format.

## 13. Report back

Give the owner, in this order:

1. What runs where: host, directory, image tag, URL, published port, reverse proxy rule, certificate and its renewal.
2. Where every secret is (file names only, never values) and that `secret_keys` needs an offline copy.
3. Sign-in: providers, group → role mapping, the break-glass account, whether the interactive sign-in was confirmed.
4. Repositories created or imported, and differences from the old registry.
5. Every machine switched and its verification result; machines not checked and why.
6. Monitoring and backup state (scheduled or not).
7. Rollback: the exact steps and what was kept to make them possible.
8. Open items for the owner.

## Appendix A: API calls for scripting

Sign in as the bootstrap admin and keep the CSRF token: unsafe requests with a session cookie must send it back in
`X-CSRF-Token` (it is in the sign-in response and in the readable `ws_csrf` cookie).

```python
import http.cookiejar, json, urllib.request
BASE = 'https://registry.example.com'
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
csrf = None

def call(method, path, body=None):
    headers = {'Content-Type': 'application/json', **({'X-CSRF-Token': csrf} if csrf else {})}
    req = urllib.request.Request(BASE + path, method=method, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with op.open(req, timeout=300) as r:
            return r.status, json.loads(r.read() or b'null')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'null')

status, _ = call('POST', '/api/v1/auth/login', {'username': 'admin', 'password': open('admin_password').read()})
csrf = next(c.value for c in jar if c.name == 'ws_csrf')
```

| Call | Use |
|---|---|
| `GET /api/v1/repositories?limit=100` | list repositories |
| `POST /api/v1/import/nexus/preview` body `{url, username, password, allow_private, ca_pem, docker_aliases}` | dry-run import (credentials used for this request only) |
| `POST /api/v1/import/nexus/apply` same body + `repositories: [names]` | import the selection |
| `GET /api/v1/cleanup-policies`, `PUT /api/v1/repositories/<name>/cleanup` body `{policy_ids: [...]}` | attach cleanup policies |
| `GET /api/v1/auth-providers`, `GET /api/v1/auth-providers/<id>` | providers; `oidc.redirect_uri` is the callback to register |
| `POST /api/v1/auth-providers/test-connection` body `{id, provider}` | test a provider (`provider` = the provider's config as `GET` returns it, minus the read-only fields `*_set`, `redirect_uri`, `post_logout_redirect_uri`) |
| `POST /api/v1/auth-providers/test-user` body `{id, provider, username}` | look a user up (and their groups) without their password |

Some identity providers sit behind Cloudflare, which can block Python's default `User-Agent`; use `curl` or set a
browser-like `User-Agent` for calls to the IdP itself (Waystation's own API is not affected).

## Appendix B: problems found in real deployments

| Symptom | Cause | Fix |
|---|---|---|
| A secret is "two lines" or a sourced env file runs a fragment as a command | `base64` wrapped at 76 characters | `base64 -w0` |
| `/metrics` 404 for the monitoring server, rate limits or audit show the wrong client | `WAYSTATION_TRUSTED_PROXIES` set to the proxy's LAN address instead of the address the container sees; or the client arrives over IPv6 | section 6 table; allow both IPv4 and IPv6 addresses |
| `docker pull` works but nothing shows in Waystation's logs | the daemon fell back to Docker Hub (`registry-mirrors` falls back silently) | check `docker info` mirrors and DNS on that machine |
| Clients still reach the old registry after the DNS change | a caching resolver (often the router) kept the old answer | flush it; forward the old front door to the new host (section 10) |
| MongoDB slow or the SSD wearing on btrfs | copy-on-write on database files | `chattr +C` on the empty MongoDB directory before the first start |
| The OIDC button fails or the IdP shows "Redirect URI Error" | issuer or client wrong, or the callback (with the provider id) not registered | `test-connection`; register the exact or regex callback (section 7) |
| pnpm/Yarn via corepack fail with 404 on `<registry>/<pkg>/<version>` | an older Waystation build without npm version documents | upgrade (served since 2026-11-11) |
| yum `repomd.xml` differs from Nexus in a side-by-side check | Nexus removes the `*_db` entries; Waystation serves the upstream file unchanged | expected; compare with the upstream instead |
| Gitea Actions: `make` targets fail with `can't open file '/src/...'` | act_runner's default volume workspaces leave bind mounts empty | runner `container.bind_workdir: true` (CI.md) |
| Synology: `scp` fails | DSM's root has no SFTP subsystem | stream files: `ssh root@nas 'cat > file' < file` |
| Synology: commands not found over SSH | non-interactive PATH lacks `/usr/local/bin` | call `/usr/local/bin/docker` |
