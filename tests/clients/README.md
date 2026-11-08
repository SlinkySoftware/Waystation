# Real-client harness

Runs the real package clients (curl, docker/skopeo, npm, apt, ...) in containers against a Waystation stack whose
upstreams are a **record/replay fake upstream**. SPECIFICATION §11; FORMATS.md "Definition of done for a format".

```
make clients-test                          # every format, replay (offline, deterministic) - the default
make clients-test FORMAT=npm               # one format
make clients-test FORMAT=npm MODE=record   # re-record fixtures from the live upstream
make clients-test FORMAT=npm MODE=live     # live upstream, store nothing (nightly smoke)
make clients-check                         # forbidden-strings + size check (part of make check)
STACK=1 make clients-test FORMAT=echo      # also start the Waystation stack for a format that doesn't need it
KEEP=1 make clients-test FORMAT=npm        # leave the containers and the run dir for debugging
```

Host needs only bash and docker. Each run is its own compose project, `waystation-harness-<checkout>-<format>`,
with its own container names and no published ports, so it never collides with `make dev-up` or another worktree.

## How it works

```
client-<x> container ──► waystation-web ──► server ──► https://registry.npmjs.org/...
        │                                                │ (extra_hosts: every HOSTS name -> fake-upstream)
        └── (echo only: straight to the upstream) ───────┴──► fake-upstream :80/:443 ──► fixtures (replay)
                                                                                    └─► live upstream (record/live)
```

- `run.sh` loads `Build/Containers/compose.yaml` + `compose.dev.yaml` (images built from this checkout) +
  `compose.harness.yaml` + a generated override, starts `fake-upstream`, then (if the format needs it) web, server,
  worker, mongodb and valkey, then runs the format's script in its client container.
- **Name resolution, not URL rewriting.** The fake upstream impersonates the public upstream hosts listed in
  `HOSTS`: every client and the server/worker get `extra_hosts` entries pointing those names at it, and it serves
  TLS with a per-run throwaway CA (generated at start-up, key discarded, never committed). So repo upstream URLs,
  absolute URLs in metadata (npm tarballs), token realms (Docker Hub) and CDN redirects all work unchanged. The
  server trusts the CA via `WAYSTATION_EXTRA_CA_FILE=/harness-ca/ca.crt` and gets
  `WAYSTATION_SSRF_ALLOW_CIDRS=<fake-upstream>/32`; clients find it at `$HARNESS_CA` (curl via `CURL_CA_BUNDLE`;
  Debian/Ubuntu images: `trust_harness_ca` from `lib.sh`).
- **Replay** serves only from `tests/fixtures/upstreams/<format>/`. A miss answers `502` *and* is written to the
  run's `misses.txt`; any miss fails the run even if the client tolerated it. Requests for a host not in `HOSTS`
  fail the same way.
- **Record** forwards to the live upstream (or to `ORIGINS` overrides), stores scrubbed fixtures in
  `tests/fixtures/upstreams/recordings-tmp/<format>/`, and only when the client script passed, nothing missed and
  `check_fixtures` is clean replaces `tests/fixtures/upstreams/<format>/` with them. Then run replay and commit.
- **Live** forwards and stores nothing.

## Adding a format

Create `tests/clients/<format>/`:

| File | Purpose |
|---|---|
| `format.env` | `HOSTS` (public upstream names to impersonate, include CDN redirect targets and token hosts), `CLIENT` (`curl`, `node`, `debian`, `ubuntu`, `rocky`, `alma`, `skopeo`, `docker`, or several separated by spaces, run in turn against one stack, each with `HARNESS_CLIENT` set; add a `client-<x>` service with a pinned tag to `compose.harness.yaml` for more), `NEEDS_STACK=1`, optional `ORIGINS`, `RECORD_SERVICES`, `DROP_JSON_KEYS` (JSON keys removed from recorded JSON bodies at any depth, e.g. npm readmes and maintainer e-mails), `FIXTURE_BUDGET_KB`, `FIXTURE_FILE_MAX_KB` |
| `setup.sh` | optional, runs first in the client container: create the repos the test needs through `$WAYSTATION_URL/api/v1/...` as the per-run bootstrap admin: `ws_login`, then `ws_api <METHOD> <path> [json]` from `lib.sh` (curl, or bash `/dev/tcp` in images without curl, e.g. debian/ubuntu; `_ws_http` is the bash request primitive and also leaves the response headers in `/tmp/ws-api.headers`). With several clients it runs once per client: keep it idempotent |
| `test.sh` | POSIX sh in the client container: configure the client to use `$WAYSTATION_URL/repository/<repo>/` (or `/v2/`), run real installs/pulls, assert, `exit 1` on failure. Source `/harness/lib.sh` for `step`, `fail`, `pass`, `wait_for`, `trust_harness_ca` |
| `allowed_hosts.txt` | optional: extra public hosts that legitimately appear in this format's upstream content |

