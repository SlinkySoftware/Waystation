# ADR 0003: Nexus-compatible URL layout and Docker routing

Status: accepted (2026-11-08, approved by James).

## Context
The owner's machines are already configured for Nexus paths (`/repository/<name>/...`) behind one hostname, with
Docker reached at `/v2/` using prefixes (`<host>/ghcr.io/<owner>/<image>`) and Docker Hub as the unprefixed
default (daemon `registry-mirrors`). Migration should need no client changes.

## Decision
- Every format is served under `/repository/<repo>/` exactly like Nexus.
- Docker `/v2/` routing: first path segment = repository name, else a declared upstream alias of a docker proxy
  (`docker.io`, `ghcr.io`, ...), else the `default_docker_repo`. Dedicated port connectors stay optional.
- Repository names are chosen to match the existing Nexus names during migration (import tool keeps names).

## Consequences
- Cutover = point the reverse proxy at Waystation; clients unchanged.
- Repo names that collide with an upstream alias or with reserved roots (`api`, `ui`, `v2`, `auth`, `repository`)
  are rejected at creation.
