# Waystation

**Every package, one stop.** Waystation is a fast, self-hosted artifact repository: a pull-through cache for
public package ecosystems and a private registry for the things you build, with a live, modern web UI.

- **Proxy (cache)** Docker Hub, GHCR and any OCI registry, npm, PyPI, Maven, Debian/Ubuntu apt, RPM yum/dnf,
  Go modules, Helm, Terraform/OpenTofu providers and modules, Ansible Galaxy, Swift packages and more.
- **Host** your own container images, npm/PyPI/Maven packages, Helm charts, apt and yum repositories.
- **Group** proxies and hosted repositories behind one URL.
- **See it happen**: live transfers, cache hit ratios, bandwidth saved, upstream health and per-package stats.
- **Sign in** with local accounts, LDAP or OIDC; role-based access control down to the repository.
- **No caps**: no component or request limits, no mandatory telemetry.
- **Nexus-compatible URLs** (`/repository/<name>/...`) so existing clients can switch without reconfiguration.

Status: M0 feedback build (proxy repositories for Docker, npm, apt and raw). To run it, see
`Documents/Operations/TRY_IT.md`. The plan is in `Documents/SPECIFICATION.md` and `Documents/TASKS.md`.

## Architecture at a glance

```
clients ──► web (nginx) ──► server (FastAPI, async) ──► upstream registries
              │  ▲              │        │
              │  └ X-Accel ─────┘        ├──► MongoDB   (metadata, stats, users, audit)
              └── blob store (ro)        ├──► Valkey    (locks, live counters, events, jobs)
                                         └──► blob store (rw)  ◄── workers (indexing, cleanup, GC, rollups)
```

One directory holds every cached and hosted file (`WAYSTATION_DATA_DIR`); mount it from local disk or a NAS.

## Development

Everything runs in Docker; you need only `docker` and `make`. `make help` lists the targets.

```bash
make check   # format check, lint + architecture contracts, mypy strict, tests
make hooks   # install the pre-commit hook
```

## Licence

GPL-3.0-or-later. See `LICENSE`.