The client container sees `tests/clients` at `/harness` (read-only), the CA at `/harness-ca/ca.crt`, and
`HARNESS_MODE`, `HARNESS_FORMAT`, `HARNESS_STACK`, `WAYSTATION_URL=http://waystation-web:8080`. The `docker` client
image is `docker:*-dind` plus curl and skopeo, with the entrypoint cleared: start `dockerd` in `setup.sh`
(with the stack in `insecure-registries`/`registry-mirrors`). Scripts must behave the same in every mode; assert only
on things that survive scrubbing. Then `make clients-test FORMAT=<f> MODE=record`, `make clients-test FORMAT=<f>`, commit
`tests/fixtures/upstreams/<f>/`.

`echo/` is the harness self-test: curl straight at a tiny test-only origin (`harness/echo_origin.py`, started
only in record/live mode) that sets cookies, issues tokens, redirects to signed URLs and answers 401/200/304, so
it exercises every scrubbing and keying rule.

## Fixtures and scrubbing

One exchange = `<host>/<method>-<last path segment>-<key16>.json` (request summary, status, kept headers, body
sha256) + `.body` (raw bytes, verified on load). The key hashes the **scrubbed** request: method, URL,
`Accept`, `Range`, `If-None-Match`, `If-Modified-Since`, `If-Range`, the *scheme* of `Authorization` (`none`,
`bearer`, `basic`; never the credential) and the request body hash. The same scrubbing runs at record and replay
time, so keys always line up. Upstream requests are made with `Accept-Encoding: identity`.

Scrubbed at record time (`harness/upstream_store.py`):
- Response headers are allowlisted (content type/length/range, ETag, Last-Modified, Cache-Control, Expires, Vary,
  Location, Link, WWW-Authenticate, Docker digest/API headers, `ratelimit-limit/remaining`). Cookies, request ids,
  CDN node names, client-IP headers (e.g. Docker Hub's rate-limit source) and everything else are dropped.
- Signed-URL query values (`X-Amz-*`, `Signature`, `Expires`, `token`, SAS params, ...) and URL credentials in
  `Location` and in keys are replaced with `harness-redacted`.
- JSON bodies: string values of keys containing `token`, `secret`, `password`, `credential`, `session`, `cookie`
  are replaced with `harness-redacted`, and the format's `DROP_JSON_KEYS` are removed (upstream bytes are kept
  untouched when nothing matched). Only metadata the client does not verify byte-for-byte may be scrubbed this way.

`make clients-check` (also run after every record, and in `make check`) scans `tests/clients/` and
`tests/fixtures/upstreams/` and fails on: host names outside the repository allowlist `Build/ci/allowed_hosts.txt`
(+ the format's own list; the same scanner runs repository-wide as `make forbidden-strings`,
`Documents/Operations/CI.md`),
internal-only suffixes (never allowable), IPs outside RFC 5737/3849/loopback, private keys and token shapes
(GitHub, npm, GitLab, Slack, AWS, JWT), strings from an optional local deny list
(`WAYSTATION_FORBIDDEN_FILE=/path/outside/the/repo`, e.g. your real domains), and size budgets. Host/IP scanning
covers text files only; binary bodies are scanned for key/token shapes.

**Size budget**: 8 MiB per format and 4 MiB per file by default (`FIXTURE_BUDGET_KB`, `FIXTURE_FILE_MAX_KB` in
`format.env`). Prefer tiny real artifacts: `hello-world`/`busybox` for docker, `is-number` for npm, a single small
`.deb` for apt. Raising a budget is a reviewed change. apt (owner decision 2026-11-08): fixtures are recorded
against small real suites only (`trixie-updates`, `trixie-security`, `resolute-updates`, `resolute-security`; one
component, amd64, compressed `Packages` only: no translations, contents or dep11) with a 32 MiB budget
(`tests/clients/apt/format.env`); full suites are covered by the nightly live run, not fixtures.
yum: fixtures come from the small `extras` repositories of Rocky Linux 10 and AlmaLinux 10 (a tiny release package
each, about 50 KB in total, within the default budget); BaseOS/AppStream and EPEL are left to the nightly live run.
