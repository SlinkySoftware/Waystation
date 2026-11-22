# Real-client harness

Runs the real package clients (curl, docker/skopeo, npm, apt, ...) in containers against a Waystation stack whose
upstreams are a **record/replay fake upstream**. SPECIFICATION §11; FORMATS.md "Definition of done for a format".

```
make clients-test                          # every format, replay (offline, deterministic) - the default
make clients-test FORMAT=npm               # one format
make clients-test FORMAT=npm MODE=record   # re-record fixtures from the live upstream
make clients-test FORMAT=npm MODE=live     # live upstream, store nothing (live smoke)
make clients-check                         # forbidden-strings + size check (part of make check)
STACK=1 make clients-test FORMAT=echo      # also start the Waystation stack for a format that doesn't need it
KEEP=1 make clients-test FORMAT=npm        # leave the containers and the run dir for debugging
ARM64=1 make clients-test FORMAT=apt       # also run the format's arm64 clients (needs arm64 or qemu binfmt)
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
| `format.env` | `HOSTS` (public upstream names to impersonate, include CDN redirect targets and token hosts), `CLIENT` (`curl`, `node`, `nodepm` (pnpm, Yarn 1, Yarn 4 at `/opt/berry/yarn.cjs` and Bun on Node 24), `debian`, `ubuntu`, `rocky`, `alma`, `oracle` (Oracle Linux 9), `fedora` (dnf 5), `opensuse` (zypper), `skopeo`, `docker`, `podman` (podman 6, privileged), `conformance` (the OCI distribution-spec conformance suite), `maven` (Maven 3.9; its build plugins are pre-seeded from Maven Central into `/opt/m2-seed` at image build, so fixtures hold only what the test resolves), `jvm` (Maven 4 as `mvn4` with plugins in `/opt/m2-seed4`, Gradle 8 and 9 as `gradle8`/`gradle9`, sbt with its own artifacts pre-seeded; `maven/jvm/Dockerfile`, same JDK 21 base), `python` (pip, uv, twine, Poetry, PDM), `go` (the go command and curl), `ansible` (ansible-core's ansible-galaxy and curl), `dotnet` (the .NET 10 SDK: dotnet restore/build/pack, `dotnet nuget push/delete`, `dotnet package search`, curl), `composer` (the official composer image: PHP, composer, zip, unzip, curl), `ruby` (the official ruby image: gem, Bundler, bash), or several separated by spaces, run in turn against one stack, each with `HARNESS_CLIENT` set; add a `client-<x>` service with a pinned tag to `compose.harness.yaml` for more), `NEEDS_STACK=1`, optional `CLIENT_ARM64` (`linux/arm64` clients, e.g. `raspios`, run after `CLIENT` only with `ARM64=1` on a host that runs arm64 containers: an arm64 host, or `qemu-aarch64` registered in `binfmt_misc`; otherwise skipped with a message), `SERVICES` (extra harness services started in every mode, profile `idp`: `openldap`, `keycloak`), `STACK_ENV` (extra `WAYSTATION_*=value` settings for the stack), `ORIGINS`, `RECORD_SERVICES`, `DROP_JSON_KEYS` (JSON keys removed from recorded JSON bodies at any depth, e.g. npm readmes and maintainer e-mails), `KEEP_LINES` (space separated `<path>=<regex>`: a `200` text body at that request path is reduced to the lines that fully match when recorded, and record mode answers with the reduced body too; for huge line indexes such as RubyGems' `versions`), `FIXTURE_BUDGET_KB`, `FIXTURE_FILE_MAX_KB` |
| `setup.sh` | optional, runs first in the client container: create the repos the test needs through `$WAYSTATION_URL/api/v1/...` as the per-run bootstrap admin: `ws_login` (or `ws_login <user> <password> [<provider id>]` for another account), then `ws_api <METHOD> <path> [json]`, `ws_token`, `ws_revoke`, `ws_revoke_any` from `lib.sh` (curl, or bash `/dev/tcp` in images without curl, e.g. debian/ubuntu; `_ws_http` is the bash request primitive and also leaves the response headers in `/tmp/ws-api.headers`). With several clients it runs once per client: keep it idempotent |
| `test.sh` | POSIX sh in the client container: configure the client to use `$WAYSTATION_URL/repository/<repo>/` (or `/v2/`), run real installs/pulls, assert, `exit 1` on failure. Source `/harness/lib.sh` for `step`, `fail`, `pass`, `wait_for`, `trust_harness_ca` |
| `allowed_hosts.txt` | optional: extra public hosts that legitimately appear in this format's upstream content |

The client container sees `tests/clients` at `/harness` (read-only), the CA at `/harness-ca/ca.crt`, a per-run
scratch directory shared by the clients of one run at `/harness-shared` (e.g. tokens an earlier client created; deleted
with the run), the identity providers' throwaway CA at `/harness-idp/ca.crt` (when `SERVICES` starts them), and
`HARNESS_MODE`, `HARNESS_FORMAT`, `HARNESS_STACK`, `WAYSTATION_URL=http://waystation-web:8080`. The `docker` client
image is `docker:*-dind` plus curl, skopeo, crane, oras and cosign, with the entrypoint cleared: start `dockerd` in
`setup.sh` (with the stack in `insecure-registries`/`registry-mirrors`). The `conformance` client runs the OCI
distribution-spec conformance suite (`conformance.test`, built from the spec's release source) and curl. Scripts must behave the same in every mode; assert only
on things that survive scrubbing. Then `make clients-test FORMAT=<f> MODE=record`, `make clients-test FORMAT=<f>`, commit
`tests/fixtures/upstreams/<f>/`.

`echo/` is the harness self-test: curl straight at a tiny test-only origin (`harness/echo_origin.py`, started
only in record/live mode) that sets cookies, issues tokens, redirects to signed URLs and answers 401/200/304, so
it exercises every scrubbing and keying rule.

## Fixtures and scrubbing

One exchange = `<host>/<method>-<last path segment>-<key16>.json` (request summary, status, kept headers, body
sha256) + `.body` (raw bytes, verified on load). Fixtures recorded before 2026-11-10 dropped the key from names
with a dot in the last segment (`get-is-4.6.0.json`); they are still read, but only for the request they recorded.
The key hashes the **scrubbed** request: method, URL, `Accept`, `Range`, `If-None-Match`, `If-Modified-Since`, `If-Range`, the *scheme* of `Authorization` (`none`,
`bearer`, `basic`; never the credential) and the request body hash. The same scrubbing runs at record and replay
time, so keys always line up. Upstream requests are made with `Accept-Encoding: identity`; a gzip or deflate body an
upstream sends anyway (static files stored compressed, e.g. nuget.org's `registration5-gz` hives) is decoded before it is
stored (`upstream_store.identity_body`), so fixtures always hold identity bytes.

Scrubbed at record time (`harness/upstream_store.py`):
- Response headers are allowlisted (content type/length/range, ETag, Last-Modified, Cache-Control, Expires, Vary,
  Location, Link, WWW-Authenticate, Docker digest/API headers, `ratelimit-limit/remaining`, `X-Terraform-Get`).
  Cookies, request ids, CDN node names, client-IP headers (e.g. Docker Hub's rate-limit source) and everything else
  are dropped.
- Signed-URL query values (`X-Amz-*`, `Signature`, `Expires`, `token`, SAS params, ...) and URL credentials in
  `Location`, `X-Terraform-Get` and in keys are replaced with `harness-redacted`.
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
(`tests/clients/apt/format.env`); full suites are covered by the live run, not fixtures. The debian client
also runs `apt/hosted.sh`: a `.deb` built with dpkg-deb is uploaded to a hosted repository and installed by apt with
`Signed-By` the key Waystation serves (no upstream traffic; bash sends the uploads, debian-slim has no curl).
Maven: fixtures are hamcrest-library/hamcrest-core 1.3 and reactive-streams 1.0.3/1.0.4 from Maven Central (about
130 KB); everything Maven needs to build (plugins) comes from the client image. `client-maven` (mvn 3.9) runs first,
then `client-jvm`: Maven 4 (the same `maven/mvn.sh`), Gradle 8 and 9 (`maven/gradle.sh`: strict dependency
verification against the metadata written on the proxy run, a tampered checksum must fail, `maven-publish` release and
SNAPSHOT builds) and sbt (`maven/sbt.sh`: coursier with SHA-1 required, Maven-style publish); each publishes its own
artifactId. They resolve the same dependencies from Waystation's cache, so they add only one fixture: Central's 404
for the group's `maven-metadata.xml` lookup of Maven 4's artifactId. Plain HTTP needs `allowInsecureProtocol` in Gradle and sbt.
yum: fixtures come from small repositories: the `extras` repositories of Rocky Linux 10 and AlmaLinux 10, Oracle
Linux 9 `addons` and EPEL 10 `Everything` (dnf fetches its primary, updateinfo and comps: about 8 MB), with one tiny
package each (dnf 5 on Fedora and zypper on openSUSE Leap run smoke tests against the cached Rocky `extras`); budget raised to 16 MiB / 8 MiB per file (2026-11-10, `yum/format.env`). The `rocky` repository is
created with the distribution key as `repomd_keys`, so replay also checks Waystation's own `repomd.xml.asc`
verification against the recorded signature. The Rocky client image adds `rpm-build`: `yum/hosted.sh` builds a
package with rpmbuild, uploads it to a hosted repository and installs it with `repo_gpgcheck=1` against the key
Waystation serves (no upstream traffic). BaseOS/AppStream are left to the live run.
pypi: fixtures are the simple pages, wheels and PEP 658 `.metadata` files of `six` and `iniconfig` (about 60 KB);
the distributions uploaded to the hosted repository are generated by `pypi/make_dist.py` at run time. The `.metadata`
files are verified by clients against the page's hash, so the public hosts in them are allowlisted
(`pypi/allowed_hosts.txt`) rather than scrubbed.
go: fixtures are the version lists, `.info`/`.mod`/`.zip` files of `github.com/google/uuid` and
`github.com/Masterminds/semver/v3` from proxy.golang.org and the sum.golang.org lookups and tiles that prove them
(about 150 KB). The test downloads the two modules one at a time before building: the tiles the go command asks for
depend on the order of its lookups. The go command sends credentials over HTTPS only, so the group scenario runs
through `go/tlsfront.go` (a standard-library TLS reverse proxy with a throwaway self-signed certificate, started in the
client container).
galaxy: fixtures are `community.library_inventory_filtering_v1` 1.1.5 from galaxy.ansible.com (collection, version
list and version documents, the artifact through its two redirects to the S3 bucket, whose host is allowlisted in
`galaxy/allowed_hosts.txt`) and the `geerlingguy.ntp` 4.0.1 role (v1 search, version list, GitHub tag archive),
about 75 KB; the version documents' `files` lists are dropped (`DROP_JSON_KEYS`). The collection published to the
hosted repository is built with `ansible-galaxy collection init/build` at run time.
terraform: fixtures are `hashicorp/null` 3.1.0 for linux_amd64 from both origin registries: service discovery,
version lists, download documents, `SHA256SUMS`, signatures and the two provider archives (HashiCorp's release site;
GitHub release assets behind a redirect for OpenTofu). A provider archive is a Go binary of about 4.6 MiB and no
smaller real one with plugin protocol 5 exists, so the budget is raised to 11 MiB (5 MiB per file,
`tests/clients/terraform/format.env`). Both clients accept only `https://` mirrors: `client-terraform` is Alpine with
terraform, tofu, curl and `go/tlsfront.go` built in.
swift: fixtures are the github.com ref advertisement of `JohnSundell/Files` and its 4.2.0 tag tarball (with the
codeload redirect), about 200 KB (budget 1 MiB, `swift/format.env`); the package published to the hosted registry is
generated at run time. `client-swift` is `swift:6.4.3-noble` with curl, zip and `go/tlsfront.go` (SwiftPM accepts only
https registries); the anonymous resolve uses an empty netrc and its own cache path, since SwiftPM finds root's
credentials and caches through the passwd home, not `$HOME`.
nuget: fixtures are api.nuget.org's service index, the version list, registration (about 95 KB) and catalog leaf of
`Ardalis.GuardClauses` and its 5.0.0 package (about 165 KB), plus the 404 for the hosted package's id that the group's
proxy member asks about (about 280 KB, budget 1 MiB, `nuget/format.env`); the library pushed to the hosted repository
is packed at run time. `client-dotnet` is the official `mcr.microsoft.com/dotnet/sdk` image (curl included); the test
sets `DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE=true` (`1` is not enough), otherwise the SDK updates its workload
manifests from the configured NuGet sources, i.e. through Waystation. Plain-HTTP sources need
`allowInsecureConnections="true"` (.NET 9+).
composer: fixtures are repo.packagist.org's `packages.json` and the `p2` document of `psr/container`, its 2.0.2
archive from codeload.github.com (Waystation fetches GitHub API zipballs from there directly), and the 404 for the
hosted package's name that the group's proxy member asks about (budget 256 KiB, `composer/format.env`); the package
uploaded to the hosted repository is zipped at run time. `client-composer` is the official `composer` image with its
entrypoint cleared (it would run `sh` as `composer show`).
rubygems: fixtures are rubygems.org's compact index `versions` and `names` reduced to the `hola` lines when recorded
(`KEEP_LINES` in `rubygems/format.env`: the version line keeps its info MD5), `info/hola`, the 0.1.3 gemspec and
`.gem`, and the 404 for the hosted gem's info file that the group's proxy member asks about (budget 256 KiB); the
gems pushed to the hosted repository are built with `gem build` at run time. `client-ruby` is the official
`ruby:3.4.13-slim` image (gem 3.6, Bundler 2.6, bash; no curl, so `lib.sh` talks to the API with bash).
npm: after npm 11, `client-nodepm` runs pnpm, Yarn 1, Yarn 4 and Bun (`npm/pm.sh`): frozen installs of `pinned/` from
each client's committed lockfile (`pnpm-lock.yaml`, `yarn-classic.lock`, `yarn-berry.lock`, `bun.lock`; Yarn 1's
`resolved` URLs are pointed at Waystation first, as it fetches them verbatim) and each client's audit command. The
packages are cached by the npm client by then: only the audit POSTs add fixtures.
docker: containerd (`ctr --local --hosts-dir` against the client's dockerd's containerd, `docker/containerd.sh`)
and podman (`client-podman`, `docker/podman.sh`) pull the same images by mirror, prefix and alias; `ctr` fetches
every manifest of an index, so the Docker Hub fixtures include all of `hello-world`'s platform manifests.

### auth: the client-auth matrix

`auth/` (T-A04) runs without upstream content except one apt suite (`trixie-updates`, about 50 KB). Its `SERVICES`
start OpenLDAP (with `ldap-certs`, StartTLS) and Keycloak, the directory and realm of `make test-integration`
(`Build/Containers/test/`; the realm also allows the harness callback `http://waystation-web:8080/auth/oidc/*`), on
the stack's network. `client-curl` configures Waystation (LDAP provider: group `ops` → role `auth-matrix`; OIDC
provider: group `/developers` → the same role; private repositories `private-raw|docker|npm|apt|yum`; anonymous
access off), signs the OIDC user in through Keycloak's login form with curl and leaves one API token per client in
`/harness-shared`. Then, per client, positive and negative cases:

| Client | Accepted | Refused |
|---|---|---|
| curl (raw) | LDAP user's API token (basic), OIDC user's token (basic, bearer) | anonymous (401 Basic challenge), the LDAP password as basic, a wrong password, an unknown token, revoked tokens; a user without a mapped group sees 404 |
| docker | `docker login` with the LDAP password (push, pull), with the OIDC user's token | anonymous push/pull, a wrong password, a user without a role (pull, push), a revoked token (next pull and a new login) |
| npm | `npm login --auth-type=legacy` with the LDAP password (publish, install, whoami), `_authToken` = OIDC token | anonymous, a wrong login password, a wrong token, a revoked token, the login token after `npm logout` |
| apt | `auth.conf` with the OIDC and the LDAP user's tokens | anonymous, the LDAP password, a wrong password, a revoked token |
| dnf | `.repo` `username`/`password` with the LDAP and the OIDC user's tokens (upload with curl, install) | anonymous, the LDAP password, a wrong password, a revoked token |

The format raises the stack's sign-in and token-failure rate limits (`STACK_ENV`): it makes dozens of sign-ins from a
handful of addresses within minutes.

### arm64 clients

`CLIENT_ARM64` clients (`client-raspios`: Raspberry Pi OS = Debian 13 arm64 + the Raspberry Pi archive, for `apt`)
run with `ARM64=1` only. The harness never installs emulation: on an x86 host an administrator registers
`qemu-aarch64` first (e.g. `docker run --privileged --rm tonistiigi/binfmt --install arm64`), otherwise the run
prints `SKIP arm64 clients` and passes. Their fixtures are not committed yet: record them once on such a host
(`ARM64=1 make clients-test FORMAT=apt MODE=record`) before replaying with `ARM64=1`. The live workflows run them
(GitHub registers QEMU on its runner; Gitea when `WAYSTATION_CI_ARM64` is `true`).

### Nightly live run

`.gitea/workflows/clients-live.yml` runs `make clients-test FORMAT=all MODE=live` every night on the Gitea runners
(manual runs can pick one format); `.github/workflows/clients-live.yml` is the same, manual only.
Documents/Operations/CI.md.
