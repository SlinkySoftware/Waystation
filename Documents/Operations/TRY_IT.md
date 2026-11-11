# Try Waystation in ten minutes

This guide gets Waystation running on one Docker host in about ten minutes. You sign in, create proxy repositories
for Docker Hub, npm, Debian and GitHub releases, point real clients at them, and watch the traffic on the dashboard.
It is a try-out setup: plain HTTP, one admin account, anonymous reads. Production deployment (TLS, LDAP/OIDC
sign-in, backups, monitoring) is covered in [DEPLOYMENT.md](DEPLOYMENT.md), and an AI agent can do it for you with
[AGENT_DEPLOYMENT.md](AGENT_DEPLOYMENT.md).

The examples use `registry.example.com` for the Docker host and port 8080. Replace both with your own values.
To try it on your own machine, use `localhost`.

## 1. What you need

- Docker Engine 24+ with Compose v2.20+ (`docker compose version`), on x86-64 or arm64.
- About 2 GB of RAM and some disk for the cache. Writes stop when free space drops below `WAYSTATION_MIN_FREE_GB`
  (default 5 GiB).
- Outbound HTTPS to the upstreams you proxy (registry-1.docker.io, auth.docker.io, registry.npmjs.org,
  deb.debian.org, security.debian.org, github.com and its download CDN).
