# CI and release

Every CI step is a `make` target, so anything CI runs can be reproduced locally with Docker and make only.

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `.github/workflows/ci.yml` | major release tag `vX.0.0`, manual | `make check`; the generated API client is current (`make gen-api` then `git diff --exit-code`); the budget report in the job summary; in a parallel job, `make clients-test FORMAT=all MODE=replay` |
| `.github/workflows/release.yml` | every release tag `vX.Y.Z` (and `vX.Y.Z-<pre>`) | server, worker and web images for linux/amd64 and linux/arm64 pushed to GHCR as `X.Y.Z`, `X.Y`, `sha-<commit>` and (stable releases) `latest`, with SBOM and SLSA provenance attestations |
| `.github/workflows/clients-live.yml` | manual (one format or all) | the real-client harness against the live upstreams (`MODE=live`), arm64 clients included (QEMU registered on the runner, `ARM64=1`) |
| `.gitea/workflows/clients-live.yml` | manual (Gitea; one format or all) | the same live client smoke on the Gitea runners; arm64 clients when `WAYSTATION_CI_ARM64` is `true` |
| `.gitea/workflows/check.yml` | push to `main` and pull requests (Gitea), manual | `make check` without `test-integration`, step by step (`forbidden-strings`, `workflow-lint`, `fmt-check`, `lint`, `typecheck`, `test`, `test-nfs`, `clients-check`, `frontend-check`), the generated API client diff and the budget report; then, only on push to `main` and manual runs, `make test-integration` in a second job |
| `.gitea/workflows/images.yml` | push to `main` (Gitea), manual | server, worker and web images pushed to the Gitea container registry |
| `.gitea/workflows/perf.yml` | push to `main` (Gitea), manual | `make perf`: the k6 suite, CI profile on push, either profile on manual runs; results uploaded as the `perf-results` artifact |

GitHub runs the full check only for major releases (`vX.0.0` tags) or when started by hand, and the release images
for every release tag. Gitea
runs on local runners at no cost: it checks every pull request and every push to `main`, and builds images on every
push to `main`. Run `make check` locally before pushing.

Every third-party action is pinned by full commit SHA with its version in a comment. To update one, resolve the new
release tag to its commit and replace the SHA and the comment everywhere it is used. `make workflow-lint` (part of
`make check`) runs actionlint and shellcheck over both workflow directories.

### `make check`

In order: `forbidden-strings`, `workflow-lint`, `fmt-check`, `lint` (ruff + import-linter), `typecheck` (mypy
strict), `test` (unit + coverage; includes `test_api_docs.py`, which fails when the static API reference in
`Documents/API/` no longer matches the server, fix with `make api-docs`), `test-nfs` (the blob store on NFS, below), `test-integration` (throwaway
MongoDB/Valkey), `clients-check` (fixture scan +
size budgets), `frontend-check` (generated-client check, ESLint, vue-tsc, Vitest, production build + bundle budget),
`budgets` (report).

`test-integration` holds a machine-wide lock (`TEST_LOCK`, default `/tmp/waystation-test-integration.lock`) for
the whole run, so runs from parallel worktrees queue instead of starting several container stacks at once.
`make test-integration TEST_LOCK=` skips it (also skipped where `flock` is missing).

In CI the dev image is built once per job by `docker/build-push-action` with the GitHub Actions layer cache
(`scope=dev-image`), loaded as `waystation-dev:local`, and `SKIP_DEV_IMAGE_BUILD=1` stops make from rebuilding it.
The buildx builder is not made the default, so make, compose and the harness keep using the normal Docker builder.

### Blob store on NFS (`make test-nfs`)

