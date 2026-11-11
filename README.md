# Waystation

**Every package, one stop.** Waystation is a free, open-source, self-hosted **artifact repository** and
**package registry**: one fast pull-through cache for Docker Hub, GHCR, npm, PyPI, Maven Central, Debian and Ubuntu
apt, RPM yum/dnf, Go modules, Helm charts, Terraform providers and Ansible Galaxy, plus a private registry for the
images and packages you build. It is a modern, lightweight **alternative to Sonatype Nexus Repository and JFrog
Artifactory**, with no component caps, no request limits, no licence keys and no mandatory telemetry.

Run it on a home lab server, a NAS, a small office box or a CI fleet, point your clients at it, and every
`docker pull`, `npm install`, `pip install`, `apt upgrade` and `dnf update` after the first one comes straight from
your own disk, at LAN speed, even when Docker Hub rate-limits you or the upstream is down.

```sh
docker pull ghcr.io/slinkysoftware/waystation-server:0.2.1     # multi-arch: linux/amd64 and linux/arm64
```

## Why Waystation

- **One registry for everything.** A Docker registry mirror, an npm registry, a PyPI mirror, an apt and yum mirror,
  a Maven proxy, a Go module proxy, a Helm chart repository and a Terraform provider mirror, all at one URL with one
  sign-in.
- **Save bandwidth, beat rate limits.** Cache Docker Hub pulls once for the whole network; daemon `registry-mirrors`
  mode works out of the box. Identical files are stored once (content-addressed, de-duplicated).
- **Fast by design.** An async Python core with nginx serving cached files directly (`X-Accel-Redirect`), request
  coalescing so a hundred clients asking for the same layer cause one upstream fetch, and downloads that finish
  caching even when the client that started them gives up.
- **Safe by default.** Every digest and checksum is verified, sizes are bounded, paths are normalised, redirects to
  private addresses are refused (SSRF guard), and signed apt/yum metadata passes through byte for byte.
- **See it happen.** A live dashboard: transfers in flight, cache hit ratio, bandwidth saved, upstream health,
  per-repository and per-package statistics, an activity feed and an audit log.
- **Sign in your way.** Local accounts with TOTP two-factor, LDAP / Active Directory and OIDC single sign-on
  (Keycloak, authentik, Entra ID, ...), group-to-role mapping and role-based access control down to the repository.
  API tokens and service accounts for CI.
- **Built to operate.** Cleanup policies, garbage collection, quotas and a low-disk guard, backups and restores,
  webhooks, Prometheus metrics with a Grafana dashboard and a Zabbix template, and declarative provisioning from a
  YAML file.
- **Leaving Nexus?** The built-in importer reads your Nexus 3 repositories (names, upstreams, Docker aliases) and a
  side-by-side comparison script checks the two before you switch. Keep your host name and repository paths, and
  your clients don't change at all.

## Formats

| Format | Proxy (cache) | Hosted (private) | Group | Clients |
|---|---|---|---|---|
| Docker / OCI (Docker Hub, GHCR, Quay, any registry) | yes | yes | yes | docker, containerd (`hosts.toml`), skopeo, crane |
| npm | yes | yes | yes | npm, yarn, pnpm (corepack) |
| PyPI | yes | yes | yes | pip, uv, twine |
| Maven | yes | yes | yes | mvn |
| apt (Debian, Ubuntu, Raspberry Pi OS) | yes | planned | | apt |
| yum / dnf (Rocky, Alma, RHEL, Oracle, Fedora, EPEL) | yes | yes | | dnf, yum |
| Helm charts | yes | yes | yes | helm |
| Go modules | yes | | | go (GOPROXY) |
| Terraform / OpenTofu providers | yes (network mirror) | | | terraform, tofu |
| Ansible Galaxy collections | yes | yes | yes | ansible-galaxy |
| Raw files (GitHub releases, any HTTP) | yes | yes | yes | curl, wget |

Client setup for every format is in [`Documents/Operations/clients/`](Documents/Operations/clients/), and each
repository's "Set me up" panel in the UI shows the exact configuration. Coming next: NuGet, Cargo, RubyGems,
Composer, Conda, Alpine apk, Hugging Face, Swift packages, Terraform modules.

## Get started

