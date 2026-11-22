# Dependency and container audit

How Waystation checks the components it ships for known vulnerabilities, the result of the release scan and the
findings accepted for the release. Rerun with `make security-scan` (Build/ci/security_scan.sh: pip-audit over `uv.lock`,
`npm audit --omit=dev` over `Application/Web/package-lock.json`, Trivy over the built server, worker and web images;
`SKIP_IMAGES=1` scans images already built, `IMAGE_PREFIX`/`IMAGE_TAG` name them). It fails on any pip-audit or npm
audit finding and on any HIGH/CRITICAL image finding that has a fix; findings without a fix are listed below.

## Scan for the v1.0 release

| Check | Tool | Scope | Result |
|---|---|---|---|
| Python dependencies | pip-audit 2.10.1 (PyPI advisory database + OSV) | `uv export --all-packages` of `uv.lock`: 72 locked packages, runtime and dev | No known vulnerabilities |
| SPA dependencies | npm 11.20.0 `npm audit --omit=dev` | `package-lock.json` (what the bundle contains); a full `npm audit` incl. dev tools also clean | 0 vulnerabilities |
| Server image | Trivy 0.76.2, HIGH/CRITICAL | `python:3.14-slim-trixie` (Debian 13.8) + the app venv | Before: 51 OS + 5 Python. After the fixes below: 50 OS, all without a fixed package; 0 Python |
| Worker image | Trivy 0.76.2 | same base | same as the server |
| Web image | Trivy 0.76.2 | `nginx:1.30.7-alpine` (Alpine 3.24.4) + SPA | Before: 1 (`libxml2`). After: 0 |

### Fixed

| Finding | Where | Fix |
|---|---|---|
| CVE-2026-85091 (zlib heap overflow in `gz_vacate`), fixed package available | Debian `zlib1g` in server/worker | `apt-get upgrade` in the final stage of the server and worker Dockerfiles (security updates published after the base image was built) |
| CVE-2026-86144, fixed package available | Alpine `libxml2` in web | `apk upgrade --no-cache` in the web Dockerfile |
| CVE-2026-114613, GHSA-6v7p-g79w-8964 (`msgpack`), CVE-2026-97687, CVE-2026-97689 (`urllib3`), CVE-2025-47273 (`setuptools`) | the base image's own pip (vendored copies) and `ensurepip` bundle, not Waystation's dependencies (pip-audit is clean) | pip, its vendored packages and `ensurepip` removed from the runtime images: nothing installs packages at run time |

### Accepted (no fixed package published; re-checked on every `make security-scan`)

All in the Debian base of the server and worker images. The containers run as uid 10001, `cap_drop: [ALL]`,
`no-new-privileges`, read-only root filesystem, and Waystation never runs these programs or loads these libraries
on a code path an attacker reaches.

| CVE | Packages | Why it is not reachable |
|---|---|---|
| CVE-2026-76642, CVE-2026-78408, CVE-2026-78409, CVE-2026-78410 | util-linux family (`mount`, `libmount1`, `libblkid1`, `login`, `bsdutils`, ...) | mount/nsenter privilege issues: no mount or namespace capability in the container, never executed |
| CVE-2026-54369 | `libacl1` | symlink traversal by privileged callers of libacl; Python does not use libacl, the process is unprivileged |
| CVE-2025-69720 | ncurses | terminal UI library, not loaded by the server or worker |
| CVE-2026-119373, CVE-2026-119374, CVE-2026-119379 | `libsqlite3-0` | FTS5 / session changesets; Waystation does not use SQLite (MongoDB + Valkey) |
| CVE-2026-124709 | OpenSSL 3.5 | memory leak in QUIC stream handling; Waystation's outbound and inbound HTTP use TCP + TLS only (HTTP/1.1, HTTP/2) |
| CVE-2026-16742 | `libsystemd0`, `libudev1` | systemd-homed privilege escalation; no systemd in the container |
| CVE-2026-9538 (fix deferred by Debian) | `perl-base` | Archive::Tar DoS; Perl is never run |

Replacing the Debian base by a smaller one (distroless, or Alpine for Python) would drop most of these packages; that
is an image change for after v1.0 and would need its own measurements (musl and the wheels in `uv.lock`).

## Container hardening (checked against the compose files)

| Control | server | worker | web | init | mongodb | valkey |
|---|---|---|---|---|---|---|
| Non-root user | 10001 (image `USER`) | 10001 | 101 | root, then drops to 10001 (fixes data-dir ownership) | 999 | 999 |
| `cap_drop: [ALL]` | yes | yes | yes | yes, `cap_add` CHOWN, FOWNER, DAC_OVERRIDE, SETUID, SETGID only | yes | yes |
| `no-new-privileges` | yes | yes | yes | yes | yes | yes |
| Read-only root filesystem | yes (tmpfs `/tmp`) | yes | yes | yes | no (mongod writes `/data/db`, its own files) | yes |
| Published port | none (standalone server file: the server port, for a separate web host) | none | 8080 (or 8443 with TLS) | none | none (internal network) | none (internal network) |

`Build/Containers/compose.yaml` applies these through the `x-hardening` anchor; the standalone files
(`compose.standalone.{server,worker,web}.yaml`) repeat them, and `test_compose_files.py` keeps the environment blocks
identical.
