# CI and release

Every CI step is a `make` target, so anything CI runs can be reproduced locally with Docker and make only.

## Workflows

| Workflow | Trigger | What it does |
|---|---|---|
| `.github/workflows/ci.yml` | pull request, push to `main` | `make check`; the generated API client is current (`make gen-api` then `git diff --exit-code`); the budget report in the job summary; in a parallel job, `make clients-test FORMAT=all MODE=replay` |
| `.github/workflows/release.yml` | tag `v*` | server, worker and web images for linux/amd64 and linux/arm64 pushed to GHCR, with SBOM and SLSA provenance attestations |
| `.github/workflows/clients-live.yml` | nightly, manual | the real-client harness against the live upstreams (`MODE=live`) |
| `.gitea/workflows/images.yml` | push to `main` (Gitea), manual | server, worker and web images pushed to the Gitea container registry |

Every third-party action is pinned by full commit SHA with its version in a comment. To update one, resolve the new
release tag to its commit and replace the SHA and the comment everywhere it is used. `make workflow-lint` (part of
`make check`) runs actionlint and shellcheck over both workflow directories.

### `make check`

In order: `forbidden-strings`, `workflow-lint`, `fmt-check`, `lint` (ruff + import-linter), `typecheck` (mypy
strict), `test` (unit + coverage), `test-integration` (throwaway MongoDB/Valkey), `clients-check` (fixture scan +
size budgets), `frontend-check` (generated-client check, ESLint, vue-tsc, Vitest, production build + bundle budget),
`budgets` (report).

`test-integration` holds a machine-wide lock (`TEST_LOCK`, default `/tmp/waystation-test-integration.lock`) for
the whole run, so runs from parallel worktrees queue instead of starting several container stacks at once.
`make test-integration TEST_LOCK=` skips it (also skipped where `flock` is missing).

In CI the dev image is built once per job by `docker/build-push-action` with the GitHub Actions layer cache
(`scope=dev-image`), loaded as `waystation-dev:local`, and `SKIP_DEV_IMAGE_BUILD=1` stops make from rebuilding it.
The buildx builder is not made the default, so make, compose and the harness keep using the normal Docker builder.

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
never fails the build; `"fail"` fails `make budgets`. All budgets are `warn` until milestone M2, except the SPA
initial-route bundle, which `make frontend-build` already enforces (its limit is read from `budgets.json`).
Latency and throughput budgets show "not measured" until the k6 suite lands (`make perf`, T-P2-PERF).