`make test-nfs` starts `Build/Containers/compose.nfs.yaml`, a userspace NFSv4.1 server (NFS-Ganesha with its VFS
backend, built from `Build/Containers/nfs/`; the host's kernel nfsd is never touched), then runs
`Build/ci/test_nfs.sh` in a privileged dev-image container: it mounts the export with the kernel NFS client (the
mount exists only in that container), and runs `Application/Common/tests/test_storage.py` on it as the calling user
(`WAYSTATION_TEST_BLOB_DIR`): layout and modes, verified commits, kill mid-write, sweeps, resumable uploads and
identical commits racing from six processes (exactly one places the blob, adr/0011). It takes a few seconds, needs
no lock, and always ends with `down -v`. The runner must allow privileged containers.

### Opt-in: Nexus import against a real Nexus (`make test-nexus`)

Not part of `make check` or any workflow. `make test-nexus` starts `Build/Containers/compose.nexus.yaml` (MongoDB,
Valkey and `sonatype/nexus3`, pinned, Community Edition, data on tmpfs, about 3 GB of memory, one to three minutes
to start), then runs `pytest -m nexus` (`Application/Server/tests/test_nexus_live.py`) in the dev image: the test
sets a throwaway admin password, accepts the EULA, deletes the sample repositories and creates the repositories of
the hand-written fixtures (`Application/Server/tests/fixtures/nexus3`, each detail document posted back as the create
request; Nexus contacts no upstream), previews the import against the real Nexus and against the fixtures and
requires the same plan item by item, then applies it and checks the result and an idempotent re-apply. Two known
deltas are allowed: Community Edition refuses a writable group member (a Pro feature), and the `npm` fixture has no
detail document on purpose (the list fallback of older releases). It takes the machine-wide `TEST_LOCK` like
`test-integration` and always ends with `down -v`. To move to a newer Nexus, change the image tag and run it.

### Gitea checks

`.gitea/workflows/check.yml` runs the parts of `make check` one step each, so a failure names the part. The dev image
is built once per job with plain `docker build` (the runner's Docker layer cache keeps it quick; the GitHub Actions
cache is not used on Gitea) and `SKIP_DEV_IMAGE_BUILD=1` stops make from rebuilding it.

`make test-integration` runs in a separate `integration` job after `check`, only on push to `main` and on manual
runs (never on pull requests), with a 60 minute timeout. It starts MongoDB, Valkey, OpenLDAP, Samba AD and Keycloak
and needs about 6 GB of free memory on the runner host; it takes the machine-wide `TEST_LOCK`, so it queues behind
any other integration run on the same host. A runner without that memory can skip the job with an organisation
variable:

| Kind | Name | Required | Meaning |
|---|---|---|---|
| variable | `WAYSTATION_CI_INTEGRATION` | no | `false` skips the integration-test job; anything else (or unset) runs it |
| variable | `WAYSTATION_CI_UI_SMOKE` | no | `false` skips the UI job (`make ui-smoke`: every rail page as minimal-permission users on the mock build, then `make e2e`: Playwright flows, axe, visual baseline and UI perf; the Playwright report is uploaded when it fails); anything else (or unset) runs it |

The runner needs the `ubuntu-latest` label, Docker (about 6 GB of free memory for
`integration`) and network access to the package registries. make starts its tools in containers that bind-mount
the checkout, so jobs must run on the host (an `ubuntu-latest:host` runner label) or in a job container whose
checkout path is the same on the Docker host. With act_runner (Gitea runner) that means `container.bind_workdir: true`
in the runner's `config.yaml`: the default (the workspace in a Docker volume) leaves every bind mount empty, and make
fails with `can't open file '/src/...'`. The workflows pass `make workflow-lint` (actionlint). The images workflow
needs the `REGISTRY_HOST` variable set, and the Gitea workflows use upload-artifact v3 (Gitea's artifact API does
not support v4).

### Gitea live client smoke

`.gitea/workflows/clients-live.yml` runs `make clients-test FORMAT=all MODE=live` on manual runs only (no
schedule; a single format can be chosen): every format's real clients against the live public upstreams through
the harness's fake upstream in pass-through mode, so upstream changes the committed fixtures cannot show surface
(tests/clients/README.md). Formats run one after another, each in its own compose project; a run takes
about an hour and needs network access to the public registries and about 6 GB of free memory (the `auth` format
adds Keycloak and OpenLDAP).

| Kind | Name | Required | Meaning |
|---|---|---|---|
| variable | `WAYSTATION_CI_ARM64` | no | `true` registers QEMU on the runner host (`docker/setup-qemu-action`, like `images.yml`) and runs the arm64 clients (`ARM64=1`); anything else skips them with a message |

### Releases (GitHub)

1. `make set-version V=X.Y.Z`, update the changelog, merge to `main`.
2. Tag the merge commit `vX.Y.Z` and push the tag to both remotes. The workflow refuses a tag that does not match
   `VERSION`.

Images: `ghcr.io/<owner>/waystation-{server,worker,web}` (owner in lower case), tagged `X.Y.Z`, `X.Y` and
`sha-<commit>`. Each image index carries a BuildKit SBOM (SPDX) and `mode=max` provenance, and the workflow adds a
GitHub artifact attestation pushed to the registry (`gh attestation verify oci://<image>@<digest> --owner <owner>`).
Only `GITHUB_TOKEN` is used (`packages: write`, `id-token: write`, `attestations: write`); no stored secrets.
The web image builds its SPA stage once on the build platform; server and worker build per architecture under QEMU.

### Gitea image builds

Organisation-level settings on the Gitea side, referenced by name only (the repository never contains the values):

| Kind | Name | Required | Meaning |
|---|---|---|---|
| variable | `REGISTRY_HOST` | yes | host name of the Gitea container registry (the instance's host) |
| variable | `REGISTRY_NAMESPACE` | no | image namespace; default: the repository owner, lower case |
| variable | `IMAGE_PLATFORMS` | no | buildx platforms; default `linux/amd64` (e.g. `linux/amd64,linux/arm64` with QEMU on the runner) |
| secret | `REGISTRY_USER` | yes | account allowed to push packages to the namespace |
| secret | `REGISTRY_TOKEN` | yes | that account's access token with package write scope |

Images: `<REGISTRY_HOST>/<namespace>/waystation-{server,worker,web}`, tagged `main`, `<VERSION>-dev` and
`sha-<12 hex>`, with a registry layer cache at `:buildcache`. Attestations are not attached (the signed, attested
release images come from GHCR). The runner needs the `ubuntu-latest` label, Docker and network access to the
registry.

## Forbidden strings

The repository is public, so no real host names, IP addresses or credentials may appear in it (CLAUDE.md).
`make forbidden-strings` (`Build/ci/forbidden_strings.sh`) scans every tracked and untracked-but-not-ignored file;
`make clients-check` runs the same scanner (`tests/clients/harness/check_fixtures.py`) over the client harness and
fixtures with their size budgets. A file fails on:

- a host name not in `Build/ci/allowed_hosts.txt` (plus `tests/clients/<format>/allowed_hosts.txt` for that
  format's harness files and fixtures). Internal-only suffixes (`.local`, `.lan`, `.internal`, `.corp`, `.home`,
  `.private`, ...) are always rejected; reserved names (`*.example`, `*.test`, `*.invalid`, `*.localhost`) always
  pass;
- an IP address outside RFC 5737 / RFC 3849, loopback and unspecified. CIDR definitions of the well-known special
  ranges (`10.0.0.0/8`, `fc00::/7`, ...) pass, a specific private network or address does not;
- private key material or a token shape (GitHub, npm, GitLab, Slack, AWS, JWT);
- a string from your local deny list: `WAYSTATION_FORBIDDEN_FILE=/path/outside/the/repo make forbidden-strings`
  (one string per line, e.g. your real domains; never commit it).

Allowlist syntax (`Build/ci/allowed_hosts.txt`): a host entry allows the name and its subdomains; `url <prefix>`
allows one exact URL prefix without allowing its host elsewhere (used for the secondary source-control URL);
`not-a-host <token>` skips an exact dotted code token (an i18n key). Every entry is a reviewed change with a comment.

Special cases: lockfiles (`package-lock.json`, `uv.lock`) are host-checked only on the lines that say where a
package is downloaded from (a private registry URL there fails; funding links are ignored). SVG files skip the IPv4
pattern (path data). A test that must use a non-documentation address (SSRF guards, trusted-proxy hops) marks the
line with `# forbidden-strings: allow`; this skips the host/IP scan for that line only, never the key/token scan.
Generated or vendored files whose sources are scanned (`src/api/schema.d.ts`, `public/mockServiceWorker.js`) are
skipped.

## Performance budgets

`Build/ci/budgets.json` lists the SPECIFICATION §5.1 budgets. `make budgets` (`Build/ci/budgets.py`) prints each
budget, its limit, the value measured by the check named in `source` and a status; `MARKDOWN=1` prints the table CI
appends to the job summary. A budget with `"enforce": "warn"` is reported (a CI warning annotation when over) and
never fails the build; `"fail"` fails `make budgets`. Every budget is `fail`; `warn` is only for a new budget
being introduced. The SPA initial-route bundle is also enforced by `make frontend-build` (its limit is read from
`budgets.json`).
The latency, throughput and concurrency budgets are measured by the k6 suite (`make perf`, `tests/perf/README.md`),
which leaves its values in `tests/perf/.results/measured.json` (their `source`); until a run has left that file they
show "not measured". `make budgets STRICT=1` (and `make perf STRICT=1`) also treats `warn` budgets as `fail`. The
dashboard paint budget needs a browser and is not measured by the k6 suite.

### Gitea perf runs

`.gitea/workflows/perf.yml` runs `make perf` on the runner host: the CI profile (a few minutes) after every push to
`main`, and either profile on manual runs: the full profile (1M seeded assets, 500 concurrent downloads, about half
an hour) only runs by hand. A run holds the machine-wide `TEST_LOCK`, so it queues behind integration
runs on the same host; the report and raw results are uploaded as the `perf-results` artifact and the report is
printed in the log. The runner needs about 6 GB of free memory for the CI profile, and about 14 GB of memory and 8 GB
of disk for the full one. A runner without them can skip the workflow:

| Kind | Name | Required | Meaning |
|---|---|---|---|
| variable | `WAYSTATION_CI_PERF` | no | `false` skips the perf workflow; anything else (or unset) runs it |