- A copy of `Build/Containers/compose.yaml` from the release (a checkout of the tag, or the release's source archive).

## 2. Get the images

The release images are public on GitHub Container Registry (linux/amd64 and linux/arm64):

```sh
git clone --branch v0.2.1 https://github.com/SlinkySoftware/Waystation.git && cd Waystation
for role in server worker web; do docker pull ghcr.io/slinkysoftware/waystation-$role:0.2.1; done
```

To run an unreleased commit instead, build the images from the checkout (`make images IMAGE_TAG=dev`, Docker only)
and use `WAYSTATION_IMAGE_PREFIX=waystation` and `WAYSTATION_IMAGE_TAG=dev` below.

## 3. Create a directory and an env file outside the repository

```sh
mkdir -p ~/waystation && cp Build/Containers/compose.yaml ~/waystation/
cd ~/waystation
umask 077
cat > .env <<EOF
WAYSTATION_IMAGE_PREFIX=ghcr.io/slinkysoftware
WAYSTATION_IMAGE_TAG=0.2.1
WAYSTATION_HTTP_PORT=8080
# The URL clients use: it appears in "Set me up" snippets, docker pull names and registry tokens.
WAYSTATION_BASE_URL=http://registry.example.com:8080
WAYSTATION_LOG_LEVEL=INFO
WAYSTATION_SECRET_KEYS=$(head -c 32 /dev/urandom | base64)
WAYSTATION_SESSION_SECRET=$(head -c 32 /dev/urandom | base64)
EOF
```

`.env.example` in the repository lists every setting with its default. Every secret also has a `_FILE` form
(for example `WAYSTATION_SECRET_KEYS_FILE`). Keep `WAYSTATION_SECRET_KEYS` safe: stored upstream credentials are
encrypted with it, and they cannot be read without it.

If the default Docker address pools clash with your network, set `WAYSTATION_SUBNET` and `WAYSTATION_BACKEND_SUBNET`
(for example `192.0.2.0/24` and `198.51.100.0/24`).

## 4. Start the stack

```sh
docker compose up -d --wait
docker compose ps        # web, server, worker, mongodb, valkey are healthy; init exited 0
```

Only the `web` container publishes a port. MongoDB and Valkey are on an internal network. Data lives in the
`waystation_data`, `waystation_mongodb` and `waystation_valkey` volumes.

## 5. First sign-in

On the first start, `init` creates the `admin` account and prints a one-time password once:

```sh
docker compose logs init | grep 'one-time password'
```

**Save the password now** (in a password manager or a mode-600 file). Recreating the `init` container, which
happens when you upgrade the images, discards these logs. Change it after signing in (avatar menu > Profile).
To choose the password yourself, set `WAYSTATION_BOOTSTRAP_ADMIN_PASSWORD_FILE` before the first start. It is only
used while there are no users.

Open `http://registry.example.com:8080/ui/` and sign in as `admin`.

## 6. Create the four repositories

Go to **Repositories > Create repository**, pick a format, then **Proxy**, then a preset. Use **Test upstream**
to check the connection, then **Create repository**.

| Format | Preset | Suggested name | Notes |
|---|---|---|---|
| Docker / OCI | Docker Hub | `dockerhub-proxy` | Turn on **Default repository** so mirror-mode pulls (`registry-mirrors`) land here |
| npm | npmjs.org | `npm` | |
| apt | Debian | `debian` | Add a second repo `debian-security` with upstream `https://security.debian.org/debian-security` |
| Raw | GitHub releases | `raw-releases` | Any `https://github.com/<owner>/<repo>/releases/download/...` path |

Each repository page has a **Set me up** button with copy-paste client configuration for that repository.

`make e2e-m0` (below) creates the same set through the API if they are missing.

## 7. Point clients at it

Full guides: [Docker](clients/docker.md), [npm](clients/npm.md), [apt](clients/apt.md), [raw](clients/raw.md).
In short, with `WS=http://registry.example.com:8080`:

```sh
# Docker. Prefix pull; add registry.example.com:8080 to "insecure-registries" while the web tier has no TLS
# (not needed for localhost).
docker pull registry.example.com:8080/dockerhub-proxy/library/nginx:latest

# npm
npm config set registry $WS/repository/npm/
npm install chalk

# apt, in a Debian 13 container
docker run --rm -it -e WS=$WS debian:trixie sh -c '
  rm -f /etc/apt/sources.list.d/debian.sources
  echo "deb $WS/repository/debian/ trixie main" > /etc/apt/sources.list
  echo "deb $WS/repository/debian-security/ trixie-security main" >> /etc/apt/sources.list
  apt-get update && apt-get install -y curl'

# raw
curl -fLO $WS/repository/raw-releases/jqlang/jq/releases/download/jq-1.8.1/jq-linux-amd64
```

Open the dashboard while these run. Transfers show under **Live transfers** as they happen. Requests, hit ratio,
bandwidth saved and storage update within a few seconds, **Browse** lists what was cached, **Activity** shows
every request (filter it, or export CSV) and **Storage** shows volume usage, usage by format and the largest repositories.

## 8. Run the scripted end-to-end check (optional)

From the checkout, with the stack running and Docker available:

```sh
WAYSTATION_URL=http://localhost:8080 WAYSTATION_ADMIN_PASSWORD_FILE=/path/to/admin-password make e2e-m0
```

The script runs a real Docker pull of nginx, `apt update && apt install` in a Debian 13 container, an npm install
and a raw download through Waystation. It then checks the live event stream, stats, activity and browse. It needs
internet access and takes a few minutes the first time.

## 9. Stop, upgrade, reset

```sh
docker compose stop                 # stop; start again with: docker compose up -d --wait
docker compose down                 # remove the containers; data volumes are kept
docker compose up -d --wait         # after setting a newer WAYSTATION_IMAGE_TAG in .env (data kept)
docker compose down -v              # remove everything, including the cache, users and repositories
```

After `down -v` the next start is a fresh install with a new one-time admin password.

## Where to go next

- **More formats**: PyPI, Maven, Helm, Go modules, Terraform providers, Ansible Galaxy, yum/dnf; hosted (private)
  and group repositories for most of them. Client setup per format: [clients/](clients/).
- **Users and sign-in**: Administration > Users, Groups, Roles and Auth providers (LDAP / Active Directory, OIDC).
  Format clients that send credentials (basic or bearer auth on `/repository/...`) use an API token from your
  profile page, not your password. The proxies in this guide allow anonymous reads, so the examples need none.
- **HTTPS**: the web tier speaks plain HTTP; put a reverse proxy in front ([REVERSE_PROXIES.md](REVERSE_PROXIES.md)).
- **Production**: [DEPLOYMENT.md](DEPLOYMENT.md), [BACKUP.md](BACKUP.md), [MONITORING.md](MONITORING.md),
  [PROVISIONING.md](PROVISIONING.md); moving from Nexus: [MIGRATING_FROM_NEXUS.md](MIGRATING_FROM_NEXUS.md).
- Menu items marked "soon" are placeholders.

Send feedback in `Documents/Notes/UI_FEEDBACK.md`.