**Try it in ten minutes** on any machine with Docker: [`Documents/Operations/TRY_IT.md`](Documents/Operations/TRY_IT.md).

**Run it for real** (TLS, sign-in, backups, monitoring):
[`Documents/Operations/DEPLOYMENT.md`](Documents/Operations/DEPLOYMENT.md) and the reverse-proxy recipes for nginx,
Traefik, Caddy, HAProxy and Synology DSM in
[`Documents/Operations/REVERSE_PROXIES.md`](Documents/Operations/REVERSE_PROXIES.md).

Requirements: Docker Engine 24+ with Compose v2.20+, x86-64 (with AVX) or arm64, 4 GB of RAM (8 GB comfortable) and
disk for the cache. It runs happily on a NAS such as Synology DSM 7 with Container Manager.

## Deploy it with an AI agent

Waystation ships a runbook written for AI coding agents (Claude Code, Codex, Cursor, Aider, ...) and for people who
like checklists. Point your agent at this repository and say:

> Deploy Waystation from https://github.com/SlinkySoftware/Waystation on `<host>`, at `https://registry.<your
> domain>`, behind `<your reverse proxy>`, with sign-in through `<your LDAP / OIDC provider>`. Replace my Nexus at
> `<url>` and switch my machines over.

The agent follows [`Documents/Operations/AGENT_DEPLOYMENT.md`](Documents/Operations/AGENT_DEPLOYMENT.md): it asks you
the questions that only you can answer (host, DNS, TLS, sign-in, which machines it may change), runs preflight
checks, installs the released images, provisions sign-in and repositories, imports from Nexus if you have one, tests
every client format in throwaway containers before touching a real machine, cuts over DNS with a fallback for stale
caches, sets up monitoring and backups, and reports back with a rollback plan. It never prints your secrets and asks
before anything destructive.

## Status

**0.2 is the first public preview series** (latest: 0.2.1). It is used in production to replace a Nexus Repository instance in a home lab
(Docker Hub and GHCR mirrors, apt, yum and npm for a mixed fleet of Debian, Ubuntu, Raspberry Pi OS, Rocky, Alma and
Oracle Linux machines), but it is pre-1.0: interfaces may still change between minor releases, and the 1.0 security
review is still to come. See [`Documents/SPECIFICATION.md`](Documents/SPECIFICATION.md) for exactly what it does
today, [`Documents/TASKS.md`](Documents/TASKS.md) for the road map and
[`Documents/CHANGELOG.md`](Documents/CHANGELOG.md) for what changed.

Issues, ideas and pull requests are welcome.

## Architecture at a glance

```
clients ──► web (nginx) ──► server (FastAPI, async) ──► upstream registries
              │  ▲              │        │
              │  └ X-Accel ─────┘        ├──► MongoDB   (metadata, stats, users, audit)
              └── blob store (ro)        ├──► Valkey    (locks, live counters, events, jobs)
                                         └──► blob store (rw)  ◄── workers (indexing, cleanup, GC, rollups)
```

One directory holds every cached and hosted file (`WAYSTATION_DATA_DIR`); mount it from local disk or a NAS. The UI
is Vue 3 / Quasar, talking only to the documented REST API (`/api/v1`, OpenAPI) and a live event stream.

## Development

Everything runs in Docker; you need only `docker` and `make`. `make help` lists the targets.

```bash
make check   # format check, lint + architecture contracts, mypy strict, unit, integration and frontend tests
make dev-up  # the full stack with hot reload
make hooks   # install the pre-commit hook
```

Contributor and coding-agent rules are in [`CLAUDE.md`](CLAUDE.md) and
[`Documents/Architecture/ARCHITECTURE.md`](Documents/Architecture/ARCHITECTURE.md).

## Licence

GPL-3.0-or-later. See [`LICENSE`](LICENSE).

<sub>Keywords: self-hosted artifact repository, open source Nexus alternative, free Artifactory alternative, Docker
registry mirror, Docker Hub pull-through cache, Docker Hub rate limit, private container registry, npm registry
mirror, private npm registry, PyPI mirror, private PyPI server, apt mirror, apt cache, yum mirror, dnf repository,
Maven repository manager, Go module proxy, Helm chart repository, Terraform provider mirror, Ansible Galaxy server,
package proxy, home lab, homelab, NAS, Synology.</sub>
