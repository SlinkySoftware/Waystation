# Waystation: agent instructions

Waystation is a self-hosted, open-source artifact repository: a pull-through cache for public package
ecosystems (Docker/OCI, npm, PyPI, Maven, apt, yum, Go, Helm, Terraform, Ansible Galaxy, Swift, ...) and a
private registry for the owner's own builds. It is a Nexus Repository / Artifactory alternative with no usage caps.
Components: a Python async **server**, Python async **workers**, an nginx **web** tier serving the Vue/Quasar
UI and cached files, **MongoDB** for metadata and stats, **Valkey** for hot-path coordination.

**Asked to deploy, install or operate Waystation (not to develop it)?** Follow
`Documents/Operations/AGENT_DEPLOYMENT.md` instead of this file: it is the runbook for agents deploying Waystation into
someone's environment. Everything below is for working on the code.

## Read before doing anything
1. `Documents/SPECIFICATION.md`: what the app does, requirements, deployment (living document; it always describes
   the system as built).
2. `Documents/Architecture/ARCHITECTURE.md`: **enforced** rules. Violating them is a bug.
3. `Documents/Architecture/DATA_MODEL.md`: MongoDB collections, Valkey keys, the storage layout.
4. `Documents/Architecture/FORMATS.md`: the per-format protocol notes. Read the section for any format you touch.
5. `Documents/UI_DESIGN.md`: the design system and every page. The UI must look like professional, shipped software.
6. `Documents/TASKS.md`: open work. Pick a task whose dependencies are done, mark it IN PROGRESS, and work only on
   that task. Completed tasks are archived in `Documents/History/BUILD_HISTORY.md`.

## Non-negotiables
- **No environment details in this repo.** It is public. No real hostnames, IP addresses, domains, usernames,
  registry URLs or credentials anywhere: code, docs, tests, fixtures, lockfiles, commit messages. Use
  `example.com` / `registry.example.com` / RFC 5737 / RFC 3849 addresses. Deployment specifics live outside the repo.
  The only exception: the repository URL in "Source control" below (and SPECIFICATION §13.1).
- **Only the server and workers talk to MongoDB and Valkey.** The web tier talks only to the server (and reads the
  blob store read-only for `X-Accel-Redirect`). The UI talks only to `/api/v1/*` and the event stream.
- **Every request is authorised by the RBAC engine** (`waystation_server.security.access`). Never rely on the UI
  hiding something; never check a role name directly; hidden objects return 404, not 403 (one exception, ADR 0005:
  a request with no credentials on `/repository/<repo>/...` gets the same 401 challenge for hidden and missing repos).
- **Never trust upstream content.** Verify every digest/checksum you are given, bound every size, normalise every
  path (no `..`, no absolute paths, no NUL), and never follow redirects to private address space unless the repo
  allows it (SSRF guard).
- **Never block the event loop.** No sync I/O in `async def`. CPU-heavy work (hashing large files, index
  generation, signing) goes to a thread pool or a worker job.
- **Secrets are write-only through the API**, encrypted at rest (`WAYSTATION_SECRET_KEYS`), never logged.
- Never commit secrets. `.env` is gitignored.

## Source control
- Repository: `https://github.com/SlinkySoftware/Waystation` (remote `origin`). Developers may also push to private
  mirrors configured in their own untracked local agent settings; follow those settings when they exist, and never
  name private hosts in tracked files, commit messages or PR text.
- Authentication comes from the developer's git credential helpers. Never put credentials in remote URLs or `.git/config`.
- Commit messages: `T-XXX: summary` (task ID first), imperative mood. No AI or tool attribution in commits.

## Development workflow
- **Parallel lanes**: platform, server, UI and formats are independent streams. Use subagents and separate git
  worktrees/branches (one per task), merge small PRs often, and keep `main` always runnable and releasable.
- **One integration stack at a time per machine.** `make test-integration` (and so `make check`) queues on a
  machine-wide lock: each run starts MongoDB, Valkey, OpenLDAP and Samba AD, and parallel runs exhaust the memory of
  a typical dev box. Run unit tests, lint and typecheck in parallel freely; a "waiting" message from
  `test-integration` is normal, so don't work around the lock.
- **Contract first, the UI never waits for the server**: API changes start in the OpenAPI schema with MSW mock
  handlers; the UI builds against `npm run dev:mock` (seeded fixtures and a realistic fake live-event generator) and
  switches to the real server when the endpoint lands.
- Never cut: the Non-negotiables above, ARCHITECTURE rules (X-Accel data plane, package boundaries, the single RBAC
  decision point), and tests for what you ship (unit tests plus a real-client test per format change).

## Working rules
- **Docker only.** No host Python or Node. Everything runs through `make` (`make help` lists targets):
  `make dev-up`, `make test`, `make lint`, `make fmt`, `make typecheck`, `make frontend-test`, `make e2e`,
  `make clients-test`, `make perf`, `make lock`, `make images`.
- Python 3.14, uv workspace (`uv.lock` committed), ruff (single quotes, 120 columns), mypy strict, import-linter
  contracts (ARCHITECTURE §3) enforced in CI.
- Frontend: Quasar 2 / Vue 3 / TypeScript strict, `<script setup lang="ts">` only, Pinia, `openapi-fetch` with types
  generated from the server's OpenAPI schema (`make gen-api`; CI fails if the generated file is stale).
  `package-lock.json` committed. Never render untrusted text with `v-html`.
- API change = server route + schema + regenerated client (`make gen-api`) + regenerated API reference
  (`make api-docs`, `Documents/API/`; a unit test fails when it is stale) + UI in the same change.
- New format = a plugin under `waystation_server/formats/<format>/` (a `FormatDescriptor` and a `FormatPlugin`,
  ARCHITECTURE §11), plus real-client tests in `tests/clients/<format>/` (FORMATS.md "Definition of done for a
  format").
- When you finish a task, **in the same change**: remove its row from `Documents/TASKS.md` and append it to
  `Documents/History/BUILD_HISTORY.md` as DONE (date and summary), add a dated `Documents/CHANGELOG.md` entry, and
  update every spec/architecture section whose behaviour changed. The specification and architecture documents
  describe the system as built, in the present tense; history belongs in BUILD_HISTORY and the CHANGELOG.
- Architecture changes need an ADR in `Documents/Architecture/adr/` and the owner's approval before merging.
- Keep performance budgets green (`Build/ci/budgets.json`; ARCHITECTURE §9). A regression is a failing build.
