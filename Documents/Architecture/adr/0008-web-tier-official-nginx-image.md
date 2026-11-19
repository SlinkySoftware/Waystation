# ADR 0008: Web tier on the official nginx image, run as uid 101

Status: accepted (2026-11-19, approved by the owner). Implemented in T-F05 (release 0.2.4).

## Context
ARCHITECTURE §2 named "nginx (official image, unprivileged variant)" for `web`: `nginxinc/nginx-unprivileged`, which
runs as uid 101, listens on 8080 and keeps its pid and temp files in `/tmp`. On 2026-11-19 nginx 1.30.7 / 1.31.9
fixed CVE-2026-123007 (HTTP/2 heap overflow) and CVE-2026-123633 (HTTP/2 use-after-free). The official `nginx` image
published 1.30.7 the same day; `nginx-unprivileged` was still at 1.30.6 (and 1.31.7), so the web image could not
take the fix while it depended on that variant. The web tier serves HTTP/1.1 only, so neither bug was reachable
through it, but the image must not ship a known-vulnerable nginx.

Options:

1. **Wait for `nginx-unprivileged`.** No change, but security fixes land on the variant's schedule, not nginx's.
2. **Official `nginx` image, run as uid 101.** Everything the variant changed is already in our own config:
   `nginx.conf` sets `pid` and every `*_temp_path` to `/tmp`, the site listens on `WAYSTATION_WEB_PORT` (8080), and
   the image ends with `USER 101` (the official image's `nginx` user). The container keeps a read-only root,
   `tmpfs: [/tmp]` and `cap_drop: [ALL]`.

## Decision
Option 2. `Build/Containers/web/Dockerfile` builds on the official image, pinned to a patch release
(`nginx:1.30.7-alpine`) so a rebuild cannot pick up an older nginx. The perf origin (`tests/perf`) uses the same image
as uid 101 with its own `tests/perf/origin/nginx.conf`. ARCHITECTURE §2 reads "nginx (official image, run as the
unprivileged `nginx` user, uid 101)".

## Consequences
- nginx security releases reach the web image as soon as the official image publishes them; bumping the pinned tag
  is a one-line change.
- The web image no longer depends on the variant's defaults: `nginx.conf` must keep the pid and temp paths under
  `/tmp`, or nginx fails to start on the read-only root.
- Running as a non-root user and the hardening anchor are unchanged (the blob store group membership included).
